"""Phase 5 Documentation Subgraph — Documentarian authoring, Claim Check freeze, and Gate 3."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.coordinator.dependencies import CoordinatorDeps
from src.coordinator.edges import route_after_impl_doc_review
from src.state.blackboard import (
    BlackboardState,
    BuildLogEntry,
    ContractPointer,
    GateDecisionRecord,
    ImplDocRevision,
    assert_contract_pointer_valid,
    freeze_contract_pointer,
    get_latest_impl_docs,
)
from src.state.projections import project_for_documentarian


def _coerce_pointer(raw: Any) -> Optional[ContractPointer]:
    if raw is None:
        return None
    if isinstance(raw, ContractPointer):
        return raw
    return ContractPointer.model_validate(raw)


def build_document_subgraph(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Build and compile the Phase 5 Documentation LangGraph subgraph."""
    active_deps = deps or CoordinatorDeps()
    valves = active_deps.get_safety_valves()

    def node_author_impl_docs(state: BlackboardState) -> dict[str, Any]:
        projection = project_for_documentarian(state)
        staging_impl_dir = active_deps.resolve_staging_dir(state["run_id"]) / "impl_docs"
        out = active_deps.get_documentarian().author_impl_docs(
            projection,
            output_dir=staging_impl_dir,
        )
        out.pop("impl_doc_approved", None)
        out["impl_docs_approved"] = False
        latest_rev = out["impl_doc_revisions"][-1]
        agent_name = (latest_rev.package.agent_yaml.get("agent") or {}).get("name")
        if agent_name:
            out["target_agent_name"] = str(agent_name)
        return out

    def node_gate_impl_doc_review(state: BlackboardState) -> dict[str, Any]:
        latest: Optional[ImplDocRevision] = get_latest_impl_docs(state)
        if latest is None:
            raise RuntimeError("Gate 3 Impl Doc Review reached without any ImplDocRevision.")

        impl_ptr = _coerce_pointer(state.get("impl_docs_pointer"))
        tier3_ptr = _coerce_pointer(state.get("tier3_contract_pointer"))

        if not valves.gates.require_impl_doc_approval:
            if impl_ptr is not None:
                assert_contract_pointer_valid(impl_ptr)
                impl_ptr = freeze_contract_pointer(impl_ptr, approved_by="auto_policy")
            if tier3_ptr is not None:
                assert_contract_pointer_valid(tier3_ptr)
                tier3_ptr = freeze_contract_pointer(tier3_ptr, approved_by="auto_policy")
            return {
                "current_phase": "document",
                "impl_docs_approved": True,
                "approved_impl_doc_version": latest.revision_number,
                "impl_doc_decision": "approved",
                "impl_doc_feedback": None,
                "impl_docs_pointer": impl_ptr,
                "tier3_contract_pointer": tier3_ptr,
                "active_fix_instructions": [],
                "gate_decisions": [
                    GateDecisionRecord(
                        gate_name="impl_doc_review",
                        round_or_revision=latest.revision_number,
                        decision="approved",
                        reviewer="auto_policy",
                        frozen_pointer_sha256=impl_ptr.payload_sha256 if impl_ptr else None,
                    )
                ],
            }

        payload = interrupt(
            {
                "gate": "impl_doc_review",
                "revision_number": latest.revision_number,
                "max_revisions": valves.loops.max_impl_doc_revisions,
                "agent_name": (latest.package.agent_yaml.get("agent") or {}).get("name"),
                "summary": latest.summary,
                "stages": sorted(latest.package.stages.keys()),
                "contracts": sorted(latest.package.contracts.keys()),
                "has_tier3_contract": latest.package.tier3_contract_yaml is not None,
                "impl_docs_sha256": impl_ptr.payload_sha256 if impl_ptr else None,
                "storage_uri": impl_ptr.storage_uri if impl_ptr else None,
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

        if decision == "approved":
            if impl_ptr is not None:
                assert_contract_pointer_valid(impl_ptr)
                impl_ptr = freeze_contract_pointer(impl_ptr, approved_by=reviewer)
            if tier3_ptr is not None:
                assert_contract_pointer_valid(tier3_ptr)
                tier3_ptr = freeze_contract_pointer(tier3_ptr, approved_by=reviewer)

            gate_rec = GateDecisionRecord(
                gate_name="impl_doc_review",
                round_or_revision=latest.revision_number,
                decision="approved",
                reviewer=reviewer,
                feedback=feedback_str,
                frozen_pointer_sha256=impl_ptr.payload_sha256 if impl_ptr else None,
            )
            return {
                "current_phase": "document",
                "impl_docs_approved": True,
                "approved_impl_doc_version": latest.revision_number,
                "impl_doc_decision": "approved",
                "impl_doc_feedback": None,
                "impl_docs_pointer": impl_ptr,
                "tier3_contract_pointer": tier3_ptr,
                "active_fix_instructions": [],
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="document",
                        actor="coordinator",
                        action="gate_impl_doc_review",
                        status="completed",
                        details={
                            "decision": "approved",
                            "revision": latest.revision_number,
                            "frozen_sha256": impl_ptr.payload_sha256 if impl_ptr else None,
                        },
                    )
                ],
            }

        gate_rec = GateDecisionRecord(
            gate_name="impl_doc_review",
            round_or_revision=latest.revision_number,
            decision=decision,  # type: ignore[arg-type]
            reviewer=reviewer,
            feedback=feedback_str,
        )

        if decision == "revise":
            return {
                "current_phase": "document",
                "impl_docs_approved": False,
                "impl_doc_decision": "revise",
                "impl_doc_feedback": feedback_str or "Revisions requested at Gate 3.",
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="document",
                        actor="coordinator",
                        action="gate_impl_doc_review",
                        status="completed",
                        details={"decision": "revise", "revision": latest.revision_number},
                    )
                ],
            }

        reason = feedback_str or f"Implementation document review ended with '{decision}'."
        return {
            "current_phase": "aborted" if decision == "abort" else "failed",
            "impl_docs_approved": False,
            "impl_doc_decision": decision,
            "escalation_required": decision == "escalate",
            "escalation_reason": reason,
            "gate_decisions": [gate_rec],
            "build_logs": [
                BuildLogEntry(
                    phase="document",
                    actor="coordinator",
                    action="gate_impl_doc_review",
                    status="failed",
                    details={"decision": decision, "revision": latest.revision_number},
                )
            ],
        }

    def node_escalate_impl_docs(state: BlackboardState) -> dict[str, Any]:
        rev_count = int(state.get("impl_doc_revision_count", 0))
        msg = (
            f"Safety valve triggered: impl doc revisions ({rev_count}) exceeded "
            f"max_impl_doc_revisions ({valves.loops.max_impl_doc_revisions})."
        )
        return {
            "current_phase": "failed",
            "impl_docs_approved": False,
            "escalation_required": True,
            "escalation_reason": msg,
            "last_error": msg,
            "build_logs": [
                BuildLogEntry(
                    phase="document",
                    actor="coordinator",
                    action="impl_doc_safety_valve",
                    status="failed",
                    details={
                        "impl_doc_revision_count": rev_count,
                        "max_impl_doc_revisions": valves.loops.max_impl_doc_revisions,
                    },
                )
            ],
        }

    def _route_docs(state: BlackboardState) -> str:
        return route_after_impl_doc_review(state, safety_valves=valves)

    workflow = StateGraph(BlackboardState)
    workflow.add_node("author_impl_docs", node_author_impl_docs)
    workflow.add_node("gate_impl_doc_review", node_gate_impl_doc_review)
    workflow.add_node("escalate_impl_docs", node_escalate_impl_docs)

    workflow.set_entry_point("author_impl_docs")
    workflow.add_edge("author_impl_docs", "gate_impl_doc_review")
    workflow.add_conditional_edges(
        "gate_impl_doc_review",
        _route_docs,
        {
            "revise": "author_impl_docs",
            "approved": END,
            "escalate": "escalate_impl_docs",
            "abort": END,
        },
    )
    workflow.add_edge("escalate_impl_docs", END)
    return workflow.compile()
