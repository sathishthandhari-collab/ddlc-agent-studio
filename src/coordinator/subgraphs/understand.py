"""Phase 2 Understanding Subgraph — Spec Analyst validation, gap detection, and Gate 1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.coordinator.dependencies import CoordinatorDeps
from src.coordinator.edges import (
    route_after_clarification_gate,
    route_after_spec_analysis,
)
from src.state.blackboard import (
    BlackboardState,
    BuildLogEntry,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    GateDecisionRecord,
    get_open_clarification_questions,
)
from src.state.projections import project_for_spec_analyst


def _coerce_answers(
    raw_answers: Any,
    questions: Sequence[ClarificationQuestion],
) -> list[ClarificationAnswer]:
    """Normalize Gate 1 human answer payloads into ClarificationAnswer models."""
    parsed: list[ClarificationAnswer] = []
    if isinstance(raw_answers, Mapping):
        for q_id, ans_val in raw_answers.items():
            text = str(ans_val).strip()
            if text:
                parsed.append(ClarificationAnswer(question_id=str(q_id), answer=text))
        return parsed

    if isinstance(raw_answers, Sequence) and not isinstance(raw_answers, (str, bytes)):
        for idx, item in enumerate(raw_answers):
            if isinstance(item, ClarificationAnswer):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(ClarificationAnswer.model_validate(item))
            elif idx < len(questions):
                text = str(item).strip()
                if text:
                    parsed.append(
                        ClarificationAnswer(question_id=questions[idx].question_id, answer=text)
                    )
    return parsed


def build_understand_subgraph(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Build and compile the Phase 2 Understanding LangGraph subgraph."""
    active_deps = deps or CoordinatorDeps()
    valves = active_deps.get_safety_valves()

    def node_analyze_spec(state: BlackboardState) -> dict[str, Any]:
        projection = project_for_spec_analyst(state)
        return active_deps.get_spec_analyst().analyze(projection)

    def node_gate_clarification(state: BlackboardState) -> dict[str, Any]:
        rounds = state.get("clarification_rounds") or []
        latest_round = (
            rounds[-1]
            if isinstance(rounds[-1], ClarificationRound)
            else ClarificationRound.model_validate(rounds[-1])
        )
        open_questions = get_open_clarification_questions(state)
        round_num = int(state.get("clarification_round_count", latest_round.round_number))

        payload = interrupt(
            {
                "gate": "clarification",
                "round_number": round_num,
                "max_rounds": valves.loops.max_clarification_rounds,
                "agent_name": state.get("target_agent_name"),
                "questions": [q.model_dump(mode="json") for q in open_questions],
            }
        )

        if isinstance(payload, Mapping):
            decision = str(payload.get("decision", "approved")).strip().lower()
            reviewer = str(payload.get("reviewer") or payload.get("approved_by") or "human").strip() or "human"
            feedback = payload.get("feedback")
            raw_answers = payload.get("answers", [])
        else:
            decision = "approved"
            reviewer = "human"
            feedback = None
            raw_answers = payload

        if decision == "abort":
            return {
                "current_phase": "aborted",
                "human_escalated_decision": "abort",
                "escalation_reason": feedback or "User aborted at Gate 1 clarification.",
                "gate_decisions": [
                    GateDecisionRecord(
                        gate_name="clarification",
                        round_or_revision=round_num,
                        decision="abort",
                        reviewer=reviewer,
                        feedback=str(feedback) if feedback else None,
                    )
                ],
                "build_logs": [
                    BuildLogEntry(
                        phase="understand",
                        actor="coordinator",
                        action="gate_clarification",
                        status="failed",
                        details={"decision": "abort", "round": round_num},
                    )
                ],
            }

        answers = _coerce_answers(raw_answers, open_questions)
        if not answers:
            # Apply suggested defaults cleanly if user approved without overriding individual questions
            answers = [
                ClarificationAnswer(
                    question_id=q.question_id,
                    answer=q.suggested_default or "Proceed with standard studio default.",
                    answered_by=reviewer,
                )
                for q in open_questions
            ]

        answered_round = latest_round.model_copy(
            update={"answers": answers, "resolved": False}
        )
        return {
            "current_phase": "understand",
            "clarification_rounds": [answered_round],
            "gate_decisions": [
                GateDecisionRecord(
                    gate_name="clarification",
                    round_or_revision=round_num,
                    decision="approved",
                    reviewer=reviewer,
                    feedback=str(feedback) if feedback else None,
                )
            ],
            "build_logs": [
                BuildLogEntry(
                    phase="understand",
                    actor="coordinator",
                    action="gate_clarification",
                    status="completed",
                    details={"round": round_num, "answers_count": len(answers)},
                )
            ],
        }

    def node_incorporate_answers(state: BlackboardState) -> dict[str, Any]:
        projection = project_for_spec_analyst(state)
        latest_round = projection.clarification_rounds[-1]
        return active_deps.get_spec_analyst().incorporate_answers(
            projection,
            latest_round.answers,
        )

    def node_reject_spec(state: BlackboardState) -> dict[str, Any]:
        errs = list(state.get("spec_rejected_errors") or [])
        msg = "Day-0 specification rejected by schema validator: " + "; ".join(errs)
        return {
            "current_phase": "failed",
            "escalation_required": True,
            "escalation_reason": msg,
            "last_error": msg,
            "build_logs": [
                BuildLogEntry(
                    phase="understand",
                    actor="coordinator",
                    action="reject_spec",
                    status="failed",
                    details={"errors": errs},
                )
            ],
        }

    def node_escalate_clarification(state: BlackboardState) -> dict[str, Any]:
        round_count = int(state.get("clarification_round_count", 0))
        msg = (
            f"Safety valve triggered: clarification rounds ({round_count}) exceeded "
            f"max_clarification_rounds ({valves.loops.max_clarification_rounds})."
        )
        return {
            "current_phase": "failed",
            "escalation_required": True,
            "escalation_reason": msg,
            "last_error": msg,
            "build_logs": [
                BuildLogEntry(
                    phase="understand",
                    actor="coordinator",
                    action="clarification_safety_valve",
                    status="failed",
                    details={
                        "clarification_round_count": round_count,
                        "max_clarification_rounds": valves.loops.max_clarification_rounds,
                    },
                )
            ],
        }

    def _route_spec(state: BlackboardState) -> str:
        return route_after_spec_analysis(state, safety_valves=valves)

    workflow = StateGraph(BlackboardState)
    workflow.add_node("analyze_spec", node_analyze_spec)
    workflow.add_node("gate_clarification", node_gate_clarification)
    workflow.add_node("incorporate_answers", node_incorporate_answers)
    workflow.add_node("reject_spec", node_reject_spec)
    workflow.add_node("escalate_clarification", node_escalate_clarification)

    workflow.set_entry_point("analyze_spec")
    workflow.add_conditional_edges(
        "analyze_spec",
        _route_spec,
        {
            "gate_clarification": "gate_clarification",
            "escalate": "escalate_clarification",
            "reject": "reject_spec",
            "done": END,
        },
    )
    workflow.add_conditional_edges(
        "gate_clarification",
        route_after_clarification_gate,
        {
            "incorporate_answers": "incorporate_answers",
            "abort": END,
        },
    )
    workflow.add_edge("incorporate_answers", "analyze_spec")
    workflow.add_edge("reject_spec", END)
    workflow.add_edge("escalate_clarification", END)
    return workflow.compile()
