"""PostgreSQL + pgvector storage layer for DDLC Agent Studio RAG."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from typing import Any, Optional
from uuid import uuid4

import asyncpg
import psycopg
from pgvector.asyncpg import register_vector

from src.core.settings import get_env_settings, get_studio_config
from src.rag.store.models import (
    ALLOWED_DOMAINS,
    DocumentChunkDraft,
    KnowledgeDocumentRecord,
    validate_domain,
)


def _validate_sql_identifier(identifier: str) -> str:
    """Guard against unsafe dynamic SQL schema or table identifiers."""
    if not identifier or not identifier.isidentifier():
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def _normalize_psycopg_uri(uri: str) -> str:
    """Normalize SQLAlchemy-style PostgreSQL driver prefixes for psycopg."""
    if uri.startswith("postgresql+psycopg://"):
        return uri.replace("postgresql+psycopg://", "postgresql://", 1)
    if uri.startswith("postgresql+asyncpg://"):
        return uri.replace("postgresql+asyncpg://", "postgresql://", 1)
    return uri


def _normalize_asyncpg_uri(uri: str) -> str:
    """Normalize SQLAlchemy-style PostgreSQL driver prefixes for asyncpg."""
    if uri.startswith("postgresql+asyncpg://"):
        return uri.replace("postgresql+asyncpg://", "postgresql://", 1)
    if uri.startswith("postgresql+psycopg://"):
        return uri.replace("postgresql+psycopg://", "postgresql://", 1)
    return uri


def _build_rag_ddl_statements(
    schema_name: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
) -> list[str]:
    """Build idempotent DDL statements for the configured RAG schema and vector width."""
    rag_cfg = get_studio_config().rag
    resolved_schema = _validate_sql_identifier(
        schema_name if schema_name is not None else rag_cfg.database.schema_name
    )
    resolved_dim = int(
        embedding_dimension if embedding_dimension is not None else rag_cfg.embedding.dimensions
    )
    if resolved_dim <= 0:
        raise ValueError(f"Embedding dimension must be positive, got {resolved_dim}.")

    domains_sql = ", ".join(f"'{_validate_sql_identifier(d)}'" for d in ALLOWED_DOMAINS)
    docs_table = f'"{resolved_schema}"."knowledge_documents"'
    chunks_table = f'"{resolved_schema}"."knowledge_chunks"'

    statements: list[str] = [
        'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";',
        'CREATE EXTENSION IF NOT EXISTS "vector";',
        f'CREATE SCHEMA IF NOT EXISTS "{resolved_schema}";',
        (
            f"CREATE TABLE IF NOT EXISTS {docs_table} (\n"
            "    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n"
            "    domain VARCHAR(64) NOT NULL,\n"
            "    source_path VARCHAR(512) NOT NULL,\n"
            "    title VARCHAR(256) NOT NULL,\n"
            "    file_sha256 CHAR(64) NOT NULL,\n"
            "    embedding_model VARCHAR(128) NOT NULL DEFAULT 'text-embedding-3-small',\n"
            f"    embedding_dimension INT NOT NULL DEFAULT {resolved_dim},\n"
            "    chunk_count INT NOT NULL DEFAULT 0,\n"
            "    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
            "    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),\n"
            "    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),\n"
            "    CONSTRAINT uq_rag_source UNIQUE (domain, source_path),\n"
            f"    CONSTRAINT chk_rag_domain CHECK (domain IN ({domains_sql}))\n"
            ");"
        ),
        (
            f"CREATE TABLE IF NOT EXISTS {chunks_table} (\n"
            "    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n"
            f"    document_id UUID NOT NULL REFERENCES {docs_table}(document_id) ON DELETE CASCADE,\n"
            "    domain VARCHAR(64) NOT NULL,\n"
            "    chunk_index INT NOT NULL,\n"
            "    chunk_sha256 CHAR(64) NOT NULL,\n"
            "    breadcrumb VARCHAR(512),\n"
            "    content TEXT NOT NULL,\n"
            f"    embedding vector({resolved_dim}) NOT NULL,\n"
            "    tsv_content_simple tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,\n"
            "    tsv_content_english tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,\n"
            "    token_count INT NOT NULL,\n"
            "    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
            "    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),\n"
            "    CONSTRAINT uq_rag_chunk_order UNIQUE (document_id, chunk_index),\n"
            f"    CONSTRAINT chk_rag_chunk_domain CHECK (domain IN ({domains_sql}))\n"
            ");"
        ),
        f"CREATE INDEX IF NOT EXISTS idx_rag_docs_domain ON {docs_table}(domain);",
        f"CREATE INDEX IF NOT EXISTS idx_rag_chunks_domain ON {chunks_table}(domain);",
        f"CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc_id ON {chunks_table}(document_id);",
    ]

    for domain in ALLOWED_DOMAINS:
        safe_domain = _validate_sql_identifier(domain)
        statements.append(
            f"CREATE INDEX IF NOT EXISTS idx_rag_hnsw_{safe_domain} "
            f"ON {chunks_table} USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) "
            f"WHERE domain = '{safe_domain}';"
        )

    statements.extend(
        [
            f"CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv_simple ON {chunks_table} USING gin(tsv_content_simple);",
            f"CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv_english ON {chunks_table} USING gin(tsv_content_english);",
        ]
    )
    return statements


def build_rag_schema_ddl(
    schema_name: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
) -> str:
    """Return the full idempotent DDL script for the RAG knowledge schema."""
    return "\n\n".join(
        _build_rag_ddl_statements(
            schema_name=schema_name,
            embedding_dimension=embedding_dimension,
        )
    )


RAG_SCHEMA_DDL: str = build_rag_schema_ddl()


def resolve_rag_db_uri(admin: bool = False) -> str:
    """Resolve the PostgreSQL URI for RAG runtime or schema-migration operations."""
    env = get_env_settings()
    if admin:
        uri = env.dcm_migration_db_uri
        if not uri:
            raise ValueError(
                "RAG admin database URI is required (set DCM_MIGRATION_DB_URI)."
            )
        return uri

    uri = env.rag_db_uri or env.metadata_db_uri
    if not uri:
        raise ValueError(
            "RAG database URI is required (pass db_uri or set RAG_DB_URI or METADATA_DB_URI)."
        )
    return uri


def ensure_rag_schema(db_uri: Optional[str] = None) -> None:
    """Provision the RAG schema, tables, and indexes idempotently using psycopg."""
    uri = _normalize_psycopg_uri(db_uri or resolve_rag_db_uri(admin=True))
    statements = _build_rag_ddl_statements()
    with psycopg.connect(uri) as conn:
        with conn.transaction():
            for stmt in statements:
                conn.execute(stmt)


def format_vector_literal(
    vec: Sequence[float],
    expected_dim: Optional[int] = None,
) -> str:
    """Validate finite numeric elements and dimension, returning pgvector text literal."""
    if isinstance(vec, (str, bytes)):
        raise ValueError("Embedding vector must be a sequence of numbers, not str or bytes.")

    raw_items: Any
    if hasattr(vec, "to_list"):
        raw_items = vec.to_list()
    elif hasattr(vec, "tolist"):
        raw_items = vec.tolist()
    else:
        raw_items = vec

    floats: list[float] = []
    for item in raw_items:
        if isinstance(item, bool):
            raise ValueError("Embedding vector must contain finite numeric floats, not bool.")
        val = float(item)
        if not math.isfinite(val):
            raise ValueError("Embedding vector must contain only finite float values.")
        floats.append(val)

    if not floats:
        raise ValueError("Embedding vector must not be empty.")

    if expected_dim is not None:
        if expected_dim <= 0:
            raise ValueError(f"Expected dimension must be positive, got {expected_dim}.")
        if len(floats) != expected_dim:
            raise ValueError(
                f"Embedding dimension mismatch: expected {expected_dim}, got {len(floats)}."
            )

    return "[" + ",".join(repr(val) for val in floats) + "]"


async def create_rag_pool(db_uri: Optional[str] = None) -> asyncpg.Pool:
    """Create an asyncpg connection pool configured for pgvector and RAG settings."""
    rag_db_cfg = get_studio_config().rag.database
    uri = _normalize_asyncpg_uri(db_uri or resolve_rag_db_uri(admin=False))
    pool = await asyncpg.create_pool(
        dsn=uri,
        min_size=rag_db_cfg.pool_min_size,
        max_size=rag_db_cfg.pool_max_size,
        timeout=float(rag_db_cfg.connection_timeout_seconds),
        init=register_vector,
    )
    if pool is None:
        raise RuntimeError("Failed to initialize asyncpg connection pool for RAG store.")
    return pool


class PgVectorRAGStore:
    """Async PostgreSQL + pgvector document and chunk persistence store."""

    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_uri: Optional[str] = None,
        *,
        schema_name: Optional[str] = None,
    ) -> None:
        self._pool = pool
        self._owns_pool = pool is None
        self._db_uri = db_uri
        self._schema_name_override = (
            _validate_sql_identifier(schema_name) if schema_name is not None else None
        )
        self._pool_lock: Optional[asyncio.Lock] = None

    @property
    def schema_name(self) -> str:
        """Return the validated target PostgreSQL schema name."""
        raw = self._schema_name_override or get_studio_config().rag.database.schema_name
        return _validate_sql_identifier(raw)

    @property
    def _docs_table(self) -> str:
        return f'"{self.schema_name}"."knowledge_documents"'

    @property
    def _chunks_table(self) -> str:
        return f'"{self.schema_name}"."knowledge_chunks"'

    async def get_pool(self) -> asyncpg.Pool:
        """Lazily initialize and return the underlying asyncpg pool."""
        if self._pool is not None:
            return self._pool

        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()

        async with self._pool_lock:
            if self._pool is None:
                self._pool = await create_rag_pool(db_uri=self._db_uri)
                self._owns_pool = True
            return self._pool

    async def close(self) -> None:
        """Close the owned asyncpg pool if one was created by this store."""
        if self._pool is not None and self._owns_pool:
            pool = self._pool
            self._pool = None
            await pool.close()

    async def __aenter__(self) -> PgVectorRAGStore:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @staticmethod
    def _row_to_document(row: Any) -> KnowledgeDocumentRecord:
        raw_meta = row["metadata"]
        if isinstance(raw_meta, str):
            parsed_meta = json.loads(raw_meta) if raw_meta else {}
        else:
            parsed_meta = dict(raw_meta or {})

        return KnowledgeDocumentRecord(
            document_id=row["document_id"],
            domain=row["domain"],
            source_path=row["source_path"],
            title=row["title"],
            file_sha256=str(row["file_sha256"]).rstrip(),
            embedding_model=row["embedding_model"],
            embedding_dimension=int(row["embedding_dimension"]),
            chunk_count=int(row["chunk_count"]),
            metadata=parsed_meta,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_document_hash(self, domain: str, source_path: str) -> Optional[str]:
        """Return the persisted file_sha256 for (domain, source_path), or None."""
        validated_domain = validate_domain(domain)
        sql = (
            f"SELECT file_sha256 FROM {self._docs_table} "
            "WHERE domain = $1 AND source_path = $2"
        )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(sql, validated_domain, source_path)
        return str(val).rstrip() if val is not None else None

    async def get_document(
        self,
        domain: str,
        source_path: str,
    ) -> Optional[KnowledgeDocumentRecord]:
        """Fetch a persisted KnowledgeDocumentRecord by (domain, source_path)."""
        validated_domain = validate_domain(domain)
        sql = (
            "SELECT document_id, domain, source_path, title, file_sha256, "
            "embedding_model, embedding_dimension, chunk_count, metadata, "
            "created_at, updated_at "
            f"FROM {self._docs_table} "
            "WHERE domain = $1 AND source_path = $2"
        )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, validated_domain, source_path)
        return self._row_to_document(row) if row is not None else None

    async def upsert_document_and_chunks(
        self,
        *,
        domain: str,
        source_path: str,
        title: str,
        file_sha256: str,
        embedding_model: str,
        embedding_dimension: int,
        chunks: Sequence[DocumentChunkDraft],
        embeddings: Sequence[Sequence[float]],
        metadata: Optional[dict[str, Any]] = None,
    ) -> KnowledgeDocumentRecord:
        """Atomically upsert document registry metadata and replace all document chunks."""
        validated_domain = validate_domain(domain)
        if embedding_dimension <= 0:
            raise ValueError(
                f"embedding_dimension must be positive, got {embedding_dimension}."
            )
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunk/embedding count mismatch: {len(chunks)} chunks vs {len(embeddings)} embeddings."
            )

        vector_literals = [
            format_vector_literal(embedding, expected_dim=embedding_dimension)
            for embedding in embeddings
        ]
        doc_meta_json = json.dumps(metadata or {})

        upsert_doc_sql = (
            f"INSERT INTO {self._docs_table} (\n"
            "    domain,\n"
            "    source_path,\n"
            "    title,\n"
            "    file_sha256,\n"
            "    embedding_model,\n"
            "    embedding_dimension,\n"
            "    chunk_count,\n"
            "    metadata\n"
            ")\n"
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)\n"
            "ON CONFLICT (domain, source_path)\n"
            "DO UPDATE SET\n"
            "    title = EXCLUDED.title,\n"
            "    file_sha256 = EXCLUDED.file_sha256,\n"
            "    embedding_model = EXCLUDED.embedding_model,\n"
            "    embedding_dimension = EXCLUDED.embedding_dimension,\n"
            "    chunk_count = EXCLUDED.chunk_count,\n"
            "    metadata = EXCLUDED.metadata,\n"
            "    updated_at = clock_timestamp()\n"
            "RETURNING\n"
            "    document_id,\n"
            "    domain,\n"
            "    source_path,\n"
            "    title,\n"
            "    file_sha256,\n"
            "    embedding_model,\n"
            "    embedding_dimension,\n"
            "    chunk_count,\n"
            "    metadata,\n"
            "    created_at,\n"
            "    updated_at"
        )
        delete_chunks_sql = f"DELETE FROM {self._chunks_table} WHERE document_id = $1"
        insert_chunks_sql = (
            f"INSERT INTO {self._chunks_table} (\n"
            "    chunk_id,\n"
            "    document_id,\n"
            "    domain,\n"
            "    chunk_index,\n"
            "    chunk_sha256,\n"
            "    breadcrumb,\n"
            "    content,\n"
            "    embedding,\n"
            "    token_count,\n"
            "    metadata\n"
            ")\n"
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text::vector, $9, $10::jsonb)"
        )

        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                doc_row = await conn.fetchrow(
                    upsert_doc_sql,
                    validated_domain,
                    source_path,
                    title,
                    file_sha256,
                    embedding_model,
                    embedding_dimension,
                    len(chunks),
                    doc_meta_json,
                )
                if doc_row is None:
                    raise RuntimeError(
                        f"Failed to upsert knowledge_documents row for ({validated_domain!r}, {source_path!r})."
                    )

                document_id = doc_row["document_id"]
                await conn.execute(delete_chunks_sql, document_id)

                if chunks:
                    chunk_rows = [
                        (
                            uuid4(),
                            document_id,
                            validated_domain,
                            chunk.chunk_index,
                            chunk.chunk_sha256,
                            chunk.breadcrumb,
                            chunk.content,
                            vec_lit,
                            chunk.token_count,
                            json.dumps(chunk.metadata),
                        )
                        for chunk, vec_lit in zip(chunks, vector_literals, strict=True)
                    ]
                    await conn.executemany(insert_chunks_sql, chunk_rows)

        return self._row_to_document(doc_row)

    async def list_documents(
        self,
        domain: Optional[str] = None,
    ) -> list[KnowledgeDocumentRecord]:
        """List persisted RAG documents, optionally filtered to one domain."""
        base_select = (
            "SELECT document_id, domain, source_path, title, file_sha256, "
            "embedding_model, embedding_dimension, chunk_count, metadata, "
            "created_at, updated_at "
            f"FROM {self._docs_table} "
        )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            if domain is not None:
                validated_domain = validate_domain(domain)
                rows = await conn.fetch(
                    base_select + "WHERE domain = $1 ORDER BY domain, source_path",
                    validated_domain,
                )
            else:
                rows = await conn.fetch(
                    base_select + "ORDER BY domain, source_path"
                )
        return [self._row_to_document(row) for row in rows]

    async def delete_document(self, domain: str, source_path: str) -> bool:
        """Delete a document (and cascading chunks) by (domain, source_path)."""
        validated_domain = validate_domain(domain)
        sql = (
            f"DELETE FROM {self._docs_table} "
            "WHERE domain = $1 AND source_path = $2"
        )
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(sql, validated_domain, source_path)

        if isinstance(status, str):
            return status.rsplit(" ", 1)[-1] != "0"
        return bool(status)

    async def get_domain_counts(self) -> dict[str, dict[str, int]]:
        """Return document and chunk counts across all 6 authorized RAG domains."""
        sql = (
            "SELECT d.domain, "
            "COUNT(DISTINCT d.document_id)::int AS document_count, "
            "COUNT(c.chunk_id)::int AS chunk_count "
            f"FROM {self._docs_table} d "
            f"LEFT JOIN {self._chunks_table} c ON c.document_id = d.document_id "
            "GROUP BY d.domain"
        )
        counts: dict[str, dict[str, int]] = {
            domain: {"documents": 0, "chunks": 0} for domain in ALLOWED_DOMAINS
        }
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)

        for row in rows:
            domain = row["domain"]
            counts[domain] = {
                "documents": int(row["document_count"]),
                "chunks": int(row["chunk_count"]),
            }
        return counts
