"""Lazy, timeout-guarded FlashRank cross-encoder reranker with RRF fallback."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

from src.core.settings import get_studio_config
from src.rag.store.models import RAGSearchResult

logger = logging.getLogger(__name__)


def _fallback_sort(candidates: list[RAGSearchResult]) -> list[RAGSearchResult]:
    return sorted(candidates, key=lambda c: float(c.rrf_score), reverse=True)


class FlashRankReranker:
    """Thread-offloaded FlashRank cross-encoder wrapper with circuit-breaker fallback."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        cache_dir: Optional[Path | str] = None,
        timeout_ms: Optional[int] = None,
        enabled: Optional[bool] = None,
        fallback_to_rrf: Optional[bool] = None,
        ranker: Optional[Any] = None,
    ) -> None:
        self._model_name_override = model_name
        self._cache_dir_override = Path(cache_dir) if cache_dir is not None else None
        self._timeout_ms_override = timeout_ms
        self._enabled_override = enabled
        self._fallback_override = fallback_to_rrf
        self._ranker: Optional[Any] = ranker
        self._init_attempted: bool = ranker is not None

    def _resolve_settings(self) -> tuple[bool, str, Path, int, bool]:
        if (
            self._enabled_override is not None
            and self._model_name_override is not None
            and self._timeout_ms_override is not None
            and self._fallback_override is not None
        ):
            cache = self._cache_dir_override or (Path(tempfile.gettempdir()) / "flashrank")
            return (
                self._enabled_override,
                self._model_name_override,
                cache,
                self._timeout_ms_override,
                self._fallback_override,
            )

        cfg = get_studio_config().rag.retrieval.reranker
        enabled = cfg.enabled if self._enabled_override is None else self._enabled_override
        model_name = self._model_name_override or cfg.model
        cache = self._cache_dir_override or (Path(tempfile.gettempdir()) / "flashrank")
        timeout_ms = cfg.timeout_ms if self._timeout_ms_override is None else self._timeout_ms_override
        fallback = cfg.fallback_to_rrf if self._fallback_override is None else self._fallback_override
        return enabled, model_name, cache, timeout_ms, fallback

    @property
    def model_name(self) -> str:
        return self._resolve_settings()[1]

    @property
    def cache_dir(self) -> Path:
        return self._resolve_settings()[2]

    @property
    def timeout_ms(self) -> int:
        return self._resolve_settings()[3]

    @property
    def enabled(self) -> bool:
        return self._resolve_settings()[0]

    @property
    def fallback_to_rrf(self) -> bool:
        return self._resolve_settings()[4]

    @property
    def ranker(self) -> Optional[Any]:
        return self._ranker

    @ranker.setter
    def ranker(self, value: Optional[Any]) -> None:
        self._ranker = value
        self._init_attempted = True

    def _ensure_ranker(self, model_name: str, cache_dir: Path) -> Optional[Any]:
        if self._ranker is not None:
            return self._ranker
        if self._init_attempted:
            return None

        self._init_attempted = True
        try:
            from flashrank import Ranker

            cache_dir.mkdir(parents=True, exist_ok=True)
            self._ranker = Ranker(model_name=model_name, cache_dir=str(cache_dir))
        except Exception as exc:
            logger.warning(
                "FlashRank Ranker unavailable (%s); falling back to RRF ordering: %s",
                model_name,
                exc,
            )
            self._ranker = None
        return self._ranker

    async def rerank(
        self,
        query: str,
        candidates: list[RAGSearchResult],
    ) -> list[RAGSearchResult]:
        """Rerank candidate chunks asynchronously; fall back to RRF on error or timeout."""
        if not candidates:
            return []

        enabled, model_name, cache_dir, timeout_ms, fallback_to_rrf = self._resolve_settings()
        if not enabled:
            return _fallback_sort(candidates)

        ranker = self._ensure_ranker(model_name, cache_dir)
        if ranker is None:
            return _fallback_sort(candidates)

        from flashrank import RerankRequest

        passages = [{"id": str(c.chunk_id), "text": c.content} for c in candidates]
        request = RerankRequest(query=query, passages=passages)
        timeout_s = max(0.0, timeout_ms / 1000.0)

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(ranker.rerank, request),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning(
                "FlashRank reranking timed out after %sms; falling back to RRF.",
                timeout_ms,
            )
            if not fallback_to_rrf:
                raise exc
            return _fallback_sort(candidates)
        except Exception as exc:
            logger.warning("FlashRank reranking failed (%s); falling back to RRF.", exc)
            if not fallback_to_rrf:
                raise
            return _fallback_sort(candidates)

        if not results:
            return _fallback_sort(candidates)

        score_map: dict[str, float] = {}
        for item in results:
            if isinstance(item, dict) and "id" in item and "score" in item:
                score_map[str(item["id"])] = float(item["score"])

        if not score_map:
            return _fallback_sort(candidates)

        reranked = [
            c.model_copy(update={"rerank_score": float(score_map.get(str(c.chunk_id), 0.0))})
            for c in candidates
        ]
        reranked.sort(
            key=lambda c: (
                c.rerank_score if c.rerank_score is not None else float("-inf"),
                float(c.rrf_score),
            ),
            reverse=True,
        )
        return reranked
