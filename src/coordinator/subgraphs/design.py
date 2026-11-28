"""Phase 4 Design Subgraph — Architect proposal generation, Gate 2 review, and revision cap."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.coordinator.edges import route_after_architecture_review
from src.state.blackboard import (
    ArchitectureProposal,
    BlackboardState,
    BuildLogEntry,
    GateDecisionRecord,
    get_latest_architecture,
)
from src.state.projections import project_for_architect


def build_design_subgraph(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Build and compile the Phase 4 Design LangGraph subgraph."""
    active_deps = deps or CoordinatorDeps()
    valves = active_deps.get_safety_valves()

    def _sync_propose(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_architect()
        projection = project_for_architect(state)
        if getattr(specialist, "rag_facade", None) is None and getattr(specialist, "embedder", None) is None:
            return specialist.design(projection)
        return run_coro_sync(specialist.adesign(projection))

    async def _async_propose(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_architect()
        projection = project_for_architect(state)
        return await specialist.adesign(projection)

    def node_gate_architecture_review(state: BlackboardState) -> dict[str, Any]:
        latest: Optional[ArchitectureProposal] = get_latest_architecture(state)
        if latest is None:
            raise RuntimeError("Gate 2 Architecture Review reached without an ArchitectureProposal.")

        if not valves.gates.require_architecture_approval:
            return {
                "current_phase": "design",
                "architecture_approved": True,
                "approved_architecture_version": latest.revision_number,
                "architecture_decision": "approved",
                "architecture_feedback": None,
                "gate_decisions": [
                    GateDecisionRecord(
                        gate_name="architecture_review",
                        round_or_revision=latest.revision_number,
                        decision="approved",
                        reviewer="auto_policy",
                    )
                ],
            }

        payload = interrupt(
            {
                "gate": "architecture_review",
                "revision_number": latest.revision_number,
                "max_revisions": valves.loops.max_architecture_revisions,
                "agent_name": latest.agent_name,
                "pattern": latest.pattern,
                "runtime": latest.runtime,
                "summary": latest.summary,
                "proposal": latest.model_dump(mode="json"),
            }
        )

        if isinstance(payload, Mapping):
            raw_decision = str(payload.get("decision", "abort")).strip().lower()
            feedback = payload.get("feedback")
            reviewer = str(payload.get("reviewer") or payload.get("approved_by") or "human").strip() or "human"
        else:
            raw_decision = "abort"
            feedback = None
            reviewer = "human"

        decision = raw_decision if raw_decision in {"approved", "revise", "abort", "escalate"} else "abort"
        feedback_str = str(feedback).strip() if feedback is not None and str(feedback).strip() else None

        gate_rec = GateDecisionRecord(
            gate_name="architecture_review",
            round_or_revision=latest.revision_number,
            decision=decision,  # type: ignore[arg-type]
            reviewer=reviewer,
            feedback=feedback_str,
        )

        if decision == "approved":
            return {
                "current_phase": "design",
                "architecture_approved": True,
                "approved_architecture_version": latest.revision_number,
                "architecture_decision": "approved",
                "architecture_feedback": None,
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="design",
                        actor="coordinator",
                        action="gate_architecture_review",
                        status="completed",
                        details={"decision": "approved", "revision": latest.revision_number},
                    )
                ],
            }

        if decision == "revise":
            return {
                "current_phase": "design",
                "architecture_approved": False,
                "architecture_decision": "revise",
                "architecture_feedback": feedback_str or "Revisions requested at Gate 2.",
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="design",
                        actor="coordinator",
                        action="gate_architecture_review",
                        status="completed",
                        details={"decision": "revise", "revision": latest.revision_number},
                    )
                ],
            }

        reason = feedback_str or f"Architecture review ended with '{decision}'."
        return {
            "current_phase": "aborted" if decision == "abort" else "failed",
            "architecture_approved": False,
            "architecture_decision": decision,
            "escalation_required": decision == "escalate",
            "escalation_reason": reason,
            "gate_decisions": [gate_rec],
            "build_logs": [
                BuildLogEntry(
                    phase="design",
                    actor="coordinator",
                    action="gate_architecture_review",
                    status="failed",
                    details={"decision": decision, "revision": latest.revision_number},
                )
            ],
        }

    def node_escalate_architecture(state: BlackboardState) -> dict[str, Any]:
        rev_count = int(state.get("architecture_revision_count", 0))
        msg = (
            f"Safety valve triggered: architecture revisions ({rev_count}) exceeded "
            f"max_architecture_revisions ({valves.loops.max_architecture_revisions})."
        )
        return {
            "current_phase": "failed",
            "architecture_approved": False,
            "escalation_required": True,
            "escalation_reason": msg,
            "last_error": msg,
            "build_logs": [
                BuildLogEntry(
                    phase="design",
                    actor="coordinator",
                    action="architecture_safety_valve",
                    status="failed",
                    details={
                        "architecture_revision_count": rev_count,
                        "max_architecture_revisions": valves.loops.max_architecture_revisions,
                    },
                )
            ],
        }

    def _route_arch(state: BlackboardState) -> str:
        return route_after_architecture_review(state, safety_valves=valves)

    workflow = StateGraph(BlackboardState)
    workflow.add_node("propose_architecture", make_dual_node(_sync_propose, _async_propose))
    workflow.add_node("gate_architecture_review", node_gate_architecture_review)
    workflow.add_node("escalate_architecture", node_escalate_architecture)

    workflow.set_entry_point("propose_architecture")
    workflow.add_edge("propose_architecture", "gate_architecture_review")
    workflow.add_conditional_edges(
        "gate_architecture_review",
        _route_arch,
        {
            "revise": "propose_architecture",
            "approved": END,
            "escalate": "escalate_architecture",
            "abort": END,
        },
    )
    workflow.add_edge("escalate_architecture", END)
    return workflow.compile()
