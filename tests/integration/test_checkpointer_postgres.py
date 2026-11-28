"""Live PostgreSQL integration tests for Step 6 PostgresSaver checkpointer & recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from src.core.settings import get_env_settings
from src.state import (
    ArchitectureProposal,
    BlackboardState,
    ClarificationQuestion,
    ClarificationRound,
    ContractPointer,
    GateDecisionRecord,
    adelete_thread_checkpoints,
    aget_thread_history,
    arecover_thread_state,
    build_contract_pointer_from_directory,
    check_checkpointer_schema,
    delete_thread_checkpoints,
    ensure_checkpointer_setup,
    get_thread_history,
    initial_blackboard_state,
    make_resume_command,
    make_thread_config,
    normalize_checkpointer_uri,
    open_async_checkpointer,
    open_sync_checkpointer,
    recover_thread_state,
    run_async_selector,
)


def _live_checkpointer_uri_or_skip() -> str:
    """Resolve CHECKPOINTER_DB_URI (or fallback METADATA_DB_URI) and skip if unreachable."""
    env = get_env_settings()
    raw = env.checkpointer_db_uri or env.metadata_db_uri
    if not raw:
        pytest.skip("CHECKPOINTER_DB_URI is not configured.")
    uri = normalize_checkpointer_uri(raw)
    try:
        with psycopg.connect(uri, connect_timeout=2):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")
    return uri


def _build_interruptible_test_graph(bundle_dir: Path):
    def understand_node(state: BlackboardState) -> dict[str, Any]:
        return {
            "current_phase": "understand",
            "clarification_rounds": [
                ClarificationRound(
                    round_number=1,
                    questions=[
                        ClarificationQuestion(
                            question_id="q1",
                            category="architecture",
                            question="Use LangGraph?",
                        )
                    ],
                )
            ],
            "clarification_round_count": 1,
        }

    def design_node(state: BlackboardState) -> dict[str, Any]:
        ptr = build_contract_pointer_from_directory(
            contract_name="impl_docs_bundle",
            base_dir=bundle_dir,
        )
        return {
            "current_phase": "design",
            "impl_docs_pointer": ptr,
            "architecture_revisions": [
                ArchitectureProposal(
                    revision_number=1,
                    agent_name="dbt_pipeline_builder_agent",
                    pattern="stateful_pipeline_with_gates",
                    runtime="langgraph",
                    summary="Proposed LangGraph architecture",
                )
            ],
            "architecture_revision_count": 1,
        }

    def gate_node(state: BlackboardState) -> dict[str, Any]:
        payload = interrupt({"gate": "architecture_review", "revision": 1})
        approved = payload.get("decision") == "approved"
        return {
            "current_phase": "completed" if approved else "aborted",
            "architecture_approved": approved,
            "architecture_decision": payload.get("decision", "abort"),
            "approved_architecture_version": 1 if approved else None,
            "gate_decisions": [
                GateDecisionRecord(
                    gate_name="architecture_review",
                    round_or_revision=1,
                    decision=payload.get("decision", "abort"),
                    reviewer=payload.get("reviewer", "human"),
                )
            ],
        }

    workflow = StateGraph(BlackboardState)
    workflow.add_node("understand", understand_node)
    workflow.add_node("design", design_node)
    workflow.add_node("gate", gate_node)
    workflow.set_entry_point("understand")
    workflow.add_edge("understand", "design")
    workflow.add_edge("design", "gate")
    workflow.add_edge("gate", END)
    return workflow


class TestLivePostgresCheckpointer:
    def test_schema_readiness_and_least_privilege_setup_noop(self) -> None:
        uri = _live_checkpointer_uri_or_skip()
        status = check_checkpointer_schema(db_uri=uri)
        assert status.ready is True
        assert status.missing_tables == []
        assert status.current_version >= status.expected_version

        # Must succeed even with least-privilege ddlc_app role because tables already exist
        ensured = ensure_checkpointer_setup(db_uri=uri)
        assert ensured.ready is True

    def test_sync_postgres_checkpoint_interrupt_recover_and_stale_pointer_check(
        self, tmp_path: Path
    ) -> None:
        uri = _live_checkpointer_uri_or_skip()
        bundle_dir = tmp_path / "impl_docs"
        bundle_dir.mkdir()
        agent_yaml = bundle_dir / "agent.yaml"
        agent_yaml.write_text("agent:\n  name: dbt_pipeline_builder_agent\n", encoding="utf-8")

        thread_id = f"test-sync-{uuid4().hex[:10]}"
        cfg = make_thread_config(thread_id)
        workflow = _build_interruptible_test_graph(bundle_dir)

        try:
            # Session 1: run until interrupt() gate
            with open_sync_checkpointer(db_uri=uri, verify_setup=True) as saver1:
                app1 = workflow.compile(checkpointer=saver1)
                paused_result = app1.invoke(
                    initial_blackboard_state(
                        run_id=thread_id,
                        raw_spec_yaml="agent:\n  name: dbt_pipeline_builder_agent\n",
                    ),
                    config=cfg,
                )
                assert paused_result["current_phase"] == "design"

            # Session 2: brand new pooled PostgresSaver recovers paused thread from Postgres
            with open_sync_checkpointer(db_uri=uri) as saver2:
                recovered = recover_thread_state(saver2, thread_id, verify_pointers=True)
                assert recovered is not None
                assert recovered.resumable is True
                assert recovered.next_nodes == ["gate"]
                assert recovered.pointer_integrity == {"impl_docs_pointer": True}
                assert len(recovered.pending_interrupts) == 1
                assert recovered.pending_interrupts[0]["value"]["gate"] == "architecture_review"
                assert isinstance(recovered.state["impl_docs_pointer"], ContractPointer)
                assert isinstance(recovered.state["clarification_rounds"][0], ClarificationRound)

                # Simulate external disk tamper before resume (FM-06)
                agent_yaml.write_text("agent:\n  name: tampered\n", encoding="utf-8")
                stale = recover_thread_state(saver2, thread_id, verify_pointers=True)
                assert stale is not None
                assert stale.pointer_integrity == {"impl_docs_pointer": False}

                # Resume to completion
                app2 = workflow.compile(checkpointer=saver2)
                done = app2.invoke(
                    make_resume_command({"decision": "approved", "reviewer": "qa_reviewer"}),
                    config=cfg,
                )
                assert done["current_phase"] == "completed"
                assert done["architecture_approved"] is True

                final_snap = recover_thread_state(saver2, thread_id)
                assert final_snap is not None
                assert final_snap.resumable is False
                assert final_snap.next_nodes == []

                history = get_thread_history(saver2, thread_id)
                assert len(history) >= 4
                assert history[0].current_phase == "completed"
        finally:
            with open_sync_checkpointer(db_uri=uri) as cleanup_saver:
                delete_thread_checkpoints(cleanup_saver, thread_id)

    def test_async_postgres_checkpoint_and_recovery_with_selector_loop(
        self, tmp_path: Path
    ) -> None:
        uri = _live_checkpointer_uri_or_skip()
        bundle_dir = tmp_path / "async_impl_docs"
        bundle_dir.mkdir()
        (bundle_dir / "agent.yaml").write_text("agent:\n  name: async_demo\n", encoding="utf-8")

        thread_id = f"test-async-{uuid4().hex[:10]}"
        cfg = make_thread_config(thread_id)
        workflow = _build_interruptible_test_graph(bundle_dir)

        async def _run_async_lifecycle() -> None:
            try:
                async with open_async_checkpointer(db_uri=uri, verify_setup=True) as asaver1:
                    app1 = workflow.compile(checkpointer=asaver1)
                    await app1.ainvoke(initial_blackboard_state(run_id=thread_id), config=cfg)

                async with open_async_checkpointer(db_uri=uri) as asaver2:
                    snap = await arecover_thread_state(asaver2, thread_id, verify_pointers=True)
                    assert snap is not None
                    assert snap.resumable is True
                    assert snap.next_nodes == ["gate"]
                    assert snap.pointer_integrity == {"impl_docs_pointer": True}

                    app2 = workflow.compile(checkpointer=asaver2)
                    done = await app2.ainvoke(
                        make_resume_command({"decision": "approved", "reviewer": "async_user"}),
                        config=cfg,
                    )
                    assert done["current_phase"] == "completed"

                    hist = await aget_thread_history(asaver2, thread_id)
                    assert len(hist) >= 4
                    assert hist[0].current_phase == "completed"
            finally:
                async with open_async_checkpointer(db_uri=uri) as cleanup_saver:
                    await adelete_thread_checkpoints(cleanup_saver, thread_id)

        run_async_selector(_run_async_lifecycle())

    def test_sync_postgres_subgraph_interrupt_namespace_history_and_resume(
        self, tmp_path: Path
    ) -> None:
        uri = _live_checkpointer_uri_or_skip()
        thread_id = f"test-sub-{uuid4().hex[:10]}"
        cfg = make_thread_config(thread_id)

        def sub_gate_node(state: BlackboardState) -> dict[str, Any]:
            decision = interrupt({"gate": "subgraph_clarification", "round": 1})
            return {
                "current_phase": "design",
                "architecture_approved": decision.get("decision") == "approved",
            }

        sub_builder = StateGraph(BlackboardState)
        sub_builder.add_node("sub_gate", sub_gate_node)
        sub_builder.set_entry_point("sub_gate")
        sub_builder.add_edge("sub_gate", END)

        def finalize_node(state: BlackboardState) -> dict[str, Any]:
            return {"current_phase": "completed"}

        parent_builder = StateGraph(BlackboardState)
        parent_builder.add_node("understand_sub", sub_builder.compile())
        parent_builder.add_node("finalize", finalize_node)
        parent_builder.set_entry_point("understand_sub")
        parent_builder.add_edge("understand_sub", "finalize")
        parent_builder.add_edge("finalize", END)

        try:
            with open_sync_checkpointer(db_uri=uri, verify_setup=True) as saver1:
                app1 = parent_builder.compile(checkpointer=saver1)
                app1.invoke(initial_blackboard_state(run_id=thread_id), config=cfg)

            with open_sync_checkpointer(db_uri=uri) as saver2:
                paused = recover_thread_state(saver2, thread_id)
                assert paused is not None
                assert paused.resumable is True
                assert paused.next_nodes == ["understand_sub"]
                assert len(paused.pending_interrupts) == 1
                assert paused.pending_interrupts[0]["value"] == {
                    "gate": "subgraph_clarification",
                    "round": 1,
                }

                all_hist = get_thread_history(saver2, thread_id, checkpoint_ns=None)
                root_hist = get_thread_history(saver2, thread_id, checkpoint_ns="")
                assert any(item.checkpoint_ns.startswith("understand_sub:") for item in all_hist)
                assert all(item.checkpoint_ns == "" for item in root_hist)
                assert len(all_hist) > len(root_hist)

                app2 = parent_builder.compile(checkpointer=saver2)
                resumed = app2.invoke(
                    make_resume_command({"decision": "approved"}),
                    config=cfg,
                )
                assert resumed["current_phase"] == "completed"
                assert resumed["architecture_approved"] is True
        finally:
            with open_sync_checkpointer(db_uri=uri) as cleanup_saver:
                delete_thread_checkpoints(cleanup_saver, thread_id)
