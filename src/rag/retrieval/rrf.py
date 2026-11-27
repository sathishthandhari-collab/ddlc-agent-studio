"""Reciprocal Rank Fusion (RRF) scoring and ranked-list merging utilities."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any, Optional, TypeVar

from src.rag.store.models import RAGSearchResult

T = TypeVar("T")


def compute_rrf_score(
    dense_rank: Optional[int] = None,
    sparse_rank: Optional[int] = None,
    k: int = 60,
) -> float:
    """Compute Reciprocal Rank Fusion score: 1 / (k + r_dense) + 1 / (k + r_sparse)."""
    if k < 0:
        raise ValueError(f"RRF constant k must be non-negative, got {k}.")

    score = 0.0
    if dense_rank is not None:
        if dense_rank < 1:
            raise ValueError(f"dense_rank must be >= 1, got {dense_rank}.")
        score += 1.0 / (k + dense_rank)
    if sparse_rank is not None:
        if sparse_rank < 1:
            raise ValueError(f"sparse_rank must be >= 1, got {sparse_rank}.")
        score += 1.0 / (k + sparse_rank)
    return score


def _default_item_key(item: Any) -> Hashable:
    if hasattr(item, "chunk_id"):
        return getattr(item, "chunk_id")
    if isinstance(item, Mapping) and "chunk_id" in item:
        return item["chunk_id"]
    return item


def fuse_ranked_lists(
    dense_results: Sequence[T],
    sparse_results: Sequence[T],
    k: int = 60,
    top_k: Optional[int] = None,
    key: Optional[Callable[[T], Hashable]] = None,
) -> list[tuple[T, float]]:
    """Merge dense and sparse ranked sequences using Reciprocal Rank Fusion.

    Returns a list of ``(item, rrf_score)`` tuples ordered from highest to lowest
    fused score. Duplicate keys within the same input list keep their best rank.
    """
    if k < 0:
        raise ValueError(f"RRF constant k must be non-negative, got {k}.")
    if top_k is not None and top_k <= 0:
        return []

    key_fn = key or _default_item_key
    items_by_key: dict[Hashable, T] = {}
    dense_ranks: dict[Hashable, int] = {}
    sparse_ranks: dict[Hashable, int] = {}
    first_seen_order: dict[Hashable, int] = {}

    for rank, item in enumerate(dense_results, start=1):
        item_key = key_fn(item)
        if item_key not in dense_ranks:
            dense_ranks[item_key] = rank
        items_by_key.setdefault(item_key, item)
        first_seen_order.setdefault(item_key, len(first_seen_order))

    for rank, item in enumerate(sparse_results, start=1):
        item_key = key_fn(item)
        if item_key not in sparse_ranks:
            sparse_ranks[item_key] = rank
        items_by_key.setdefault(item_key, item)
        first_seen_order.setdefault(item_key, len(first_seen_order))

    scored: list[tuple[float, int, T]] = []
    for item_key, item in items_by_key.items():
        score = compute_rrf_score(
            dense_rank=dense_ranks.get(item_key),
            sparse_rank=sparse_ranks.get(item_key),
            k=k,
        )
        scored.append((score, first_seen_order[item_key], item))

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    fused = [(item, score) for score, _, item in scored]
    return fused[:top_k] if top_k is not None else fused


def fuse_search_results(
    dense_results: Sequence[RAGSearchResult],
    sparse_results: Sequence[RAGSearchResult],
    k: int = 60,
    top_k: Optional[int] = None,
) -> list[RAGSearchResult]:
    """Fuse two ranked RAGSearchResult lists and return copies with updated rrf_score."""
    fused = fuse_ranked_lists(dense_results, sparse_results, k=k, top_k=top_k)
    return [item.model_copy(update={"rrf_score": float(score)}) for item, score in fused]
