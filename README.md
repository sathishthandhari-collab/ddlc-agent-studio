# DDLC Agent Studio

Deterministic **Meta-Agent Factory**, **DCM Schema Engine**, **3-Tier Unified Metadata Framework**, and **Hybrid RAG Knowledge Platform** for Data Development Lifecycle (DDLC) agents.

## Core Principles

1. **The Graph Orchestrates, the LLM Reasons** — Workflow control lives in deterministic state machines with explicit edges, retry caps, and human gates.
2. **Skeleton vs. Muscle** — Orchestration wiring, contracts, and PostgreSQL DDL (`src/dcm/`) are compiled deterministically from YAML specs; LLMs write only isolated leaf logic.
3. **Single Source of Truth** — Declarative YAML contracts (`contracts/`) govern PostgreSQL schemas via SQLGlot + Alembic.
4. **Decoupled Metadata (Decision 30)** — Passive operational telemetry (`src/metadata/callbacks.py`) never blocks execution; transactional catalog, lineage, and delivery mutations (`src/metadata/catalog.py`, `src/metadata/lineage.py`) fail fast.

## Quickstart

```bash
# 1. Install dependencies into Python 3.12+ virtual environment
uv sync

# 2. Configure local environment variables
cp .env.example .env

# 3. Validate contracts, inspect plan, and apply metadata schemas
.venv/Scripts/dcm.exe validate contracts/
.venv/Scripts/dcm.exe plan contracts/
.venv/Scripts/dcm.exe apply

# 4. Run linters and test suite
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/pytest.exe -v
```
