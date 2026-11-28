"""Phase 6 & 7 Construction/Validation Subgraph — Dual-Tier Builder, Validator loop & Gate 4."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.coordinator.edges import route_after_build_escalation, route_after_validation
from src.state.blackboard import (
    BlackboardState,
    BuildLogEntry,
    ContractPointer,
    GateDecisionRecord,
    assert_contract_pointer_valid,
    build_contract_pointer_from_directory,
    get_latest_validation_report,
)
from src.state.projections import project_for_builder, project_for_validator

_SEED_IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "impl_docs"}


def _seed_existing_agent_into_staging(source_dir: Path, staging_dir: Path) -> None:
    """Copy existing agent Muscle/code into staging before Day-2 patch build (Decision 25)."""
    if not source_dir.is_dir() or staging_dir.exists():
        return
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source_dir,
        staging_dir,
        ignore=shutil.ignore_patterns(*_SEED_IGNORE_DIRS),
    )


def _resolve_build_output_dir(deps: CoordinatorDeps, state: Mapping[str, Any], agent_name: str) -> Path:
    return (deps.resolve_staging_dir(str(state["run_id"])) / "agent_build" / agent_name).resolve()


def build_construct_subgraph(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Build and compile the Phase 6 & 7 Construction + Validation LangGraph subgraph."""
    active_deps = deps or CoordinatorDeps()
    valves = active_deps.get_safety_valves()

    def _prepare_build(state: BlackboardState) -> tuple[Any, Path]:
        impl_ptr_raw = state.get("impl_docs_pointer")
        if impl_ptr_raw is not None:
            impl_ptr = (
                impl_ptr_raw
                if isinstance(impl_ptr_raw, ContractPointer)
                else ContractPointer.model_validate(impl_ptr_raw)
            )
            assert_contract_pointer_valid(impl_ptr)

        projection = project_for_builder(state)
        agent_name = str(projection.target_agent_name or "generated_agent")
        build_dir = _resolve_build_output_dir(active_deps, state, agent_name)

        if projection.mode == "change" and projection.build_fix_attempt_count == 0:
            existing_agent_dir = (active_deps.resolve_agents_output_dir() / agent_name).resolve()
            _seed_existing_agent_into_staging(existing_agent_dir, build_dir)
        return projection, build_dir

    def _sync_build(state: BlackboardState) -> dict[str, Any]:
        projection, build_dir = _prepare_build(state)
        builder = active_deps.get_builder()
        if getattr(builder, "rag_facade", None) is None:
            out = builder.build(projection, output_dir=build_dir)
        else:
            out = run_coro_sync(builder.abuild(projection, output_dir=build_dir))
        out["validation_passed"] = False
        return out

    async def _async_build(state: BlackboardState) -> dict[str, Any]:
        projection, build_dir = _prepare_build(state)
        builder = active_deps.get_builder()
        out = await builder.abuild(projection, output_dir=build_dir)
        out["validation_passed"] = False
        return out

    def node_validate_build(state: BlackboardState) -> dict[str, Any]:
        projection = project_for_validator(state)
        return active_deps.get_validator().validate(projection)

    def node_prepare_builder_fix(state: BlackboardState) -> dict[str, Any]:
        next_attempt = int(state.get("build_fix_attempt_count", 0)) + 1
        instructions = list(state.get("active_fix_instructions") or [])
        return {
            "current_phase": "construct",
            "build_fix_attempt_count": next_attempt,
            "build_logs": [
                BuildLogEntry(
                    phase="validate",
                    actor="coordinator",
                    action="schedule_builder_fix",
                    status="completed",
                    details={
                        "attempt_number": next_attempt,
                        "fix_instructions": instructions,
                    },
                )
            ],
        }

    def node_prepare_documentarian_fix(state: BlackboardState) -> dict[str, Any]:
        instructions = list(state.get("active_fix_instructions") or [])
        feedback = "; ".join(instructions) if instructions else "Validation flagged impl_docs integrity errors."
        return {
            "current_phase": "document",
            "impl_docs_approved": False,
            "impl_doc_decision": "revise",
            "impl_doc_feedback": feedback,
            "build_logs": [
                BuildLogEntry(
                    phase="validate",
                    actor="coordinator",
                    action="route_to_documentarian",
                    status="completed",
                    details={"feedback": feedback},
                )
            ],
        }

    def node_gate_build_escalation(state: BlackboardState) -> dict[str, Any]:
        report = get_latest_validation_report(state)
        code_ptr_raw = state.get("generated_code_pointer")
        code_ptr = (
            code_ptr_raw
            if isinstance(code_ptr_raw, ContractPointer)
            else ContractPointer.model_validate(code_ptr_raw)
        )
        attempts = int(state.get("build_fix_attempt_count", 0))

        payload = interrupt(
            {
                "gate": "build_escalation",
                "build_fix_attempt_count": attempts,
                "max_build_fix_attempts": valves.loops.max_build_fix_attempts,
                "code_storage_uri": code_ptr.storage_uri,
                "fix_instructions": list(state.get("active_fix_instructions") or []),
                "issues": [iss.model_dump(mode="json") for iss in report.issues] if report else [],
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
            gate_name="build_escalation",
            round_or_revision=max(1, attempts),
            decision=decision,  # type: ignore[arg-type]
            reviewer=reviewer,
            feedback=feedback_str,
            frozen_pointer_sha256=code_ptr.payload_sha256,
        )

        if decision == "approved":
            # Human intervened manually on disk; recompute Claim Check pointer and re-run Validator.
            refreshed_ptr = build_contract_pointer_from_directory(
                contract_name=code_ptr.contract_name,
                base_dir=Path(code_ptr.storage_uri),
                contract_version=code_ptr.contract_version,
                metadata=code_ptr.metadata,
            )
            return {
                "current_phase": "validate",
                "human_escalated_decision": "approved",
                "escalation_required": False,
                "escalation_reason": None,
                "generated_code_pointer": refreshed_ptr,
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="validate",
                        actor="coordinator",
                        action="gate_build_escalation_revalidate",
                        status="completed",
                        details={"reviewer": reviewer},
                    )
                ],
            }

        if decision == "revise":
            merged_fixes = list(state.get("active_fix_instructions") or [])
            if feedback_str:
                merged_fixes.append(f"Reviewer feedback: {feedback_str}")
            return {
                "current_phase": "construct",
                "human_escalated_decision": "revise",
                "escalation_required": False,
                "escalation_reason": None,
                "build_fix_attempt_count": 0,
                "active_fix_instructions": merged_fixes,
                "gate_decisions": [gate_rec],
                "build_logs": [
                    BuildLogEntry(
                        phase="validate",
                        actor="coordinator",
                        action="gate_build_escalation_rebuild",
                        status="completed",
                        details={"feedback": feedback_str},
                    )
                ],
            }

        reason = feedback_str or (
            f"Build escalated and aborted after {attempts} validation fix attempt(s)."
        )
        return {
            "current_phase": "aborted",
            "human_escalated_decision": "abort",
            "escalation_required": True,
            "escalation_reason": reason,
            "last_error": reason,
            "gate_decisions": [gate_rec],
            "build_logs": [
                BuildLogEntry(
                    phase="validate",
                    actor="coordinator",
                    action="gate_build_escalation_abort",
                    status="failed",
                    details={"reason": reason},
                )
            ],
        }

    def _route_val(state: BlackboardState) -> str:
        return route_after_validation(state, safety_valves=valves)

    workflow = StateGraph(BlackboardState)
    workflow.add_node("build_agent", make_dual_node(_sync_build, _async_build))
    workflow.add_node("validate_build", node_validate_build)
    workflow.add_node("prepare_builder_fix", node_prepare_builder_fix)
    workflow.add_node("prepare_documentarian_fix", node_prepare_documentarian_fix)
    workflow.add_node("gate_build_escalation", node_gate_build_escalation)

    workflow.set_entry_point("build_agent")
    workflow.add_edge("build_agent", "validate_build")
    workflow.add_conditional_edges(
        "validate_build",
        _route_val,
        {
            "passed": END,
            "fix_builder": "prepare_builder_fix",
            "fix_documentarian": "prepare_documentarian_fix",
            "escalate": "gate_build_escalation",
        },
    )
    workflow.add_edge("prepare_builder_fix", "build_agent")
    workflow.add_edge("prepare_documentarian_fix", END)
    workflow.add_conditional_edges(
        "gate_build_escalation",
        route_after_build_escalation,
        {
            "revalidate": "validate_build",
            "rebuild": "build_agent",
            "abort": END,
        },
    )
    return workflow.compile()


def _export_delivery_symbols() -> tuple[Any, Any, Any]:
    from src.coordinator.delivery import DeliveryError, anode_delivery, node_delivery

    return DeliveryError, anode_delivery, node_delivery


def __getattr__(name: str) -> Any:
    if name in {"DeliveryError", "anode_delivery", "node_delivery"}:
        from src.coordinator import delivery

        return getattr(delivery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

