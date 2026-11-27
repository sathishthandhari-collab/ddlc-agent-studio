"""Domain-tagged hybrid RAG engine (PostgreSQL + pgvector + FlashRank)."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.rag.chunking import (
        BaseChunker,
        CodeChunker,
        MarkdownChunker,
        YamlChunker,
        chunk_file,
        compute_sha256,
        count_tokens,
    )
    from src.rag.embeddings.base import BaseEmbedder
    from src.rag.embeddings.embedder import EmbeddingConfigError, LiteLLMEmbedder
    from src.rag.ingest import (
        IngestFileResult,
        IngestSummary,
        cli_app,
        ingest_knowledge_corpus,
        rag_app,
    )
    from src.rag.interfaces import (
        ArchitectRAGFacade,
        BuilderRAGFacade,
        ResearcherRAGFacade,
    )
    from src.rag.retrieval import (
        FlashRankReranker,
        HybridRAGRetriever,
        compute_rrf_score,
        fuse_ranked_lists,
        fuse_search_results,
    )
    from src.rag.store.models import (
        ALLOWED_DOMAINS,
        DocumentChunkDraft,
        RAGContextSlice,
        RAGDomain,
        RAGSearchResult,
        validate_domain,
    )
    from src.rag.store.pgvector_store import PgVectorRAGStore, ensure_rag_schema

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Chunking
    "BaseChunker": ("src.rag.chunking", "BaseChunker"),
    "CodeChunker": ("src.rag.chunking", "CodeChunker"),
    "MarkdownChunker": ("src.rag.chunking", "MarkdownChunker"),
    "YamlChunker": ("src.rag.chunking", "YamlChunker"),
    "chunk_file": ("src.rag.chunking", "chunk_file"),
    "compute_sha256": ("src.rag.chunking", "compute_sha256"),
    "count_tokens": ("src.rag.chunking", "count_tokens"),
    # Embeddings
    "BaseEmbedder": ("src.rag.embeddings.base", "BaseEmbedder"),
    "EmbeddingConfigError": ("src.rag.embeddings.embedder", "EmbeddingConfigError"),
    "LiteLLMEmbedder": ("src.rag.embeddings.embedder", "LiteLLMEmbedder"),
    # Store
    "ALLOWED_DOMAINS": ("src.rag.store.models", "ALLOWED_DOMAINS"),
    "DocumentChunkDraft": ("src.rag.store.models", "DocumentChunkDraft"),
    "PgVectorRAGStore": ("src.rag.store.pgvector_store", "PgVectorRAGStore"),
    "RAGContextSlice": ("src.rag.store.models", "RAGContextSlice"),
    "RAGDomain": ("src.rag.store.models", "RAGDomain"),
    "RAGSearchResult": ("src.rag.store.models", "RAGSearchResult"),
    "ensure_rag_schema": ("src.rag.store.pgvector_store", "ensure_rag_schema"),
    "validate_domain": ("src.rag.store.models", "validate_domain"),
    # Retrieval
    "FlashRankReranker": ("src.rag.retrieval", "FlashRankReranker"),
    "HybridRAGRetriever": ("src.rag.retrieval", "HybridRAGRetriever"),
    "compute_rrf_score": ("src.rag.retrieval", "compute_rrf_score"),
    "fuse_ranked_lists": ("src.rag.retrieval", "fuse_ranked_lists"),
    "fuse_search_results": ("src.rag.retrieval", "fuse_search_results"),
    # Specialist facades
    "ArchitectRAGFacade": ("src.rag.interfaces", "ArchitectRAGFacade"),
    "BuilderRAGFacade": ("src.rag.interfaces", "BuilderRAGFacade"),
    "ResearcherRAGFacade": ("src.rag.interfaces", "ResearcherRAGFacade"),
    # Ingestion & CLI
    "IngestFileResult": ("src.rag.ingest", "IngestFileResult"),
    "IngestSummary": ("src.rag.ingest", "IngestSummary"),
    "cli_app": ("src.rag.ingest", "cli_app"),
    "ingest_knowledge_corpus": ("src.rag.ingest", "ingest_knowledge_corpus"),
    "rag_app": ("src.rag.ingest", "rag_app"),
}

__all__ = [
    "ALLOWED_DOMAINS",
    "ArchitectRAGFacade",
    "BaseChunker",
    "BaseEmbedder",
    "BuilderRAGFacade",
    "CodeChunker",
    "DocumentChunkDraft",
    "EmbeddingConfigError",
    "FlashRankReranker",
    "HybridRAGRetriever",
    "IngestFileResult",
    "IngestSummary",
    "LiteLLMEmbedder",
    "MarkdownChunker",
    "PgVectorRAGStore",
    "RAGContextSlice",
    "RAGDomain",
    "RAGSearchResult",
    "ResearcherRAGFacade",
    "YamlChunker",
    "chunk_file",
    "cli_app",
    "compute_rrf_score",
    "compute_sha256",
    "count_tokens",
    "ensure_rag_schema",
    "fuse_ranked_lists",
    "fuse_search_results",
    "ingest_knowledge_corpus",
    "rag_app",
    "validate_domain",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr_name = target
    module = import_module(module_path)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
