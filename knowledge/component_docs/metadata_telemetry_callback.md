# Component Guide: `metadata_telemetry_callback` (`MetadataCallbackHandler`)

Deep implementation reference for the Tier 2 shared observability component (`metadata_telemetry_callback` v1.0.0), implemented in `src/metadata/callbacks.py` and backed by `src/metadata/db.py`.

---

## 1. Component Identity & Decision 30 Boundary

| Attribute | Value |
|---|---|
| **Component ID** | `metadata_telemetry_callback` (`version: "1.0.0"`) |
| **Python Implementation** | `src/metadata/callbacks.py::MetadataCallbackHandler` |
| **Database Layer** | `src/metadata/db.py::MetadataDatabase` (`psycopg_pool.ConnectionPool` + `pgvector`) |
| **Target Tables (Tier 1)** | `agent_metadata.agent_run_header`, `execution_log`, `artifact_registry`, `artifact_manifest`, `decision_log`, `audit_log` |
| **Determinism & Failure Mode** | Passive observer (`raise_error = False`); logs `_warn()` on DB/serialization failure and **never aborts graph execution** |

Per **Decision 30**, `MetadataCallbackHandler` handles **passive Tier 1 operational telemetry only**. Transactional Tier 2 mutations (`agent_registry`, `contract_lineage`, Git PR creation, and RAG indexing) must execute inside an explicit fail-fast Delivery node (`node_deliver_stage` / Phase 8 `node_delivery`), never inside callback hooks.

---

## 2. Initialization & LangGraph Wiring

Generated agents bind `MetadataCallbackHandler` into the LangGraph execution `config` dictionary (`builder.md` §4.1):

```python
from src.metadata.callbacks import MetadataCallbackHandler
from src.core.config import get_settings

def run_agent(run_id: str, input_db_ref: dict):
    settings = get_settings()
    app = build_graph()
    telemetry_handler = MetadataCallbackHandler(
        run_id=run_id,
        agent_id="dbt_pipeline_builder_agent",
        agent_version="1.0.0",
        db_uri=settings.METADATA_DB_URI,
    )
    config = {
        "callbacks": [telemetry_handler],
        "configurable": {"thread_id": run_id},
    }
    return app.invoke(initial_state, config=config)
```

### Key Constructor Parameters (`__init__`)
- `run_id`, `parent_run_id`: `UUID | str | None` (lazy-created via `start_run()` if omitted).
- `agent_id`, `agent_version`, `operation_kind`: Run identity strings (`"meta-agent"`, `"0.1.0"`, `"standard"`).
- `initiator_id`, `initiator_type` (`human | agent | system | schedule`), `trigger_type` (`manual | api | schedule | event | parent_run`), `source_revision`.
- `db` (`MetadataDatabase`), `db_uri` (`str`), or `pool` (`psycopg_pool.ConnectionPool`).
- `enabled`: Optional `bool` override (otherwise reads `config/settings.yaml` `telemetry.enabled`).

---

## 3. Run & Span Lifecycle Tracking

### Run Header (`start_run` & `complete_run`)
- `start_run(...)` inserts a `running` row into `agent_metadata.agent_run_header` (`ON CONFLICT (run_id) DO NOTHING`), resetting the thread-safe span map and monotonic sequence counter (`itertools.count(1)`).
- `complete_run(status="completed", total_cost_usd=...)` executes a single atomic SQL CTE rolling up `SUM(input_token_count)` and `SUM(output_token_count)` from `execution_log`, computing `duration_ms`, and setting terminal `completed_at`.

### Automatic LangChain / LangGraph Callbacks
Inheriting `langchain_core.callbacks.BaseCallbackHandler`, the handler intercepts:
- `on_chain_start` / `on_chain_end` / `on_chain_error` -> `step_type="node"` (extracts `metadata["langgraph_node"]`)
- `on_llm_start` / `on_chat_model_start` / `on_llm_end` / `on_llm_error` -> `step_type="llm_call"`
- `on_tool_start` / `on_tool_end` / `on_tool_error` -> `step_type="tool_call"`

Every span records `event_sequence`, `parent_log_id` (enforcing composite same-run FK `(run_id, parent_log_id)`), OpenTelemetry `trace_id` / `span_id`, and multi-provider token metrics via `extract_llm_telemetry()` (normalizing `input_token_count`, `output_token_count`, `cached_token_count`, `reasoning_token_count`, `model_provider`, and `model_used`).

---

## 4. Explicit Retry, Artifact, Decision & Audit APIs

Beyond passive span hooks, `MetadataCallbackHandler` provides explicit Tier 1 helpers:

- **`record_retry_span(...)`**: Records a retry execution span linked to the failed span via `retry_of_log_id` and `attempt_number >= 1`.
- **`record_artifact(...)` & `link_artifact_manifest(...)`**: Registers immutable assets (`storage_uri`, `checksum_sha256`, `byte_size`) in `artifact_registry` and links run Bill-of-Materials entries (`role` in `{'input', 'output', 'intermediate'}`) in `artifact_manifest`.
- **`record_decision(...)`**: Logs `pending | decided | rejected | cancelled` checkpoints in `decision_log`. When `decision_status == "decided"`, validates non-null `decision_value`, `decided_by`, `decided_by_type` (`human | agent | rule_engine`), and `rationale` (`chk_decision_resolution`).
- **`record_audit_event(...)`**: Appends immutable compliance events to `agent_metadata.audit_log` (`actor_type` in `{'human', 'agent', 'service_account', 'system'}`, `outcome` in `{'success', 'denied', 'failed', 'error'}`), protected at the DB layer by trigger `trg_audit_log_immutable` (Compliance Rule `SEC-01`).

---

## 5. Sanitization Guards & Zero-Infra Testing

### Automatic Payload Sanitization (`src/metadata/db.py`)
All text, stack traces, and JSONB columns pass through centralized guards configured in `config/settings.yaml`:
- `sanitize_text()` / `sanitize_payload()`: Redacts `sk-[A-Za-z0-9\-_]{16,}` and `Bearer ...` tokens to `[REDACTED]`.
- `truncate_stack_trace()`: Truncates tracebacks to `stack_trace_max_bytes` (32 KB) with `\n... [TRUNCATED]`.
- `guard_inline_jsonb()`: Caps inline JSONB payloads at `inline_payload_max_bytes` (64 KB), replacing oversized blobs with a bounded notice pointing to `agent_metadata.artifact_registry`.

### Unit Testing Pattern (`tests/metadata/test_callbacks.py`)
Test telemetry offline with zero database dependencies by passing `MetadataDatabase(pool=_FakePool(conn))` where `_RecordingConnection` captures executed SQL and bound parameters, and `_FailingPool` verifies non-blocking warning behavior.
