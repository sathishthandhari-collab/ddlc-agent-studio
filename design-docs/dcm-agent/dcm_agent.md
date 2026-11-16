# DDLC Agent Studio — DCM Schema Engine

## Declarative Schema Lifecycle with Alembic & SQLGlot from YAML Data Contracts

> **Document Status**: Production-Ready Design v0.2  
> **Last Updated**: 2026-11-14  
> **Audience**: Architecture & Development Team  
> **Prerequisites**:  
> - [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md)  
> - [`tier_1_data_dictionary.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/tier_1_data_dictionary.md)  

---

## 1. Document Purpose & Architectural Pivot

The metadata framework defines *what* persistent tables exist across three tiers and four concerns. This document defines *the engine that provisions and migrates those schemas* — the **DCM (Data Catalog Management) Schema Engine**.

### The Architectural Decision: Deterministic Compiler over LLM Parser

Earlier drafts explored using an LLM within the DCM CLI to interpret natural language requests into DDL (e.g., `$ dcm "add retry_count to execution_log"`). **We have explicitly stripped the natural-language LLM out of the DCM execution engine** in favor of a **100% deterministic, GitOps-driven compiler** powered by **SQLGlot** and **Alembic** reading directly from **YAML Data Contracts**.

| Concern | Why an LLM in DDL Execution Was Rejected | How Declarative YAML + SQLGlot + Alembic Solves It |
|---|---|---|
| **Determinism & Safety** | LLM token generation is probabilistic. Subtle hallucinations in types (`TIMESTAMP` vs `TIMESTAMPTZ`), missing `CASCADE` rules, or forgotten foreign keys risk schema and data corruption. | **SQLGlot AST validation** guarantees mathematically deterministic DDL emission. Zero hallucination risk. |
| **Source of Truth** | Ad-hoc CLI commands mean schema changes live in terminal history or database logs, breaking GitOps. | **YAML contracts in Git** are the single source of truth. Every schema modification is a version-controlled commit subject to pull request review. |
| **Meta-Agent Compatibility** | The Meta-Agent's Documentarian specialist *already* outputs structured YAML contracts. Converting `YAML → Natural Language → LLM → SQL` is an error-prone round-trip anti-pattern. | **Direct Compilation**: The engine compiles `YAML Contract → SQLGlot AST → Alembic Migration` directly and instantaneously. |
| **Migration Tooling** | Custom migration tracking reinvents dependency trees, rollbacks, and schema hashes. | **Alembic** is the battle-tested industry standard for Python database migrations (handles revision graphs, downgrade rollbacks, dry-run SQL generation). |

> [!NOTE]
> **Where does natural language belong?**  
> Upstream on the YAML files! If a developer or operator wants to edit a schema using natural language, an AI assistant (like Antigravity, Claude Code, or the Meta-Agent) modifies the YAML contract. The DCM engine then deterministically validates, diffs, and migrates the database.

---

## 2. Core Architecture & Workflow

```
┌────────────────────────────────────────────────────────┐
│             DECLARATIVE YAML DATA CONTRACTS            │
│                 (Single Source of Truth)               │
│                                                        │
│  • contracts/tier_1_agent_metadata.yaml                │
│  • contracts/tier_2_ddlc_shared.yaml                   │
│  • contracts/tier_3_{agent}_metadata.yaml              │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                        SQLGLOT                         │
│                  (Contract Compiler)                   │
│                                                        │
│  1. Ingests & validates YAML schema contracts          │
│  2. Resolves cross-schema FK dependencies & data types │
│  3. Builds SQLGlot Schema AST                          │
│  4. Emits canonical PostgreSQL DDL & constraints       │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                        ALEMBIC                         │
│                  (Migration Engine)                    │
│                                                        │
│  1. Compares compiled AST vs. live Postgres DB catalog │
│  2. Detects schema drift, added/altered columns        │
│  3. Generates versioned migration script in Git        │
│  4. Enforces Human Gates for destructive operations    │
│  5. Executes transactional migration (upgrade/downgrade│
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                 POSTGRESQL METADATA DB                 │
│                                                        │
│  • schema: agent_metadata                              │
│  • schema: ddlc_shared_metadata                        │
│  • schema: ddlc_{agent}_metadata                       │
│  • table:  public.alembic_version                      │
└────────────────────────────────────────────────────────┘
```

---

## 3. The YAML Contract Specification

Each tier's schema is authored as a declarative YAML contract adhering to standard Data Contract conventions (aligning with `tier_1_data_dictionary.md`).

### Example: Tier 1 YAML Contract Snippet (`contracts/tier_1_agent_metadata.yaml`)

```yaml
version: "1.0"
schema: "agent_metadata"
description: "Universal framework metadata for autonomous agent execution and observability."

tables:
  - name: "agent_run_header"
    description: "Master execution lifecycle record per agent run."
    columns:
      - name: "run_id"
        type: "UUID"
        primary_key: true
        nullable: false
        description: "Globally unique identifier for this run."
      - name: "parent_run_id"
        type: "UUID"
        nullable: true
        references: "agent_metadata.agent_run_header(run_id)"
        on_delete: "SET NULL"
        description: "Parent run ID for sub-agent orchestrations."
      - name: "agent_id"
        type: "VARCHAR(255)"
        nullable: false
      - name: "agent_version"
        type: "VARCHAR(64)"
        nullable: false
      - name: "run_mode"
        type: "VARCHAR(32)"
        nullable: false
        check: "run_mode IN ('build', 'change', 'pipeline', 'validation', 'evaluation')"
      - name: "started_at"
        type: "TIMESTAMPTZ"
        nullable: false
        default: "NOW()"
      - name: "completed_at"
        type: "TIMESTAMPTZ"
        nullable: true
      - name: "final_status"
        type: "VARCHAR(32)"
        nullable: false
        default: "'RUNNING'"
        check: "final_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'PAUSED_GATE')"
    indexes:
      - name: "idx_run_header_agent_lookup"
        columns: ["agent_id", "started_at"]
      - name: "idx_run_header_status"
        columns: ["final_status"]

  - name: "execution_log"
    description: "Ordered execution spans capturing telemetry, token counts, and step state."
    columns:
      - name: "log_id"
        type: "BIGSERIAL"
        primary_key: true
      - name: "run_id"
        type: "UUID"
        nullable: false
        references: "agent_metadata.agent_run_header(run_id)"
        on_delete: "CASCADE"
      - name: "node_name"
        type: "VARCHAR(128)"
        nullable: false
      - name: "duration_ms"
        type: "INTEGER"
        nullable: true
      - name: "details"
        type: "JSONB"
        nullable: false
        default: "'{}'::jsonb"
    indexes:
      - name: "idx_exec_log_run_seq"
        columns: ["run_id", "log_id"]
```

---

## 4. Responsibilities & Component Division

### 4.1 SQLGlot Compiler (`src/dcm/compiler.py`)
1. **Contract Ingestion**: Loads and validates YAML structure against a Pydantic `DataContractSchema`.
2. **Type Mapping & Dialect Safety**: Normalizes abstract types (`UUID`, `TIMESTAMPTZ`, `JSONB`, `BIGSERIAL`) into dialect-correct PostgreSQL types using SQLGlot expressions (`sqlglot.exp`).
3. **Constraint Resolution**: Validates that all `references` point to valid existing tables and columns, checking foreign key referential integrity in memory before generating SQL.
4. **DDL Generation**: Produces clean, formatted PostgreSQL DDL statements:
   - `CREATE SCHEMA IF NOT EXISTS`
   - `CREATE TABLE IF NOT EXISTS`
   - `CREATE INDEX IF NOT EXISTS`
   - `ALTER TABLE ... ADD CONSTRAINT`

### 4.2 Alembic Migration Manager (`src/dcm/migrator.py`)
1. **Catalog Introspection**: Connects to the target PostgreSQL database via SQLAlchemy and inspects live tables across `agent_metadata`, `ddlc_shared_metadata`, and `ddlc_{agent}_metadata`.
2. **Diff & Drift Detection**: Compares SQLGlot-compiled target schema AST against the live database catalog:
   - Identifies newly added tables and columns.
   - Identifies altered column types, nullability changes, and default values.
   - Identifies dropped columns or renamed entities.
3. **Versioned Migration Scripts**: Emits standard, auditable Python migration files into `alembic/versions/` with explicit `upgrade()` and `downgrade()` methods.
4. **Execution & History**: Executes migrations wrapped in atomic transactions and records state in `public.alembic_version`.

---

## 5. CLI Interface & Developer Workflow

The DCM tool provides a fast, deterministic CLI for both developers and automated CI/CD pipelines:

```bash
# 1. Validate YAML contracts (syntax, data types, FK referential integrity)
$ dcm validate contracts/

# 2. Plan / Diff: inspect what changes are required against the live database
$ dcm plan
Comparing live database against contracts/...
[INFO] Schema: agent_metadata
  + CREATE TABLE agent_run_header (...)
  + CREATE TABLE execution_log (...)
[INFO] Schema: ddlc_shared_metadata
  + CREATE TABLE contract_registry (...)
No destructive operations detected.

# 3. Generate a versioned Alembic migration file
$ dcm revision -m "initial_metadata_framework_baseline"
Created alembic/versions/20261114_01_initial_metadata_framework_baseline.py

# 4. Apply migrations to the database
$ dcm apply
Running migrations against postgresql://localhost:5432/ddlc_metadata...
Applying 20261114_01_initial_metadata_framework_baseline... OK
All schemas up to date at revision: 20261114_01.

# 5. Check migration status
$ dcm status
Current revision: 20261114_01 (head)
Target schemas:
  - agent_metadata (synced)
  - ddlc_shared_metadata (synced)
```

### Dry-Run SQL Generation (Zero DB Execution)
For security audits or DBA review, DCM can emit raw SQL without executing it:
```bash
$ dcm apply --sql > deploy_migration.sql
```

---

## 6. Human Gates for Destructive Operations

Safety valves prevent accidental data loss during schema evolution:

| Change Type | Classification | Behavior |
|---|---|---|
| `CREATE TABLE`, `CREATE INDEX` | **Non-Destructive** | Auto-applies cleanly in automated pipelines |
| `ADD COLUMN (NULLABLE / WITH DEFAULT)` | **Non-Destructive** | Auto-applies cleanly |
| `ALTER COLUMN TYPE` | **Destructive** | Flags type conversion warning; requires `--approve-destructive` or interactive `[y/N]` confirmation |
| `DROP COLUMN` | **Destructive** | Flags potential data loss; requires explicit approval |
| `DROP TABLE` / `DROP SCHEMA` | **Destructive** | Strict human confirmation required |

When a destructive operation is detected:
```bash
$ dcm apply

[WARNING] Destructive operations detected in revision 20261114_02:
  ⚠ DROP COLUMN retry_attempts FROM execution_log (Data loss!)
  ⚠ ALTER COLUMN agent_id TYPE VARCHAR(128) (Potential truncation!)

Execution halted by Human Gate.
To proceed, re-run with: dcm apply --approve-destructive
```

---

## 7. Integration with the Meta-Agent

The Meta-Agent's **Builder** specialist does not write raw SQL or call an LLM to generate DDL. It invokes the DCM Python library directly during the Construction phase:

```
Meta-Agent Workflow
    │
    ▼
[Phase 5: DOCUMENTARIAN]
    Outputs Tier 3 YAML Contract:
    `contracts/ddlc_customer_pipeline_metadata.yaml`
    │
    ▼
[Phase 6: BUILDER]
    Calls DCM Schema Engine programmatically:
    ┌─────────────────────────────────────────────────────────────┐
    │ from dcm.engine import SchemaEngine                        │
    │                                                             │
    │ engine = SchemaEngine(db_url=config.database_url)           │
    │ # 1. Validate contract                                      │
    │ engine.validate_contract("contracts/tier_3_customer.yaml") │
    │ # 2. Generate and apply migration                           │
    │ revision = engine.migrate(                                  │
    │     contract_path="contracts/tier_3_customer.yaml",         │
    │     message="provision_customer_pipeline_tier3"             │
    │ )                                                           │
    └─────────────────────────────────────────────────────────────┘
    │
    ▼
[Phase 7: VALIDATOR]
    Verifies tables exist in Postgres with exact columns & types.
```

---

## 8. Folder & Package Structure

The DCM engine is organized as a clean, modular Python package located at `src/metadata/schema_engine/` (with a CLI entrypoint `dcm`):

```
ddlc-agent-studio/
├── contracts/                               # Declarative YAML Data Contracts
│   ├── tier_1_agent_metadata.yaml           # Universal agent framework schema
│   ├── tier_2_ddlc_shared_metadata.yaml     # Shared DDLC lifecycle catalog & lineage
│   └── tier_3_examples/                     # Templates for agent-specific metadata
│
├── alembic/                                 # Alembic migration management
│   ├── env.py                               # Alembic environment configured for multi-schema
│   ├── script.py.mako                       # Migration script template
│   └── versions/                            # Version-controlled migration history (.py)
│       └── 20261114_01_baseline.py
│
├── src/
│   └── metadata/
│       ├── schema_engine/                   # DCM Engine Package
│       │   ├── __init__.py
│       │   ├── cli.py                       # Typer/Click CLI entry point (`dcm`)
│       │   ├── compiler.py                  # SQLGlot contract compiler & AST validator
│       │   ├── migrator.py                  # Alembic runner & diff generator
│       │   ├── models.py                    # Pydantic schemas for YAML contract format
│       │   └── safety.py                    # Destructive operation detection & gate logic
│       │
│       ├── catalog.py                       # Operational queries for Tier 2 catalog
│       ├── lineage.py                       # Lineage tracking queries
│       └── callbacks.py                     # LangGraph lifecycle logging callbacks
│
├── tests/
│   └── schema_engine/
│       ├── test_compiler.py                 # Tests YAML -> SQLGlot AST -> Postgres DDL
│       ├── test_migrator.py                 # Tests diff detection against test DB
│       └── test_safety.py                   # Tests destructive gate detection
│
├── alembic.ini                              # Alembic global configuration
└── pyproject.toml                           # Dependencies: sqlglot, alembic, sqlalchemy
```

---

## 9. Technology Stack

| Layer | Tool | Rationale |
|---|---|---|
| **Contract Language** | YAML | Human-readable, machine-parseable, version-controlled in Git. Standard for Data Contracts. |
| **Contract AST & DDL** | **SQLGlot** | Extremely fast, pure-Python SQL parser and transpiler. Builds typed ASTs, validates dialect types, generates flawless PostgreSQL DDL. Zero external dependencies. |
| **Migration & State** | **Alembic + SQLAlchemy** | De facto standard for Python database migrations. Solves dependency graphs, schema drift, atomic upgrade/downgrade scripts, and revision tracking. |
| **Contract Validation** | **Pydantic v2** | Validates YAML contract structure (required keys, field types, index syntax) before compilation. |
| **CLI Framework** | **Typer** | Ergonomic, typed CLI with auto-generated help, colorized diffs, and confirmation prompts. |

---

## 10. Decisions Log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Execution Engine | Deterministic YAML + SQLGlot + Alembic | Replaced probabilistic LLM parser with deterministic compiler. Eliminates hallucination risk, ensures 100% reproducible DDL, and aligns with Schema-as-Code. |
| 2 | Source of Truth | Declarative YAML contracts in Git | All schemas (Tiers 1–3) are committed to Git as YAML contracts. Changes are reviewed in PRs and applied via migrations. |
| 3 | Upstream LLM Assistance | LLM assists via YAML, not SQL execution | Users or agents modify YAML contracts with AI assistance; the DCM tool deterministically compiles and applies them. |
| 4 | Migration Versioning | Standard Alembic version tree | Uses Alembic's linear/branching revision tree in `alembic/versions/` and `public.alembic_version`. No custom migration tables needed. |
| 5 | Multi-Schema Support | Postgres `search_path` & explicit schema prefixes | Alembic and SQLGlot explicitly qualify table names (`agent_metadata.run_header`, etc.) ensuring clean 3-tier namespace isolation. |
| 6 | Safety & Gates | Automated gate for destructive ops | Additive operations run automatically; destructive operations require explicit `--approve-destructive` flag or user confirmation. |

---

## 11. What This Document Does NOT Cover

- **Live Data Ingestion**: DCM manages DDL and schema structure, not data loading or ETL jobs.
- **RAG pgvector Embeddings Table**: The vector store table structure is managed by the RAG storage module.
- **Target Platform (Databricks / Snowflake) DDL**: DCM governs the internal PostgreSQL metadata framework; target platform pipelines are compiled by the Builder specialist strategies.
