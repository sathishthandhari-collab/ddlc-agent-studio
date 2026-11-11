# DDLC Agent Studio — Metadata Framework

## Three-Tier Unified Metadata Architecture

> **Document Status**: Draft v0.2  
> **Last Updated**: 2026-11-11  
> **Audience**: Architecture & Development Team  
> **Prerequisite**: [02_architecture.md](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/02_architecture.md) — read that first

---

## 1. Document Purpose

The architecture document defines the meta-agent's internal structure: coordinator, specialists, blackboard state, and knowledge layer. The blackboard is **in-memory workflow state** — it holds the data a running graph needs to route, project, and checkpoint. It is *not* the right place for persistent operational metadata like audit trails, artifact registries, execution logs, or event histories.

This document defines the **Metadata Framework** — the unified persistent metadata layer for the entire DDLC Agent Studio. It replaces the "portfolio memory" concept from the architecture document's earlier drafts by absorbing those tables (agent registry, component index, dependency graph, build history) into a single, concern-organized framework.

### 1.1 Why "Portfolio Memory" Was Merged

The architecture document originally described two separate persistence layers: "portfolio memory" (agent registry, components, dependencies, build history) and a planned "metadata framework" (execution logs, audit trails, artifacts). In practice, these are the same concern — persistent structured metadata — split by accident of when they were designed, not by any real architectural boundary:

| "Portfolio Memory" Table | What It Actually Is | Where It Now Lives |
|---|---|---|
| Agent registry | **Catalog** | Tier 2 — `agent_registry` |
| Shared component index | **Catalog** | Tier 2 — `component_registry` |
| Cross-agent dependencies | **Lineage** | Tier 2 — `agent_dependency` |
| Build history & metrics | **Operational** | Tier 2 — `build_summary` |

Industry platforms (DataHub, Unity Catalog, Airflow, Dagster) organize persistent metadata by **concern** — catalog, lineage, operational, audit — not by when-in-the-lifecycle you think about it. We follow that pattern.

### 1.2 What the Blackboard Holds vs. What the Metadata Framework Holds

| Concern | Blackboard (LangGraph State) | Metadata Framework (Postgres Tables) |
|---|---|---|
| **Purpose** | Drive the current workflow execution — routing, projection, checkpointing | Record persistent operational history — what happened, what was produced, who decided what |
| **Lifetime** | Per-run (survives checkpoints, but scoped to one execution) | Permanent (survives across runs, agents, and versions) |
| **Audience** | Coordinator & specialists during execution | Operators, auditors, dashboards, cross-agent queries, post-mortem analysis |
| **Examples** | Current phase flag, parsed spec, approved architecture, builder output | Execution log entries, artifact manifests, audit trail, decision records, agent registry, contract lineage |

The blackboard says *"here is the approved architecture so the Documentarian can work."*  
The metadata framework says *"architecture v2 was approved by user X at timestamp T, after 2 revision rounds, replacing v1 which was rejected for reason Y."*

---

## 2. The Three Tiers

The metadata framework is organized into three tiers, each building on the previous one. This layering separates generic agent concerns from DDLC-specific concerns from individual agent concerns. Within each tier, tables are organized by **concern**: catalog, lineage, operational, and audit.

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
│   Tier 1 tables + DDLC-specific shared tables. Agent registry, component        │
│   catalog, contract lineage, stage execution, governance audit, data quality    │
│   — concepts every DDLC agent has but that are DDLC-domain-specific.            │
│                                                                                 │
│   Shared across all DDLC agents. Cross-agent queries live here.                 │
│   Absorbs all former "portfolio memory" tables.                                 │
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

### Organizing by Concern

Within each tier, tables group into four concerns:

| Concern | What It Answers | Industry Analogues |
|---|---|---|
| **Catalog** | *"What assets exist?"* — agents, components, contracts, artifacts | DataHub, Unity Catalog, Amundsen, AWS Glue Catalog |
| **Lineage** | *"How do things connect?"* — producer/consumer, dependency graphs, impact paths | DataHub lineage, OpenLineage, Marquez |
| **Operational** | *"What happened when things ran?"* — execution records, metrics, timing, errors | Airflow metadata DB, Dagster event log, OpenTelemetry |
| **Audit** | *"Who decided what, when?"* — approvals, rejections, overrides, governance checks | Immutable audit tables, compliance logs |

---

## 3. Tier 1 — Agent Metadata (Generic)

**Schema**: `agent_metadata`

These tables are universal — any agent (meta-agent, DDLC agents, future agents) can use them. They capture the fundamental operational concerns that every agent system needs.

### 3.1 Catalog Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`artifact_registry`** | Central catalog of every artifact produced across all runs. | `artifact_id`, `run_id`, `artifact_type` (spec, architecture, impl_doc, code, test, config), `artifact_name`, `version`, `storage_path`, `checksum`, `created_at`, `created_by_node` |
| **`artifact_manifest`** | Maps which artifacts belong to a given run — the "bill of materials." | `manifest_id`, `run_id`, `artifact_id`, `role` (input/output/intermediate), `phase_produced` |

### 3.2 Operational Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`agent_run_header`** | One row per agent execution run. The "master record" that all other tables FK to. | `run_id`, `agent_id`, `agent_version`, `run_mode` (build/change/rerun), `initiated_by`, `started_at`, `completed_at`, `final_status`, `parent_run_id` (for sub-runs) |
| **`execution_log`** | Ordered log of every node/step executed within a run. | `log_id`, `run_id`, `node_name`, `phase`, `sequence_number`, `started_at`, `completed_at`, `status`, `input_token_count`, `output_token_count`, `model_used`, `duration_ms` |
| **`event_log`** | Fine-grained event stream for observability. Designed for consumption by dashboards, alerts, and replay. | `event_id`, `run_id`, `event_type`, `event_source` (node/coordinator/gate/system), `payload` (JSONB), `severity` (info/warn/error), `timestamp` |
| **`error_catalog`** | Structured record of every error encountered, with classification. | `error_id`, `run_id`, `node_name`, `error_type` (validation/llm/timeout/infrastructure), `error_code`, `message`, `stack_trace`, `recoverable`, `resolution` (retry/fix/escalate), `timestamp` |
| **`config_snapshot`** | Captures the full configuration state at the start of each run. Enables exact reproduction. | `snapshot_id`, `run_id`, `config_type` (model_tiers/safety_valves/settings), `config_body` (JSONB), `captured_at` |

### 3.3 Audit Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`audit_log`** | Immutable, append-only security/compliance audit trail. Who did what, when, from where. | `audit_id`, `run_id`, `action` (submit_spec, approve_gate, reject_gate, override, config_change), `actor`, `actor_type` (human/system), `target_entity`, `before_state`, `after_state`, `timestamp`, `ip_address` |
| **`decision_log`** | Records every significant decision point: what was decided, who decided, and why. | `decision_id`, `run_id`, `node_name`, `decision_type` (routing/approval/rejection/escalation), `decision_value`, `decided_by` (human/system), `rationale`, `timestamp` |

### 3.4 Design Principles

- **`run_id` is the universal FK**: Every table ties back to `agent_run_header.run_id`. You can reconstruct everything about a run by joining on it.
- **Append-only where it matters**: `audit_log` and `event_log` are strictly append-only. No updates, no deletes.
- **JSONB for flexible payloads**: `event_log.payload` and `config_snapshot.config_body` use JSONB for schema-flexible data that varies by event type or config version.
- **Timestamps everywhere**: Every row carries a `timestamp` or `created_at`. The metadata framework is a timeline.

---

## 4. Tier 2 — DDLC Shared Metadata

**Schema**: `ddlc_shared_metadata`

These tables capture concepts specific to the Data Development Lifecycle — agents, components, stages, contracts, governance, data quality — that are shared across all DDLC agents. This tier absorbs all former "portfolio memory" tables alongside DDLC-specific operational and audit tables.

### 4.1 Catalog Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`agent_registry`** | Master catalog of all DDLC agents — what exists, current version, status, ownership. *(Formerly "portfolio memory — agent registry".)* | `agent_id`, `agent_name`, `agent_version`, `description`, `architecture_pattern`, `runtime`, `status` (active/deprecated/draft/building), `owner`, `created_at`, `updated_at` |
| **`component_registry`** | Catalog of all shared components — what reusable pieces exist and what they do. Includes pgvector embeddings for semantic search ("find something like a PII masker"). *(Formerly "portfolio memory — shared component index".)* | `component_id`, `component_name`, `component_version`, `description`, `interface_spec` (JSONB), `storage_path`, `embedding` (vector), `status`, `created_at` |
| **`contract_registry`** | Master catalog of all data contracts across all agents. Enables cross-agent lineage. | `contract_id`, `agent_id`, `contract_name`, `contract_version`, `direction` (input/output/internal), `schema_definition` (JSONB), `registered_at`, `status` (active/deprecated/draft) |

### 4.2 Lineage Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`contract_lineage`** | Tracks producer → consumer relationships between contracts. "Agent A's output feeds Agent B's input." | `lineage_id`, `producer_agent_id`, `producer_contract_id`, `consumer_agent_id`, `consumer_contract_id`, `established_at`, `status` |
| **`agent_dependency`** | Cross-agent dependency graph. Answers: "If I change Agent A, which agents are affected?" *(Formerly "portfolio memory — cross-agent dependencies".)* | `dependency_id`, `upstream_agent_id`, `downstream_agent_id`, `dependency_type` (data/component/config), `contract_id`, `established_at`, `status` |
| **`component_usage`** | Tracks which DDLC agents use which shared components, with version. | `usage_id`, `agent_id`, `component_id`, `component_version`, `usage_type` (import/extend/wrap), `first_used_at`, `last_used_at` |

### 4.3 Operational Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`build_summary`** | Aggregated summary of each build run — operational metrics and revision counts. *(Formerly "portfolio memory — build history".)* | `build_id`, `run_id`, `agent_id`, `spec_version`, `clarification_rounds`, `architecture_revisions`, `impl_doc_revisions`, `build_fix_attempts`, `total_duration_ms`, `outcome` (success/partial/failed), `completed_at` |
| **`stage_execution`** | Records every stage execution across all DDLC agents. Answers: "How did Stage X perform for Agent Y on this run?" | `stage_exec_id`, `run_id`, `agent_id`, `stage_id`, `stage_name`, `stage_type`, `started_at`, `completed_at`, `status`, `records_in`, `records_out`, `records_quarantined` |
| **`data_quality_summary`** | Aggregated quality metrics per stage per run. Feeds quality dashboards. | `quality_id`, `run_id`, `agent_id`, `stage_id`, `total_records`, `valid_records`, `quarantined_records`, `quality_score`, `rules_evaluated`, `rules_passed`, `rules_failed`, `timestamp` |

### 4.4 Audit Tables

| Table | Purpose | Key Columns (Conceptual) |
|---|---|---|
| **`governance_audit`** | Records every governance rule evaluation. "PII masking rule was applied to field X in Stage Y." | `gov_audit_id`, `run_id`, `agent_id`, `stage_id`, `rule_id`, `rule_name`, `rule_type` (pii_masking/access_control/retention/quality), `evaluation_result` (pass/fail/warning), `details` (JSONB), `timestamp` |
| **`stage_gate_record`** | Records every gate check (human or automated) at stage boundaries. | `gate_id`, `run_id`, `stage_id`, `gate_type` (human_approval/automated_check/quality_gate), `gate_status` (passed/failed/overridden), `checked_by`, `timestamp`, `notes` |

### 4.5 Design Principles

- **Agent-addressable**: Every row carries `agent_id`. Cross-agent queries are natural: `WHERE agent_id IN (...)` or aggregate across all agents.
- **Lineage-first**: `contract_lineage` and `agent_dependency` are the backbone for impact analysis. "If I change Agent A's output contract, who breaks?" is a SQL join.
- **Governance as data**: Governance evaluations are rows in a table, not log messages buried in stdout. They are queryable, reportable, and auditable.
- **Semantic search for components**: `component_registry` stores pgvector embeddings alongside relational data. "Find something like a PII masker" is a vector similarity search on the `embedding` column. This is the one place where the metadata framework uses vector search.

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
- **Convention-driven**: The meta-agent generates the Tier 3 schema DDL as part of the build output. The human reviews and executes the DDL manually.

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
                │  Unified persistent layer.        │
                │  Organized by concern:            │
                │                                  │
                │  ┌────────────────────────────┐  │
                │  │  Catalog    — what exists   │  │
                │  │  Lineage    — how connected │  │
                │  │  Operational — what happened│  │
                │  │  Audit      — who decided   │  │
                │  └────────────────────────────┘  │
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

Metadata writes happen via **LangGraph callback hooks** that intercept node lifecycle events. Specialists do not write metadata directly — the callback infrastructure handles it automatically. This keeps metadata concerns out of specialist logic entirely.

| Event | Blackboard Action | Metadata Action (via callbacks) |
|---|---|---|
| Run begins | Initialize state with spec | Write `agent_run_header`, `config_snapshot` |
| Node starts executing | Update current phase/node flags | Write `execution_log` entry (started) |
| Node completes | Write results to blackboard fields | Update `execution_log` entry (completed), write `event_log` |
| Artifact produced | Store artifact reference in blackboard (for projection) | Write `artifact_registry`, `artifact_manifest` |
| Human gate reached | Set gate flag, pause | Write `decision_log` (pending), `event_log` |
| Human approves/rejects | Update approval flag, resume | Write `decision_log` (decided), `audit_log` |
| Error occurs | Write error flag (for routing) | Write `error_catalog`, `event_log` |
| Stage executes (DDLC agent) | Update stage completion in workflow state | Write `stage_execution`, `data_quality_summary`, `governance_audit` |
| Run completes | Set terminal state | Update `agent_run_header` (final_status, completed_at) |
| Agent delivered | — | Write `agent_registry`, `component_registry`, `agent_dependency`, `build_summary` |

### 6.3 Metadata Framework vs. RAG Knowledge Layer

The RAG knowledge layer (pgvector + document embeddings) and the metadata framework are genuinely different concerns:

| Aspect | RAG Knowledge Layer | Metadata Framework |
|---|---|---|
| **Data type** | Unstructured documents (library docs, pattern catalogs, code conventions) | Structured relational data (tables, rows, FKs) |
| **Query type** | Semantic search (embed query → vector similarity → top-k chunks) | SQL queries (joins, filters, aggregates) |
| **Purpose** | Inform specialist reasoning during execution | Record what happened for operational/audit purposes |
| **Grows via** | Document ingestion pipeline | Side-effect writes during agent runs |

The one **overlap**: `component_registry` in Tier 2 stores pgvector embeddings for semantic component discovery ("find something like a PII masker"). This is a catalog table that also supports vector search — it lives in the metadata framework, not in the RAG layer.

---

## 7. Schema Naming Convention

| Tier | Schema Name | Example |
|---|---|---|
| Tier 1 | `agent_metadata` | `agent_metadata.execution_log` |
| Tier 2 | `ddlc_shared_metadata` | `ddlc_shared_metadata.agent_registry` |
| Tier 3 | `ddlc_{agent_name}_metadata` | `ddlc_customer_pipeline_metadata.source_profiling_results` |

All three schemas live in the same Postgres database alongside the LangGraph checkpoint schema and the pgvector RAG schema.

---

## 8. Schema Provisioning Strategy

### Tiers 1 & 2 — Pre-provisioned

Tiers 1 and 2 are created once during initial setup. Their DDL is version-controlled in the project repository and applied manually.

### Tier 3 — Generated by Meta-Agent, Executed by Human

When the meta-agent builds a new DDLC agent, it generates Tier 3 DDL as part of the build output — alongside the agent code, configuration, and tests. The human reviews the DDL and executes it manually.

This is the **Option B** approach — simple and human-controlled for MVP. The system is designed so that a future **DCM (Data Catalog Management) agent** could absorb the DDL generation logic and handle schema lifecycle (creation, migration, evolution) autonomously. But that is a post-MVP concern.

> [!NOTE]
> The DDL generation logic should be isolated into its own module within the Builder specialist, making it easy to extract into a DCM agent later.

---

## 9. Decisions Log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Metadata lives outside the blackboard state | Separate Postgres schemas | The blackboard drives workflow execution; metadata captures operational history. Different lifetimes, different audiences, different query patterns. |
| 2 | Three-tier architecture | Tier 1 (generic) + Tier 2 (DDLC shared) + Tier 3 (agent-specific) | Separates reusable framework from domain-specific concerns from per-agent concerns. Each tier evolves independently. |
| 3 | Kill "portfolio memory" as separate concept | Merge into metadata framework Tier 2 | Portfolio memory and metadata framework were the same concern split by accident. Industry practice (DataHub, Unity Catalog) organizes by concern (catalog/lineage/operational/audit), not by lifecycle phase. |
| 4 | Organize by concern | Catalog, Lineage, Operational, Audit | Follows industry-standard metadata platform organization. Makes cross-cutting queries natural. |
| 5 | Schema-per-tier in Postgres | `agent_metadata`, `ddlc_shared_metadata`, `ddlc_{agent}_metadata` | Clean namespace isolation. Drop a tier or agent without affecting others. Standard Postgres schema search_path management. |
| 6 | Audit log is append-only | No UPDATE or DELETE on `audit_log` | Compliance requirement. Audit trails must be immutable. |
| 7 | JSONB for flexible payloads | `event_log.payload`, `config_snapshot.config_body`, `governance_audit.details` | Event types and config formats evolve faster than the table schema. JSONB accommodates this without DDL changes. |
| 8 | Metadata write mechanism | LangGraph callback hooks | Keeps metadata writes out of specialist logic. Callback hooks intercept node lifecycle events (start, complete, error) and write metadata as a side effect. Clean separation. |
| 9 | Retention policy | Keep everything | Start simple. Add archival/aggregation if volume becomes a problem. No premature optimization. |
| 10 | Tier 3 schema provisioning | Meta-agent generates DDL, human executes | Option B — simple and human-controlled for MVP. Designed for future DCM agent to absorb this responsibility. |
| 11 | Event log format | Custom JSONB | No premature standardization (CloudEvents, OpenTelemetry). Start with custom format, evolve if integration needs arise. |
| 12 | `build_summary` vs. `agent_run_header` | `build_summary` in Tier 2 aggregates from `agent_run_header` in Tier 1 | `agent_run_header` is the generic per-run record. `build_summary` adds DDLC-specific counters (clarification rounds, architecture revisions). Could become a view/materialized view over `agent_run_header` — resolve during implementation. |

---

## 10. Next Steps

1. **Detailed DDL** — Column-level definitions, data types, indexes, constraints, and partitioning strategy for each table
2. **Metadata callback design** — The LangGraph callback hooks that intercept node lifecycle events and write metadata
3. **Dashboard design** — What operational dashboards consume from the metadata framework
4. **DCM agent feasibility** — Design doc for a future Data Catalog Management agent that handles schema lifecycle

---

## 11. What This Document Does NOT Cover

- **Column-level DDL** — Data types, indexes, constraints, partitioning → a subsequent specification document
- **Metadata callback implementation** — How LangGraph callbacks write metadata → an implementation document
- **Dashboard / reporting layer** — What dashboards exist and what queries they run → a design specification
- **Data retention policies** — How long metadata is kept, archival strategy → an operational document (decision: keep everything for now)
- **Security & access control** — Who can read/write which tier → a security specification
- **DCM agent design** — A future agent for schema lifecycle management → post-MVP design document
