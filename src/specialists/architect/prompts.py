"""Prompt builders for the Architect specialist."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.state.projections import ArchitectProjection

ARCHITECT_SYSTEM_PROMPT = """\
You are the Architect specialist for DDLC Agent Studio (Phase 4 Design).
Design the architecture for a Layer 2 DDLC AI Agent by selecting a proven pattern from the
Pattern Catalog, choosing the runtime ("langgraph" or "python_litellm"), mapping stages,
reusing Tier 2 shared components before creating new ones, and documenting risks & trade-offs.

Catalog Patterns (choose one exact identifier):
- "stateful_pipeline_with_gates": PAT-01 multi-step pipeline with reflection loops & human gates (langgraph)
- "react_tool_agent": PAT-05 bounded exploratory read-only tool loop + post-loop gate (langgraph)
- "multi_step_planner_agent": PAT-02 / planner-generator multi-stage agent (langgraph)
- "lightweight_event_cli_agent": stateless linear CLI/webhook runner without gates (python_litellm)
- "multi_source_lineage_inspector": PAT-04 / multi-source catalog reconciliation (langgraph)

Rules:
- If human approval gates or reflection loops are required, runtime MUST be "langgraph".
- Reuse matching Tier 2 shared components with explicit usage_type ("import", "extend", or "wrap").
- Incorporate any human architecture_feedback completely when revising.
"""


def build_architecture_messages(
    projection: ArchitectProjection,
    *,
    patterns_xml: str,
    past_decisions_xml: str,
    org_standards_xml: str,
    component_docs_xml: str,
    semantic_components: Sequence[Mapping[str, Any]],
    downstream_impact: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    user_payload = {
        "mode": projection.mode,
        "parsed_spec_summary": projection.parsed_spec_summary.model_dump(mode="json"),
        "enriched_requirements": [
            r.model_dump(mode="json") for r in projection.enriched_requirements
        ],
        "research_findings": (
            projection.research_findings.model_dump(mode="json")
            if projection.research_findings is not None
            else None
        ),
        "catalog_context": (
            projection.catalog_context.model_dump(mode="json")
            if projection.catalog_context is not None
            else None
        ),
        "semantic_component_matches": [dict(c) for c in semantic_components],
        "current_architecture": (
            projection.current_architecture.model_dump(mode="json")
            if projection.current_architecture is not None
            else None
        ),
        "architecture_feedback": projection.architecture_feedback,
        "change_request": (
            projection.change_request.model_dump(mode="json")
            if projection.change_request is not None
            else None
        ),
        "downstream_lineage_impact": [dict(d) for d in downstream_impact],
        "rag_pattern_catalog": patterns_xml,
        "rag_past_decisions": past_decisions_xml,
        "rag_org_standards": org_standards_xml,
        "rag_component_docs": component_docs_xml,
    }
    return [
        {"role": "system", "content": ARCHITECT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]
