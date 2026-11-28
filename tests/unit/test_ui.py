"""Unit tests for Chainlit UI gate handlers, Coordinator stream bridge, telemetry, and routing."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from src.coordinator.dependencies import CoordinatorDeps
from src.state.blackboard import (
    ArchitectureProposal,
    BuildLogEntry,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    ImplDocPackage,
    ImplDocRevision,
    ParsedSpecSummary,
    ResearchFindings,
    StageDesignSpec,
    ValidationReport,
    build_contract_pointer_from_directory,
    initial_blackboard_state,
)
from src.state.checkpointer import (
    arecover_thread_state,
    create_memory_checkpointer,
    make_resume_command,
)
from src.ui import app as ui_app
from src.ui import gates as ui_gates
from src.ui import handlers as ui_handlers

# ---------------------------------------------------------------------------
# Pure Gate Renderers, Parsers & URI / impl_docs Preview Tests
# ---------------------------------------------------------------------------


def test_uri_to_local_path_handles_windows_and_plain_paths() -> None:
    assert ui_gates.uri_to_local_path(None) is None
    assert ui_gates.uri_to_local_path("") is None
    win_path = ui_gates.uri_to_local_path("file:///s:/DATA/agents/demo%20agent/impl_docs")
    assert win_path is not None
    assert "demo agent" in str(win_path)
    plain = ui_gates.uri_to_local_path("agents/demo/impl_docs")
    assert plain == Path("agents/demo/impl_docs")


def test_load_impl_doc_previews_reads_and_truncates(tmp_path: Path) -> None:
    impl_dir = tmp_path / "impl_docs"
    impl_dir.mkdir(parents=True)
    (impl_dir / "agent.yaml").write_text("agent:\n  name: demo_agent\n" + "x" * 200, encoding="utf-8")
    (impl_dir / "stages.yaml").write_text("stages:\n  - id: ingest\n", encoding="utf-8")
    (impl_dir / "tier3_contract.yaml").write_text("schema_name: ddlc_demo\n", encoding="utf-8")
    (impl_dir / "extra.yaml").write_text("extra: true\n", encoding="utf-8")

    previews = ui_gates.load_impl_doc_previews(impl_dir.as_uri(), max_chars_per_file=60)
    assert list(previews.keys()) == ["agent.yaml", "stages.yaml", "tier3_contract.yaml", "extra.yaml"]
    assert "(truncated for preview)" in previews["agent.yaml"]
    assert "ddlc_demo" in previews["tier3_contract.yaml"]
    assert ui_gates.load_impl_doc_previews(str(tmp_path / "nonexistent")) == {}


def test_parse_clarification_answers_supports_all_formats() -> None:
    questions = [
        {"question_id": "q1", "field_path": "governance.pii_handling.strategy"},
        {"question_id": "q2", "field_path": "infrastructure.schedule"},
    ]

    assert ui_gates.parse_clarification_answers("defaults", questions) == {}
    assert ui_gates.parse_clarification_answers('{"q1": "mask", "q2": "daily"}', questions) == {
        "q1": "mask",
        "q2": "daily",
    }
    assert ui_gates.parse_clarification_answers("1. redact\n2. hourly", questions) == {
        "q1": "redact",
        "q2": "hourly",
    }
    assert ui_gates.parse_clarification_answers("Q1: hash\ninfrastructure.schedule: 0 2 * * *", questions) == {
        "q1": "hash",
        "q2": "0 2 * * *",
    }
    assert ui_gates.parse_clarification_answers("mask\nweekly", questions) == {
        "q1": "mask",
        "q2": "weekly",
    }
    assert ui_gates.parse_clarification_answers("hash_sha256", [questions[0]]) == {"q1": "hash_sha256"}


def test_render_gate_prompt_covers_all_five_gate_types(tmp_path: Path) -> None:
    impl_dir = tmp_path / "impl_docs"
    impl_dir.mkdir()
    (impl_dir / "agent.yaml").write_text("agent:\n  name: dbt_pipeline_builder_agent\n", encoding="utf-8")

    g1 = ui_gates.render_gate_prompt(
        {
            "gate": "clarification",
            "round_number": 1,
            "max_rounds": 3,
            "agent_name": "dbt_agent",
            "questions": [
                {
                    "question_id": "q1",
                    "category": "governance",
                    "field_path": "governance.pii_handling",
                    "question": "Mask SSN columns?",
                    "suggested_default": "hash",
                    "impact_area": "PII safety",
                }
            ],
        }
    )
    assert "Gate 1" in g1 and "Mask SSN columns?" in g1 and "`hash`" in g1

    g1b = ui_gates.render_gate_prompt(
        {
            "gate": "feasibility_escalation",
            "verdict": "not_feasible",
            "summary": "Blocked library requested",
            "policy_constraints": ["Avoid polars-legacy"],
            "risks": ["Missing connector"],
        }
    )
    assert "Gate 1b" in g1b and "Avoid polars-legacy" in g1b

    g2 = ui_gates.render_gate_prompt(
        {
            "gate": "architecture_review",
            "revision_number": 2,
            "max_revisions": 3,
            "agent_name": "dbt_agent",
            "pattern": "stateful_pipeline_with_gates",
            "runtime": "langgraph",
            "summary": "Six-stage graph",
            "proposal": {
                "stages": [{"stage_id": "s1", "stage_type": "discovery", "autonomy": "autonomous", "tools": ["sql"]}],
                "reused_components": [{"component_name": "schema_inspector_v1", "usage_type": "import"}],
                "contracts_to_define": ["SchemaInventory"],
                "tier_3_schema_required": True,
                "tier_3_tables": ["dbt_runs"],
            },
        }
    )
    assert "Gate 2" in g2 and "schema_inspector_v1" in g2 and "dbt_runs" in g2

    g3 = ui_gates.render_gate_prompt(
        {
            "gate": "impl_doc_review",
            "revision_number": 1,
            "max_revisions": 3,
            "agent_name": "dbt_pipeline_builder_agent",
            "summary": "Ready for freeze",
            "stages": ["discover", "deliver"],
            "contracts": ["SchemaInventory"],
            "has_tier3_contract": True,
            "impl_docs_sha256": "abc123def456",
            "storage_uri": impl_dir.as_uri(),
        }
    )
    assert "Gate 3" in g3 and "abc123def456" in g3 and "dbt_pipeline_builder_agent" in g3

    g4 = ui_gates.render_gate_prompt(
        {
            "gate": "build_escalation",
            "build_fix_attempt_count": 3,
            "max_build_fix_attempts": 3,
            "code_storage_uri": str(tmp_path / "code"),
            "fix_instructions": ["Fix syntax error"],
            "issues": [{"severity": "error", "check_name": "pytest", "message": "1 failed", "file_path": "tests/test_x.py"}],
        }
    )
    assert "Gate 4" in g4 and "tests/test_x.py" in g4 and "Fix syntax error" in g4


# ---------------------------------------------------------------------------
# Interactive Gate Handlers (Mocked Chainlit AskActionMessage / AskUserMessage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_gates_all_decisions_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_messages: list[str] = []
    action_queue: list[Optional[dict[str, Any]]] = []
    text_queue: list[Optional[dict[str, Any]]] = []

    class _FakeAskAction:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def send(self) -> Optional[dict[str, Any]]:
            assert self.kwargs["timeout"] == ui_gates.DEFAULT_GATE_TIMEOUT_SECONDS
            return action_queue.pop(0) if action_queue else None

    class _FakeAskUser:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def send(self) -> Optional[dict[str, Any]]:
            return text_queue.pop(0) if text_queue else None

    class _FakeMessage:
        def __init__(self, content: str = "", **_: Any) -> None:
            self.content = content

        async def send(self) -> Any:
            sent_messages.append(self.content)
            return self

    monkeypatch.setattr(ui_gates.cl, "AskActionMessage", _FakeAskAction)
    monkeypatch.setattr(ui_gates.cl, "AskUserMessage", _FakeAskUser)
    monkeypatch.setattr(ui_gates.cl, "Message", _FakeMessage)

    # Gate 1: answer questions, then timeout
    action_queue.extend([{"name": "answer_questions"}, None])
    text_queue.append({"output": "q1: hash_pii"})
    res1 = await ui_gates.handle_gate_interrupt(
        {
            "gate": "clarification",
            "questions": [{"question_id": "q1", "question": "PII strategy?"}],
        },
        reviewer="alice",
    )
    assert res1 == {"decision": "approved", "answers": {"q1": "hash_pii"}, "reviewer": "alice", "feedback": None}
    assert (
        await ui_gates.handle_gate_interrupt({"gate": "clarification", "questions": []})
    ) is None

    # Gate 1b: revise with feedback, then override
    action_queue.extend([{"name": "revise"}, {"name": "approved"}])
    text_queue.append({"output": "Use sqlglot instead"})
    assert await ui_gates.handle_gate_interrupt({"gate": "feasibility_escalation"}, reviewer="bob") == {
        "decision": "revise",
        "reviewer": "bob",
        "feedback": "Use sqlglot instead",
    }
    res1b_override = await ui_gates.handle_gate_interrupt({"gate": "feasibility_escalation"}, reviewer="bob")
    assert res1b_override is not None and res1b_override["decision"] == "approved"

    # Gate 2: escalate with feedback
    action_queue.append({"name": "escalate"})
    text_queue.append({"output": "Need clarified SLA"})
    assert await ui_gates.handle_gate_interrupt({"gate": "architecture_review"}, reviewer="carol") == {
        "decision": "escalate",
        "reviewer": "carol",
        "feedback": "Need clarified SLA",
    }

    # Gate 3: inspect YAML first, then approve
    impl_dir = tmp_path / "impl_docs"
    impl_dir.mkdir()
    (impl_dir / "agent.yaml").write_text("agent:\n  name: demo\n", encoding="utf-8")
    action_queue.extend([{"name": "inspect_yaml"}, {"name": "approved"}])
    res3 = await ui_gates.handle_gate_interrupt(
        {"gate": "impl_doc_review", "storage_uri": impl_dir.as_uri()},
        reviewer="dave",
    )
    assert res3 == {"decision": "approved", "reviewer": "dave", "feedback": None}
    assert any("agent.yaml" in msg for msg in sent_messages)

    # Gate 4: approve (re-validate) and revise
    action_queue.extend([{"name": "approved"}, {"name": "revise"}])
    text_queue.append({"output": "Fix import in src/tools.py"})
    res4_ok = await ui_gates.handle_gate_interrupt({"gate": "build_escalation"}, reviewer="erin")
    assert res4_ok is not None and res4_ok["decision"] == "approved"
    assert await ui_gates.handle_gate_interrupt({"gate": "build_escalation"}, reviewer="erin") == {
        "decision": "revise",
        "reviewer": "erin",
        "feedback": "Fix import in src/tools.py",
    }


# ---------------------------------------------------------------------------
# Stream Handlers, Telemetry Lifecycle & Proactor Bridge Tests
# ---------------------------------------------------------------------------


def test_formatters_and_interrupt_extraction() -> None:
    log_line = ui_handlers.format_build_log_entry(
        BuildLogEntry(phase="design", actor="architect", action="propose", status="completed", details={"revision": 1})
    )
    assert "[design]" in log_line and "revision=1" in log_line

    chunk = (
        ("understand:task1",),
        {
            "analyze_spec": {"current_phase": "intake", "build_logs": [{"phase": "intake", "actor": "spec_analyst", "action": "parse", "status": "completed"}]},
            "__interrupt__": (SimpleNamespace(value={"gate": "clarification", "round_number": 1}),),
        },
    )
    extracted = ui_handlers.extract_interrupt_payload(chunk)
    assert extracted == {"gate": "clarification", "round_number": 1}

    summary = ui_handlers.format_delivery_summary(
        {
            "delivery_manifest": {
                "agent_id": "dbt_agent",
                "agent_version": "1.0.0",
                "workspace_path": "/tmp/dbt_agent",
                "impl_docs_path": "/tmp/dbt_agent/impl_docs",
                "git_branch": "ddlc/agent-dbt-agent",
                "git_commit_sha": "1234567890abcdef",
                "adr_document_path": "knowledge/past_decisions/ADR-dbt.md",
                "tier2_registered": True,
                "rag_indexed": True,
            },
            "validation_reports": [
                ValidationReport(
                    attempt_number=1,
                    passed=True,
                    checks_run=["ast_parse", "pytest"],
                    test_summary={"summary": "All checks pass"},
                )
            ],
        }
    )
    assert "Delivery Complete: `dbt_agent`" in summary and "PASSED" in summary
    aborted_summary = ui_handlers.format_delivery_summary({"aborted": True, "current_phase": "aborted", "abort_reason": "User cancelled"})
    assert "User cancelled" in aborted_summary


class _StubSpecAnalyst:
    def analyze(self, proj: Any) -> dict[str, Any]:
        summary = ParsedSpecSummary(
            agent_name="ui_smoke_agent",
            agent_version="1.0.0",
            description="Test Coordinator UI",
            autonomy_level="semi_autonomous",
            stage_ids=["run"],
            contract_names=["input_c", "output_c"],
        )
        if proj.clarification_round_count >= 1 or proj.clarification_rounds:
            return {
                "current_phase": "understand",
                "spec_validation_status": "valid",
                "parsed_spec": {"agent": {"name": "ui_smoke_agent", "version": "1.0.0"}},
                "parsed_spec_summary": summary,
                "has_open_gaps": False,
                "clarification_round_count": proj.clarification_round_count,
                "build_logs": [BuildLogEntry(phase="intake", actor="spec_analyst", action="analyze", status="completed")],
            }
        q1 = ClarificationQuestion(
            question_id="q1",
            category="governance",
            field_path="governance.audit",
            question="Enable full audit logs?",
            suggested_default="yes",
        )
        return {
            "current_phase": "clarify",
            "spec_validation_status": "valid",
            "parsed_spec": {"agent": {"name": "ui_smoke_agent", "version": "1.0.0"}},
            "parsed_spec_summary": summary,
            "has_open_gaps": True,
            "clarification_round_count": 1,
            "clarification_rounds": [ClarificationRound(round_number=1, questions=[q1])],
            "build_logs": [BuildLogEntry(phase="intake", actor="spec_analyst", action="analyze", status="completed")],
        }

    def incorporate_answers(self, proj: Any, answers: Any) -> dict[str, Any]:
        ans_list = [
            a
            if isinstance(a, ClarificationAnswer)
            else ClarificationAnswer(question_id=str(a.get("question_id", "q1")), answer=str(a.get("answer", "yes")))
            for a in (answers or [])
        ]
        return {
            "current_phase": "clarify",
            "has_open_gaps": False,
            "clarification_rounds": [ClarificationRound(round_number=1, answers=ans_list, resolved=True)],
            "build_logs": [BuildLogEntry(phase="clarify", actor="spec_analyst", action="incorporate_answers", status="completed")],
        }


class _StubResearcher:
    def research(self, proj: Any) -> dict[str, Any]:
        findings = ResearchFindings(verdict="feasible", summary="Feasible with SQLGlot")
        return {
            "current_phase": "research",
            "research_findings": findings,
            "feasibility_verdict": "feasible",
            "build_logs": [BuildLogEntry(phase="research", actor="researcher", action="research", status="completed")],
        }

    async def aresearch(self, proj: Any) -> dict[str, Any]:
        return self.research(proj)


class _StubArchitect:
    def design(self, proj: Any) -> dict[str, Any]:
        return {
            "current_phase": "design",
            "architecture_revisions": [
                ArchitectureProposal(
                    revision_number=1,
                    agent_name="ui_smoke_agent",
                    agent_version="1.0.0",
                    pattern="stateful_pipeline_with_gates",
                    runtime="langgraph",
                    summary="Single-stage test graph",
                    stages=[
                        StageDesignSpec(
                            stage_id="run",
                            name="Run Transform",
                            stage_type="transform",
                            autonomy="autonomous",
                        )
                    ],
                )
            ],
            "architecture_revision_count": 1,
            "build_logs": [BuildLogEntry(phase="design", actor="architect", action="design", status="completed")],
        }

    async def adesign(self, proj: Any) -> dict[str, Any]:
        return self.design(proj)


class _StubDocumentarian:
    def author_impl_docs(self, proj: Any, *, output_dir: Optional[Path] = None) -> dict[str, Any]:
        target_dir = Path(output_dir or Path.cwd())
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "agent.yaml").write_text(
            "agent:\n  name: ui_smoke_agent\n  version: '1.0.0'\n",
            encoding="utf-8",
        )
        (target_dir / "stages.yaml").write_text("stages:\n  run: {}\n", encoding="utf-8")
        (target_dir / "contracts.yaml").write_text("contracts: {}\n", encoding="utf-8")
        (target_dir / "config.yaml").write_text("config: {}\n", encoding="utf-8")
        (target_dir / "tests.yaml").write_text("tests: {}\n", encoding="utf-8")
        pointer = build_contract_pointer_from_directory(
            contract_name="ui_smoke_agent_impl_docs",
            contract_version="1.0.0",
            base_dir=target_dir,
        )
        pkg = ImplDocPackage(
            agent_yaml={"agent": {"name": "ui_smoke_agent", "version": "1.0.0", "runtime": "langgraph"}},
            stages={"run": {"stage": {"id": "run", "name": "Run", "type": "transform", "autonomy": "autonomous"}}},
            contracts={},
            config_yaml={"config": {"defaults": {}, "environments": {}, "secrets": []}},
            tests_yaml={"tests": {"contract_tests": [], "custom_tests": []}},
            pointer=pointer,
        )
        return {
            "current_phase": "document",
            "impl_doc_revisions": [
                ImplDocRevision(
                    revision_number=1,
                    package=pkg,
                    summary="Generated impl docs",
                )
            ],
            "impl_doc_revision_count": 1,
            "impl_docs_pointer": pointer,
            "build_logs": [BuildLogEntry(phase="document", actor="documentarian", action="author_impl_docs", status="completed")],
        }


class _StubBuilder:
    def build(
        self,
        proj: Any,
        *,
        output_dir: Optional[Path] = None,
        rag_context_xml: Optional[str] = None,
    ) -> dict[str, Any]:
        out = Path(output_dir or Path.cwd())
        out.mkdir(parents=True, exist_ok=True)
        (out / "main.py").write_text("print('ok')\n", encoding="utf-8")
        code_ptr = build_contract_pointer_from_directory(
            contract_name="ui_smoke_agent_code",
            contract_version="1.0.0",
            base_dir=out,
        )
        return {
            "current_phase": "construct",
            "generated_code_pointer": code_ptr,
            "build_logs": [BuildLogEntry(phase="construct", actor="builder", action="build", status="completed")],
        }

    async def abuild(
        self,
        proj: Any,
        *,
        output_dir: Optional[Path] = None,
        rag_context_xml: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.build(proj, output_dir=output_dir, rag_context_xml=rag_context_xml)


class _StubValidator:
    def validate(self, proj: Any) -> dict[str, Any]:
        return {
            "current_phase": "validate",
            "validation_passed": True,
            "validation_reports": [
                ValidationReport(
                    attempt_number=1,
                    passed=True,
                    checks_run=["ast_parse", "pytest"],
                    test_summary={"summary": "Valid"},
                    issues=[],
                )
            ],
            "build_logs": [BuildLogEntry(phase="validate", actor="validator", action="validate", status="completed")],
        }


class _StubTelemetryHandler(BaseCallbackHandler):
    def __init__(self) -> None:
        super().__init__()
        self.enabled = True
        self.initiator_id = "alice"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_run(self, **kwargs: Any) -> None:
        self.calls.append(("start_run", kwargs))

    def complete_run(self, status: str = "completed", **kwargs: Any) -> None:
        self.calls.append(("complete_run", {"status": status, **kwargs}))

    def record_decision(self, **kwargs: Any) -> None:
        self.calls.append(("record_decision", kwargs))

    def record_audit_event(self, **kwargs: Any) -> None:
        self.calls.append(("record_audit_event", kwargs))


@pytest.mark.asyncio
async def test_drive_coordinator_multi_gate_day0_telemetry_and_timeout_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cp = create_memory_checkpointer()
    deps = CoordinatorDeps(
        spec_analyst=_StubSpecAnalyst(),
        researcher=_StubResearcher(),
        architect=_StubArchitect(),
        documentarian=_StubDocumentarian(),
        builder=_StubBuilder(),
        validator=_StubValidator(),
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        staging_root=tmp_path / "staging",
        enable_tier2_registration=False,
        enable_rag_indexing=False,
        enable_rag_retrieval=False,
        enable_git_delivery=False,
        enable_tier3_migration=False,
        strict_delivery=False,
    )
    monkeypatch.setattr(deps, "get_metadata_db", lambda: None)
    telemetry = _StubTelemetryHandler()
    init_state = initial_blackboard_state(
        run_id="run-ui-001",
        thread_id="thread-ui-001",
        mode="build",
        raw_spec_yaml="agent:\n  name: ui_smoke_agent\n",
        target_agent_name="ui_smoke_agent",
    )

    events: list[ui_handlers.NodeDisplayEvent] = []
    notifications: list[str] = []

    # 1. Simulate Gate 1 timing out cleanly without corrupting checkpoint state.
    async def _timeout_gate(payload: Any, *, reviewer: str = "alice") -> Optional[dict[str, Any]]:
        assert payload["gate"] == "clarification"
        return None

    timeout_res = await ui_app.drive_coordinator_until_pause_or_done(
        initial_or_command=init_state,
        thread_id="thread-ui-001",
        run_id="run-ui-001",
        deps=deps,
        checkpointer=cp,
        telemetry_handler=telemetry,
        reviewer="alice",
        gate_handler=_timeout_gate,
        stream_to_chainlit=False,
        step_sink=events.append,
        notify_fn=lambda msg: asyncio.sleep(0, result=notifications.append(msg)),
    )
    assert timeout_res.status == "interrupted", timeout_res.error
    assert any("paused at its checkpoint" in msg for msg in notifications)
    assert any(c == ("complete_run", {"status": "paused", "run_id": "run-ui-001"}) for c in telemetry.calls)

    # Checkpoint remains resumable!
    snap = await arecover_thread_state(cp, "thread-ui-001")
    assert snap is not None and snap.pending_interrupts
    recovered_gate = ui_handlers.unwrap_recovered_interrupt(snap.pending_interrupts[0])
    assert recovered_gate is not None and recovered_gate["gate"] == "clarification"

    # 2. Resume checkpoint from Gate 1 through Gate 2, Gate 3, and Delivery!
    seen_gates: list[str] = []

    async def _approve_all_gates(payload: Any, *, reviewer: str = "alice") -> dict[str, Any]:
        gate = payload["gate"]
        seen_gates.append(gate)
        if gate == "clarification":
            return {"decision": "approved", "answers": {"q1": "yes"}, "reviewer": reviewer}
        return {"decision": "approved", "reviewer": reviewer, "feedback": None}

    first_decision = await _approve_all_gates(recovered_gate, reviewer="alice")
    final_res = await ui_app.drive_coordinator_until_pause_or_done(
        initial_or_command=make_resume_command(first_decision),
        thread_id="thread-ui-001",
        run_id="run-ui-001",
        deps=deps,
        checkpointer=cp,
        telemetry_handler=telemetry,
        reviewer="alice",
        is_resume=True,
        resume_decision=first_decision,
        gate_handler=_approve_all_gates,
        stream_to_chainlit=False,
        step_sink=events.append,
        notify_fn=lambda msg: asyncio.sleep(0, result=notifications.append(msg)),
    )
    assert final_res.status == "completed", final_res.error
    assert seen_gates == ["clarification", "architecture_review", "impl_doc_review"]
    assert final_res.delivery_summary and "Delivery Complete: `ui_smoke_agent`" in final_res.delivery_summary
    assert any(c[0] == "record_decision" for c in telemetry.calls)
    assert any(c == ("complete_run", {"status": "completed", "run_id": "run-ui-001"}) for c in telemetry.calls)
    # Parent wrapper updates are deduplicated once inner subgraph steps ran.
    emitted_nodes = [ev.node_name for ev in events]
    assert "analyze_spec" in emitted_nodes and "understand" not in emitted_nodes


@pytest.mark.asyncio
async def test_proactor_sync_thread_bridge_executes_without_loop_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify _run_sync_stream_bridged handles sync checkpointer execution on Proactor loop."""
    inner_cp = create_memory_checkpointer()

    # Thin proxy exposing only sync methods like langgraph PostgresSaver.
    class PostgresSaver:  # noqa: N801 - intentional name match for sync checkpointer detection
        def __getattr__(self, name: str) -> Any:
            if name.startswith("a"):
                raise NotImplementedError(name)
            return getattr(inner_cp, name)

    sync_cp = PostgresSaver()
    monkeypatch.setattr(ui_handlers, "is_windows_proactor_loop", lambda: True)

    deps = CoordinatorDeps(
        spec_analyst=_StubSpecAnalyst(),
        researcher=_StubResearcher(),
        architect=_StubArchitect(),
        documentarian=_StubDocumentarian(),
        builder=_StubBuilder(),
        validator=_StubValidator(),
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        staging_root=tmp_path / "staging",
        enable_tier2_registration=False,
        enable_rag_indexing=False,
        enable_rag_retrieval=False,
        enable_git_delivery=False,
        enable_tier3_migration=False,
        strict_delivery=False,
    )
    monkeypatch.setattr(deps, "get_metadata_db", lambda: None)
    init_state = initial_blackboard_state(
        run_id="run-sync-bridge",
        thread_id="thread-sync-bridge",
        mode="build",
        raw_spec_yaml="agent:\n  name: ui_smoke_agent\n",
        target_agent_name="ui_smoke_agent",
    )

    events: list[ui_handlers.NodeDisplayEvent] = []
    res = await ui_handlers.run_coordinator_turn(
        initial_or_command=init_state,
        thread_id="thread-sync-bridge",
        run_id="run-sync-bridge",
        deps=deps,
        checkpointer=sync_cp,
        stream_to_chainlit=False,
        step_sink=events.append,
    )
    assert res.status == "interrupted", res.error
    assert res.interrupt_payload and res.interrupt_payload["gate"] == "clarification"
    assert any(ev.node_name == "analyze_spec" for ev in events)


# ---------------------------------------------------------------------------
# App Classification, Discovery, Canonical Loading & Entrypoint Tests
# ---------------------------------------------------------------------------


def test_app_routing_discovery_and_canonical_spec_helpers(tmp_path: Path) -> None:
    fenced = "```yaml\nagent:\n  name: demo_agent\nstages:\n  - id: ingest\n```"
    assert ui_app.strip_yaml_code_fences(fenced).startswith("agent:")
    parsed = ui_app.parse_yaml_spec_text(fenced)
    assert parsed is not None and parsed["agent"]["name"] == "demo_agent"
    assert ui_app.parse_yaml_spec_text("just normal prose conversation") is None

    # Local agent discovery + Day-2 resolution
    agent_impl = tmp_path / "agents" / "dbt_pipeline_builder_agent" / "impl_docs"
    agent_impl.mkdir(parents=True)
    (agent_impl / "agent.yaml").write_text("agent:\n  name: dbt_pipeline_builder_agent\n", encoding="utf-8")

    discovered = ui_app.discover_local_delivered_agents(tmp_path / "agents")
    assert "dbt_pipeline_builder_agent" in discovered
    resolved, err = ui_app.resolve_agent_impl_docs_dir("dbt_pipeline_builder_agent", agents_dir=tmp_path / "agents")
    assert err is None and resolved == agent_impl.resolve()

    fake_catalog = SimpleNamespace(
        list_agents=lambda: [
            SimpleNamespace(
                agent_id="catalog_only_agent",
                agent_version="1.2.0",
                status="active",
                runtime="langgraph",
                pattern="stateful_pipeline_with_gates",
                code_repo_uri=(tmp_path / "agents" / "catalog_only_agent").as_uri(),
            )
        ],
        get_agent=lambda aid: SimpleNamespace(
            code_repo_uri=(tmp_path / "agents" / "dbt_pipeline_builder_agent").as_uri()
        )
        if aid == "dbt_pipeline_builder_agent"
        else None,
    )
    listed = ui_app.list_available_agents(catalog_client=fake_catalog, agents_dir=tmp_path / "agents")
    assert {item["agent_id"] for item in listed} == {"catalog_only_agent", "dbt_pipeline_builder_agent"}

    # Classification routing
    assert ui_app.classify_user_input("/help")["kind"] == "help"
    assert ui_app.classify_user_input("/catalog")["kind"] == "catalog"
    assert ui_app.classify_user_input("/status")["kind"] == "status"
    assert ui_app.classify_user_input("/canonical")["kind"] == "canonical"
    assert ui_app.classify_user_input("/resume ddlc-abc") == {"kind": "resume", "thread_id": "ddlc-abc"}
    assert ui_app.classify_user_input("/resume") == {"kind": "resume", "thread_id": None}
    assert ui_app.classify_user_input("/change dbt_agent Add PII mask")["agent_name"] == "dbt_agent"
    assert ui_app.classify_user_input(fenced)["kind"] == "day0_spec"
    assert ui_app.classify_user_input(
        "Update dbt_pipeline_builder_agent to add lineage checks",
        active_agent_names=["dbt_pipeline_builder_agent"],
    ) == {
        "kind": "day2_change",
        "agent_name": "dbt_pipeline_builder_agent",
        "request_text": "Update dbt_pipeline_builder_agent to add lineage checks",
    }
    assert ui_app.classify_user_input("Random chatter")["kind"] == "guidance"

    # File upload element extraction
    spec_file = tmp_path / "my_spec.yaml"
    spec_file.write_text("agent:\n  name: file_agent\nstages: []\n", encoding="utf-8")
    extracted = ui_app.extract_yaml_from_elements([SimpleNamespace(name="my_spec.yaml", path=str(spec_file), content=None)])
    assert extracted is not None and extracted[0] == "my_spec.yaml"

    # Canonical reference spec check + module entrypoint import
    raw_canon, canon_dict = ui_app.load_canonical_reference_spec()
    assert "dbt_pipeline_builder_agent" in raw_canon
    assert canon_dict["agent"]["name"] == "dbt_pipeline_builder_agent"
    deps = ui_app.build_coordinator_deps_from_settings({"enable_rag_retrieval": False, "strict_delivery": False})
    assert deps.enable_rag_retrieval is False and deps.strict_delivery is False

    ui_main = importlib.import_module("src.ui.__main__")
    assert callable(ui_main.main)
