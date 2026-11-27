"""Deterministic PostgreSQL hybrid search (pgvector HNSW + dual-tsvector RRF + FlashRank)."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from src.core.settings import get_studio_config
from src.rag.embeddings.base import BaseEmbedder
from src.rag.retrieval.reranker import FlashRankReranker
from src.rag.store.models import RAGContextSlice, RAGSearchResult, validate_domain

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


def _validate_schema_name(schema_name: str) -> str:
    if not schema_name.isidentifier():
        raise ValueError(f"Invalid SQL schema identifier: {schema_name!r}")
    return schema_name


def _format_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _build_hybrid_cte_sql(schema_name: str) -> str:
    schema = _validate_schema_name(schema_name)
    return f"""
WITH vector_candidates AS (
    SELECT
        chunk_id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> $2::text::vector ASC) AS dense_rank
    FROM {schema}.knowledge_chunks
    WHERE domain = $1
    ORDER BY embedding <=> $2::text::vector ASC
    LIMIT $5
),
text_candidates AS (
    SELECT
        chunk_id,
        ROW_NUMBER() OVER (
            ORDER BY (
                ts_rank_cd(tsv_content_simple, websearch_to_tsquery('simple', $3)) * 1.5 +
                ts_rank_cd(tsv_content_english, websearch_to_tsquery('english', $3)) * 1.0
            ) DESC
        ) AS sparse_rank
    FROM {schema}.knowledge_chunks
    WHERE domain = $1
      AND (
          tsv_content_simple @@ websearch_to_tsquery('simple', $3) OR
          tsv_content_english @@ websearch_to_tsquery('english', $3)
      )
    ORDER BY sparse_rank ASC
    LIMIT $5
),
fused_scores AS (
    SELECT
        COALESCE(v.chunk_id, t.chunk_id) AS chunk_id,
        (
            COALESCE(1.0 / ($4::double precision + v.dense_rank), 0.0) +
            COALESCE(1.0 / ($4::double precision + t.sparse_rank), 0.0)
        )::float8 AS rrf_score
    FROM vector_candidates v
    FULL OUTER JOIN text_candidates t ON v.chunk_id = t.chunk_id
)
SELECT
    c.chunk_id,
    c.document_id,
    c.domain,
    d.source_path,
    c.breadcrumb,
    c.content,
    c.token_count,
    f.rrf_score::float8 AS rrf_score
FROM fused_scores f
JOIN {schema}.knowledge_chunks c ON f.chunk_id = c.chunk_id
JOIN {schema}.knowledge_documents d ON c.document_id = d.document_id
ORDER BY f.rrf_score DESC
LIMIT $5
""".strip()


class HybridRAGRetriever:
    """Domain-isolated hybrid retriever combining dense HNSW, dual BM25, RRF, and FlashRank."""

    def __init__(
        self,
        db_pool: "asyncpg.Pool | Any",
        embedder: BaseEmbedder,
        reranker: Optional[FlashRankReranker] = None,
        rrf_k: Optional[int] = None,
        initial_candidates: Optional[int] = None,
        schema_name: Optional[str] = None,
        on_telemetry: Optional[Callable[[dict[str, Any]], Any]] = None,
        telemetry_handler: Optional[Any] = None,
    ) -> None:
        self.pool = db_pool
        self.embedder = embedder
        self.reranker = reranker if reranker is not None else FlashRankReranker()
        self._rrf_k_override = rrf_k
        self._initial_candidates_override = initial_candidates
        self._schema_name_override = schema_name
        self.on_telemetry = on_telemetry
        self.telemetry_handler = telemetry_handler

    def _resolve_retrieval_config(self) -> tuple[int, int, str]:
        if (
            self._rrf_k_override is not None
            and self._initial_candidates_override is not None
            and self._schema_name_override is not None
        ):
            return (
                self._rrf_k_override,
                self._initial_candidates_override,
                _validate_schema_name(self._schema_name_override),
            )

        rag_cfg = get_studio_config().rag
        rrf_k = rag_cfg.retrieval.rrf_k if self._rrf_k_override is None else self._rrf_k_override
        initial_candidates = (
            rag_cfg.retrieval.initial_candidates
            if self._initial_candidates_override is None
            else self._initial_candidates_override
        )
        schema_name = self._schema_name_override or rag_cfg.database.schema_name
        return rrf_k, initial_candidates, _validate_schema_name(schema_name)

    async def _emit_telemetry_start(
        self,
        *,
        span_run_id: Any,
        domain: str,
        query: str,
    ) -> None:
        if self.telemetry_handler is None:
            return
        try:
            hook = getattr(self.telemetry_handler, "on_tool_start", None)
            if callable(hook):
                res = hook(
                    {"name": "rag_retrieval"},
                    query,
                    run_id=span_run_id,
                    inputs={"domain": domain, "query": query},
                    metadata={"domain": domain, "step_type": "tool_call"},
                )
                if inspect.isawaitable(res):
                    await res
        except Exception as exc:
            logger.warning("Non-blocking RAG telemetry start failed: %s", exc)

    async def _emit_telemetry_end(
        self,
        *,
        span_run_id: Any,
        event: dict[str, Any],
    ) -> None:
        if self.on_telemetry is not None:
            try:
                res = self.on_telemetry(event)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("Non-blocking on_telemetry callback failed: %s", exc)

        if self.telemetry_handler is not None:
            try:
                end_hook = getattr(self.telemetry_handler, "on_tool_end", None)
                if callable(end_hook):
                    res = end_hook(event, run_id=span_run_id)
                    if inspect.isawaitable(res):
                        await res
                custom_hook = getattr(self.telemetry_handler, "on_custom_event", None)
                if callable(custom_hook):
                    res = custom_hook("rag_retrieval", event, run_id=span_run_id)
                    if inspect.isawaitable(res):
                        await res
            except Exception as exc:
                logger.warning("Non-blocking RAG telemetry end failed: %s", exc)

    async def _emit_telemetry_error(self, *, span_run_id: Any, error: BaseException) -> None:
        if self.telemetry_handler is None:
            return
        try:
            err_hook = getattr(self.telemetry_handler, "on_tool_error", None)
            if callable(err_hook):
                res = err_hook(error, run_id=span_run_id)
                if inspect.isawaitable(res):
                    await res
        except Exception as exc:
            logger.warning("Non-blocking RAG telemetry error hook failed: %s", exc)

    async def search(
        self,
        domain: str,
        query: str,
        top_k: int = 3,
        token_ceiling: int = 1000,
    ) -> RAGContextSlice:
        """Execute strict-domain hybrid search, RRF fusion, reranking, and budget clamping."""
        validated_domain = validate_domain(domain)
        t_total_start = time.perf_counter()
        span_run_id = uuid4()

        if not query or not query.strip() or top_k <= 0 or token_ceiling <= 0:
            empty_slice = RAGContextSlice(
                domain=validated_domain,
                query=query,
                chunks=[],
                total_tokens=0,
            )
            await self._emit_telemetry_end(
                span_run_id=span_run_id,
                event=self._build_telemetry_event(
                    domain=validated_domain,
                    query=query,
                    candidate_count=0,
                    final_chunks=[],
                    total_tokens=0,
                    embedding_ms=0,
                    sql_cte_ms=0,
                    rerank_ms=0,
                    total_ms=round((time.perf_counter() - t_total_start) * 1000),
                ),
            )
            return empty_slice

        await self._emit_telemetry_start(
            span_run_id=span_run_id,
            domain=validated_domain,
            query=query,
        )

        rrf_k, initial_candidates, schema_name = self._resolve_retrieval_config()
        sql_query = _build_hybrid_cte_sql(schema_name)

        try:
            t_embed = time.perf_counter()
            query_embedding = await self.embedder.embed_query(query)
            embedding_ms = round((time.perf_counter() - t_embed) * 1000)
            vector_str = _format_pgvector(query_embedding)

            t_sql = time.perf_counter()
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
                    records = await conn.fetch(
                        sql_query,
                        validated_domain,
                        vector_str,
                        query,
                        rrf_k,
                        initial_candidates,
                    )
            sql_cte_ms = round((time.perf_counter() - t_sql) * 1000)
        except Exception as exc:
            await self._emit_telemetry_error(span_run_id=span_run_id, error=exc)
            raise

        if not records:
            total_ms = round((time.perf_counter() - t_total_start) * 1000)
            await self._emit_telemetry_end(
                span_run_id=span_run_id,
                event=self._build_telemetry_event(
                    domain=validated_domain,
                    query=query,
                    candidate_count=0,
                    final_chunks=[],
                    total_tokens=0,
                    embedding_ms=embedding_ms,
                    sql_cte_ms=sql_cte_ms,
                    rerank_ms=0,
                    total_ms=total_ms,
                ),
            )
            return RAGContextSlice(
                domain=validated_domain,
                query=query,
                chunks=[],
                total_tokens=0,
            )

        candidates = [
            RAGSearchResult(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                domain=r["domain"],
                source_path=r["source_path"],
                breadcrumb=r["breadcrumb"],
                content=r["content"],
                token_count=int(r["token_count"]),
                rrf_score=float(r["rrf_score"]),
                rerank_score=None,
            )
            for r in records
        ]

        t_rerank = time.perf_counter()
        reranked = await self.reranker.rerank(query, candidates)
        rerank_ms = round((time.perf_counter() - t_rerank) * 1000)

        final_chunks: list[RAGSearchResult] = []
        accumulated_tokens = 0
        for chunk in reranked[:top_k]:
            if accumulated_tokens + chunk.token_count > token_ceiling and final_chunks:
                break
            final_chunks.append(chunk)
            accumulated_tokens += chunk.token_count

        total_ms = round((time.perf_counter() - t_total_start) * 1000)
        telemetry_event = self._build_telemetry_event(
            domain=validated_domain,
            query=query,
            candidate_count=len(candidates),
            final_chunks=final_chunks,
            total_tokens=accumulated_tokens,
            embedding_ms=embedding_ms,
            sql_cte_ms=sql_cte_ms,
            rerank_ms=rerank_ms,
            total_ms=total_ms,
        )
        await self._emit_telemetry_end(span_run_id=span_run_id, event=telemetry_event)

        return RAGContextSlice(
            domain=validated_domain,
            query=query,
            chunks=final_chunks,
            total_tokens=accumulated_tokens,
        )

    @staticmethod
    def _build_telemetry_event(
        *,
        domain: str,
        query: str,
        candidate_count: int,
        final_chunks: list[RAGSearchResult],
        total_tokens: int,
        embedding_ms: int,
        sql_cte_ms: int,
        rerank_ms: int,
        total_ms: int,
    ) -> dict[str, Any]:
        return {
            "event": "rag_retrieval",
            "domain": domain,
            "query": query,
            "candidate_count": candidate_count,
            "top_chunks": [
                {
                    "chunk_id": str(c.chunk_id),
                    "rrf_score": float(c.rrf_score),
                    "rerank_score": float(c.rerank_score) if c.rerank_score is not None else None,
                }
                for c in final_chunks
            ],
            "total_tokens_injected": total_tokens,
            "latency_ms": {
                "embedding_ms": embedding_ms,
                "sql_cte_ms": sql_cte_ms,
                "rerank_ms": rerank_ms,
                "total_ms": total_ms,
            },
        }
