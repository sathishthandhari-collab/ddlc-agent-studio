"""RAG PostgreSQL + pgvector storage layer and Pydantic models."""

from src.rag.store.models import (
    ALLOWED_DOMAINS,
    DocumentChunkDraft,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    RAGContextSlice,
    RAGDomain,
    RAGSearchResult,
    validate_domain,
)
from src.rag.store.pgvector_store import (
    RAG_SCHEMA_DDL,
    PgVectorRAGStore,
    build_rag_schema_ddl,
    create_rag_pool,
    ensure_rag_schema,
    format_vector_literal,
    resolve_rag_db_uri,
)

__all__ = [
    "ALLOWED_DOMAINS",
    "DocumentChunkDraft",
    "KnowledgeChunkRecord",
    "KnowledgeDocumentRecord",
    "PgVectorRAGStore",
    "RAGContextSlice",
    "RAGDomain",
    "RAGSearchResult",
    "RAG_SCHEMA_DDL",
    "build_rag_schema_ddl",
    "create_rag_pool",
    "ensure_rag_schema",
    "format_vector_literal",
    "resolve_rag_db_uri",
    "validate_domain",
]
