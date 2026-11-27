"""Researcher specialist RAG facade (library_docs, org_standards)."""

from __future__ import annotations

from src.rag.interfaces import _BaseSpecialistRAGFacade


class ResearcherRAGFacade(_BaseSpecialistRAGFacade):
    """Budget-clamped RAG interface for the Researcher specialist."""

    _specialist_key = "researcher"

    async def query_tool_docs(self, tool_name: str, query: str) -> str:
        """Query library_docs scoped to a specific tool under the researcher token ceiling."""
        return await self._query_scoped(
            domain="library_docs",
            primary=tool_name,
            secondary=query,
        )

    async def query_org_standards(self, topic: str) -> str:
        """Query org_standards under the researcher token ceiling."""
        return await self._query_domain(domain="org_standards", query=topic)
