# DDLC Agent Studio — Package & Naming Standards

Canonical library allowlists, restricted packages, and deterministic naming rules across Layer 1 Meta-Agent and Layer 2 DDLC Agents.

---

## 1. Approved Python Libraries (`pyproject.toml`)

All studio and generated DDLC agent dependencies must align with the platform's standardized stack (Python `>=3.12`):

### 1.1 Orchestration, LLM & Database Stack

| Concern | Approved Packages | Minimum Version | Purpose |
|---|---|---|---|
| **Orchestration & State** | `langgraph`, `langgraph-checkpoint-postgres` | `>=0.2.0`, `>=2.0.0` | Stateful DAG execution, `interrupt()` gates, PostgreSQL checkpointing |
| **LLM & Structured Output** | `litellm`, `instructor`, `tenacity` | `>=1.50.0`, `>=1.7.0`, `>=9.0.0` | Unified provider routing, Pydantic v2 structured output, transient retries |
| **Contracts & Config** | `pydantic`, `pydantic-settings`, `pyyaml` | `>=2.9.0`, `>=2.6.0`, `>=6.0.2` | Contract models, strict YAML validation, nested env overrides |
| **Callbacks & Chunking** | `langchain-core`, `langchain-text-splitters` | `>=0.3.0` | `BaseCallbackHandler` telemetry interface and baseline text utilities |
| **SQL AST & Migrations** | `sqlglot`, `alembic`, `sqlalchemy` | `>=25.0.0`, `>=1.14.0`, `>=2.0.35` | SQL AST verification, dialect transpilation, deterministic DDL migrations |
| **PostgreSQL & Vectors** | `psycopg[binary,pool]`, `asyncpg`, `pgvector` | `>=3.2.0`, `>=0.30.0`, `>=0.3.6` | Sync/async pooled DB access, `vector(1536)` ops |

### 1.2 RAG, Templating, CLI & Target DDLC Stack

| Concern | Approved Packages | Minimum Version | Purpose |
|---|---|---|---|
| **RAG Reranking & Tokens** | `flashrank`, `tiktoken` | `>=0.2.9`, `>=0.8.0` | Lightweight ONNX cross-encoder reranking (`ms-marco-TinyBERT-L-2-v2`) & token counting |
| **Templating, CLI & UI** | `jinja2`, `typer`, `rich`, `python-dotenv`, `chainlit` | `>=3.1.4`, `>=0.13.0`, `>=13.9.0` | Code/prompt rendering (`StrictUndefined`), CLIs (`dcm`), interactive UI gates |
| **Target DDLC Tools** | `dbt-core`, `duckdb` | `>=1.7.0` | Generated dbt pipeline compilation and ephemeral local sandbox testing |
| **Dev & Quality** | `pytest`, `pytest-asyncio`, `ruff` | `>=8.3.0`, `>=0.24.0`, `>=0.7.0` | Zero-infra unit/async testing and `E/F/I/W` linting |

---

## 2. Avoided & Restricted Libraries

| Disallowed / Avoided Technology | Reason | Approved Replacement |
|---|---|---|
| **PyTorch / `transformers` / `sentence-transformers`** | Adds 2GB+ container image bloat and heavy GPU/CPU overhead for reranking (`ADR-RAG-04`). | `flashrank` (4 MB ONNX runtime) + `litellm.aembedding` API embeddings |
| **External Vector DBs (Pinecone, Qdrant, Chroma, Milvus) or Neo4j** | Violates Unified Storage Engine principle; introduces distributed transaction desync (`ADR-RAG-02`). | PostgreSQL 17 + `pgvector` (`rag_knowledge` schema) |
| **LangChain LCEL Chains, Agents, or `langchain_community` LLM wrappers** | Opaque abstraction layers and unpredictable prompt chains (`constraints.avoided_libraries`). | Explicit `langgraph` `StateGraph` + `litellm` + `instructor` |
| **`pyspark` / `pandera` (in Layer 2 AI Agents)** | Retired Layer 3 batch ETL constructs; Layer 2 DDLC agents operate on typed engineering contracts, not 500k-row DataFrames (Decision 26). | Pydantic v2 models (`contracts_pydantic.py`) + target SQL/dbt code |
| **`datetime.utcnow()`** | Deprecated in Python 3.12+ and produces naive datetimes (Decision 31). | `datetime.now(timezone.utc)` |

---

## 3. Identifier & File Naming Conventions

- **Agent Names (`agent.name`)**: Strict lowercase `snake_case` matching `^[a-z][a-z0-9_]*$` (e.g., `dbt_pipeline_builder_agent`, `snowflake_lineage_inspector_agent`).
- **Stage IDs & Filenames**: Lowercase `snake_case` matching `^[a-z][a-z0-9_]*$` (canonical stages: `discover`, `plan`, `generate`, `dry_run`, `review`, `deliver`).
  - Stage YAML: `impl_docs/stages/stage_<id>.yaml`
  - Graph node function: `node_<id>_stage` (e.g., `node_plan_stage`)
  - Prompt template: `src/prompts/stage_<id>.jinja2` (referenced as `prompts/stage_<id>.jinja2`)
- **Contract Names & Pydantic Models**:
  - Contract YAML: `impl_docs/contracts/<snake_case_name>.yaml` (e.g., `transformation_plan.yaml`)
  - Compiled Pydantic class: `PascalCase` (`TransformationPlan`, `DbtArtifactBundle`, `DeliveryManifest`)
  - Nested object models: explicit `items.model_name` in `PascalCase` (`TableProfile`, `ColumnProfile`, `StagingModelSpec`, `MartModelSpec`).
- **Config Paths & Secrets**: Lowercase hierarchical dot-paths in `config.yaml` (`${config.sources.postgres_oltp.uri}`) mapped 1:1 via `env_nested_delimiter="__"` (`settings.sources.postgres_oltp.uri`), and lowercase secret keys (`${secrets.source_db_password}`, `${secrets.github_token}`) resolved from uppercase env vars (`SOURCE_DB_PASSWORD`, `GITHUB_TOKEN`).
- **Git Delivery Branches**: `feat/<scope>-<run_id[:8]>` (e.g., `feat/dbt-models-{run_id}`).

---

## 4. PostgreSQL Metadata & RAG Schema Naming

All database namespaces follow strict tier isolation configured in `config/settings.yaml`:

| Layer | PostgreSQL Schema Name | Ownership & Purpose |
|---|---|---|
| **Tier 1 (Generic)** | `agent_metadata` | Universal run headers, spans, artifacts, decision logs, immutable `audit_log` |
| **Tier 2 (DDLC Shared)** | `ddlc_shared_metadata` | Cross-agent catalog (`agent_registry`, `component_registry`, `contract_registry`), lineage, quality & gate audits |
| **Tier 3 (Agent-Specific)** | `ddlc_<agent_name>_metadata` | Isolated operational tables per DDLC agent (e.g., `ddlc_dbt_builder_metadata`), compiled via DCM |
| **RAG Corpus** | `rag_knowledge` | `knowledge_documents` & `knowledge_chunks` with 6 domain-partitioned partial HNSW indexes |
