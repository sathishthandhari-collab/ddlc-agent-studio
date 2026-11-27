"""Specialist retrieval facades enforcing domain isolation and token budgets."""

from __future__ import annotations

from typing import Optional

from src.core.settings import SpecialistBudgetConfig, get_studio_config
from src.rag.retrieval.hybrid_search import HybridRAGRetriever


class _BaseSpecialistRAGFacade:
    """Shared domain-scoped query helper reading specialist budgets lazily."""

    _specialist_key: str

    def __init__(self, retriever: HybridRAGRetriever) -> None:
        self.retriever = retriever

    def _get_budget(self) -> SpecialistBudgetConfig:
        return get_studio_config().rag.budgets[self._specialist_key]

    async def _query_domain(self, *, domain: str, query: str) -> str:
        budget = self._get_budget()
        slice_result = await self.retriever.search(
            domain=domain,
            query=query,
            top_k=budget.top_k,
            token_ceiling=budget.max_tokens,
        )
        return slice_result.to_xml()

    async def _query_scoped(
        self,
        *,
        domain: str,
        primary: str,
        secondary: Optional[str] = None,
    ) -> str:
        scoped_query = (
            f"{primary}: {secondary}"
            if secondary is not None and secondary.strip() and primary.strip()
            else (secondary if secondary is not None and not primary.strip() else primary)
        )
        return await self._query_domain(domain=domain, query=scoped_query)


from src.rag.interfaces.architect_rag import ArchitectRAGFacade  # noqa: E402
from src.rag.interfaces.builder_rag import BuilderRAGFacade  # noqa: E402
from src.rag.interfaces.researcher_rag import ResearcherRAGFacade  # noqa: E402

__all__ = [
    "ArchitectRAGFacade",
    "BuilderRAGFacade",
    "ResearcherRAGFacade",
]
