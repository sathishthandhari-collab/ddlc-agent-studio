"""Embedding protocol and LiteLLM embedding client for DDLC Agent Studio RAG."""

from src.rag.embeddings.base import BaseEmbedder
from src.rag.embeddings.embedder import EmbeddingConfigError, LiteLLMEmbedder

__all__ = [
    "BaseEmbedder",
    "EmbeddingConfigError",
    "LiteLLMEmbedder",
]
