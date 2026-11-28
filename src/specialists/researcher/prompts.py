"""Prompt builders for the Researcher specialist."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.state.projections import ResearcherProjection

RESEARCHER_SYSTEM_PROMPT = """\
You are the Researcher specialist for DDLC Agent Studio (Phase 3 Feasibility).
Your job is to evaluate whether the agent specification is technically feasible using
the approved stack (LiteLLM, Instructor, LangGraph, dbt-core, SQLGlot, PostgreSQL/pgvector)
and organizational standards.

Rules:
- Ground your tool evaluations strictly in the provided RAG <rag_context> excerpts and spec constraints.
- Never approve libraries listed in constraints.avoided_libraries.
- Surface concrete limitations, compatibility risks, and policy constraints.
- Produce a clear verdict: "feasible", "feasible_with_caveats", or "not_feasible".
"""


def build_research_messages(
    projection: ResearcherProjection,
    *,
    tool_rag_slices: Mapping[str, str],
    org_standards_xml: str,
    avoided_conflicts: Sequence[str],
    candidate_components: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    user_payload = {
        "parsed_spec": projection.parsed_spec,
        "parsed_spec_summary": (
            projection.parsed_spec_summary.model_dump(mode="json")
            if projection.parsed_spec_summary is not None
            else None
        ),
        "enriched_requirements": [
            r.model_dump(mode="json") for r in projection.enriched_requirements
        ],
        "deterministic_avoided_library_conflicts": list(avoided_conflicts),
        "candidate_shared_components": [dict(c) for c in candidate_components],
        "rag_library_docs": dict(tool_rag_slices),
        "rag_org_standards": org_standards_xml,
    }
    return [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]
