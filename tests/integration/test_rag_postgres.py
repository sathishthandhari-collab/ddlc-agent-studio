"""Live PostgreSQL + pgvector integration tests for Step 5 RAG storage and hybrid retrieval."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from uuid import uuid4

import psycopg
import pytest
from src.core.settings import get_env_settings, get_studio_config
from src.rag.chunking.base import compute_sha256, count_tokens
from src.rag.retrieval.hybrid_search import HybridRAGRetriever
from src.rag.retrieval.reranker import FlashRankReranker
from src.rag.store.models import ALLOWED_DOMAINS, DocumentChunkDraft
from src.rag.store.pgvector_store import PgVectorRAGStore, _normalize_psycopg_uri


def _live_rag_uri_or_skip() -> str:
    """Resolve RAG_DB_URI (or fallback METADATA_DB_URI) and auto-skip if Postgres is unreachable."""
    env = get_env_settings()
    uri = env.rag_db_uri or env.metadata_db_uri
    if not uri:
        pytest.skip("RAG_DB_URI and METADATA_DB_URI are not configured.")
    try:
        conn = psycopg.connect(_normalize_psycopg_uri(uri), connect_timeout=2)
        conn.close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")
    return uri


def _unit_vector(dim_index: int, dims: int = 1536) -> list[float]:
    vec = [0.0] * dims
    vec[dim_index] = 1.0
    return vec


class _SyntheticUnitEmbedder:
    """Deterministic 1536-dim synthetic embedder avoiding external OpenAI API calls."""

    def __init__(self, query_dim_index: int = 0, dimensions: int = 1536) -> None:
        self.model_name = "synthetic-unit-test"
        self.dimensions = dimensions
        self.query_dim_index = query_dim_index

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [_unit_vector(self.query_dim_index, self.dimensions) for _ in texts]

    async def embed_query(self, query: str) -> list[float]:
        return _unit_vector(self.query_dim_index, self.dimensions)


class TestLiveRagSchemaAndPrivileges:
    def test_rag_schema_pgvector_version_indexes_and_least_privilege_grants(self) -> None:
        uri = _live_rag_uri_or_skip()
        schema_name = get_studio_config().rag.database.schema_name

        with psycopg.connect(_normalize_psycopg_uri(uri), connect_timeout=2) as conn:
            # 1. Verify pgvector >= 0.8 installed
            ext_row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            assert ext_row is not None, "pgvector extension 'vector' is not installed"
            version_parts = tuple(int(p) for p in str(ext_row[0]).split(".")[:2])
            assert version_parts >= (0, 8), f"Expected pgvector >= 0.8, found {ext_row[0]}"

            # 2. Verify tables exist
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (schema_name,),
                ).fetchall()
            }
            assert {"knowledge_documents", "knowledge_chunks"}.issubset(tables)

            # 3. Verify 6 partial HNSW indexes, 2 GIN indexes, and 3 B-tree indexes
            index_rows = conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s",
                (schema_name,),
            ).fetchall()
            indexes = {str(name): str(defn) for name, defn in index_rows}

            for domain in ALLOWED_DOMAINS:
                hnsw_name = f"idx_rag_hnsw_{domain}"
                assert hnsw_name in indexes, f"Missing partial HNSW index {hnsw_name}"
                assert "USING hnsw" in indexes[hnsw_name]
                assert "vector_cosine_ops" in indexes[hnsw_name]
                assert domain in indexes[hnsw_name]

            for gin_name in ("idx_rag_chunks_tsv_simple", "idx_rag_chunks_tsv_english"):
                assert gin_name in indexes, f"Missing GIN index {gin_name}"
                assert "USING gin" in indexes[gin_name]

            for btree_name in (
                "idx_rag_docs_domain",
                "idx_rag_chunks_domain",
                "idx_rag_chunks_doc_id",
            ):
                assert btree_name in indexes, f"Missing B-tree index {btree_name}"
                assert "USING btree" in indexes[btree_name]

            # 4. Verify ddlc_app least-privilege grants on rag_knowledge
            role_exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'ddlc_app'"
            ).fetchone()
            assert role_exists is not None, "Least-privilege role 'ddlc_app' not found"

            priv_row = conn.execute(
                "SELECT "
                "has_schema_privilege('ddlc_app', %s, 'USAGE'), "
                "has_schema_privilege('ddlc_app', %s, 'CREATE')",
                (schema_name, schema_name),
            ).fetchone()
            assert priv_row == (True, False)

            for table_name in ("knowledge_documents", "knowledge_chunks"):
                qualified = f"{schema_name}.{table_name}"
                table_privs = conn.execute(
                    "SELECT "
                    "has_table_privilege('ddlc_app', %s, 'SELECT'), "
                    "has_table_privilege('ddlc_app', %s, 'INSERT'), "
                    "has_table_privilege('ddlc_app', %s, 'UPDATE'), "
                    "has_table_privilege('ddlc_app', %s, 'DELETE'), "
                    "has_table_privilege('ddlc_app', %s, 'TRUNCATE')",
                    (qualified, qualified, qualified, qualified, qualified),
                ).fetchone()
                assert table_privs == (True, True, True, True, False), (
                    f"Unexpected ddlc_app privileges on {qualified}: {table_privs}"
                )


class TestLiveRagStoreAndHybridSearch:
    async def test_store_upsert_replace_dual_tsvector_and_hybrid_domain_isolation(self) -> None:
        uri = _live_rag_uri_or_skip()
        dims = get_studio_config().rag.embedding.dimensions
        suffix = uuid4().hex[:10]

        lib_source = f"__pytest__/lib_{suffix}.md"
        conv_source = f"__pytest__/conv_{suffix}.md"
        unique_symbol = f"StrictUndefinedToken_{suffix}"

        store = PgVectorRAGStore(db_uri=uri)
        created_sources: list[tuple[str, str]] = []

        try:
            # 1. Insert v1 document + 2 chunks into library_docs
            c1_text = (
                f"Use {unique_symbol} and duckdb-engine when rendering strict Jinja2 workflows."
            )
            c2_text = "Second chunk explaining running pipelines and transformations."
            drafts_v1 = [
                DocumentChunkDraft(
                    chunk_index=0,
                    chunk_sha256=compute_sha256(c1_text),
                    breadcrumb="Jinja2 > StrictUndefined",
                    content=c1_text,
                    token_count=count_tokens(c1_text),
                    metadata={"version": 1},
                ),
                DocumentChunkDraft(
                    chunk_index=1,
                    chunk_sha256=compute_sha256(c2_text),
                    breadcrumb="Jinja2 > Workflows",
                    content=c2_text,
                    token_count=count_tokens(c2_text),
                    metadata={"version": 1},
                ),
            ]
            sha_v1 = compute_sha256("v1-body-" + suffix)
            rec_v1 = await store.upsert_document_and_chunks(
                domain="library_docs",
                source_path=lib_source,
                title="Jinja2 Strict Reference",
                file_sha256=sha_v1,
                embedding_model="synthetic-unit-test",
                embedding_dimension=dims,
                chunks=drafts_v1,
                embeddings=[_unit_vector(0, dims), _unit_vector(1, dims)],
                metadata={"suite": "integration"},
            )
            created_sources.append(("library_docs", lib_source))

            assert rec_v1.chunk_count == 2
            assert await store.get_document_hash("library_docs", lib_source) == sha_v1

            # 2. Verify dual tsvector generated columns (simple preserves exact symbols; english stems)
            pool = await store.get_pool()
            async with pool.acquire() as conn:
                tsv_row = await conn.fetchrow(
                    f"""
                    SELECT
                        tsv_content_simple @@ websearch_to_tsquery('simple', $1) AS simple_symbol_hit,
                        tsv_content_simple @@ websearch_to_tsquery('simple', 'duckdb-engine') AS simple_hyphen_hit,
                        tsv_content_simple @@ websearch_to_tsquery('simple', 'workflow') AS simple_un_stemmed,
                        tsv_content_english @@ websearch_to_tsquery('english', 'workflow') AS english_stemmed_hit
                    FROM "{store.schema_name}"."knowledge_chunks"
                    WHERE document_id = $2 AND chunk_index = 0
                    """,
                    unique_symbol,
                    rec_v1.document_id,
                )
            assert tsv_row is not None
            assert tsv_row["simple_symbol_hit"] is True
            assert tsv_row["simple_hyphen_hit"] is True
            assert tsv_row["simple_un_stemmed"] is False
            assert tsv_row["english_stemmed_hit"] is True

            # 3. Update with changed SHA-256 -> replaces old chunks cleanly & advances updated_at
            await asyncio.sleep(0.01)
            replaced_text = (
                f"Updated {unique_symbol} reference with duckdb-engine and running workflows."
            )
            drafts_v2 = [
                DocumentChunkDraft(
                    chunk_index=0,
                    chunk_sha256=compute_sha256(replaced_text),
                    breadcrumb="Jinja2 > Updated",
                    content=replaced_text,
                    token_count=count_tokens(replaced_text),
                    metadata={"version": 2},
                )
            ]
            sha_v2 = compute_sha256("v2-body-" + suffix)
            rec_v2 = await store.upsert_document_and_chunks(
                domain="library_docs",
                source_path=lib_source,
                title="Jinja2 Strict Reference v2",
                file_sha256=sha_v2,
                embedding_model="synthetic-unit-test",
                embedding_dimension=dims,
                chunks=drafts_v2,
                embeddings=[_unit_vector(2, dims)],
                metadata={"suite": "integration", "rev": 2},
            )
            assert rec_v2.document_id == rec_v1.document_id
            assert rec_v2.chunk_count == 1
            assert rec_v2.updated_at is not None and rec_v1.updated_at is not None
            assert rec_v2.updated_at > rec_v1.updated_at
            assert await store.get_document_hash("library_docs", lib_source) == sha_v2

            async with pool.acquire() as conn:
                chunk_rows = await conn.fetch(
                    f'SELECT chunk_index, content FROM "{store.schema_name}"."knowledge_chunks" '
                    "WHERE document_id = $1 ORDER BY chunk_index ASC",
                    rec_v2.document_id,
                )
            assert len(chunk_rows) == 1
            assert chunk_rows[0]["content"] == replaced_text

            # 4. Insert an unrelated chunk into code_conventions to verify strict domain isolation
            conv_text = f"Unrelated Python style standard OtherToken_{suffix}"
            await store.upsert_document_and_chunks(
                domain="code_conventions",
                source_path=conv_source,
                title="Conv Test",
                file_sha256=compute_sha256(conv_text),
                embedding_model="synthetic-unit-test",
                embedding_dimension=dims,
                chunks=[
                    DocumentChunkDraft(
                        chunk_index=0,
                        chunk_sha256=compute_sha256(conv_text),
                        breadcrumb="Style > Typing",
                        content=conv_text,
                        token_count=count_tokens(conv_text),
                    )
                ],
                embeddings=[_unit_vector(3, dims)],
            )
            created_sources.append(("code_conventions", conv_source))

            # 5. End-to-end HybridRAGRetriever.search with FlashRank fallback (no HuggingFace download)
            fallback_reranker = FlashRankReranker(enabled=True, fallback_to_rrf=True)
            fallback_reranker.ranker = None  # Forces clean fallback to RRF offline

            retriever = HybridRAGRetriever(
                db_pool=pool,
                embedder=_SyntheticUnitEmbedder(query_dim_index=2, dimensions=dims),
                reranker=fallback_reranker,
            )

            lib_slice = await retriever.search(
                domain="library_docs",
                query=f"{unique_symbol} workflow",
                top_k=3,
                token_ceiling=1000,
            )
            assert len(lib_slice.chunks) >= 1
            assert lib_slice.chunks[0].source_path == lib_source
            assert lib_slice.chunks[0].breadcrumb == "Jinja2 > Updated"
            assert lib_slice.chunks[0].rerank_score is None
            assert lib_slice.chunks[0].rrf_score > 0.0

            xml_output = lib_slice.to_xml()
            parsed_xml = ET.fromstring(xml_output)
            assert parsed_xml.tag == "rag_context"
            assert parsed_xml.attrib["domain"] == "library_docs"
            assert any(
                ch.attrib["source"] == lib_source for ch in parsed_xml.findall("chunk")
            )

            # Querying code_conventions with the exact library_docs vector & symbol must never leak library_docs
            conv_slice = await retriever.search(
                domain="code_conventions",
                query=f"{unique_symbol} duckdb-engine",
                top_k=5,
                token_ceiling=1000,
            )
            assert all(ch.domain == "code_conventions" for ch in conv_slice.chunks)
            assert all(ch.source_path != lib_source for ch in conv_slice.chunks)
            assert all(unique_symbol not in ch.content for ch in conv_slice.chunks)
        finally:
            for dom, src in created_sources:
                await store.delete_document(dom, src)
            await store.close()
