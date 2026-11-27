# ADR-001: Canonical dbt Pipeline Builder Agent (`dbt_pipeline_builder_agent`)

- **ADR ID**: `ADR-001`
- **Status**: Approved (Canonical Reference Agent)
- **Pattern**: `PAT-01` — Staged / Gated Pipeline (`stateful_pipeline_with_gates` / `staged_gated`)
- **Runtime**: `langgraph` (with `PostgresSaver` checkpointer)
- **Primary Consumers**: Architect Specialist, Documentarian Specialist

---

## Context

Enterprise analytics engineering teams need a Layer 2 DDLC AI agent (`dbt_pipeline_builder_agent`) capable of onboarding relational OLTP source schemas into production-ready dbt transformation pipelines. Specifically, the agent must:

1. Introspect source PostgreSQL OLTP schemas (`public`, `sales`, `inventory`) to extract tables, column data types, primary keys, and foreign key relationships.
2. Reason over source catalog metadata to design a layered dbt DAG (`stg_` staging views, `int_` intermediate models, `fct_` / `dim_` mart tables or incrementals) with explicit materializations and test plans.
3. Synthesize modular dbt Jinja-SQL model files, `schema.yml` generic tests (`unique`, `not_null`, `relationships`), and `sources.yml` freshness declarations.
4. Validate compilation deterministically in an isolated sandbox via `dbt compile` and SQLGlot AST parsing before exposing artifacts to human engineers.
5. Halt unconditionally for human review prior to committing side-effecting Git branches, opening Pull Requests, and recording cross-agent lineage.

Because the generated outputs include executable SQL and Pull Requests against shared production analytics repositories, the design must guarantee auditability, bounded LLM cost, tamper-evident human sign-off, and resumable execution across multi-hour review gates.

---

## Decision

Select **Pattern 01 (`PAT-01` Staged / Gated Pipeline)** implemented via the **`langgraph`** runtime generation strategy (`architecture.pattern: stateful_pipeline_with_gates`, `architecture.autonomy_level: semi_autonomous`):

1. **Deterministic StateGraph Orchestration**: Wire a fixed 6-stage pipeline (`discover` -> `plan` -> `generate` -> `dry_run` -> `review` -> `deliver`) in `src/graph.py` backed by `PostgresSaver.from_conn_string(settings.CHECKPOINTER_DB_URI)` for durable checkpointing.
2. **Structured LLM Reasoning via LiteLLM + Instructor**: Bind `instructor.from_litellm(litellm.completion)` on `plan` and `generate` stages (`model_tier: reasoning`, low temperature `0.0–0.1`) to enforce Pydantic v2 output schemas (`TransformationPlan`, `GeneratedFileSet`).
3. **Claim Check Pattern for Generated File Bundles**: Keep `AgentState` lightweight (`< 1 KB`). The `generate` stage LLM emits `GeneratedFileSet(files=[GeneratedFile(path, content)])`, and the deterministic Skeleton node stages files to disk under `DBT_SANDBOX_DIR / bundle_id` (enforcing `Path.resolve().is_relative_to()`), computes a true SHA-256 digest, and stores only the `DbtArtifactBundle` Claim Check pointer in state.
4. **Bounded Self-Correction Reflection Loop**: Wire `dry_run` (`dbt compile` + `validate_sql_ast` over `target/compiled/`) back to `generate` via `route_dry_run` with `safety_valves.max_reflection_retries: 3`, escalating to `review` once retries are exhausted.
5. **Multi-Way Conditional Human Review Gate & Fail-Fast Delivery**: Use LangGraph `interrupt()` in `review` paired with `route_review` (`approved` -> `deliver`, `revise` -> `generate`, `abort` -> `END`), followed by an explicit transactional `node_deliver_stage` (Decision 30).

---

## Runtime & Contracts

### Stage Pipeline & Autonomy Matrix

| Stage ID | Type | Processing Type | Autonomy | Tools / Components |
|---|---|---|---|---|
| `discover` | `discovery` | `deterministic_tool` | `autonomous` | `schema_inspector_v1` (`shared_component`, `read_only`) |
| `plan` | `reasoning` | `llm_reasoning` | `autonomous` | Instructor + `prompts/stage_plan.jinja2` (`query_tier2_catalog`) |
| `generate` | `code_generation` | `llm_reasoning` | `autonomous` | Instructor `GeneratedFileSet` + `prompts/stage_generate.jinja2` |
| `dry_run` | `sandbox_testing` | `deterministic_tool` | `autonomous` | `compile_dbt_project` + `tools/sqlglot_verifier.py::validate_sql_ast` |
| `review` | `human_review` | `human_gate` | `human_required` | LangGraph `interrupt()` + Chainlit diff/lineage view |
| `deliver` | `delivery` | `deterministic_tool` | `autonomous` | `git_deliverer_v1` (`shared_component`, `side_effecting`) |

### The 7-Contract Chain

1. `source_database_reference` (`input`): `database_name`, `schema_names`, `connection_ref`.
2. `source_schema_profile` (`internal`): `database_name`, `tables: List[TableProfile]` (with nested `ColumnProfile`), `foreign_keys`, `profiled_at`.
3. `transformation_plan` (`internal`): `project_name`, `target_dialect` (`snowflake | postgres | bigquery | databricks`), `staging_models: List[StagingModelSpec]`, `marts_models: List[MartModelSpec]`, `total_models_planned`.
4. `dbt_artifact_bundle` (`internal`, `storage_mode: claim_check`): `bundle_id`, `storage_uri`, `file_manifest`, `artifact_sha256`, `generated_at`.
5. `compilation_report` (`internal`): `bundle_id`, `success`, `compilation_errors`, `sandbox_runtime_seconds`.
6. `reviewed_artifact_bundle` (`internal`): `bundle_id`, `approved_by`, `approval_timestamp`.
7. `delivery_manifest` (`output`): `manifest_id`, `run_id`, `repository`, `branch_name` (`feat/dbt-models-*`), `commit_sha`, `pull_request_url`, `artifact_sha256`, `delivered_at`.

### Telemetry & Tier 2 Registration

- **Passive Telemetry (`MetadataCallbackHandler`)**: Attached via `config["callbacks"]` to record Tier 1 spans, token counts, and gate events without impacting execution on logging failure.
- **Transactional Delivery (`node_deliver_stage`)**: Invokes `publish_dbt_pull_request` and `register_pipeline_delivery` (Tier 2 `ddlc_shared_metadata.contract_lineage` / `agent_registry`), raising `RuntimeError` fail-fast on Git or database failures (Decision 30).

---

## Trade-offs & Rejected Alternatives

### Rejected: `react_tool_agent` (`PAT-05`) as Top-Level Architecture
- **Why Rejected**: The workflow stages (`discover` -> `plan` -> `generate` -> `dry_run` -> `review` -> `deliver`) are 100% known at design time. Using a ReAct agent to dynamically rediscover stage ordering on every run wastes tokens, breaks structural reproducibility, and complicates compliance auditing.
- **Accepted Trade-off**: Fixed stage topology means adding a new modeling phase requires an explicit `impl_docs/` update rather than prompt tuning.

### Rejected: `python_litellm` Linear Runner
- **Why Rejected**: A linear runner lacks durable PostgreSQL state checkpointing (`PostgresSaver`), native `interrupt()` pause/resume across asynchronous human reviews, and cyclic reflection edges between `dry_run` and `generate`.

### Rejected: Inline Artifact Payloads in `AgentState`
- **Why Rejected**: Storing complete SQL repositories and compiled target files directly in `AgentState` inflates PostgreSQL checkpoints, causes serialization latency/OOM risks, and makes cryptographic gate freeze verification fragile.
- **Accepted Trade-off**: Claim Check requires deterministic disk staging and path-traversal validation (`is_relative_to`) in `node_generate_stage`.

### Rejected: Asking the LLM to Emit `DbtArtifactBundle` Directly
- **Why Rejected**: LLMs cannot write files to disk reliably or compute authentic SHA-256 digests (`artifact_sha256`). Separating LLM `GeneratedFileSet` output from deterministic node staging eliminates fabricated hashes.

---

## Human Gate Rationale

### Multi-Way Conditional Review Gate (`review` -> `route_review`)
- **Placement Justification (`PAT-01` §9.1)**: `deliver` creates remote Git branches, commits executable SQL, opens Pull Requests, and mutates Tier 2 lineage — all external, accountable side effects. No gate is placed after mechanical stages (`discover`, `dry_run`).
- **Three-Way Routing Semantics**:
  - `"approved"`: Validates `human_approved = True`, writes `ReviewedArtifactBundle` freezing `bundle_id` and `artifact_sha256`, and routes to `deliver_stage`.
  - `"revise"`: Resets `reflection_count = 0`, appends `"Reviewer feedback: <comment>"` to `diagnostics` (projected into the Universal Prompt Contract `feedback` key), and loops back to `generate_stage`.
  - `"abort"`: Routes cleanly to `END` without touching Git or Tier 2 catalogs.
- **Freeze Integrity**: `node_deliver_stage` verifies `state.get("human_approved", False)` and validates both `ReviewedArtifactBundle` and `DbtArtifactBundle` before publishing, guaranteeing downstream code cannot alter approved artifacts.
