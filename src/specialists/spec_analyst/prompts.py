"""Prompt builders for the Spec Analyst specialist."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.state.blackboard import ClarificationAnswer, ClarificationRound
from src.state.projections import SpecAnalystProjection

SPEC_ANALYST_SYSTEM_PROMPT = """\
You are the Spec Analyst specialist for DDLC Agent Studio.
Your job is to inspect a validated Day-0 DDLC Agent YAML specification and identify
remaining ambiguities, implicit assumptions, governance omissions, or logical gaps
that downstream specialists (Researcher, Architect, Documentarian) need clarified.

Rules:
- Never re-ask questions already asked or answered in prior clarification rounds.
- Focus on high-impact gaps: unclear tool/library details, missing governance or audit rules,
  disconnected stage contracts, missing source/destination systems, or ambiguous edge cases.
- Suggest sensible defaults on each question when possible.
- If the specification is complete and unambiguous enough for feasibility research and architecture design,
  return an empty questions list and has_open_gaps=false.
"""

SPEC_REFINEMENT_SYSTEM_PROMPT = """\
You are the Spec Analyst specialist incorporating human Gate 1 clarification answers into
a structured DDLC agent specification.

Rules:
- Apply concrete factual clarifications directly via dotted path patches (e.g.,
  "infrastructure.sources[0].connection" or "governance.audit_requirements.enabled").
- Classify each answer as design_impacting=true if it changes architecture, gates, runtime,
  tool selection, governance policy, or contracts across stages; otherwise design_impacting=false
  if it merely fills a factual detail inside parsed_spec.
- Provide a concise resolved_requirement summary for every design-impacting answer.
"""

CHANGE_ANALYSIS_SYSTEM_PROMPT = """\
You are the Spec Analyst specialist analyzing a Day-2 maintenance change request against
an existing DDLC agent's implementation documents (impl_docs/).

Identify strictly which stages, contracts, and impl_docs files are affected, and determine
whether database table contracts require a DCM schema migration.
"""


def build_gap_analysis_messages(
    projection: SpecAnalystProjection,
    parsed_spec: Mapping[str, Any],
    deterministic_findings: Sequence[str],
) -> list[dict[str, str]]:
    prior_qa = [
        {
            "round": rnd.round_number,
            "questions": [q.model_dump() for q in rnd.questions],
            "answers": [a.model_dump() for a in rnd.answers],
        }
        for rnd in projection.clarification_rounds
    ]
    user_payload = {
        "parsed_spec": dict(parsed_spec),
        "deterministic_gap_findings": list(deterministic_findings),
        "prior_clarification_rounds": prior_qa,
        "known_agent_names": list(projection.known_agent_names),
        "known_component_names": list(projection.known_component_names),
    }
    return [
        {"role": "system", "content": SPEC_ANALYST_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]


def build_refinement_messages(
    parsed_spec: Mapping[str, Any],
    latest_round: ClarificationRound,
    answers: Sequence[ClarificationAnswer],
) -> list[dict[str, str]]:
    user_payload = {
        "parsed_spec": dict(parsed_spec),
        "questions": [q.model_dump() for q in latest_round.questions],
        "answers": [a.model_dump() for a in answers],
    }
    return [
        {"role": "system", "content": SPEC_REFINEMENT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]


def build_change_analysis_messages(projection: SpecAnalystProjection) -> list[dict[str, str]]:
    impl_summary = (
        {
            "agent_yaml": projection.active_impl_docs.agent_yaml,
            "stages": list(projection.active_impl_docs.stages.keys()),
            "contracts": list(projection.active_impl_docs.contracts.keys()),
            "has_tier3_contract": projection.active_impl_docs.tier3_contract_yaml is not None,
        }
        if projection.active_impl_docs is not None
        else None
    )
    user_payload = {
        "change_request": projection.change_request.model_dump() if projection.change_request else None,
        "active_impl_docs": impl_summary,
    }
    return [
        {"role": "system", "content": CHANGE_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]
