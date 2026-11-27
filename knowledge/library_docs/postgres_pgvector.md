# PostgreSQL 17 & pgvector 0.8+ — Storage, Indexing & Checkpointing Reference

Technical reference for `pgvector>=0.3.6`, `psycopg[binary,pool]>=3.2.0`, `asyncpg>=0.30.0`, and `langgraph-checkpoint-postgres>=2.0.0` on DDLC Studio's unified PostgreSQL engine.

---

## 1. Unified Schema Layout & Extensions

A single PostgreSQL 17 (`pgvector/pgvector:pg17`) instance hosts:
- `agent_metadata` (Tier 1 telemetry, artifacts, decisions, immutable `audit_log`)
- `ddlc_shared_metadata` (Tier 2 catalog, `component_registry` with `vector(1536)`, lineage)
- `ddlc_<agent_name>_metadata` (Tier 3 agent schemas)
- `rag_knowledge` (`knowledge_documents`, `knowledge_chunks`)
- LangGraph `PostgresSaver` checkpoint tables

Required extensions provisioned by `infra/postgres/init/01_init.sh`:
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
```

---

## 2. Filtered-HNSW Recall Collapse & Partial Indexes (`ADR-RAG-07`)

When a single global HNSW index is filtered by a selective `WHERE domain = $1` clause, HNSW graph traversal can terminate early and miss valid neighbors in small partitions (such as Day-0 `past_decisions` with only 3 ADRs).

DDLC Studio eliminates recall collapse with two mechanisms:
1. **Six Domain-Partitioned Partial HNSW Indexes** (`rag.md` §6.1):
   ```sql
   CREATE INDEX idx_rag_hnsw_past_decisions ON rag_knowledge.knowledge_chunks
   USING hnsw (embedding vector_cosine_ops) WHERE domain = 'past_decisions'
   WITH (m = 16, ef_construction = 64);
   ```
   (One partial HNSW index each for `library_docs`, `org_standards`, `pattern_catalog`, `past_decisions`, `component_docs`, `code_conventions`.)
2. **Iterative Scanning in `pgvector` 0.8+**: Every retrieval query runs inside a transaction/block setting:
   ```sql
   SET LOCAL hnsw.iterative_scan = 'strict_order';
   ```
   Cosine distance uses `<=>`, and similarity is `1.0 - (embedding <=> $1::vector)`.

---

## 3. Dual `tsvector` Hybrid Search & RRF (`ADR-RAG-03`)

Standard `'english'` stemming mangles technical identifiers (`StrictUndefined` -> `strictundefin`, `dbt_project` -> `dbt` + `project`). `rag_knowledge.knowledge_chunks` stores **two** generated `tsvector` columns indexed with GIN:

```sql
tsv_content_simple tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
tsv_content_english tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
```

- **Query Parsing**: Use `websearch_to_tsquery` instead of `plainto_tsquery` (which forces brittle boolean `AND` across every query word).
- **Weighted Ranking**: Prioritize exact code matches (`'simple'` × `1.5`) over stemmed prose (`'english'` × `1.0`) via `ts_rank_cd(...)`.
- **Reciprocal Rank Fusion (RRF)**: Combine dense (`vector_candidates`) and sparse (`text_candidates`) ranks inside PostgreSQL with `k_rrf = 60`:
  `COALESCE(1.0 / (60 + v.dense_rank), 0.0) + COALESCE(1.0 / (60 + t.sparse_rank), 0.0)`.

---

## 4. `psycopg` v3, `asyncpg` & LangGraph `PostgresSaver`

### Sync Pooled `psycopg` v3 (`src/metadata/db.py`)
`MetadataDatabase` uses `psycopg_pool.ConnectionPool` with `dict_row` and a `configure` callback that registers pgvector adapters and leaves the connection in idle state:
```python
from pgvector.psycopg import register_vector

def _configure_connection(self, conn) -> None:
    register_vector(conn)
    conn.execute("SET statement_timeout = 30000")
    conn.commit()  # Required by psycopg_pool so connection starts idle
```
Wrap JSON/dict parameters with `from psycopg.types.json import Jsonb` and use `with self._db.transaction() as conn:` for fail-fast atomic writes.

### Async `asyncpg` (`src/rag/retrieval/hybrid_search.py`)
`HybridRAGRetriever` uses `asyncpg.Pool` with `$1`, `$2::vector`, `$3` positional parameters (`str(query_embedding)` cast to `::vector`).

### LangGraph Checkpointing
Generated LangGraph agents compile with:
```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver.from_conn_string(settings.CHECKPOINTER_DB_URI)
graph = workflow.compile(checkpointer=checkpointer)
# Invoked with config={"configurable": {"thread_id": run_id}, "callbacks": [telemetry_handler]}
```
Runtime connections use the least-privilege `ddlc_app` role (no `DROP` or `TRUNCATE`).
