"""Reusable Tier 2 Shared Components runtime registry (`_shared/`)."""

from __future__ import annotations

from _shared.components import COMPONENT_REGISTRY, resolve_component

__all__ = ["COMPONENT_REGISTRY", "resolve_component"]
