"""100% deterministic Coordinator routing edges and safety valve enforcement.

No LLMs, no prompts, and no external I/O live here: every edge function performs
pure field lookups on BlackboardState against SafetyValvesConfig limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Optional

from src.core.settings import SafetyValvesConfig, get_safety_valves_config
from src.state.blackboard import ValidationReport


def _resolve_valves(safety_valves: Optional[SafetyValvesConfig]) -> SafetyValvesConfig:
    return safety_valves if safety_valves is not None else get_safety_valves_config()


def _latest_gate_decision(state: Mapping[str, Any], gate_name: str) -> Optional[str]:
    decisions = state.get("gate_decisions") or []
    for item in reversed(decisions):
        gname = item.get("gate_name") if isinstance(item, Mapping) else getattr(item, "gate_name", None)
        if gname == gate_name:
            dec = item.get("decision") if isinstance(item, Mapping) else getattr(item, "decision", None)
            return str(dec) if dec is not None else None
    return None


# ---------------------------------------------------------------------------
# Top-Level Phase & Mode Edges
# ---------------------------------------------------------------------------


def route_after_setup(
    state: Mapping[str, Any],
) -> Literal["understand_phase", "change_analysis", "abort"]:
    """Dispatch Day-0 build vs. Day-2 change flow after Phase 1 Setup."""
    if state.get("escalation_required") or state.get("current_phase") in {"aborted", "failed"}:
        return "abort"
    if state.get("mode") == "change":
        return "change_analysis"
    return "understand_phase"


def route_after_understand_phase(
    state: Mapping[str, Any],
) -> Literal["research_phase", "abort"]:
    """Proceed to Phase 3 Research unless spec was rejected, aborted, or escalated."""
    if (
        state.get("escalation_required")
        or state.get("current_phase") in {"aborted", "failed"}
        or state.get("spec_validation_status") == "rejected"
    ):
        return "abort"
    return "research_phase"


def route_after_research_phase(
    state: Mapping[str, Any],
) -> Literal["design_phase", "abort"]:
    """Proceed to Phase 4 Design unless feasibility escalated/aborted."""
    if state.get("escalation_required") or state.get("current_phase") in {"aborted", "failed"}:
        return "abort"
    return "design_phase"


def route_after_design_phase(
    state: Mapping[str, Any],
) -> Literal["document_phase", "abort"]:
    """Proceed to Phase 5 Documentation only when architecture is explicitly approved."""
    if (
        state.get("escalation_required")
        or state.get("current_phase") in {"aborted", "failed"}
        or not state.get("architecture_approved")
    ):
        return "abort"
    return "document_phase"


def route_after_document_phase(
    state: Mapping[str, Any],
) -> Literal["construct_phase", "abort"]:
    """Proceed to Phase 6 Construction only when implementation docs are approved."""
    if (
        state.get("escalation_required")
        or state.get("current_phase") in {"aborted", "failed"}
        or not state.get("impl_docs_approved")
    ):
        return "abort"
    return "construct_phase"


def route_after_construct_phase(
    state: Mapping[str, Any],
) -> Literal["deliver", "document_phase", "abort"]:
    """Route validated builds to Phase 8 Delivery, doc fixes to Document, or failures to Abort."""
    if state.get("escalation_required") or state.get("current_phase") in {"aborted", "failed"}:
        return "abort"
    if state.get("validation_passed"):
        return "deliver"
    if state.get("current_phase") == "document" and not state.get("impl_docs_approved"):
        return "document_phase"
    return "abort"


# ---------------------------------------------------------------------------
# Phase 2 Understanding Subgraph Edges (Gate 1 + Clarification Safety Valve)
# ---------------------------------------------------------------------------


def route_after_spec_analysis(
    state: Mapping[str, Any],
    *,
    safety_valves: Optional[SafetyValvesConfig] = None,
) -> Literal["gate_clarification", "escalate", "reject", "done"]:
    """Deterministic routing after Spec Analyst gap analysis."""
    if state.get("spec_validation_status") == "rejected":
        return "reject"

    if not state.get("has_open_gaps"):
        return "done"

    valves = _resolve_valves(safety_valves)
    if not valves.gates.require_clarification_gate:
        return "done"

    round_count = int(state.get("clarification_round_count", 0))
    if round_count > valves.loops.max_clarification_rounds:
        return "escalate"
    return "gate_clarification"


def route_after_clarification_gate(
    state: Mapping[str, Any],
) -> Literal["incorporate_answers", "abort"]:
    """Route Gate 1 user responses to Spec Analyst refinement or abort."""
    if (
        state.get("current_phase") in {"aborted", "failed"}
        or state.get("human_escalated_decision") == "abort"
        or _latest_gate_decision(state, "clarification") == "abort"
    ):
        return "abort"
    return "incorporate_answers"


# ---------------------------------------------------------------------------
# Phase 3 Research Subgraph Edges (Feasibility Contingency)
# ---------------------------------------------------------------------------


def route_after_research(
    state: Mapping[str, Any],
) -> Literal["gate_feasibility", "done"]:
    """Route 'not_feasible' verdict to human escalation gate per architecture.md §8.2."""
    if state.get("feasibility_verdict") == "not_feasible":
        return "gate_feasibility"
    return "done"


def route_after_feasibility_gate(
    state: Mapping[str, Any],
) -> Literal["re_research", "proceed", "abort"]:
    """Route human decision after a 'not_feasible' feasibility escalation."""
    if state.get("current_phase") in {"aborted", "failed"}:
        return "abort"
    decision = str(state.get("human_escalated_decision") or "").strip().lower()
    if decision == "revise":
        return "re_research"
    if decision == "approved":
        return "proceed"
    return "abort"


# ---------------------------------------------------------------------------
# Phase 4 Design Subgraph Edges (Gate 2 + Architecture Revision Safety Valve)
# ---------------------------------------------------------------------------


def route_after_architecture_review(
    state: Mapping[str, Any],
    *,
    safety_valves: Optional[SafetyValvesConfig] = None,
) -> Literal["revise", "approved", "escalate", "abort"]:
    """Deterministic multi-way edge after Gate 2 Architecture Review."""
    decision = str(
        state.get("architecture_decision")
        or _latest_gate_decision(state, "architecture_review")
        or "abort"
    ).strip().lower()

    if decision == "approved" and state.get("architecture_approved"):
        return "approved"
    if decision == "escalate":
        return "escalate"
    if decision == "revise":
        valves = _resolve_valves(safety_valves)
        rev_count = int(state.get("architecture_revision_count", 0))
        if rev_count > valves.loops.max_architecture_revisions:
            return "escalate"
        return "revise"
    return "abort"


# ---------------------------------------------------------------------------
# Phase 5 Documentation Subgraph Edges (Gate 3 + Impl Doc Revision Valve)
# ---------------------------------------------------------------------------


def route_after_impl_doc_review(
    state: Mapping[str, Any],
    *,
    safety_valves: Optional[SafetyValvesConfig] = None,
) -> Literal["revise", "approved", "escalate", "abort"]:
    """Deterministic multi-way edge after Gate 3 Implementation Document Review."""
    decision = str(
        state.get("impl_doc_decision")
        or _latest_gate_decision(state, "impl_doc_review")
        or "abort"
    ).strip().lower()

    if decision == "approved" and state.get("impl_docs_approved"):
        return "approved"
    if decision == "escalate":
        return "escalate"
    if decision == "revise":
        valves = _resolve_valves(safety_valves)
        rev_count = int(state.get("impl_doc_revision_count", 0))
        if rev_count > valves.loops.max_impl_doc_revisions:
            return "escalate"
        return "revise"
    return "abort"


# ---------------------------------------------------------------------------
# Phase 6 & 7 Construction/Validation Subgraph Edges (Fix Loop + Gate 4)
# ---------------------------------------------------------------------------


def _coerce_latest_report(state: Mapping[str, Any]) -> Optional[ValidationReport]:
    reports = state.get("validation_reports") or []
    if not reports:
        return None
    last = reports[-1]
    if isinstance(last, ValidationReport):
        return last
    return ValidationReport.model_validate(last)


def route_after_validation(
    state: Mapping[str, Any],
    *,
    safety_valves: Optional[SafetyValvesConfig] = None,
) -> Literal["passed", "fix_builder", "fix_documentarian", "escalate"]:
    """Route Phase 7 Validator results to pass, bounded Builder/Doc fix, or Gate 4 escalation."""
    if state.get("validation_passed"):
        return "passed"

    report = _coerce_latest_report(state)
    if report is not None:
        blocking = [iss for iss in report.issues if iss.severity in {"error", "critical"}]
        if any(iss.route_target == "human" for iss in blocking):
            return "escalate"
    else:
        blocking = []

    valves = _resolve_valves(safety_valves)
    fix_attempts = int(state.get("build_fix_attempt_count", 0))
    if fix_attempts >= valves.loops.max_build_fix_attempts:
        return "escalate"

    if blocking and all(iss.route_target == "documentarian" for iss in blocking):
        return "fix_documentarian"
    return "fix_builder"


def route_after_build_escalation(
    state: Mapping[str, Any],
) -> Literal["revalidate", "rebuild", "abort"]:
    """Route human decision after Gate 4 Build Escalation."""
    if state.get("current_phase") in {"aborted", "failed"}:
        return "abort"
    decision = str(
        state.get("human_escalated_decision")
        or _latest_gate_decision(state, "build_escalation")
        or "abort"
    ).strip().lower()
    if decision == "approved":
        return "revalidate"
    if decision == "revise":
        return "rebuild"
    return "abort"
