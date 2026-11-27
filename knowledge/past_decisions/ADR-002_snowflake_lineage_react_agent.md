# ADR-002: Exploratory Snowflake Lineage ReAct Agent (`snowflake_lineage_inspector_agent`)

- **ADR ID**: `ADR-002`
- **Status**: Approved (Canonical Reference ADR for `PAT-05`)
- **Pattern**: `PAT-05` — ReAct Tool Agent (`react_tool_agent`)
- **Runtime**: `langgraph` (with `PostgresSaver` checkpointer and bounded `ToolNode` dispatch)
- **Primary Consumers**: Architect Specialist, Researcher Specialist

---

## Context

Data platform governance teams need an automated agent (`snowflake_lineage_inspector_agent`) to trace upstream and downstream column-level lineage when investigating broken warehouse dashboards, undocumented dynamic SQL views, or cross-schema dependencies in Snowflake.

Unlike `dbt_pipeline_builder_agent` (`ADR-001`), the traversal sequence cannot be statically fixed at design time:

1. Root-cause lineage paths span variable depths across `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY`, `INFORMATION_SCHEMA.VIEWS`, materialized view definitions, and downstream BI references.
2. Each step's next query depends dynamically on intermediate tool observations (e.g., discovering whether an object is a base table, a secure view wrapping dynamic SQL, or a dbt-managed incremental table).
3. Unbounded autonomous tool loops pose severe cost, latency, context-overflow, and prompt-injection risks if database object comments or view SQL contain untrusted text.

---

## Decision

Select **Pattern 05 (`PAT-05` ReAct Tool Agent)** implemented on the **`langgraph`** runtime (`architecture.pattern: react_tool_agent`, `architecture.autonomy_level: semi_autonomous`), strictly constrained by a **read-only exploration loop + post-loop human gate**:

1. **Read-Only Tool Allowlist Guard**: Confine the ReAct loop exclusively to `access: read_only` catalog and SQL parsing tools. Enforce allowlist membership deterministically inside the Skeleton dispatcher (`CMP-ALLOW-GUARD`) and reject SQL arguments containing mutating keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `MERGE`).
2. **Hard Execution Bounds & Safety Valves**: Configure `safety_valves` (`max_tool_iterations: 10`, `timeout_seconds: 180`), per-tool timeouts (`30s`), per-tool call quotas (`5` calls per tool), and `llm_config.cost_cap_usd: 1.50`.
3. **Observation Truncation & Injection Framing**: Enforce `CTR-OBSERVATION` truncation at 4,000 tokens with explicit `[TRUNCATED - n tokens omitted]` markers, store full raw observations outside the prompt trajectory, and wrap tool outputs in structured untrusted delimiters to mitigate `FM-OBSERVATION-INJECTION`.
4. **Cycle & No-Progress Detection**: Terminate deterministically if identical `(tool_name, args)` pairs repeat consecutively or if cumulative cost/iteration limits are hit, returning best partial findings with escalation status.
5. **Post-Loop Human Review Gate (`GATE-RESULT`)**: Halt unconditionally via LangGraph `interrupt()` before registering discovered lineage edges into Tier 2 `ddlc_shared_metadata.contract_lineage` or `agent_dependency`.

---

## Runtime & Contracts

### Stage Pipeline & Tool Allowlist

| Stage ID | Type | Processing Type | Autonomy | Tools & Safety Valves |
|---|---|---|---|---|
| `scope_init` | `discovery` | `deterministic_tool` | `autonomous` | Validates target Snowflake object & freezes read-only allowlist |
| `explore_lineage` | `reasoning` | `llm_reasoning` | `autonomous` | Bounded ReAct loop (`max_tool_iterations: 10`, `cost_cap_usd: 1.50`) |
| `synthesize_graph` | `reasoning` | `llm_reasoning` | `autonomous` | Instructor compiles `DiscoveredLineageGraph` from `CTR-TRAJECTORY` |
| `review` | `human_review` | `human_gate` | `human_required` | LangGraph `interrupt()` presenting graph + full trajectory |
| `publish_lineage` | `delivery` | `deterministic_tool` | `autonomous` | Transactional Tier 2 `LineageClient` upsert (fail-fast) |

### Bound Read-Only Tools

- `inspect_snowflake_catalog` (`read_only`): Queries `INFORMATION_SCHEMA.TABLES` / `COLUMNS` with parameterized schema/object filters and `LIMIT` bounds.
- `query_account_usage_lineage` (`read_only`): Executes parameterized read-only queries against `SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES` and `ACCESS_HISTORY` with mandatory `LIMIT <= 200` and AST write-keyword rejection.
- `parse_sql_lineage_ast` (`read_only`): Deterministic SQLGlot (`dialect="snowflake"`) parser extracting table and column projections from view DDL without executing SQL.
- `lookup_tier2_contracts` (`read_only`): Queries `ddlc_shared_metadata.contract_registry` via `CatalogClient` to match Snowflake objects to DDLC agents.

### State Contracts

1. `lineage_investigation_request` (`input`): `target_database`, `target_schema`, `target_object`, `direction` (`upstream | downstream | both`), `max_depth`.
2. `lineage_trajectory` (`internal`): Append-only `CTR-TRAJECTORY` record capturing `(iteration, thought, action, truncated_observation, token_usage)` persisted via `PostgresSaver` and `MetadataCallbackHandler`.
3. `discovered_lineage_graph` (`internal`): Instructor-validated Pydantic model listing `nodes`, `edges`, `confidence_scores`, `unresolved_objects`, and `bound_exhausted` flag.
4. `reviewed_lineage_graph` (`internal`): Human-signed approval payload freezing the `graph_sha256` digest.
5. `lineage_publish_manifest` (`output`): Registered Tier 2 `lineage_id` / `dependency_id` references and audit timestamp.

---

## Trade-offs & Rejected Alternatives

### Rejected: `stateful_pipeline_with_gates` (`PAT-01`) for Discovery Traversal
- **Why Rejected**: Static pipeline stages cannot express variable-depth recursive discovery across heterogeneous Snowflake objects (dynamic views, multi-hop CTEs, external stages, dbt aliases) where each query depends on the previous result.
- **Accepted Trade-off**: Adopting `PAT-05` trades strict trajectory reproducibility (exact sequence of queries may vary across runs even at `temperature: 0.0`) and introduces variable token cost up to `cost_cap_usd`. We mitigate this by nesting the ReAct loop inside a single bounded stage (`explore_lineage`) surrounded by deterministic `scope_init`, `synthesize_graph`, `review`, and `publish_lineage` stages (`PAT-05` §12.3).

### Rejected: Write-Capable ReAct Loop with Direct Metadata Updates
- **Why Rejected**: Allowing the LLM inside the reasoning loop to directly call `LineageClient.record_contract_lineage()` or mutate Snowflake tags violates `PAT-05` containment doctrine and risks corrupting Tier 2 metadata on partial exploration or observation prompt injection (`FM-OBSERVATION-INJECTION`).

### Rejected: Unbounded Context Accumulation
- **Why Rejected**: Passing raw 500-row Snowflake `ACCESS_HISTORY` JSON blobs into prompt history exhausts context windows within 3 iterations. Sliding-window history (first task turn + last 3 full observations + deterministic one-line summaries of older steps) keeps prompts predictable.

---

## Human Gate Rationale

### Post-Loop Approval Gate (`GATE-RESULT`)
- **Placement Justification**: Placing a human gate on *every* read-only Snowflake metadata query would destroy the exploratory efficiency of `PAT-05`, whereas omitting a gate before mutating `ddlc_shared_metadata` would allow probabilistic LLM inferences (or `bound_exhausted` partial graphs) to pollute enterprise lineage.
- **Review Payload**: LangGraph `interrupt()` surfaces the synthesized `discovered_lineage_graph`, confidence scores, any `unresolved_objects`, total iterations/cost consumed, and the full `CTR-TRAJECTORY` audit reference.
- **Decision Outcomes**:
  - `"approved"`: Freezes `reviewed_lineage_graph` (`graph_sha256`) and routes to deterministic `publish_lineage` using `LineageClient`.
  - `"revise"`: Injects reviewer scope hints into `feedback` and re-runs `explore_lineage` with a fresh iteration counter.
  - `"abort"`: Terminates cleanly at `END` while retaining the read-only trajectory in Tier 1 `execution_log` for forensic audit.
