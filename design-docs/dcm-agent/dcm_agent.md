# DDLC Agent Studio — DCM Agent Design

## Data Catalog Management Agent — Schema Lifecycle for the Metadata Framework

> **Document Status**: Draft v0.1  
> **Last Updated**: 2026-11-12  
> **Audience**: Architecture & Development Team  
> **Prerequisite**: [01_metadata_framework.md](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md) — read that first

---

## 1. Document Purpose

The metadata framework defines *what* persistent tables exist across three tiers and four concerns. This document defines *who manages those tables* — the **DCM (Data Catalog Management) Agent**.

The DCM agent owns the full schema lifecycle for the metadata framework: creating tables, altering columns, adding indexes, managing migrations, and tracking schema history. It is the **first agent built** in the DDLC Agent Studio — both because the metadata framework must exist before the meta-agent can run, and because it serves as a proving ground for the core stack (LiteLLM, Instructor, Postgres).

---

## 2. Agent Type — Lightweight CLI Agent

The DCM agent is **not** a full LangGraph state machine. It is a focused, lightweight CLI agent:

| Aspect | DCM Agent | Meta-Agent (for comparison) |
|---|---|---|
| **Architecture** | Python CLI with LLM calls | LangGraph state machine with subgraphs |
| **Complexity** | Single-purpose, linear workflow | Multi-phase, branching, looping |
| **State** | Stateless per invocation (migration history lives in Postgres) | Stateful blackboard with checkpoints |
| **Human gates** | Only for destructive operations (ALTER/DROP) | Four structural gates in the workflow |
| **UI** | CLI / terminal output | Chainlit conversational UI |
| **Specialists** | None — single agent | Six domain specialists |

This is deliberate. The DCM agent's job — generate DDL, validate it, apply it — does not need a state machine. It is a **tool**, not a workflow. If its scope grows later (e.g., automated schema evolution, cross-agent migration coordination), it can be upgraded to a LangGraph agent. The design accommodates that by keeping its interfaces clean.

---

## 3. Responsibilities

### 3.1 What the DCM Agent Does

1. **Schema Creation** — Generate CREATE TABLE/INDEX DDL from a schema specification (natural language or structured YAML)
2. **Schema Evolution** — Generate ALTER TABLE DDL for adding columns, renaming fields, changing types, adding constraints
3. **Migration Tracking** — Maintain a migration history table that records every DDL change applied, when, and by whom
4. **Validation** — Before applying any DDL:
   - Check FK integrity (does the referenced table/column exist?)
   - Check naming convention compliance (schema naming rules from the metadata framework)
   - Check backward compatibility (will this ALTER break existing queries?)
   - Check for duplicates (is this column already there?)
5. **Human Gating** — Present DDL for human review before executing destructive operations
6. **Seed Script Generation** — Generate the initial bootstrap DDL for Tiers 1 and 2 (one-time setup)
7. **Tier 3 Generation** — When the meta-agent builds a new DDLC agent, the DCM agent generates the agent-specific schema

### 3.2 What the DCM Agent Does NOT Do

- **It does not manage application code** — no Python, no agent logic, no configuration files
- **It does not manage RAG schemas** — pgvector tables for the RAG layer are managed separately
- **It does not manage LangGraph checkpoint schemas** — those are managed by `langgraph-checkpoint-postgres`
- **It does not manage Databricks / target platform schemas** — it manages only the Agent Studio's internal metadata

### 3.3 Scope Boundary

```
DCM Agent Manages                          DCM Agent Does NOT Manage
─────────────────                          ─────────────────────────
agent_metadata.*                           langgraph checkpoint tables
ddlc_shared_metadata.*                     pgvector RAG tables
ddlc_{agent}_metadata.*                    Target platform schemas (Databricks, etc.)
dcm_metadata.migration_history             Application code or config
```

---

## 4. How It Works

### 4.1 Invocation Modes

The DCM agent operates in three modes:

**Mode 1 — Interactive (CLI)**
```
$ dcm "add a retry_count integer column to execution_log with default 0"

DCM Agent:
  Schema:    agent_metadata
  Table:     execution_log
  Operation: ALTER TABLE

  Generated DDL:
    ALTER TABLE agent_metadata.execution_log
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;

  Validation:
    ✓ Table exists
    ✓ Column does not already exist
    ✓ Naming convention compliant
    ✓ Non-destructive operation

  → Applying automatically (CREATE/ADD operations are autonomous)
  ✓ Applied. Migration #47 recorded.
```

**Mode 2 — Destructive (Human Gate)**
```
$ dcm "rename column 'error_type' to 'error_category' in error_catalog"

DCM Agent:
  Schema:    agent_metadata
  Table:     error_catalog
  Operation: ALTER TABLE (RENAME COLUMN — destructive)

  Generated DDL:
    ALTER TABLE agent_metadata.error_catalog
    RENAME COLUMN error_type TO error_category;

  Validation:
    ✓ Table exists
    ✓ Column exists
    ⚠ Destructive: column rename may break existing queries
    ⚠ 3 references found in src/metadata/ that use 'error_type'

  → Requires human approval (destructive operation)
  Approve? [y/N]:
```

**Mode 3 — Programmatic (Called by Meta-Agent)**
```python
# The meta-agent's Builder specialist calls DCM to generate Tier 3
dcm_client.generate_tier3_schema(
    agent_name="customer_pipeline",
    tables=[
        {"name": "source_profiling_results", "columns": [...]},
        {"name": "staging_state", "columns": [...]}
    ]
)
# Returns generated DDL + migration record
```

### 4.2 The Processing Pipeline

Every DCM invocation follows the same pipeline:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│    PARSE     │────▶│   RESOLVE    │────▶│   VALIDATE   │────▶│    GATE      │────▶│    APPLY     │
│              │     │              │     │              │     │              │     │              │
│ Natural lang │     │ Identify     │     │ FK integrity │     │ Destructive? │     │ Execute DDL  │
│ or YAML  →   │     │ schema,      │     │ Naming rules │     │ → human gate │     │ Record in    │
│ structured   │     │ table, op    │     │ Compat check │     │ Non-destr?   │     │ migration    │
│ intent       │     │ type         │     │ Dup check    │     │ → auto-apply │     │ history      │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
     LLM                 LLM                Deterministic          Conditional          SQL exec
```

- **PARSE**: LLM (via LiteLLM + Instructor) interprets the request into a structured `MigrationIntent` (Pydantic model)
- **RESOLVE**: LLM + DB introspection identifies the target schema, table, and operation type against the live database schema
- **VALIDATE**: Deterministic checks — no LLM needed. Query `information_schema` for FK references, column existence, naming patterns
- **GATE**: Route decision — destructive ops (ALTER column type, RENAME, DROP) require human approval; additive ops (CREATE TABLE, ADD COLUMN, ADD INDEX) auto-apply
- **APPLY**: Execute DDL via connection, record in `dcm_metadata.migration_history`

---

## 5. The Migration History Table

The DCM agent's own metadata lives in a dedicated schema that is **manually seeded once** (bootstrap — see Section 7).

**Schema**: `dcm_metadata`

| Table | Purpose | Key Columns |
|---|---|---|
| **`migration_history`** | Immutable, append-only log of every DDL change applied | `migration_id` (serial), `target_schema`, `target_table`, `operation_type` (CREATE/ALTER/DROP/INDEX), `ddl_statement` (text), `description`, `applied_by` (human/dcm_auto), `approved_by` (human/null for auto), `applied_at` (timestamp), `rollback_ddl` (text, nullable), `status` (applied/rolled_back/failed) |
| **`schema_snapshot`** | Periodic snapshot of the full schema state (via `pg_dump --schema-only`) for point-in-time comparison | `snapshot_id`, `captured_at`, `schema_name`, `snapshot_body` (text — DDL dump), `trigger` (manual/milestone/pre-migration) |
| **`naming_conventions`** | Configurable naming rules the validator checks against | `rule_id`, `scope` (schema/table/column/index), `pattern` (regex), `description`, `active` |

### Design Principles

- **`migration_history` is append-only**: Once a migration is recorded, it is never modified. Rollbacks are recorded as new entries with `operation_type = 'ROLLBACK'` and a reference to the original `migration_id`.
- **Rollback DDL is generated at apply time**: For every ALTER, the DCM agent generates the inverse DDL and stores it in `rollback_ddl`. Not all operations are reversible (DROP COLUMN loses data), and those are flagged.
- **Schema snapshots are milestones**: Taken before major migrations and at user-requested points. They enable "what did the schema look like on Tuesday?" queries.

---

## 6. Technology Stack

The DCM agent uses the **same core stack** as the meta-agent (proving ground):

| Layer | Technology | Why |
|---|---|---|
| **LLM Access** | LiteLLM | Same provider-agnostic interface. Parse natural language intent into structured DDL. |
| **Structured Output** | Instructor + Pydantic v2 | `MigrationIntent`, `DDLStatement`, `ValidationResult` — all typed Pydantic models from LLM responses. |
| **Database** | PostgreSQL (psycopg3 / asyncpg) | Direct connection to the same Postgres instance. `information_schema` introspection for validation. |
| **CLI** | Typer or Click | Simple CLI framework for interactive mode. |

### Key Pydantic Models

```python
class MigrationIntent(BaseModel):
    """Structured representation of what the user wants to change."""
    target_schema: str          # e.g., "agent_metadata"
    target_table: str           # e.g., "execution_log"
    operation: OperationType    # CREATE_TABLE, ADD_COLUMN, ALTER_COLUMN, DROP_COLUMN, etc.
    columns: list[ColumnSpec]   # For CREATE/ADD — column definitions
    description: str            # Human-readable description of the change
    
class DDLStatement(BaseModel):
    """Generated SQL ready for execution."""
    sql: str                    # The DDL statement
    rollback_sql: str | None    # Inverse DDL for rollback (None if irreversible)
    is_destructive: bool        # True if ALTER type change, RENAME, DROP
    warnings: list[str]         # Compatibility warnings

class ValidationResult(BaseModel):
    """Result of pre-apply validation checks."""
    passed: bool
    checks: list[ValidationCheck]  # Each check with status and message
    blocking_issues: list[str]     # Issues that prevent apply
    warnings: list[str]            # Non-blocking concerns
```

---

## 7. Bootstrap — The Chicken-and-Egg Problem

The DCM agent manages all schemas, but it needs its own `dcm_metadata` schema to exist first. This is resolved with a **one-time manual seed**:

### Bootstrap Sequence

```
Step 1 (Manual, one-time):
  Run bootstrap.sql — creates dcm_metadata schema with
  migration_history, schema_snapshot, naming_conventions tables.
  This is the ONLY manual DDL in the system.

Step 2 (DCM Agent):
  DCM creates agent_metadata schema (Tier 1 tables).
  All DDL is tracked in dcm_metadata.migration_history.

Step 3 (DCM Agent):
  DCM creates ddlc_shared_metadata schema (Tier 2 tables).
  All DDL is tracked.

Step 4 (DCM Agent, per new DDLC agent):
  DCM creates ddlc_{agent}_metadata schema (Tier 3 tables).
  Called by meta-agent during Construction phase.
```

The `bootstrap.sql` seed script is version-controlled and idempotent (uses `CREATE TABLE IF NOT EXISTS`). It is the only SQL a human ever runs directly.

---

## 8. Integration with the Meta-Agent

### 8.1 Build-Time Integration

When the meta-agent builds a new DDLC agent, the Construction phase calls the DCM agent to generate and apply the Tier 3 schema:

```
Meta-Agent (Builder specialist)
    │
    │── "Build agent 'customer_pipeline' with these stages and contracts"
    │
    ├── Generate agent code, config, tests (Builder's job)
    │
    └── Request Tier 3 schema from DCM Agent
         │
         DCM Agent:
         ├── Parse stage definitions → derive Tier 3 tables
         ├── Generate CREATE TABLE DDL
         ├── Validate against Tier 2 FKs
         ├── Apply (CREATE is non-destructive → auto-apply)
         └── Return migration record
```

### 8.2 Development-Time Integration

During development, the human interacts with DCM directly via CLI:

```
$ dcm "I need a new table in ddlc_shared_metadata for tracking 
       data contract SLA violations — columns: violation_id, contract_id 
       (FK to contract_registry), agent_id, violated_at, sla_type, 
       expected_value, actual_value, severity"

DCM Agent generates CREATE TABLE, validates FKs, applies.
```

This is the primary use case during MVP iteration — evolving the metadata schema through conversation.

---

## 9. Folder Structure

```
dcm-agent/
├── config/
│   ├── settings.yaml            # Database connection, LLM model config
│   └── naming_rules.yaml        # Default naming conventions (loaded into dcm_metadata.naming_conventions)
│
├── src/
│   ├── cli.py                   # Typer CLI entry point — interactive and programmatic modes
│   ├── parser.py                # LLM-powered intent parsing (natural language → MigrationIntent)
│   ├── resolver.py              # Schema introspection and target resolution
│   ├── validator.py             # Deterministic validation checks (FK, naming, compat, duplicates)
│   ├── generator.py             # DDL generation from MigrationIntent (SQL + rollback SQL)
│   ├── executor.py              # DDL execution and migration_history recording
│   ├── gate.py                  # Human gate logic — prompt for approval on destructive ops
│   └── models.py                # Pydantic models (MigrationIntent, DDLStatement, ValidationResult)
│
├── seed/
│   └── bootstrap.sql            # One-time seed: dcm_metadata schema and tables
│
├── tests/
│   ├── test_parser.py           # Intent parsing tests with mock LLM responses
│   ├── test_validator.py        # Validation logic tests against test schemas
│   └── test_generator.py        # DDL generation tests — input intent → expected SQL
│
├── pyproject.toml
└── README.md
```

---

## 10. Human Gate Rules

| Operation Type | Destructive? | Gate Rule |
|---|---|---|
| `CREATE TABLE` | No | Auto-apply |
| `CREATE INDEX` | No | Auto-apply |
| `ADD COLUMN` (with default or nullable) | No | Auto-apply |
| `ADD COLUMN` (NOT NULL, no default) | ⚠ | Human gate — may fail on existing rows |
| `ALTER COLUMN` (type change) | Yes | Human gate |
| `RENAME COLUMN` | Yes | Human gate — may break queries |
| `RENAME TABLE` | Yes | Human gate — may break queries |
| `DROP COLUMN` | Yes | Human gate — data loss |
| `DROP TABLE` | Yes | Human gate — data loss |
| `DROP SCHEMA` | Yes | Human gate — mass data loss |

---

## 11. Decisions Log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | DCM agent scope | MVP (not post-MVP) | Metadata framework will undergo heavy iteration. Manual DDL management across three schemas creates unsustainable friction. DCM is also the simplest agent to build — proving ground for the stack. |
| 2 | Agent type | Lightweight CLI agent (not LangGraph state machine) | DCM's workflow is linear: parse → validate → apply. No branching, looping, or multi-phase subgraphs needed. Can be upgraded to LangGraph later if scope grows. |
| 3 | Human gate strategy | Destructive operations only | CREATE/ADD auto-apply for iteration speed. ALTER type/RENAME/DROP require human approval to prevent data loss or broken queries. |
| 4 | Bootstrap strategy | One-time manual seed for DCM's own tables | Avoids chicken-and-egg. `bootstrap.sql` is the only manual DDL ever executed. Everything else goes through DCM. |
| 5 | Migration tracking | Append-only `migration_history` table | Full audit trail of every DDL change. Rollback DDL generated and stored at apply time. Snapshots for point-in-time schema comparison. |
| 6 | Technology stack | Same as meta-agent (LiteLLM, Instructor, Postgres) | Validates the core stack before the meta-agent is built. One set of dependencies. |
| 7 | Build order | DCM agent is built FIRST, before the meta-agent | The metadata framework must exist before the meta-agent can checkpoint, log, or register. DCM creates it. |

---

## 12. Build Order — DCM Agent as Foundation

```
┌─────────────────────────────────────────────────────────────────────┐
│  BUILD SEQUENCE                                                     │
│                                                                     │
│  1. bootstrap.sql ──▶ dcm_metadata schema (one-time, manual)       │
│                                                                     │
│  2. Build DCM Agent ──▶ First agent, proves the stack               │
│                                                                     │
│  3. DCM creates Tier 1 ──▶ agent_metadata schema                   │
│                                                                     │
│  4. DCM creates Tier 2 ──▶ ddlc_shared_metadata schema             │
│                                                                     │
│  5. Build Meta-Agent ──▶ Uses Tier 1 & 2 for checkpoints, logs     │
│                                                                     │
│  6. Meta-Agent builds DDLC agents ──▶ calls DCM for Tier 3 schemas │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 13. What This Document Does NOT Cover

- **Detailed DDL for `dcm_metadata` tables** — Column-level definitions → the `bootstrap.sql` file itself
- **LLM prompt engineering** — How the parser interprets natural language into `MigrationIntent` → implementation detail
- **Cross-database migrations** — DCM manages one Postgres instance only. Multi-database sync is out of scope.
- **Schema diffing / drift detection** — Comparing live schema against expected state → post-MVP enhancement
- **Automated rollback orchestration** — Rolling back a chain of dependent migrations → post-MVP enhancement

