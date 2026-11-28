"""Coordinator streaming, checkpointer lifecycle, and Tier 1 telemetry handlers for Chainlit."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4

import chainlit as cl

from src.coordinator.dependencies import CoordinatorDeps
from src.coordinator.graph import compile_coordinator_graph
from src.core.settings import get_env_settings, get_studio_config
from src.metadata.callbacks import MetadataCallbackHandler
from src.state.checkpointer import (
    create_memory_checkpointer,
    make_thread_config,
    open_async_checkpointer,
    open_sync_checkpointer,
)

PHASE_LABELS: dict[str, str] = {
    "intake": "Phase 1 · Spec Intake",
    "clarify": "Phase 2 · Spec Clarification",
    "research": "Phase 3 · Technical Research",
    "design": "Phase 4 · Architecture Design",
    "document": "Phase 5 · Implementation Docs",
    "construct": "Phase 6 · Deterministic Build & Leaf Synthesis",
    "validate": "Phase 7 · Validation",
    "deliver": "Phase 8 · Delivery & Catalog Sync",
    "aborted": "Workflow Aborted",
}

NODE_TITLES: dict[str, str] = {
    "understand": "Phases 1–2 · Spec Analyst & Clarification",
    "intake": "Phase 1 · Spec Intake",
    "analyze_spec": "Spec Analyst · Validating Spec & Surfacing Gaps",
    "clarification_gate": "Gate 1 · Human Clarification",
    "apply_clarifications": "Spec Analyst · Applying Clarification Answers",
    "research": "Phase 3 · Technical Feasibility Research",
    "evaluate_feasibility": "Researcher · Evaluating Libraries, Standards & Catalog",
    "feasibility_gate": "Gate 1b · Feasibility Escalation",
    "design": "Phase 4 · Architecture Design",
    "propose_architecture": "Architect · Selecting Pattern, Runtime & Components",
    "architecture_gate": "Gate 2 · Architecture Review",
    "document": "Phase 5 · Implementation Documentation",
    "generate_impl_docs": "Documentarian · Writing Runtime-Agnostic impl_docs/",
    "impl_doc_gate": "Gate 3 · Implementation Docs Review & Freeze",
    "construct": "Phase 6–7 · Construction & Validation",
    "build_agent": "Builder · Deterministic Compiler & Leaf Coder",
    "validate_agent": "Validator · AST, Contract, Governance & Pytest Suite",
    "build_escalation_gate": "Gate 4 · Build & Validation Escalation",
    "delivery": "Phase 8 · Git Commit, Catalog Registration & RAG ADR",
    "abort": "Abort Handler · Finalizing Cancelled Run",
}

_WRAPPER_SUBGRAPH_NODES: frozenset[str] = frozenset(
    {"understand", "research", "design", "document", "construct"}
)

_WRAPPER_TO_PHASE: dict[str, set[str]] = {
    "understand": {"intake", "clarify"},
    "research": {"research"},
    "design": {"design"},
    "document": {"document"},
    "construct": {"construct", "validate"},
}


@dataclass(slots=True)
class NodeDisplayEvent:
    """Structured UI step payload emitted from a single Coordinator node update."""

    node_name: str
    phase: str
    title: str
    markdown: str
    namespace: tuple[str, ...] = ()


@dataclass
class WorkflowTurnResult:
    """Outcome of running or resuming the Coordinator until interrupt or terminal state."""

    thread_id: str
    run_id: str
    status: Literal["interrupted", "completed", "aborted", "failed"]
    interrupt_payload: Optional[dict[str, Any]] = None
    final_state: dict[str, Any] = field(default_factory=dict)
    delivery_summary: Optional[str] = None
    emitted_events: list[NodeDisplayEvent] = field(default_factory=list)
    error: Optional[str] = None


def _to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def is_windows_proactor_loop() -> bool:
    """Return True when the active asyncio loop is Windows ProactorEventLoop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    proactor_cls = getattr(asyncio, "ProactorEventLoop", None)
    return proactor_cls is not None and isinstance(loop, proactor_cls)


def _is_sync_only_postgres_checkpointer(checkpointer: Any) -> bool:
    cls_name = type(checkpointer).__name__
    return "PostgresSaver" in cls_name and "Async" not in cls_name


def format_build_log_entry(entry: Any) -> str:
    """Format a BuildLogEntry dict or model into a concise bullet line."""
    data = _to_dict(entry)
    if not data:
        return f"- {entry}"
    phase = data.get("phase") or "workflow"
    actor = data.get("actor") or "coordinator"
    action = data.get("action") or "step"
    status = data.get("status") or "completed"
    details = data.get("details") or {}
    detail_bits: list[str] = []
    if isinstance(details, Mapping):
        for key in ("revision", "pattern", "runtime", "passed", "error_count", "round", "mode"):
            if key in details and details[key] is not None:
                detail_bits.append(f"{key}={details[key]}")
    suffix = f" ({', '.join(detail_bits)})" if detail_bits else ""
    return f"- `[{phase}]` **{actor}** -> `{action}`: `{status}`{suffix}"


def format_node_update_markdown(node_name: str, update: Mapping[str, Any]) -> str:
    """Format a Coordinator state update dict into clean Markdown for Chainlit steps."""
    phase = str(update.get("current_phase") or "")
    phase_label = PHASE_LABELS.get(phase, phase or "Workflow Step")
    lines: list[str] = [f"**Phase:** `{phase_label}`"]

    logs = list(update.get("build_logs") or [])
    if logs:
        lines.append("")
        lines.append("**Activity:**")
        lines.extend(format_build_log_entry(item) for item in logs[-6:])

    raw_findings = update.get("research_findings")
    if raw_findings:
        latest = _to_dict(raw_findings[-1] if isinstance(raw_findings, (list, tuple)) else raw_findings)
        if latest:
            lines.extend(
                [
                    "",
                    f"**Feasibility Verdict:** `{latest.get('verdict')}`",
                    f"{latest.get('summary') or ''}".strip(),
                ]
            )

    arch_revs = list(update.get("architecture_revisions") or [])
    if arch_revs:
        latest_arch = _to_dict(arch_revs[-1])
        if latest_arch:
            lines.extend(
                [
                    "",
                    f"**Architecture Proposal Rev {latest_arch.get('revision_number', 1)}:** "
                    f"`{latest_arch.get('pattern')}` on `{latest_arch.get('runtime')}`",
                ]
            )

    impl_revs = list(update.get("impl_doc_revisions") or [])
    if impl_revs:
        latest_doc = _to_dict(impl_revs[-1])
        pkg = _to_dict(latest_doc.get("package"))
        ptr = _to_dict(pkg.get("pointer") or latest_doc.get("package_pointer"))
        sha = str(ptr.get("payload_sha256") or ptr.get("sha256") or "")[:12] or "pending"
        lines.extend(
            [
                "",
                f"**Impl Docs Rev {latest_doc.get('revision_number', 1)}:** SHA `{sha}`",
            ]
        )

    reports = list(update.get("validation_reports") or [])
    if reports:
        latest_rep = _to_dict(reports[-1])
        if latest_rep:
            passed = bool(latest_rep.get("passed"))
            test_sum = _to_dict(latest_rep.get("test_summary"))
            summary = (
                latest_rep.get("summary")
                or test_sum.get("summary")
                or (
                    f"checks={latest_rep.get('checks_run')}, issues={len(latest_rep.get('issues') or [])}"
                    if latest_rep.get("checks_run") is not None
                    else ""
                )
            )
            lines.extend(
                [
                    "",
                    f"**Validation Result:** `{'PASSED' if passed else 'FAILED'}`"
                    + (f" — {summary}" if summary else ""),
                ]
            )

    manifest = _to_dict(update.get("delivery_manifest"))
    if manifest:
        lines.extend(
            [
                "",
                f"**Delivered Agent:** `{manifest.get('agent_id')}` "
                f"(Git `{manifest.get('git_branch')}` @ `{str(manifest.get('git_commit_sha') or '')[:10]}`)",
            ]
        )

    abort_msg = update.get("abort_reason") or update.get("escalation_reason") or update.get("last_error")
    if abort_msg:
        lines.extend(["", f"**Reason / Diagnostic:** {abort_msg}"])

    return "\n".join(line for line in lines if line is not None).strip()


def format_delivery_summary(state: Mapping[str, Any]) -> str:
    """Render final completion or abort summary Markdown from BlackboardState."""
    if (
        state.get("aborted")
        or state.get("current_phase") == "aborted"
        or state.get("human_escalated_decision") == "abort"
    ):
        reason = (
            state.get("abort_reason")
            or state.get("escalation_reason")
            or state.get("last_error")
            or "Run aborted by operator or safety valve."
        )
        return "\n".join(
            [
                "### Run Stopped Cleanly",
                f"**Phase Reached:** `{state.get('current_phase', 'aborted')}`",
                f"**Reason:** {reason}",
            ]
        )

    manifest = _to_dict(state.get("delivery_manifest"))
    if not manifest:
        return "\n".join(
            [
                "### Workflow Complete",
                f"**Final Phase:** `{state.get('current_phase', 'deliver')}`",
                f"**Run ID:** `{state.get('run_id', '')}`",
            ]
        )

    lines = [
        f"### Delivery Complete: `{manifest.get('agent_id')}` (`v{manifest.get('agent_version', '1.0.0')}`)",
        "",
        "| Artifact / System | Value |",
        "| --- | --- |",
        f"| Workspace | `{manifest.get('workspace_path')}` |",
        f"| `impl_docs/` | `{manifest.get('impl_docs_path')}` |",
        f"| Git Branch & Commit | `{manifest.get('git_branch')}` @ `{str(manifest.get('git_commit_sha') or '')[:12]}` |",
        f"| ADR Document | `{manifest.get('adr_document_path') or 'n/a'}` |",
        f"| Tier 2 Catalog Registered | `{'yes' if manifest.get('tier2_registered') else 'no'}` |",
        f"| RAG ADR Indexed | `{'yes' if manifest.get('rag_indexed') else 'no'}` |",
    ]

    reports = list(state.get("validation_reports") or [])
    if reports:
        latest_rep = _to_dict(reports[-1])
        passed = bool(latest_rep.get("passed"))
        test_sum = _to_dict(latest_rep.get("test_summary"))
        summary = (
            latest_rep.get("summary")
            or test_sum.get("summary")
            or (
                f"checks={latest_rep.get('checks_run')}, issues={len(latest_rep.get('issues') or [])}"
                if latest_rep.get("checks_run") is not None
                else ""
            )
        )
        lines.extend(
            [
                "",
                f"**Final Validation:** `{'PASSED' if passed else 'FAILED'}`"
                + (f" — {summary}" if summary else ""),
            ]
        )

    lines.extend(
        [
            "",
            f"_Ready for Day-2 maintenance! Use `/change {manifest.get('agent_id')} <requested change>` "
            "or describe updates directly in chat._",
        ]
    )
    return "\n".join(lines)


def _normalize_interrupt_value(raw_interrupt: Any) -> Optional[dict[str, Any]]:
    item = raw_interrupt[0] if isinstance(raw_interrupt, (list, tuple)) and raw_interrupt else raw_interrupt
    if item is None:
        return None
    val = getattr(item, "value", item)
    if isinstance(val, Mapping):
        return dict(val)
    return {"gate": "unknown", "value": val}


def extract_interrupt_payload(chunk: Any) -> Optional[dict[str, Any]]:
    """Extract a Coordinator interrupt payload from a LangGraph stream update chunk."""
    if chunk is None:
        return None
    if isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], tuple):
        chunk = chunk[1]
    if isinstance(chunk, Mapping):
        if "__interrupt__" in chunk:
            return _normalize_interrupt_value(chunk["__interrupt__"])
        for val in chunk.values():
            found = extract_interrupt_payload(val)
            if found is not None:
                return found
    elif isinstance(chunk, (list, tuple)):
        for elem in chunk:
            found = extract_interrupt_payload(elem)
            if found is not None:
                return found
    return None


def extract_interrupt_from_snapshot(snapshot: Any) -> Optional[dict[str, Any]]:
    """Recursively extract pending interrupt payload from a LangGraph StateSnapshot."""
    if snapshot is None:
        return None
    direct_interrupts = getattr(snapshot, "interrupts", None)
    if direct_interrupts:
        return _normalize_interrupt_value(direct_interrupts)

    for task in getattr(snapshot, "tasks", None) or ():
        task_interrupts = getattr(task, "interrupts", None)
        if task_interrupts:
            return _normalize_interrupt_value(task_interrupts)
        nested_state = getattr(task, "state", None)
        if nested_state is not None:
            found = extract_interrupt_from_snapshot(nested_state)
            if found is not None:
                return found
    return None


def unwrap_recovered_interrupt(entry: Any) -> Optional[dict[str, Any]]:
    """Extract normalized gate payload from ThreadRecoverySnapshot.pending_interrupts item."""
    if entry is None:
        return None
    if isinstance(entry, Mapping):
        if "gate" in entry:
            return dict(entry)
        val = entry.get("value")
        if isinstance(val, Mapping):
            return dict(val)
        return dict(entry)
    return _normalize_interrupt_value(entry)


def build_telemetry_handler(
    *,
    run_id: str,
    mode: str = "build",
    initiator_id: str = "studio-user",
    enabled: Optional[bool] = None,
    db: Optional[Any] = None,
    db_uri: Optional[str] = None,
) -> MetadataCallbackHandler:
    """Construct a passive Tier 1 MetadataCallbackHandler for a Coordinator run."""
    telemetry_cfg = get_studio_config().telemetry
    env = get_env_settings()
    resolved_uri = db_uri if db_uri is not None else env.metadata_db_uri
    should_enable = bool(telemetry_cfg.enabled) if enabled is None else bool(enabled)
    if db is None and not resolved_uri:
        should_enable = False

    return MetadataCallbackHandler(
        run_id=run_id,
        agent_id="meta-agent",
        agent_version="0.1.0",
        operation_kind=f"meta_{mode}",
        initiator_id=initiator_id,
        initiator_type="human",
        trigger_type="manual",
        db=db,
        db_uri=resolved_uri,
        enabled=should_enable,
    )


def _record_telemetry_pre_turn(
    handler: Optional[MetadataCallbackHandler],
    *,
    run_id: str,
    thread_id: str,
    mode_label: str,
    is_resume: bool,
    resume_decision: Optional[Mapping[str, Any]],
) -> None:
    if handler is None or not handler.enabled:
        return
    try:
        if not is_resume:
            handler.start_run(
                run_id=run_id,
                operation_kind=f"meta_{mode_label}",
                run_parameters={"thread_id": thread_id, "mode": mode_label},
            )
            handler.record_audit_event(
                action="meta_run_started",
                actor_id=handler.initiator_id or "studio-user",
                actor_type="human",
                target_entity_type="agent_run",
                target_entity_id=run_id,
                outcome="success",
                run_id=run_id,
                details={"thread_id": thread_id, "mode": mode_label},
            )
            return

        if resume_decision:
            decision = str(resume_decision.get("decision") or "approved")
            reviewer = str(resume_decision.get("reviewer") or handler.initiator_id or "studio-user")
            feedback = resume_decision.get("feedback")
            rationale = str(feedback).strip() if feedback and str(feedback).strip() else (
                f"Operator selected '{decision}' at interactive Chainlit gate."
            )
            handler.record_decision(
                decision_type="human_gate_decision",
                decision_status="decided",
                decision_value=decision,
                decided_by=reviewer,
                decided_by_type="human",
                rationale=rationale,
                details={"thread_id": thread_id, "payload": dict(resume_decision)},
                run_id=run_id,
            )
            handler.record_audit_event(
                action="human_gate_resumed",
                actor_id=reviewer,
                actor_type="human",
                target_entity_type="agent_run",
                target_entity_id=run_id,
                outcome="success",
                run_id=run_id,
                details={"thread_id": thread_id, "decision": decision},
            )
        handler.complete_run("running", run_id=run_id)
    except Exception:
        pass


def _record_telemetry_post_turn(
    handler: Optional[MetadataCallbackHandler],
    *,
    run_id: str,
    status: Literal["interrupted", "completed", "aborted", "failed"],
) -> None:
    if handler is None or not handler.enabled:
        return
    mapping = {
        "interrupted": "paused",
        "completed": "completed",
        "aborted": "cancelled",
        "failed": "failed",
    }
    try:
        handler.complete_run(mapping[status], run_id=run_id)
    except Exception:
        pass


async def _emit_step_event(
    event: NodeDisplayEvent,
    *,
    stream_to_chainlit: bool,
    step_sink: Optional[Callable[[NodeDisplayEvent], Awaitable[None] | None]],
) -> None:
    if step_sink is not None:
        res = step_sink(event)
        if inspect.isawaitable(res):
            await res

    if stream_to_chainlit:
        try:
            async with cl.Step(name=event.title, type="run") as step:
                step.output = event.markdown
        except Exception:
            # Headless unit tests or non-Chainlit callers may invoke without an active UI session.
            pass


async def _consume_stream_item(
    raw_item: Any,
    *,
    emitted_events: list[NodeDisplayEvent],
    seen_inner_phases: set[str],
    stream_to_chainlit: bool,
    step_sink: Optional[Callable[[NodeDisplayEvent], Awaitable[None] | None]],
) -> Optional[dict[str, Any]]:
    interrupt_payload = extract_interrupt_payload(raw_item)

    if isinstance(raw_item, tuple) and len(raw_item) == 2 and isinstance(raw_item[0], tuple):
        namespace, payload = raw_item
    else:
        namespace, payload = (), raw_item

    if not isinstance(payload, Mapping):
        return interrupt_payload

    for node_name, update in payload.items():
        if node_name == "__interrupt__" or not isinstance(update, Mapping):
            continue

        phase = str(update.get("current_phase") or "")
        if namespace:
            if phase:
                seen_inner_phases.add(phase)
        elif node_name in _WRAPPER_SUBGRAPH_NODES:
            covered_phases = _WRAPPER_TO_PHASE.get(node_name, set())
            if covered_phases & seen_inner_phases and not update.get("abort_reason"):
                continue

        title = NODE_TITLES.get(node_name, f"Node · {node_name}")
        markdown = format_node_update_markdown(node_name, update)
        event = NodeDisplayEvent(
            node_name=str(node_name),
            phase=phase,
            title=title,
            markdown=markdown,
            namespace=namespace,
        )
        emitted_events.append(event)
        await _emit_step_event(
            event,
            stream_to_chainlit=stream_to_chainlit,
            step_sink=step_sink,
        )

    return interrupt_payload


async def _run_sync_stream_bridged(
    *,
    initial_or_command: Any,
    config: dict[str, Any],
    deps: CoordinatorDeps,
    sync_checkpointer: Optional[Any],
    db_uri: Optional[str],
    compiled_graph: Optional[Any],
    emitted_events: list[NodeDisplayEvent],
    seen_inner_phases: set[str],
    stream_to_chainlit: bool,
    step_sink: Optional[Callable[[NodeDisplayEvent], Awaitable[None] | None]],
) -> tuple[Optional[dict[str, Any]], Any]:
    """Run synchronous graph.stream() in a worker thread while streaming steps to Chainlit."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def _worker() -> None:
        try:
            if compiled_graph is not None:
                graph = compiled_graph
                for item in graph.stream(
                    initial_or_command,
                    config=config,
                    subgraphs=True,
                    stream_mode="updates",
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", item))
                snapshot = graph.get_state(config, subgraphs=True)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", snapshot))
                return

            if sync_checkpointer is not None:
                graph = compile_coordinator_graph(deps=deps, checkpointer=sync_checkpointer)
                for item in graph.stream(
                    initial_or_command,
                    config=config,
                    subgraphs=True,
                    stream_mode="updates",
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", item))
                snapshot = graph.get_state(config, subgraphs=True)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", snapshot))
                return

            with open_sync_checkpointer(db_uri, verify_setup=False) as opened_cp:
                graph = compile_coordinator_graph(deps=deps, checkpointer=opened_cp)
                for item in graph.stream(
                    initial_or_command,
                    config=config,
                    subgraphs=True,
                    stream_mode="updates",
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", item))
                snapshot = graph.get_state(config, subgraphs=True)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", snapshot))
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

    thread = threading.Thread(target=_worker, name="ddlc-coordinator-sync-stream", daemon=True)
    thread.start()

    interrupt_payload: Optional[dict[str, Any]] = None
    snapshot: Any = None
    while True:
        kind, payload = await queue.get()
        if kind == "chunk":
            found = await _consume_stream_item(
                payload,
                emitted_events=emitted_events,
                seen_inner_phases=seen_inner_phases,
                stream_to_chainlit=stream_to_chainlit,
                step_sink=step_sink,
            )
            if found is not None:
                interrupt_payload = found
        elif kind == "done":
            snapshot = payload
            break
        elif kind == "error":
            thread.join(timeout=2.0)
            raise payload

    thread.join(timeout=2.0)
    return interrupt_payload, snapshot


async def _run_async_stream(
    *,
    graph: Any,
    initial_or_command: Any,
    config: dict[str, Any],
    emitted_events: list[NodeDisplayEvent],
    seen_inner_phases: set[str],
    stream_to_chainlit: bool,
    step_sink: Optional[Callable[[NodeDisplayEvent], Awaitable[None] | None]],
) -> tuple[Optional[dict[str, Any]], Any]:
    interrupt_payload: Optional[dict[str, Any]] = None
    async for item in graph.astream(
        initial_or_command,
        config=config,
        subgraphs=True,
        stream_mode="updates",
    ):
        found = await _consume_stream_item(
            item,
            emitted_events=emitted_events,
            seen_inner_phases=seen_inner_phases,
            stream_to_chainlit=stream_to_chainlit,
            step_sink=step_sink,
        )
        if found is not None:
            interrupt_payload = found

    snapshot = await graph.aget_state(config, subgraphs=True)
    return interrupt_payload, snapshot


async def run_coordinator_turn(
    *,
    initial_or_command: Any,
    thread_id: str,
    run_id: Optional[str] = None,
    deps: Optional[CoordinatorDeps] = None,
    checkpointer: Optional[Any] = None,
    compiled_graph: Optional[Any] = None,
    db_uri: Optional[str] = None,
    force_memory_checkpointer: bool = False,
    telemetry_handler: Optional[MetadataCallbackHandler] = None,
    stream_to_chainlit: bool = True,
    step_sink: Optional[Callable[[NodeDisplayEvent], Awaitable[None] | None]] = None,
    mode_label: str = "build",
    is_resume: bool = False,
    resume_decision: Optional[Mapping[str, Any]] = None,
) -> WorkflowTurnResult:
    """Execute one Coordinator turn until human gate interrupt or terminal state."""
    resolved_run_id = str(run_id or uuid4())
    resolved_deps = deps or CoordinatorDeps()
    if telemetry_handler is not None and resolved_deps.telemetry_handler is None:
        resolved_deps.telemetry_handler = telemetry_handler

    callbacks = [telemetry_handler] if telemetry_handler is not None and telemetry_handler.enabled else []
    config = make_thread_config(
        thread_id,
        callbacks=callbacks,
        metadata={"run_id": resolved_run_id, "mode": mode_label, "source": "chainlit_ui"},
    )

    emitted_events: list[NodeDisplayEvent] = []
    seen_inner_phases: set[str] = set()
    resolved_uri = db_uri if db_uri is not None else get_env_settings().checkpointer_db_uri

    _record_telemetry_pre_turn(
        telemetry_handler,
        run_id=resolved_run_id,
        thread_id=thread_id,
        mode_label=mode_label,
        is_resume=is_resume,
        resume_decision=resume_decision,
    )

    try:
        interrupt_payload: Optional[dict[str, Any]] = None
        snapshot: Any = None

        if compiled_graph is not None:
            if is_windows_proactor_loop() and checkpointer is not None and _is_sync_only_postgres_checkpointer(checkpointer):
                interrupt_payload, snapshot = await _run_sync_stream_bridged(
                    initial_or_command=initial_or_command,
                    config=config,
                    deps=resolved_deps,
                    sync_checkpointer=checkpointer,
                    db_uri=resolved_uri,
                    compiled_graph=compiled_graph,
                    emitted_events=emitted_events,
                    seen_inner_phases=seen_inner_phases,
                    stream_to_chainlit=stream_to_chainlit,
                    step_sink=step_sink,
                )
            else:
                interrupt_payload, snapshot = await _run_async_stream(
                    graph=compiled_graph,
                    initial_or_command=initial_or_command,
                    config=config,
                    emitted_events=emitted_events,
                    seen_inner_phases=seen_inner_phases,
                    stream_to_chainlit=stream_to_chainlit,
                    step_sink=step_sink,
                )
        elif checkpointer is not None:
            if _is_sync_only_postgres_checkpointer(checkpointer):
                interrupt_payload, snapshot = await _run_sync_stream_bridged(
                    initial_or_command=initial_or_command,
                    config=config,
                    deps=resolved_deps,
                    sync_checkpointer=checkpointer,
                    db_uri=resolved_uri,
                    compiled_graph=None,
                    emitted_events=emitted_events,
                    seen_inner_phases=seen_inner_phases,
                    stream_to_chainlit=stream_to_chainlit,
                    step_sink=step_sink,
                )
            else:
                graph = compile_coordinator_graph(deps=resolved_deps, checkpointer=checkpointer)
                interrupt_payload, snapshot = await _run_async_stream(
                    graph=graph,
                    initial_or_command=initial_or_command,
                    config=config,
                    emitted_events=emitted_events,
                    seen_inner_phases=seen_inner_phases,
                    stream_to_chainlit=stream_to_chainlit,
                    step_sink=step_sink,
                )
        elif force_memory_checkpointer or not resolved_uri:
            mem_cp = create_memory_checkpointer()
            graph = compile_coordinator_graph(deps=resolved_deps, checkpointer=mem_cp)
            interrupt_payload, snapshot = await _run_async_stream(
                graph=graph,
                initial_or_command=initial_or_command,
                config=config,
                emitted_events=emitted_events,
                seen_inner_phases=seen_inner_phases,
                stream_to_chainlit=stream_to_chainlit,
                step_sink=step_sink,
            )
        elif is_windows_proactor_loop():
            interrupt_payload, snapshot = await _run_sync_stream_bridged(
                initial_or_command=initial_or_command,
                config=config,
                deps=resolved_deps,
                sync_checkpointer=None,
                db_uri=resolved_uri,
                compiled_graph=None,
                emitted_events=emitted_events,
                seen_inner_phases=seen_inner_phases,
                stream_to_chainlit=stream_to_chainlit,
                step_sink=step_sink,
            )
        else:
            async with open_async_checkpointer(resolved_uri, verify_setup=False) as async_cp:
                graph = compile_coordinator_graph(deps=resolved_deps, checkpointer=async_cp)
                interrupt_payload, snapshot = await _run_async_stream(
                    graph=graph,
                    initial_or_command=initial_or_command,
                    config=config,
                    emitted_events=emitted_events,
                    seen_inner_phases=seen_inner_phases,
                    stream_to_chainlit=stream_to_chainlit,
                    step_sink=step_sink,
                )

        if interrupt_payload is None:
            interrupt_payload = extract_interrupt_from_snapshot(snapshot)

        final_state = dict(getattr(snapshot, "values", {}) or {})
        if interrupt_payload is not None:
            _record_telemetry_post_turn(telemetry_handler, run_id=resolved_run_id, status="interrupted")
            return WorkflowTurnResult(
                thread_id=thread_id,
                run_id=resolved_run_id,
                status="interrupted",
                interrupt_payload=interrupt_payload,
                final_state=final_state,
                emitted_events=emitted_events,
            )

        is_aborted = bool(
            final_state.get("aborted")
            or final_state.get("current_phase") == "aborted"
            or final_state.get("human_escalated_decision") == "abort"
        )
        outcome_status: Literal["aborted", "completed"] = "aborted" if is_aborted else "completed"
        _record_telemetry_post_turn(telemetry_handler, run_id=resolved_run_id, status=outcome_status)
        return WorkflowTurnResult(
            thread_id=thread_id,
            run_id=resolved_run_id,
            status=outcome_status,
            final_state=final_state,
            delivery_summary=format_delivery_summary(final_state),
            emitted_events=emitted_events,
        )
    except Exception as exc:
        _record_telemetry_post_turn(telemetry_handler, run_id=resolved_run_id, status="failed")
        return WorkflowTurnResult(
            thread_id=thread_id,
            run_id=resolved_run_id,
            status="failed",
            emitted_events=emitted_events,
            error=f"{type(exc).__name__}: {exc}",
        )
