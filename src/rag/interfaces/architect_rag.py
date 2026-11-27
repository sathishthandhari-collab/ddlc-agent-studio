"""Architect specialist RAG facade (pattern_catalog, past_decisions, org_standards, component_docs)."""

from __future__ import annotations

from typing import Optional

from src.rag.interfaces import _BaseSpecialistRAGFacade


class ArchitectRAGFacade(_BaseSpecialistRAGFacade):
    """Budget-clamped RAG interface for the Architect specialist."""

    _specialist_key = "architect"

    async def query_patterns(self, query: str) -> str:
        """Query pattern_catalog under the architect token ceiling."""
        return await self._query_domain(domain="pattern_catalog", query=query)

    async def query_past_decisions(self, query: str) -> str:
        """Query past_decisions ADRs; returns empty XML (count='0') on cold start."""
        return await self._query_domain(domain="past_decisions", query=query)

    async def query_org_standards(self, topic: str) -> str:
        """Query org_standards under the architect token ceiling."""
        return await self._query_domain(domain="org_standards", query=topic)

    async def query_component_docs(
        self,
        component_or_query: str,
        query: Optional[str] = None,
    ) -> str:
        """Query component_docs under the architect token ceiling."""
        return await self._query_scoped(
            domain="component_docs",
            primary=component_or_query,
            secondary=query,
        )
