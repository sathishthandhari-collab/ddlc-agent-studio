"""Phase 3 Research Subgraph — RAG feasibility evaluation and not_feasible contingency gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.coordinator.edges import route_after_feasibility_gate, route_after_research
from src.state.blackboard import (
    BlackboardState,
    BuildLogEntry,
    ClarifiedRequirement,
    ResearchFindings,
)
from src.state.projections import project_for_researcher


def build_research_subgraph(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Build and compile the Phase 3 Research LangGraph subgraph."""
    active_deps = deps or CoordinatorDeps()

    def _sync_evaluate(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_researcher()
        projection = project_for_researcher(state)
        if getattr(specialist, "rag_facade", None) is None and getattr(specialist, "embedder", None) is None:
            return specialist.research(projection)
        return run_coro_sync(specialist.aresearch(projection))

    async def _async_evaluate(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_researcher()
        projection = project_for_researcher(state)
        return await specialist.aresearch(projection)

    def node_gate_feasibility(state: BlackboardState) -> dict[str, Any]:
        findings_raw = state.get("research_findings")
        findings = (
            findings_raw
            if isinstance(findings_raw, ResearchFindings)
            else ResearchFindings.model_validate(findings_raw)
        )
        payload = interrupt(
            {
                "gate": "feasibility_escalation",
                "verdict": findings.verdict,
                "summary": findings.summary,
                "policy_constraints": list(findings.policy_constraints),
                "risks": list(findings.risks),
            }
        )

        if isinstance(payload, Mapping):
            decision = str(payload.get("decision", "abort")).strip().lower()
            feedback = payload.get("feedback") or payload.get("guidance")
        else:
            decision = "abort"
            feedback = None

        if decision == "revise":
            extra_reqs: list[ClarifiedRequirement] = []
            if feedback and str(feedback).strip():
                extra_reqs.append(
                    ClarifiedRequirement(
                        requirement_id=f"feasibility_revise_{len(state.get('enriched_requirements') or []) + 1}",
                        source_round=max(1, int(state.get("clarification_round_count", 1))),
                        category="feasibility_override",
                        summary=str(feedback).strip(),
                        affected_sections=["constraints", "tools"],
                    )
                )
            return {
                "current_phase": "research",
                "human_escalated_decision": "revise",
                "escalation_required": False,
                "escalation_reason": None,
                "enriched_requirements": extra_reqs,
                "build_logs": [
                    BuildLogEntry(
                        phase="research",
                        actor="coordinator",
                        action="gate_feasibility_revise",
                        status="completed",
                        details={"feedback": feedback},
                    )
                ],
            }

        if decision == "approved":
            return {
                "current_phase": "research",
                "human_escalated_decision": "approved",
                "escalation_required": False,
                "escalation_reason": None,
                "build_logs": [
                    BuildLogEntry(
                        phase="research",
                        actor="coordinator",
                        action="gate_feasibility_override",
                        status="completed",
                        details={"feedback": feedback},
                    )
                ],
            }

        reason = str(feedback or f"Specification deemed not feasible: {findings.summary}")
        return {
            "current_phase": "aborted",
            "human_escalated_decision": "abort",
            "escalation_required": True,
            "escalation_reason": reason,
            "last_error": reason,
            "build_logs": [
                BuildLogEntry(
                    phase="research",
                    actor="coordinator",
                    action="gate_feasibility_abort",
                    status="failed",
                    details={"reason": reason},
                )
            ],
        }

    workflow = StateGraph(BlackboardState)
    workflow.add_node("evaluate_feasibility", make_dual_node(_sync_evaluate, _async_evaluate))
    workflow.add_node("gate_feasibility", node_gate_feasibility)

    workflow.set_entry_point("evaluate_feasibility")
    workflow.add_conditional_edges(
        "evaluate_feasibility",
        route_after_research,
        {
            "gate_feasibility": "gate_feasibility",
            "done": END,
        },
    )
    workflow.add_conditional_edges(
        "gate_feasibility",
        route_after_feasibility_gate,
        {
            "re_research": "evaluate_feasibility",
            "proceed": END,
            "abort": END,
        },
    )
    return workflow.compile()
