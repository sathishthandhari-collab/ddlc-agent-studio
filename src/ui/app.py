"""Chainlit application for DDLC Agent Studio (Day-0 Builds & Day-2 Changes)."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import chainlit as cl
import yaml

from src.coordinator.dependencies import CoordinatorDeps
from src.coordinator.graph import load_impl_docs_package_from_dir
from src.core.settings import DEFAULT_PROJECT_ROOT, get_env_settings
from src.metadata.catalog import CatalogClient
from src.metadata.db import MetadataDatabase
from src.state.blackboard import ChangeRequestContext, initial_blackboard_state
from src.state.checkpointer import (
    ThreadRecoverySnapshot,
    arecover_thread_state,
    create_memory_checkpointer,
    make_resume_command,
    open_async_checkpointer,
    open_sync_checkpointer,
    recover_thread_state,
)
from src.ui.gates import handle_gate_interrupt, render_gate_prompt, uri_to_local_path
from src.ui.handlers import (
    WorkflowTurnResult,
    build_telemetry_handler,
    format_delivery_summary,
    is_windows_proactor_loop,
    run_coordinator_turn,
)

CANONICAL_SPEC_REL_PATH = Path("tests/reference_specs/dbt_pipeline_builder_agent.yaml")

_DEFAULT_CHAT_SETTINGS: dict[str, Any] = {
    "enable_rag_retrieval": True,
    "enable_tier2_registration": True,
    "enable_rag_indexing": True,
    "enable_git_delivery": True,
    "enable_tier3_migration": True,
    "strict_delivery": True,
    "use_memory_checkpointer": False,
}


def strip_yaml_code_fences(text: str) -> str:
    """Remove surrounding ```yaml ... ``` or ``` ... ``` markdown code fences if present."""
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if not lines:
        return stripped
    if lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_yaml_spec_text(text: str) -> Optional[dict[str, Any]]:
    """Parse raw or fenced text as a DDLC intake YAML dictionary if structurally recognizable."""
    candidate = strip_yaml_code_fences(text)
    if not candidate or ":" not in candidate:
        return None
    try:
        loaded = yaml.safe_load(candidate)
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    agent_section = loaded.get("agent")
    if not isinstance(agent_section, Mapping):
        return None
    if not any(k in loaded for k in ("stages", "contracts", "purpose", "infrastructure", "governance")):
        return None
    return loaded


def discover_local_delivered_agents(agents_dir: Optional[Path] = None) -> dict[str, Path]:
    """Scan local ``agents/<name>/impl_docs`` directories containing ``agent.yaml``."""
    root = Path(agents_dir).resolve() if agents_dir is not None else (DEFAULT_PROJECT_ROOT / "agents")
    if not root.is_dir():
        return {}
    discovered: dict[str, Path] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        impl_dir = entry / "impl_docs"
        if (impl_dir / "agent.yaml").is_file():
            discovered[entry.name] = impl_dir.resolve()
    return discovered


def list_available_agents(
    *,
    catalog_client: Optional[CatalogClient] = None,
    agents_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """List known DDLC agents from Tier 2 CatalogClient and local ``agents/*/impl_docs``."""
    combined: dict[str, dict[str, Any]] = {}

    if catalog_client is not None:
        try:
            for rec in catalog_client.list_agents():
                combined[rec.agent_id] = {
                    "agent_id": rec.agent_id,
                    "version": rec.agent_version,
                    "status": rec.status,
                    "runtime": rec.runtime,
                    "pattern": rec.pattern,
                    "source": "tier2_catalog",
                    "code_repo_uri": rec.code_repo_uri,
                }
        except Exception:
            pass

    for agent_name, impl_dir in discover_local_delivered_agents(agents_dir).items():
        if agent_name not in combined:
            combined[agent_name] = {
                "agent_id": agent_name,
                "version": "local",
                "status": "local",
                "runtime": "unknown",
                "pattern": "unknown",
                "source": "workspace",
                "impl_docs_dir": str(impl_dir),
            }
        else:
            combined[agent_name]["impl_docs_dir"] = str(impl_dir)

    return [combined[k] for k in sorted(combined)]


def resolve_agent_impl_docs_dir(
    agent_name: str,
    *,
    catalog_client: Optional[CatalogClient] = None,
    agents_dir: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve an existing delivered agent's ``impl_docs/`` directory for Day-2 change mode."""
    target = str(agent_name or "").strip()
    if not target:
        return None, "Agent name cannot be empty."

    local_agents = discover_local_delivered_agents(agents_dir)
    if target in local_agents:
        return local_agents[target], None

    if catalog_client is not None:
        try:
            rec = catalog_client.get_agent(target)
            if rec is not None and rec.code_repo_uri:
                repo_path = uri_to_local_path(rec.code_repo_uri)
                if repo_path is not None:
                    candidate = (
                        repo_path
                        if repo_path.name == "impl_docs"
                        else (repo_path / "impl_docs")
                    )
                    if (candidate / "agent.yaml").is_file():
                        return candidate.resolve(), None
        except Exception as exc:
            return None, f"Failed querying Tier 2 catalog for '{target}': {exc}"

    return (
        None,
        f"No delivered agent named `{target}` with valid `impl_docs/` was found locally or in Tier 2 catalog.",
    )


def classify_user_input(
    text: str,
    *,
    active_agent_names: Sequence[str] = (),
    session_active_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Deterministically route user chat input to commands, Day-0 specs, or Day-2 change mode."""
    raw = str(text or "").strip()
    if not raw:
        return {"kind": "help"}

    lowered = raw.lower()
    if lowered in {"/help", "help", "?"}:
        return {"kind": "help"}
    if lowered in {"/catalog", "catalog"}:
        return {"kind": "catalog"}
    if lowered in {"/status", "status"}:
        return {"kind": "status"}
    if lowered in {"/canonical", "build the canonical dbt_pipeline_builder_agent"}:
        return {"kind": "canonical"}

    if lowered == "/resume" or lowered.startswith("/resume "):
        parts = raw.split(maxsplit=1)
        target_tid = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        return {"kind": "resume", "thread_id": target_tid}

    if lowered.startswith("/change"):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split(maxsplit=2)
        if len(tokens) < 2:
            return {
                "kind": "guidance",
                "message": "Usage: `/change <agent_name> <change description>`",
            }
        agent_name = tokens[1].strip()
        if len(tokens) >= 3:
            # Preserve exact prose after /change <agent_name> when shlex wasn't needed
            after_cmd = raw[len(tokens[0]) :].strip()
            if after_cmd.startswith(agent_name):
                change_text = after_cmd[len(agent_name) :].strip()
            else:
                change_text = " ".join(tokens[2:]).strip()
        else:
            change_text = ""
        return {"kind": "day2_change", "agent_name": agent_name, "request_text": change_text}

    # Check Day-0 YAML specification (raw or markdown-fenced).
    parsed = parse_yaml_spec_text(raw)
    if parsed is not None:
        cleaned_yaml = strip_yaml_code_fences(raw)
        agent_name = str((parsed.get("agent") or {}).get("name") or "").strip() or None
        return {
            "kind": "day0_spec",
            "raw_yaml": cleaned_yaml,
            "parsed": parsed,
            "agent_name": agent_name,
        }

    # Match conversational Day-2 change request referencing a known delivered agent name.
    known_sorted = sorted({str(a) for a in active_agent_names if a}, key=len, reverse=True)
    for known in known_sorted:
        if known.lower() in lowered:
            return {
                "kind": "day2_change",
                "agent_name": known,
                "request_text": raw,
            }

    if session_active_agent and not raw.startswith("/"):
        return {
            "kind": "day2_change",
            "agent_name": session_active_agent,
            "request_text": raw,
        }

    return {"kind": "guidance", "raw_text": raw}


def extract_yaml_from_elements(elements: Sequence[Any]) -> Optional[tuple[str, str]]:
    """Read an attached .yaml/.yml file element from a Chainlit message if present."""
    for elem in elements or ():
        name = str(getattr(elem, "name", "") or "")
        path_val = getattr(elem, "path", None)
        content_val = getattr(elem, "content", None)

        suffix = Path(name or str(path_val or "")).suffix.lower()
        if suffix not in {".yaml", ".yml"} and name:
            continue

        if path_val:
            file_path = Path(str(path_val))
            if file_path.is_file():
                try:
                    return (name or file_path.name, file_path.read_text(encoding="utf-8"))
                except OSError:
                    pass
        if content_val is not None:
            if isinstance(content_val, bytes):
                return (name or "spec.yaml", content_val.decode("utf-8", errors="replace"))
            return (name or "spec.yaml", str(content_val))
    return None


def load_canonical_reference_spec(workspace_root: Optional[Path] = None) -> tuple[str, dict[str, Any]]:
    """Read the canonical Day-0 ``dbt_pipeline_builder_agent.yaml`` reference spec."""
    root = Path(workspace_root).resolve() if workspace_root is not None else DEFAULT_PROJECT_ROOT
    spec_path = root / CANONICAL_SPEC_REL_PATH
    raw_yaml = spec_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_yaml) or {}
    return raw_yaml, parsed


def build_coordinator_deps_from_settings(
    settings: Optional[Mapping[str, Any]] = None,
    *,
    telemetry_handler: Optional[Any] = None,
    catalog_client: Optional[Any] = None,
) -> CoordinatorDeps:
    """Create CoordinatorDeps respecting UI toggle switches without hardcoding secrets."""
    cfg = dict(_DEFAULT_CHAT_SETTINGS)
    if settings:
        cfg.update(settings)
    return CoordinatorDeps(
        enable_rag_retrieval=bool(cfg.get("enable_rag_retrieval", True)),
        enable_tier2_registration=bool(cfg.get("enable_tier2_registration", True)),
        enable_rag_indexing=bool(cfg.get("enable_rag_indexing", True)),
        enable_git_delivery=bool(cfg.get("enable_git_delivery", True)),
        enable_tier3_migration=bool(cfg.get("enable_tier3_migration", True)),
        strict_delivery=bool(cfg.get("strict_delivery", True)),
        telemetry_handler=telemetry_handler,
        catalog_client=catalog_client,
    )


def _format_help_markdown(thread_id: str) -> str:
    return "\n".join(
        [
            "### DDLC Agent Studio",
            f"**Active Thread ID:** `{thread_id}`",
            "",
            "**Day-0 Agent Creation:**",
            "- Paste a DDLC intake YAML spec (raw or ```yaml fenced) or attach a `.yaml` file",
            "- Run `/canonical` to launch the canonical `dbt_pipeline_builder_agent` build",
            "",
            "**Day-2 Maintenance & Recovery:**",
            "- Run `/change <agent_name> <description>` (or mention an existing agent name naturally) to modify `impl_docs/` while preserving untouched custom logic",
            "- Run `/catalog` to inspect delivered agents in Tier 2 / `agents/`",
            "- Run `/status` to inspect the current thread checkpoint state",
            "- Run `/resume [thread_id]` to recover and resume a paused PostgreSQL checkpoint",
        ]
    )


def _format_catalog_markdown(agents: Sequence[Mapping[str, Any]]) -> str:
    if not agents:
        return "No delivered agents found yet in Tier 2 `agent_registry` or `agents/*/impl_docs/`."
    lines = [
        "### Registered & Delivered DDLC Agents",
        "",
        "| Agent ID | Version | Runtime | Pattern | Status | Source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in agents:
        lines.append(
            f"| `{item.get('agent_id')}` | `{item.get('version')}` | `{item.get('runtime')}` | "
            f"`{item.get('pattern')}` | `{item.get('status')}` | `{item.get('source')}` |"
        )
    lines.extend(["", "_Use `/change <agent_name> <requested change>` to modify any listed agent._"])
    return "\n".join(lines)


@contextmanager
def _open_optional_catalog_client() -> Iterator[Optional[CatalogClient]]:
    env = get_env_settings()
    uri = env.metadata_db_uri or env.rag_db_uri
    if not uri:
        yield None
        return
    db: Optional[MetadataDatabase] = None
    try:
        db = MetadataDatabase(db_uri=uri)
        yield CatalogClient(db=db)
    except Exception:
        yield None
    finally:
        if db is not None:
            db.close()


async def load_checkpoint_snapshot(
    thread_id: str,
    *,
    db_uri: Optional[str] = None,
    memory_checkpointer: Optional[Any] = None,
) -> Optional[ThreadRecoverySnapshot]:
    """Recover a Coordinator checkpoint snapshot safely across async/sync/memory backends."""
    if memory_checkpointer is not None:
        return await arecover_thread_state(memory_checkpointer, thread_id, verify_pointers=True)

    resolved_uri = db_uri if db_uri is not None else get_env_settings().checkpointer_db_uri
    if not resolved_uri:
        return None

    if is_windows_proactor_loop():
        def _sync_recover() -> Optional[ThreadRecoverySnapshot]:
            with open_sync_checkpointer(resolved_uri, verify_setup=False) as cp:
                return recover_thread_state(cp, thread_id, verify_pointers=True)

        return await asyncio.to_thread(_sync_recover)

    async with open_async_checkpointer(resolved_uri, verify_setup=False) as cp:
        return await arecover_thread_state(cp, thread_id, verify_pointers=True)


async def drive_coordinator_until_pause_or_done(
    *,
    initial_or_command: Any,
    thread_id: str,
    run_id: str,
    deps: CoordinatorDeps,
    checkpointer: Optional[Any] = None,
    compiled_graph: Optional[Any] = None,
    db_uri: Optional[str] = None,
    force_memory_checkpointer: bool = False,
    telemetry_handler: Optional[Any] = None,
    mode_label: str = "build",
    reviewer: str = "studio-user",
    is_resume: bool = False,
    resume_decision: Optional[Mapping[str, Any]] = None,
    gate_handler: Callable[..., Awaitable[Optional[dict[str, Any]]]] = handle_gate_interrupt,
    stream_to_chainlit: bool = True,
    step_sink: Optional[Callable[[Any], Awaitable[None] | None]] = None,
    notify_fn: Optional[Callable[[str], Awaitable[Any]]] = None,
    session_store: Optional[dict[str, Any]] = None,
) -> WorkflowTurnResult:
    """Run Coordinator, interactively resolve human gates cleanly, and return final turn result."""
    async def _notify(content: str) -> None:
        if notify_fn is not None:
            await notify_fn(content)
        elif stream_to_chainlit:
            await cl.Message(content=content).send()

    pending_input = initial_or_command
    current_is_resume = is_resume
    current_resume_decision = resume_decision

    while True:
        result = await run_coordinator_turn(
            initial_or_command=pending_input,
            thread_id=thread_id,
            run_id=run_id,
            deps=deps,
            checkpointer=checkpointer,
            compiled_graph=compiled_graph,
            db_uri=db_uri,
            force_memory_checkpointer=force_memory_checkpointer,
            telemetry_handler=telemetry_handler,
            stream_to_chainlit=stream_to_chainlit,
            step_sink=step_sink,
            mode_label=mode_label,
            is_resume=current_is_resume,
            resume_decision=current_resume_decision,
        )

        if result.status == "interrupted" and result.interrupt_payload is not None:
            if session_store is not None:
                session_store["awaiting_gate"] = result.interrupt_payload

            decision_payload = await gate_handler(
                result.interrupt_payload,
                reviewer=reviewer,
            )
            if decision_payload is None:
                await _notify(
                    f"Gate prompt timed out cleanly. Thread `{thread_id}` remains paused at its "
                    "checkpoint — send `/resume` whenever you are ready to continue."
                )
                return result

            if session_store is not None:
                session_store["awaiting_gate"] = None
            pending_input = make_resume_command(decision_payload)
            current_is_resume = True
            current_resume_decision = decision_payload
            continue

        if session_store is not None:
            session_store["awaiting_gate"] = None
            manifest = result.final_state.get("delivery_manifest")
            if isinstance(manifest, Mapping) and manifest.get("agent_id"):
                session_store["active_agent"] = str(manifest["agent_id"])

        if result.status in {"completed", "aborted"}:
            await _notify(result.delivery_summary or format_delivery_summary(result.final_state))
        elif result.status == "failed":
            await _notify(
                f"### Run Failed (`{thread_id}`)\n\n"
                f"**Error:** `{result.error}`\n\n"
                "_State prior to the failed step remains recoverable via `/resume`._"
            )
        return result


@cl.set_starters
async def set_starters() -> list[cl.Starter]:
    """Configure clickable Day-0 & Day-2 starter prompts on Chainlit welcome screen."""
    return [
        cl.Starter(
            label="Build Canonical dbt Pipeline Agent",
            message="/canonical",
        ),
        cl.Starter(
            label="Build New Agent from YAML",
            message="/help",
        ),
        cl.Starter(
            label="Browse Delivered Agents",
            message="/catalog",
        ),
        cl.Starter(
            label="Check Checkpoint Status",
            message="/status",
        ),
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialize session state, telemetry, checkpointer fallback, and welcome banner."""
    get_env_settings().export_provider_env_vars()
    thread_id = f"ddlc-{uuid4().hex[:12]}"
    run_id = str(uuid4())

    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("run_id", run_id)
    cl.user_session.set("user_id", "studio-user")
    cl.user_session.set("mode", "build")
    cl.user_session.set("active_agent", None)
    cl.user_session.set("awaiting_gate", None)
    cl.user_session.set("chat_settings", dict(_DEFAULT_CHAT_SETTINGS))

    has_pg = bool(get_env_settings().checkpointer_db_uri)
    cl.user_session.set("memory_checkpointer", None if has_pg else create_memory_checkpointer())

    await cl.ChatSettings(
        [
            cl.input_widget.Switch(
                id="enable_rag_retrieval",
                label="Enable Domain RAG Retrieval",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="enable_tier2_registration",
                label="Register Delivered Agents in Tier 2 Catalog",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="enable_rag_indexing",
                label="Index Approved ADRs into RAG",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="enable_git_delivery",
                label="Commit Delivered Agents to Local Git Branch",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="enable_tier3_migration",
                label="Run Tier 3 DCM Schema Migrations",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="strict_delivery",
                label="Strict Delivery Guarantees",
                initial=True,
            ),
            cl.input_widget.Switch(
                id="use_memory_checkpointer",
                label="Use Ephemeral In-Memory Checkpointer (No Postgres)",
                initial=not has_pg,
            ),
        ]
    ).send()

    await cl.Message(content=_format_help_markdown(thread_id)).send()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, Any]) -> None:
    """Persist updated Chainlit ChatSettings flags cleanly into user session."""
    merged = dict(_DEFAULT_CHAT_SETTINGS)
    merged.update(settings or {})
    cl.user_session.set("chat_settings", merged)
    if merged.get("use_memory_checkpointer") and cl.user_session.get("memory_checkpointer") is None:
        cl.user_session.set("memory_checkpointer", create_memory_checkpointer())


@cl.on_chat_resume
async def on_chat_resume(thread: dict[str, Any]) -> None:
    """Recover thread state and any pending human gate when resuming a persisted Chainlit thread."""
    get_env_settings().export_provider_env_vars()
    metadata = dict(thread.get("metadata") or {})
    thread_id = str(metadata.get("ddlc_thread_id") or thread.get("id") or f"ddlc-{uuid4().hex[:12]}")
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("user_id", str(thread.get("userIdentifier") or "studio-user"))
    cl.user_session.set("chat_settings", dict(_DEFAULT_CHAT_SETTINGS))

    snapshot = await load_checkpoint_snapshot(thread_id)
    if snapshot is None:
        cl.user_session.set("run_id", str(uuid4()))
        await cl.Message(
            content=f"Initialized resumed session for thread `{thread_id}` (no Postgres checkpoint found yet)."
        ).send()
        return

    recovered_run_id = str(snapshot.state.get("run_id") or metadata.get("run_id") or uuid4())
    cl.user_session.set("run_id", recovered_run_id)
    cl.user_session.set("mode", snapshot.state.get("mode", "build"))
    cl.user_session.set("active_agent", snapshot.state.get("target_agent_name"))

    if snapshot.pending_interrupts:
        cl.user_session.set("awaiting_gate", snapshot.pending_interrupts[0])
        await cl.Message(
            content=(
                f"Recovered paused checkpoint on thread `{thread_id}` (`run_id={recovered_run_id}`). "
                "Send `/resume` to re-open the active human approval gate."
            )
        ).send()
    else:
        await cl.Message(content=format_delivery_summary(snapshot.state)).send()


async def _resume_session_thread(
    target_thread_id: str,
    *,
    session_store: Optional[dict[str, Any]] = None,
) -> None:
    chat_settings = dict(cl.user_session.get("chat_settings") or _DEFAULT_CHAT_SETTINGS)
    mem_cp = (
        cl.user_session.get("memory_checkpointer")
        if chat_settings.get("use_memory_checkpointer") or not get_env_settings().checkpointer_db_uri
        else None
    )
    snapshot = await load_checkpoint_snapshot(target_thread_id, memory_checkpointer=mem_cp)
    if snapshot is None:
        await cl.Message(
            content=f"No recoverable checkpoint found for thread `{target_thread_id}`."
        ).send()
        return

    recovered_run_id = str(snapshot.state.get("run_id") or cl.user_session.get("run_id") or uuid4())
    mode = str(snapshot.state.get("mode") or "build")
    reviewer = str(cl.user_session.get("user_id") or "studio-user")

    cl.user_session.set("thread_id", target_thread_id)
    cl.user_session.set("run_id", recovered_run_id)
    cl.user_session.set("mode", mode)
    if snapshot.state.get("target_agent_name"):
        cl.user_session.set("active_agent", snapshot.state["target_agent_name"])

    if not snapshot.pending_interrupts:
        await cl.Message(content=format_delivery_summary(snapshot.state)).send()
        return

    pending = snapshot.pending_interrupts[0]
    decision_payload = await handle_gate_interrupt(pending, reviewer=reviewer)
    if decision_payload is None:
        await cl.Message(
            content=f"Gate prompt timed out cleanly; checkpoint on thread `{target_thread_id}` preserved."
        ).send()
        return

    telemetry = build_telemetry_handler(run_id=recovered_run_id, mode=mode, initiator_id=reviewer)
    deps = build_coordinator_deps_from_settings(chat_settings, telemetry_handler=telemetry)
    try:
        await drive_coordinator_until_pause_or_done(
            initial_or_command=make_resume_command(decision_payload),
            thread_id=target_thread_id,
            run_id=recovered_run_id,
            deps=deps,
            checkpointer=mem_cp,
            force_memory_checkpointer=bool(chat_settings.get("use_memory_checkpointer")),
            telemetry_handler=telemetry,
            mode_label=mode,
            reviewer=reviewer,
            is_resume=True,
            resume_decision=decision_payload,
            session_store=session_store,
        )
    finally:
        await deps.aclose()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Route uploaded specs, slash commands, Day-0 YAML builds, and Day-2 change requests."""
    thread_id = str(cl.user_session.get("thread_id") or f"ddlc-{uuid4().hex[:12]}")
    user_id = str(cl.user_session.get("user_id") or "studio-user")
    chat_settings = dict(cl.user_session.get("chat_settings") or _DEFAULT_CHAT_SETTINGS)
    use_mem = bool(chat_settings.get("use_memory_checkpointer") or not get_env_settings().checkpointer_db_uri)
    mem_cp = cl.user_session.get("memory_checkpointer") if use_mem else None
    if use_mem and mem_cp is None:
        mem_cp = create_memory_checkpointer()
        cl.user_session.set("memory_checkpointer", mem_cp)

    # 1. Check uploaded .yaml / .yml attachments first.
    attached_yaml = extract_yaml_from_elements(getattr(message, "elements", None) or ())
    if attached_yaml is not None:
        filename, raw_yaml = attached_yaml
        parsed = parse_yaml_spec_text(raw_yaml)
        if parsed is None:
            await cl.Message(
                content=f"Attached file `{filename}` is not a valid DDLC intake specification (missing `agent` / `stages` / `contracts`)."
            ).send()
            return
        route: dict[str, Any] = {
            "kind": "day0_spec",
            "raw_yaml": strip_yaml_code_fences(raw_yaml),
            "parsed": parsed,
            "agent_name": str((parsed.get("agent") or {}).get("name") or "").strip() or None,
        }
    else:
        with _open_optional_catalog_client() as catalog_client:
            known_agents = [a["agent_id"] for a in list_available_agents(catalog_client=catalog_client)]
        route = classify_user_input(
            message.content or "",
            active_agent_names=known_agents,
            session_active_agent=cl.user_session.get("active_agent"),
        )

    kind = route["kind"]

    if kind == "help":
        await cl.Message(content=_format_help_markdown(thread_id)).send()
        return

    if kind == "catalog":
        with _open_optional_catalog_client() as catalog_client:
            agents = list_available_agents(catalog_client=catalog_client)
        await cl.Message(content=_format_catalog_markdown(agents)).send()
        return

    if kind == "status":
        snap = await load_checkpoint_snapshot(thread_id, memory_checkpointer=mem_cp)
        if snap is None:
            await cl.Message(content=f"No active checkpoint recorded yet for thread `{thread_id}`.").send()
            return
        pending_msg = ""
        if snap.pending_interrupts:
            pending_msg = "\n\n**Pending Human Gate:**\n" + render_gate_prompt(snap.pending_interrupts[0])
        await cl.Message(
            content=(
                f"### Checkpoint Status (`{thread_id}`)\n"
                f"- **Checkpoint ID:** `{snap.checkpoint_id}`\n"
                f"- **Phase:** `{snap.state.get('current_phase', 'unknown')}`\n"
                f"- **Mode:** `{snap.state.get('mode', 'build')}`\n"
                f"- **Target Agent:** `{snap.state.get('target_agent_name') or 'n/a'}`\n"
                f"- **Pointer Integrity Verified:** `{all(snap.pointer_integrity.values()) if snap.pointer_integrity else True}`"
                f"{pending_msg}"
            )
        ).send()
        return

    if kind == "resume":
        target_tid = str(route.get("thread_id") or thread_id)
        await _resume_session_thread(target_tid)
        return

    if kind == "guidance":
        await cl.Message(
            content=route.get("message")
            or (
                "Could not classify that message as a YAML specification or a Day-2 agent change request.\n\n"
                + _format_help_markdown(thread_id)
            )
        ).send()
        return

    session_mirror: dict[str, Any] = {}

    if kind in {"canonical", "day0_spec"}:
        if kind == "canonical":
            raw_yaml, parsed = load_canonical_reference_spec()
            agent_name = str((parsed.get("agent") or {}).get("name") or "dbt_pipeline_builder_agent")
            spec_version = str((parsed.get("agent") or {}).get("version") or "1.0.0")
        else:
            raw_yaml = str(route["raw_yaml"])
            parsed = dict(route["parsed"])
            agent_name = route.get("agent_name")
            spec_version = str((parsed.get("agent") or {}).get("version") or "1.0.0")

        run_id = str(uuid4())
        cl.user_session.set("run_id", run_id)
        cl.user_session.set("mode", "build")
        cl.user_session.set("active_agent", agent_name)

        initial_state = initial_blackboard_state(
            run_id=run_id,
            thread_id=thread_id,
            mode="build",
            raw_spec_yaml=raw_yaml,
            spec_version=spec_version,
            target_agent_name=agent_name,
        )
        telemetry = build_telemetry_handler(run_id=run_id, mode="build", initiator_id=user_id)
        deps = build_coordinator_deps_from_settings(chat_settings, telemetry_handler=telemetry)
        await cl.Message(
            content=(
                f"Starting **Day-0 Build** for `{agent_name or 'new agent'}` "
                f"(`thread_id={thread_id}`, `run_id={run_id[:8]}...`)."
            )
        ).send()
        try:
            await drive_coordinator_until_pause_or_done(
                initial_or_command=initial_state,
                thread_id=thread_id,
                run_id=run_id,
                deps=deps,
                checkpointer=mem_cp,
                force_memory_checkpointer=use_mem,
                telemetry_handler=telemetry,
                mode_label="build",
                reviewer=user_id,
                session_store=session_mirror,
            )
        finally:
            await deps.aclose()
        if "active_agent" in session_mirror:
            cl.user_session.set("active_agent", session_mirror["active_agent"])
        return

    if kind == "day2_change":
        agent_name = str(route["agent_name"])
        change_text = str(route.get("request_text") or "").strip()
        if not change_text:
            await cl.Message(
                content=f"Specify what to change on `{agent_name}`, e.g. `/change {agent_name} add a validation rule for null keys`."
            ).send()
            return

        with _open_optional_catalog_client() as catalog_client:
            impl_dir, err_msg = resolve_agent_impl_docs_dir(agent_name, catalog_client=catalog_client)
        if impl_dir is None:
            await cl.Message(content=err_msg or f"Cannot locate `impl_docs/` for `{agent_name}`.").send()
            return

        baseline_pkg = load_impl_docs_package_from_dir(impl_dir)
        run_id = str(uuid4())
        cl.user_session.set("run_id", run_id)
        cl.user_session.set("mode", "change")
        cl.user_session.set("active_agent", agent_name)

        change_ctx = ChangeRequestContext(
            request_text=change_text,
            intake_source="chainlit_chat",
            agent_id=agent_name,
        )
        initial_state = initial_blackboard_state(
            run_id=run_id,
            thread_id=thread_id,
            mode="change",
            target_agent_name=agent_name,
            change_request=change_ctx,
            baseline_impl_docs=baseline_pkg,
        )
        telemetry = build_telemetry_handler(run_id=run_id, mode="change", initiator_id=user_id)
        deps = build_coordinator_deps_from_settings(
            chat_settings,
            telemetry_handler=telemetry,
        )
        await cl.Message(
            content=(
                f"Starting **Day-2 Change** on `{agent_name}` using baseline `{impl_dir}` "
                f"(`thread_id={thread_id}`, `run_id={run_id[:8]}...`)."
            )
        ).send()
        try:
            await drive_coordinator_until_pause_or_done(
                initial_or_command=initial_state,
                thread_id=thread_id,
                run_id=run_id,
                deps=deps,
                checkpointer=mem_cp,
                force_memory_checkpointer=use_mem,
                telemetry_handler=telemetry,
                mode_label="change",
                reviewer=user_id,
                session_store=session_mirror,
            )
        finally:
            await deps.aclose()
        if "active_agent" in session_mirror:
            cl.user_session.set("active_agent", session_mirror["active_agent"])
