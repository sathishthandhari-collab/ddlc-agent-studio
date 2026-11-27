"""Pydantic data models and domain validation for the RAG knowledge store."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID, uuid4
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from pydantic import BaseModel, Field

ALLOWED_DOMAINS: tuple[str, ...] = (
    "library_docs",
    "org_standards",
    "pattern_catalog",
    "past_decisions",
    "component_docs",
    "code_conventions",
)

RAGDomain = Literal[
    "library_docs",
    "org_standards",
    "pattern_catalog",
    "past_decisions",
    "component_docs",
    "code_conventions",
]


def validate_domain(domain: str) -> str:
    """Validate that a RAG domain tag belongs to the 6 authorized corpora."""
    if not isinstance(domain, str) or domain not in ALLOWED_DOMAINS:
        allowed = ", ".join(ALLOWED_DOMAINS)
        raise ValueError(f"Invalid RAG domain {domain!r}. Allowed domains: {allowed}.")
    return domain


class DocumentChunkDraft(BaseModel):
    """Pre-embedding chunk produced by a structure-aware document chunker."""

    chunk_index: int = Field(..., ge=0)
    chunk_sha256: str
    breadcrumb: Optional[str] = None
    content: str
    token_count: int = Field(..., ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeDocumentRecord(BaseModel):
    """Persisted document registry entry from rag_knowledge.knowledge_documents."""

    document_id: UUID = Field(default_factory=uuid4)
    domain: str
    source_path: str
    title: str
    file_sha256: str
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    chunk_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class KnowledgeChunkRecord(BaseModel):
    """Persisted chunk entry from rag_knowledge.knowledge_chunks."""

    chunk_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    domain: str
    chunk_index: int
    chunk_sha256: str
    breadcrumb: Optional[str] = None
    content: str
    embedding: List[float]
    token_count: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class RAGSearchResult(BaseModel):
    """Hybrid search / reranked candidate chunk projected with document provenance."""

    chunk_id: UUID
    document_id: UUID
    domain: str
    breadcrumb: Optional[str] = None
    content: str
    token_count: int
    rrf_score: float
    rerank_score: Optional[float] = None
    source_path: Optional[str] = None


class RAGContextSlice(BaseModel):
    """Budget-clamped retrieval payload formatted for specialist state projection."""

    domain: str
    query: str
    chunks: List[RAGSearchResult] = Field(default_factory=list)
    total_tokens: int = 0

    def to_xml(self) -> str:
        """Render deterministic, XML-escaped block for specialist state projection."""
        if not self.chunks:
            return (
                f"<rag_context domain={quoteattr(self.domain)} "
                f'query={quoteattr(self.query)} count="0"/>'
            )

        xml_lines = [
            f"<rag_context domain={quoteattr(self.domain)} "
            f"query={quoteattr(self.query)} count={quoteattr(str(len(self.chunks)))}>"
        ]
        for i, chunk in enumerate(self.chunks, 1):
            section = chunk.breadcrumb or "General"
            source = chunk.source_path or "unknown"
            score = (
                f"{chunk.rerank_score:.2f}"
                if chunk.rerank_score is not None
                else f"{chunk.rrf_score:.4f}"
            )
            xml_lines.append(
                f"  <chunk index={quoteattr(str(i))} "
                f"source={quoteattr(source)} "
                f"section={quoteattr(section)} "
                f"score={quoteattr(score)}>\n"
                f"{xml_escape(chunk.content.strip())}\n"
                "  </chunk>"
            )
        xml_lines.append("</rag_context>")
        return "\n".join(xml_lines)
