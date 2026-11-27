"""Abstract embedder protocol for DDLC Agent Studio RAG."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class BaseEmbedder(Protocol):
    """Protocol implemented by asynchronous text embedding clients."""

    model_name: str
    dimensions: int

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Generate embedding vectors for a sequence of texts."""
        ...

    async def embed_query(self, query: str) -> list[float]:
        """Generate an embedding vector for a single search query."""
        ...
