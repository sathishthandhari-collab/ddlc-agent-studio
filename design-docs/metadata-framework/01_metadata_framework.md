# DDLC Agent Studio — Metadata Framework

## Three-Tier Persistent Metadata Architecture

> **Document Status**: Draft v0.1  
> **Last Updated**: 2026-11-11  
> **Audience**: Architecture & Development Team  
> **Prerequisite**: [02_architecture.md](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/02_architecture.md) — read that first

---

## 1. Document Purpose

The architecture document defines the meta-agent's internal structure: coordinator, specialists, blackboard state, and knowledge layer. But the blackboard is **in-memory workflow state** — it holds the data a running graph needs to route, project, and checkpoint. It is *not* the right place for persistent operational metadata like audit trails, artifact registries, execution logs, or event histories.

Those concerns belong in a **metadata framework** — a set of PostgreSQL tables that live alongside (but separate from) the blackboard state and the knowledge layer. This document defines that framework.

### What the Blackboard Holds vs. What the Metadata Framework Holds

| Concern | Blackboard (LangGraph State) | Metadata Framework (Postgres Tables) |
|---|---|---|
| **Purpose** | Drive the current workflow execution — routing, projection, checkpointing | Record persistent operational history — what happened, what was produced, who decided what |
| **Lifetime** | Per-run (survives checkpoints, but scoped to one execution) | Permanent (survives across runs, agents, and versions) |
| **Audience** | Coordinator & specialists during execution | Operators, auditors, dashboards, cross-agent queries, post-mortem analysis |
| **Examples** | Current phase flag, parsed spec, approved architecture, builder output | Execution log entries, artifact manifests, audit trail, decision records, event history |

The blackboard says *"here is the approved architecture so the Documentarian can work."*  
The metadata framework says *"architecture v2 was approved by user X at timestamp T, after 2 revision rounds, replacing v1 which was rejected for reason Y."*

---

## 2. The Three Tiers

The metadata framework is organized into three tiers, each building on the previous one. This layering separates generic agent concerns from DDLC-specific concerns from individual agent concerns.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                                                                 │
│   TIER 3 — Agent-Specific Metadata                                              │
│   Schema: ddlc_<agent_name>_metadata                                            │
│                                                                                 │
│   Tables unique to a single DDLC agent. Source-specific profiling results,      │
│   agent-local staging state, custom quality scores, etc.                        │
│                                                                                 │
│   One schema per agent. Only that agent reads/writes here.                       │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   TIER 2 — DDLC Shared Metadata                                                │
│   Schema: ddlc_shared_metadata                                                  │
│                                                                                 │
│   Tier 1 tables + DDLC-specific shared tables. Stage execution tracking,        │
│   contract lineage, governance audit, data quality aggregates, stage gate        │
│   records — concepts every DDLC agent has but that are DDLC-domain-specific.    │
│                                                                                 │
│   Shared across all DDLC agents. Cross-agent queries live here.                 │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   TIER 1 — Agent Metadata (Generic)                                             │
│   Schema: agent_metadata                                                        │
│                                                                                 │
│   Framework-level tables common to ANY agent, not DDLC-specific.                │
│   Execution log, artifact registry, decision log, event log, audit log,         │
│   agent run header, configuration snapshot, error catalog.                       │
│                                                                                 │
│   Reusable by any agent system — the meta-agent, DDLC agents, or future agents. │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Why Three Tiers

| Design Goal | How the Tiers Serve It |
|---|---|
| **Reusability** | Tier 1 is agent-generic. If we build non-DDLC agents later, they use the same execution log, artifact registry, and audit trail. No duplication. |
| **Domain specificity without pollution** | DDLC concepts (stages, contracts, governance gates) live in Tier 2, not in the generic framework. Tier 1 stays clean. |
| **Agent isolation** | Each DDLC agent's local tables live in Tier 3's own schema. One agent's profiling results don't pollute another's namespace. |
| **Cross-agent querying** | Tier 2 tables let you ask cross-agent questions: "Which agents failed Stage 3 last week?" "What's the average quality score across all agents?" |
| **Schema evolution independence** | Tiers evolve at different rates. Tier 1 is stable. Tier 2 changes as DDLC patterns mature. Tier 3 changes per agent. Separate schemas = separate migration tracks. |

---

## 3. Tier 1 — Agent Metadata (Generic)

**Schema**: `agent_metadata`

These tables are universal — any agent (meta-agent, DDLC agents, future agents) can use them. They capture the fundamental operational concerns that every agent system needs.

### 3.1 Table Inventory

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`agent_run_header`** | One row per agent execution run. The "master record" that all other tables FK to. | `run_id`, `agent_id`, `agent_version`, `run_mode` (build/change/rerun), `initiated_by`, `started_at`, `completed_at`, `final_status`, `parent_run_id` (for sub-runs) |
| **`execution_log`** | Ordered log of every node/step executed within a run. | `log_id`, `run_id`, `node_name`, `phase`, `sequence_number`, `started_at`, `completed_at`, `status`, `input_token_count`, `output_token_count`, `model_used`, `duration_ms` |
| **`artifact_registry`** | Central catalog of every artifact produced across all runs. | `artifact_id`, `run_id`, `artifact_type` (spec, architecture, impl_doc, code, test, config), `artifact_name`, `version`, `storage_path`, `checksum`, `created_at`, `created_by_node` |
| **`artifact_manifest`** | Maps which artifacts belong to a given run — the "bill of materials." | `manifest_id`, `run_id`, `artifact_id`, `role` (input/output/intermediate), `phase_produced` |
| **`decision_log`** | Records every significant decision point: what was decided, who decided, and why. | `decision_id`, `run_id`, `node_name`, `decision_type` (routing/approval/rejection/escalation), `decision_value`, `decided_by` (human/system), `rationale`, `timestamp` |
| **`audit_log`** | Immutable, append-only security/compliance audit trail. Who did what, when, from where. | `audit_id`, `run_id`, `action` (submit_spec, approve_gate, reject_gate, override, config_change), `actor`, `actor_type` (human/system), `target_entity`, `before_state`, `after_state`, `timestamp`, `ip_address` |
| **`event_log`** | Fine-grained event stream for observability. Designed for consumption by dashboards, alerts, and replay. | `event_id`, `run_id`, `event_type`, `event_source` (node/coordinator/gate/system), `payload` (JSONB), `severity` (info/warn/error), `timestamp` |
| **`error_catalog`** | Structured record of every error encountered, with classification. | `error_id`, `run_id`, `node_name`, `error_type` (validation/llm/timeout/infrastructure), `error_code`, `message`, `stack_trace`, `recoverable`, `resolution` (retry/fix/escalate), `timestamp` |
| **`config_snapshot`** | Captures the full configuration state at the start of each run. Enables exact reproduction. | `snapshot_id`, `run_id`, `config_type` (model_tiers/safety_valves/settings), `config_body` (JSONB), `captured_at` |

### 3.2 Design Principles

- **`run_id` is the universal FK**: Every table ties back to `agent_run_header.run_id`. You can reconstruct everything about a run by joining on it.
- **Append-only where it matters**: `audit_log` and `event_log` are strictly append-only. No updates, no deletes.
- **JSONB for flexible payloads**: `event_log.payload` and `config_snapshot.config_body` use JSONB for schema-flexible data that varies by event type or config version.
- **Timestamps everywhere**: Every row carries a `timestamp` or `created_at`. The metadata framework is a timeline.

---

## 4. Tier 2 — DDLC Shared Metadata

**Schema**: `ddlc_shared_metadata`

These tables capture concepts specific to the Data Development Lifecycle — stages, contracts, governance, data quality — that are shared across all DDLC agents. Every DDLC agent writes to these tables; cross-agent queries read from them.

### 4.1 Table Inventory

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`stage_execution`** | Records every stage execution across all DDLC agents. Answers: "How did Stage X perform for Agent Y on this run?" | `stage_exec_id`, `run_id`, `agent_id`, `stage_id`, `stage_name`, `stage_type`, `started_at`, `completed_at`, `status`, `records_in`, `records_out`, `records_quarantined` |
| **`stage_gate_record`** | Records every gate check (human or automated) at stage boundaries. | `gate_id`, `run_id`, `stage_id`, `gate_type` (human_approval/automated_check/quality_gate), `gate_status` (passed/failed/overridden), `checked_by`, `timestamp`, `notes` |
| **`contract_registry`** | Master catalog of all data contracts across all agents. Enables cross-agent lineage. | `contract_id`, `agent_id`, `contract_name`, `contract_version`, `direction` (input/output/internal), `schema_definition` (JSONB), `registered_at`, `status` (active/deprecated/draft) |
| **`contract_lineage`** | Tracks producer → consumer relationships between contracts. "Agent A's output feeds Agent B's input." | `lineage_id`, `producer_agent_id`, `producer_contract_id`, `consumer_agent_id`, `consumer_contract_id`, `established_at`, `status` |
| **`governance_audit`** | Records every governance rule evaluation. "PII masking rule was applied to field X in Stage Y." | `gov_audit_id`, `run_id`, `agent_id`, `stage_id`, `rule_id`, `rule_name`, `rule_type` (pii_masking/access_control/retention/quality), `evaluation_result` (pass/fail/warning), `details` (JSONB), `timestamp` |
| **`data_quality_summary`** | Aggregated quality metrics per stage per run. Feeds quality dashboards. | `quality_id`, `run_id`, `agent_id`, `stage_id`, `total_records`, `valid_records`, `quarantined_records`, `quality_score`, `rules_evaluated`, `rules_passed`, `rules_failed`, `timestamp` |
| **`shared_component_usage`** | Tracks which DDLC agents use which shared components, with version. | `usage_id`, `agent_id`, `component_id`, `component_version`, `usage_type` (import/extend/wrap), `first_used_at`, `last_used_at` |

### 4.2 Design Principles

- **Agent-addressable**: Every row carries `agent_id`. Cross-agent queries are natural: `WHERE agent_id IN (...)` or aggregate across all agents.
- **Lineage-first**: `contract_lineage` is the backbone for impact analysis. "If I change Agent A's output contract, who breaks?" is a SQL join, not a graph traversal.
- **Governance as data**: Governance evaluations are rows in a table, not log messages buried in stdout. They are queryable, reportable, and auditable.

---

## 5. Tier 3 — Agent-Specific Metadata

**Schema**: `ddlc_<agent_name>_metadata` (one schema per DDLC agent)

These tables hold metadata unique to a single agent — data that no other agent needs and that would pollute the shared schema.

### 5.1 Examples (Varies Per Agent)

| Table (Example) | Purpose |
|---|---|
| **`source_profiling_results`** | Profiling statistics from the agent's specific data sources (column distributions, null rates, cardinality) |
| **`staging_state`** | Intermediate processing state the agent tracks between stages (e.g., batch watermarks, offset positions, dedup state) |
| **`custom_quality_rules`** | Agent-specific quality rules that go beyond the shared framework (e.g., domain-specific business rules) |
| **`reconciliation_log`** | Source-to-target reconciliation results for this agent's specific data flows |
| **`notification_log`** | Agent-specific notification history (who was notified, when, about what failure) |

### 5.2 Design Principles

- **Self-contained**: Each agent schema is independent. Dropping an agent drops its schema cleanly.
- **No cross-agent reads**: Other agents do not query Tier 3 tables. If data needs to be shared, it belongs in Tier 2.
- **Convention-driven**: The meta-agent generates the Tier 3 schema DDL as part of the build process, based on the agent's implementation documents.

---

## 6. How the Tiers Relate to the Architecture

### 6.1 Metadata Framework vs. Blackboard State

The blackboard (LangGraph state) and the metadata framework serve different masters:

```
                ┌──────────────────────────────────┐
                │        BLACKBOARD STATE          │
                │      (LangGraph Checkpoints)     │
                │                                  │
                │  In-memory workflow state.        │
                │  Drives routing & projection.     │
                │  Scoped to one run.               │
                │  Checkpointed for resumability.   │
                └──────────┬───────────────────────┘
                           │
                     writes to (at key points)
                           │
                           ▼
                ┌──────────────────────────────────┐
                │      METADATA FRAMEWORK          │
                │      (Postgres Tables)           │
                │                                  │
                │  Persistent operational history.  │
                │  Survives across runs.            │
                │  Queryable by operators.          │
                │  Feeds dashboards & audits.       │
                │                                  │
                │  ┌────────────────────────────┐  │
                │  │  Tier 1: agent_metadata    │  │
                │  ├────────────────────────────┤  │
                │  │  Tier 2: ddlc_shared_meta  │  │
                │  ├────────────────────────────┤  │
                │  │  Tier 3: agent-specific    │  │
                │  └────────────────────────────┘  │
                └──────────────────────────────────┘
```

### 6.2 When Does Metadata Get Written?

Metadata writes are **side effects** of node execution, not part of the blackboard state itself:

| Event | Blackboard Action | Metadata Action |
|---|---|---|
| Run begins | Initialize state with spec, portfolio context | Write `agent_run_header`, `config_snapshot` |
| Node starts executing | Update current phase/node flags | Write `execution_log` entry (started) |
| Node completes | Write results to blackboard fields | Update `execution_log` entry (completed), write `event_log` |
| Artifact produced | Store artifact reference in blackboard (for projection) | Write `artifact_registry`, `artifact_manifest` |
| Human gate reached | Set gate flag, pause | Write `decision_log` (pending), `event_log` |
| Human approves/rejects | Update approval flag, resume | Write `decision_log` (decided), `audit_log` |
| Error occurs | Write error flag (for routing) | Write `error_catalog`, `event_log` |
| Stage executes (DDLC agent) | Update stage completion in workflow state | Write `stage_execution`, `data_quality_summary`, `governance_audit` |
| Run completes | Set terminal state | Update `agent_run_header` (final_status, completed_at) |

### 6.3 Metadata Framework vs. Portfolio Memory

The architecture document defines **portfolio memory** (agent registry, shared component index, dependency graph, build history). The metadata framework is **complementary, not overlapping**:

| Concern | Where It Lives | Why |
|---|---|---|
| Agent registry (what agents exist, their versions, status) | Portfolio memory (`src/portfolio/registry.py`) | Master data — the source of truth for "what agents do we have" |
| Build history (what was built, when, from which spec) | Portfolio memory (`src/portfolio/history.py`) | Aggregated summary for portfolio-level queries |
| Execution log (every node invocation within a run) | Metadata framework (Tier 1 `execution_log`) | Fine-grained operational detail, not portfolio-level |
| Artifact registry (every artifact produced) | Metadata framework (Tier 1 `artifact_registry`) | Cross-run, cross-agent catalog of outputs |
| Audit trail (who approved what, when) | Metadata framework (Tier 1 `audit_log`) | Compliance/security concern, not portfolio management |
| Contract lineage (producer → consumer) | Metadata framework (Tier 2 `contract_lineage`) | DDLC operational concern, not portfolio-level |
| Governance evaluations | Metadata framework (Tier 2 `governance_audit`) | DDLC operational concern |
| Cross-agent dependencies | Portfolio memory (`src/portfolio/dependencies.py`) | Portfolio-level — informs design decisions |

> [!NOTE]
> Portfolio memory and the metadata framework share the same Postgres instance. They are separated by **schema** and **purpose**, not by database. Portfolio memory is *design-time* knowledge ("what should we build?"). The metadata framework is *runtime* knowledge ("what happened when we ran it?").

---

## 7. Schema Naming Convention

| Tier | Schema Name | Example |
|---|---|---|
| Tier 1 | `agent_metadata` | `agent_metadata.execution_log` |
| Tier 2 | `ddlc_shared_metadata` | `ddlc_shared_metadata.stage_execution` |
| Tier 3 | `ddlc_{agent_name}_metadata` | `ddlc_customer_pipeline_metadata.source_profiling_results` |

All three schemas live in the same Postgres database alongside the LangGraph checkpoint schema, the portfolio schema, and the pgvector schema.

---

## 8. Open Questions

These are design decisions to resolve as we iterate:

| # | Question | Options | Notes |
|---|---|---|---|
| 1 | **Metadata write mechanism** — How do nodes write metadata? | (a) Direct SQL calls in each node, (b) A metadata service/client abstraction, (c) LangGraph callback hooks that intercept node lifecycle events | Option (c) is cleanest — keeps metadata writes out of specialist logic. LangGraph supports custom callbacks. |
| 2 | **Retention policy** — How long do we keep fine-grained metadata (event_log, execution_log)? | (a) Keep everything, (b) Archive after N days to cold storage, (c) Aggregate and purge | Depends on volume. Start with (a), add (b) when needed. |
| 3 | **Tier 3 schema generation** — Who creates the agent-specific schema? | (a) The meta-agent generates DDL as part of the build, (b) The agent self-provisions on first run, (c) A separate migration tool | Option (a) aligns with "the meta-agent builds everything." |
| 4 | **Migration strategy** — How do we evolve schemas across tiers? | (a) Alembic migrations per schema, (b) Manual DDL scripts, (c) Mixed — Alembic for Tiers 1-2, generated for Tier 3 | TBD during implementation. |
| 5 | **Event log format** — Should `event_log` use a standardized event envelope (CloudEvents, OpenTelemetry)? | (a) Custom JSONB, (b) CloudEvents-compatible, (c) OpenTelemetry spans | Start simple with (a), evolve if integration needs arise. |
| 6 | **Relationship between `portfolio.history` and `agent_metadata.agent_run_header`** — Is build history a view over run headers, or a separate aggregated table? | (a) View/materialized view, (b) Separate table with FK, (c) Merge into one | Need to resolve to avoid duplication. Likely (a). |

---

## 9. Decisions Log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Metadata lives outside the blackboard state | Separate Postgres schemas | The blackboard drives workflow execution; metadata captures operational history. Different lifetimes, different audiences, different query patterns. |
| 2 | Three-tier architecture | Tier 1 (generic) + Tier 2 (DDLC shared) + Tier 3 (agent-specific) | Separates reusable framework from domain-specific concerns from per-agent concerns. Each tier evolves independently. |
| 3 | Schema-per-tier in Postgres | `agent_metadata`, `ddlc_shared_metadata`, `ddlc_{agent}_metadata` | Clean namespace isolation. Drop a tier or agent without affecting others. Standard Postgres schema search_path management. |
| 4 | Audit log is append-only | No UPDATE or DELETE on `audit_log` | Compliance requirement. Audit trails must be immutable. |
| 5 | JSONB for flexible payloads | `event_log.payload`, `config_snapshot.config_body`, `governance_audit.details` | Event types and config formats evolve faster than the table schema. JSONB accommodates this without DDL changes. |

---

## 10. Next Steps

1. **Detailed DDL** — Column-level definitions, data types, indexes, constraints, and partitioning strategy for each table
2. **Metadata client design** — The abstraction layer that nodes use to write metadata (likely a callback-based approach)
3. **Retention & archival policy** — Rules for how long each table retains data and how old data is archived
4. **Dashboard design** — What operational dashboards consume from the metadata framework
5. **Integration with portfolio memory** — Resolve the overlap between `portfolio.history` and `agent_metadata.agent_run_header`

---

## 11. What This Document Does NOT Cover

- **Column-level DDL** — Data types, indexes, constraints, partitioning → a subsequent specification document
- **Metadata client implementation** — How nodes call the metadata layer → an implementation document
- **Dashboard / reporting layer** — What dashboards exist and what queries they run → a design specification
- **Data retention policies** — How long metadata is kept, archival strategy → an operational document
- **Security & access control** — Who can read/write which tier → a security specification

