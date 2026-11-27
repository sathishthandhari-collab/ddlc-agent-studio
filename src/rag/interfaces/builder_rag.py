"""Builder specialist RAG facade (component_docs, code_conventions)."""

from __future__ import annotations

from typing import Optional

from src.rag.interfaces import _BaseSpecialistRAGFacade


class BuilderRAGFacade(_BaseSpecialistRAGFacade):
    """Budget-clamped RAG interface for the Builder (Leaf Coder) specialist."""

    _specialist_key = "builder"

    async def query_component_docs(
        self,
        component_or_query: str,
        query: Optional[str] = None,
    ) -> str:
        """Query component_docs under the builder token ceiling."""
        return await self._query_scoped(
            domain="component_docs",
            primary=component_or_query,
            secondary=query,
        )

    async def query_code_conventions(self, topic: str) -> str:
        """Query code_conventions under the builder token ceiling."""
        return await self._query_domain(domain="code_conventions", query=topic)
