"""Phase 8 Deterministic Delivery Node — Two-Phase Commit, Tier 2/3 registration, RAG ADR & Git."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.core.settings import get_studio_config
from src.metadata.db import (
    qualified_table,
    sanitize_payload,
    sanitize_text,
    tier1_table,
    tier2_table,
)
from src.rag.chunking.base import compute_sha256
from src.rag.chunking.markdown_chunker import MarkdownChunker
from src.rag.store.models import DocumentChunkDraft
from src.rag.store.pgvector_store import format_vector_literal
from src.specialists.documentarian.specialist import write_impl_docs_package
from src.state.blackboard import (
    ArchitectureProposal,
    BlackboardState,
    BuildLogEntry,
    ChangeRequestContext,
    ContractPointer,
    GateDecisionRecord,
    ImplDocPackage,
    ImplDocRevision,
    MetaDeliveryManifest,
    ParsedSpecSummary,
    ResearchFindings,
    assert_contract_pointer_valid,
    build_contract_pointer_from_directory,
    get_approved_architecture,
    get_approved_impl_docs,
    get_latest_architecture,
)

_SAFE_BRANCH_RE = re.compile(r"[^a-zA-Z0-9_.\-/]+")


class DeliveryError(RuntimeError):
    """Fail-fast exception raised when Phase 8 Delivery encounters transactional or Git failure."""


@dataclass(frozen=True)
class GitDeliveryResult:
    """Result returned by local or custom Git delivery publisher."""

    branch_name: str
    commit_sha: str
    repository: str = ""
    pull_request_url: Optional[str] = None


def _coerce_run_uuid(raw_run_id: str) -> UUID:
    try:
        return UUID(str(raw_run_id))
    except (ValueError, TypeError):
        return uuid5(NAMESPACE_DNS, f"ddlc-run:{raw_run_id}")


def _coerce_model[T](value: Any, cls: type[T]) -> T:
    if isinstance(value, cls):
        return value
    return cls.model_validate(value)  # type: ignore[attr-defined]


def _coerce_optional_pointer(raw: Any) -> Optional[ContractPointer]:
    if raw is None:
        return None
    if isinstance(raw, ContractPointer):
        return raw
    return ContractPointer.model_validate(raw)


def _list_non_git_relative_files(base_dir: Path) -> list[str]:
    """Return sorted relative POSIX file paths under base_dir excluding .git internals."""
    resolved = base_dir.resolve()
    files: list[str] = []
    for item in resolved.rglob("*"):
        if not item.is_file():
            continue
        rel_parts = item.relative_to(resolved).parts
        if ".git" in rel_parts or "__pycache__" in rel_parts:
            continue
        files.append(item.relative_to(resolved).as_posix())
    return sorted(files)


def _sync_directory_preserving_git(source_dir: Path, target_dir: Path) -> None:
    """Replace agent workspace files cleanly from validated staging while preserving .git/."""
    src_resolved = source_dir.resolve()
    dst_resolved = target_dir.resolve()
    if src_resolved == dst_resolved:
        return

    dst_resolved.mkdir(parents=True, exist_ok=True)
    for child in list(dst_resolved.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    for item in src_resolved.iterdir():
        if item.name in {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}:
            continue
        dest = dst_resolved / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                dest,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"),
            )
        else:
            shutil.copy2(item, dest)


def _run_git(cmd: Sequence[str], *, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise DeliveryError(f"Git command {' '.join(cmd)!r} failed in {cwd}: {err}")
    return proc.stdout.strip()


def publish_agent_to_local_git(
    *,
    agent_dir: Path,
    branch_name: str,
    commit_message: str,
) -> GitDeliveryResult:
    """Initialize or update an isolated agent Git repository inside agent_dir and commit changes."""
    resolved = agent_dir.resolve()
    if not (resolved / ".git").exists():
        _run_git(["init"], cwd=resolved)

    # Verify git operates exclusively inside the isolated agent workspace, never the parent repo.
    toplevel = Path(_run_git(["rev-parse", "--show-toplevel"], cwd=resolved)).resolve()
    if toplevel != resolved:
        raise DeliveryError(
            f"Git safety guard aborted: resolved toplevel {toplevel} != agent workspace {resolved}."
        )

    user_name = subprocess.run(
        ["git", "config", "user.name"],
        cwd=str(resolved),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not user_name:
        _run_git(["config", "--local", "user.name", "DDLC Meta-Agent"], cwd=resolved)
        _run_git(["config", "--local", "user.email", "meta-agent@ddlc.local"], cwd=resolved)

    safe_branch = _SAFE_BRANCH_RE.sub("-", branch_name).strip("-") or "feat/ddlc-build"
    has_head = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(resolved),
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )

    if has_head:
        branch_exists = (
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{safe_branch}"],
                cwd=str(resolved),
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )
        if branch_exists:
            _run_git(["checkout", safe_branch], cwd=resolved)
        else:
            _run_git(["checkout", "-b", safe_branch], cwd=resolved)
    else:
        _run_git(["checkout", "-B", safe_branch], cwd=resolved)

    _run_git(["add", "-A"], cwd=resolved)
    status_out = _run_git(["status", "--porcelain"], cwd=resolved)
    if status_out or not has_head:
        allow_empty = ["--allow-empty"] if not status_out else []
        _run_git(["commit", *allow_empty, "-m", commit_message], cwd=resolved)

    commit_sha = _run_git(["rev-parse", "HEAD"], cwd=resolved)
    return GitDeliveryResult(
        branch_name=safe_branch,
        commit_sha=commit_sha,
        repository=str(resolved),
    )


def synthesize_adr_markdown(
    state: Mapping[str, Any],
    package: ImplDocPackage,
    arch: Optional[ArchitectureProposal],
) -> tuple[str, str, str]:
    """Deterministically synthesize a past_decisions ADR markdown record (rag.md §9.1 Step 1)."""
    agent_sec = package.agent_yaml.get("agent") or {}
    arch_sec = package.agent_yaml.get("architecture") or {}

    agent_name = str(agent_sec.get("name") or state.get("target_agent_name") or "generated_agent")
    display_name = str(agent_sec.get("display_name") or agent_name)
    agent_version = str(agent_sec.get("version") or "1.0.0")
    description = str(agent_sec.get("description") or "").strip()
    pattern = str(arch.pattern if arch else arch_sec.get("pattern") or "stateful_pipeline_with_gates")
    runtime = str(arch.runtime if arch else arch_sec.get("runtime") or "langgraph")
    autonomy = str(arch.autonomy_level if arch else arch_sec.get("autonomy_level") or "semi_autonomous")
    mode = str(state.get("mode") or "build")
    run_id = str(state.get("run_id") or "")

    clar_rounds = int(state.get("clarification_round_count", 0))
    arch_revs = int(state.get("architecture_revision_count", 0))
    impl_revs = int(state.get("impl_doc_revision_count", 0))
    fix_attempts = int(state.get("build_fix_attempt_count", 0))

    raw_summary = state.get("parsed_spec_summary")
    spec_summary = _coerce_model(raw_summary, ParsedSpecSummary) if raw_summary is not None else None
    raw_research = state.get("research_findings")
    research = _coerce_model(raw_research, ResearchFindings) if raw_research is not None else None
    raw_change = state.get("change_request")
    change_req = _coerce_model(raw_change, ChangeRequestContext) if raw_change is not None else None

    gate_lines: list[str] = []
    for raw_gate in state.get("gate_decisions") or []:
        gate = _coerce_model(raw_gate, GateDecisionRecord)
        fb = f" — {gate.feedback}" if gate.feedback else ""
        gate_lines.append(
            f"- **{gate.gate_name}** (rev {gate.round_or_revision}): `{gate.decision}` by `{gate.reviewer}`{fb}"
        )

    stage_lines = [
        f"- `{sid}` ({(stg.get('stage') or {}).get('type', 'custom')} / "
        f"{(stg.get('processing') or {}).get('processing_type', 'deterministic_tool')})"
        for sid, stg in sorted(package.stages.items())
    ]
    contract_lines = [
        f"- `{cname}` (`{(cbody.get('contract') or {}).get('direction', 'internal')}`)"
        for cname, cbody in sorted(package.contracts.items())
    ]

    risks = list(arch.risks_and_tradeoffs) if arch else []
    alternatives = list(arch.alternatives_considered) if arch else []
    constraints = list(research.policy_constraints) if research else []

    title = f"ADR: {display_name} ({agent_name} v{agent_version})"
    ver_slug = agent_version.replace(".", "_")
    if mode == "change" and run_id:
        filename = f"ADR-{agent_name}-v{ver_slug}-{run_id[:8]}.md"
    else:
        filename = f"ADR-{agent_name}-v{ver_slug}.md"

    lines: list[str] = [
        f"# {title}",
        "",
        f"- **Agent Name**: `{agent_name}`",
        f"- **Version**: `{agent_version}`",
        f"- **Workflow Mode**: `{mode}`",
        f"- **Pattern**: `{pattern}`",
        f"- **Runtime**: `{runtime}`",
        f"- **Autonomy Level**: `{autonomy}`",
        f"- **Operational Metrics**: clarification_rounds={clar_rounds}, "
        f"architecture_revisions={arch_revs}, impl_doc_revisions={impl_revs}, build_fix_attempts={fix_attempts}",
        "",
        "## Context",
        "",
        description or (spec_summary.description if spec_summary else "DDLC agent build."),
    ]
    if change_req is not None:
        lines.extend(
            [
                "",
                "### Day-2 Change Request",
                "",
                change_req.request_text,
            ]
        )

    lines.extend(
        [
            "",
            "## Decision",
            "",
            arch.summary if arch else f"Selected `{pattern}` pattern on `{runtime}` runtime.",
            "",
            "### Stages",
            *stage_lines,
            "",
            "### Contracts",
            *contract_lines,
        ]
    )

    if risks or alternatives or constraints:
        lines.extend(["", "## Trade-offs, Alternatives & Constraints", ""])
        for item in risks:
            lines.append(f"- Risk/Trade-off: {item}")
        for item in alternatives:
            lines.append(f"- Rejected Alternative: {item}")
        for item in constraints:
            lines.append(f"- Policy Constraint: {item}")

    if gate_lines:
        lines.extend(["", "## Human Gate Rationale", "", *gate_lines])

    return title, filename, "\n".join(lines).rstrip() + "\n"


def _extract_cross_agent_links(
    state: Mapping[str, Any],
    package: ImplDocPackage,
) -> tuple[list[tuple[str, Optional[str]]], list[tuple[str, Optional[str]]]]:
    """Extract (peer_agent_id, contract_name) tuples for upstream dependencies & downstream consumers."""
    upstream: list[tuple[str, Optional[str]]] = []
    downstream: list[tuple[str, Optional[str]]] = []

    parsed_spec = state.get("parsed_spec") or {}
    cross = parsed_spec.get("cross_agent") or {}
    for dep in cross.get("upstream_dependencies") or []:
        if isinstance(dep, Mapping) and dep.get("agent"):
            upstream.append((str(dep["agent"]), dep.get("contract")))
    for dep in cross.get("downstream_consumers") or []:
        if isinstance(dep, Mapping) and dep.get("agent"):
            downstream.append((str(dep["agent"]), dep.get("contract")))

    if not upstream and not downstream:
        raw_sum = state.get("parsed_spec_summary")
        if raw_sum is not None:
            summary = _coerce_model(raw_sum, ParsedSpecSummary)
            for agent_ref in summary.upstream_dependencies:
                upstream.append((agent_ref, None))
            for agent_ref in summary.downstream_consumers:
                downstream.append((agent_ref, None))

    # Also inspect agent.yaml metadata.writes_to if present
    _ = package
    return upstream, downstream


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


def _execute_atomic_postgres_commit(
    *,
    db: Any,
    state: Mapping[str, Any],
    package: ImplDocPackage,
    arch: Optional[ArchitectureProposal],
    agent_id: str,
    agent_display_name: str,
    agent_version: str,
    description: str,
    pattern: str,
    runtime: str,
    owner: str,
    source_repo: str,
    adr_rel_path: str,
    adr_title: str,
    adr_sha256: str,
    adr_chunks: Sequence[DocumentChunkDraft],
    adr_embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    embedding_dimension: int,
    new_component_embeddings: Sequence[tuple[dict[str, Any], list[float]]],
    enable_tier2: bool,
    enable_rag: bool,
) -> tuple[bool, bool]:
    """Step 4: Single all-or-nothing PostgreSQL transaction covering Tier 2 + rag_knowledge."""
    run_uuid = _coerce_run_uuid(str(state.get("run_id") or uuid4()))
    now = datetime.now(timezone.utc)
    rag_schema = get_studio_config().rag.database.schema_name

    with db.transaction() as conn:
        if enable_tier2:
            # Ensure Tier 1 parent run row exists when graph executed without MetadataCallbackHandler
            run_hdr_tbl = tier1_table("agent_run_header")
            conn.execute(
                f"""
                INSERT INTO {run_hdr_tbl} (
                    run_id, agent_id, agent_version, operation_kind,
                    initiator_id, initiator_type, trigger_type, status
                )
                VALUES (
                    %(run_id)s, 'meta-agent', '0.1.0', %(operation_kind)s,
                    'coordinator', 'agent', 'manual', 'running'
                )
                ON CONFLICT (run_id) DO NOTHING
                """,
                {
                    "run_id": run_uuid,
                    "operation_kind": "maintenance" if state.get("mode") == "change" else "build",
                },
            )

            # 1. Upsert agent_registry
            agent_tbl = tier2_table("agent_registry")
            conn.execute(
                f"""
                INSERT INTO {agent_tbl} (
                    agent_id, agent_name, agent_version, description,
                    architecture_pattern, runtime, status, owner,
                    source_repo, config_snapshot
                )
                VALUES (
                    %(agent_id)s, %(agent_name)s, %(agent_version)s, %(description)s,
                    %(architecture_pattern)s, %(runtime)s, 'active', %(owner)s,
                    %(source_repo)s, %(config_snapshot)s
                )
                ON CONFLICT (agent_id) DO UPDATE
                SET agent_name = EXCLUDED.agent_name,
                    agent_version = EXCLUDED.agent_version,
                    description = EXCLUDED.description,
                    architecture_pattern = EXCLUDED.architecture_pattern,
                    runtime = EXCLUDED.runtime,
                    status = 'active',
                    owner = EXCLUDED.owner,
                    source_repo = EXCLUDED.source_repo,
                    config_snapshot = EXCLUDED.config_snapshot,
                    updated_at = clock_timestamp()
                """,
                {
                    "agent_id": agent_id[:100],
                    "agent_name": agent_display_name[:200],
                    "agent_version": agent_version[:50],
                    "description": sanitize_text(description),
                    "architecture_pattern": pattern[:50],
                    "runtime": runtime[:50],
                    "owner": owner[:100],
                    "source_repo": sanitize_text(source_repo),
                    "config_snapshot": Jsonb(sanitize_payload(package.config_yaml)),
                },
            )

            # 2. Upsert contract_registry for every contract in impl_docs/contracts
            contract_tbl = tier2_table("contract_registry")
            contract_ids: dict[str, UUID] = {}
            for cname, raw_ctr in sorted(package.contracts.items()):
                cbody = raw_ctr.get("contract") if isinstance(raw_ctr.get("contract"), Mapping) else raw_ctr
                c_ver = str(cbody.get("version") or "1.0.0")[:50]
                direction = str(cbody.get("direction") or "internal")
                if direction not in {"input", "output", "internal"}:
                    direction = "internal"
                row = conn.execute(
                    f"""
                    INSERT INTO {contract_tbl} (
                        contract_id, agent_id, contract_name, contract_version,
                        direction, schema_definition, status
                    )
                    VALUES (
                        %(contract_id)s, %(agent_id)s, %(contract_name)s, %(contract_version)s,
                        %(direction)s, %(schema_definition)s, 'active'
                    )
                    ON CONFLICT (agent_id, contract_name, contract_version) DO UPDATE
                    SET direction = EXCLUDED.direction,
                        schema_definition = EXCLUDED.schema_definition,
                        status = 'active',
                        updated_at = clock_timestamp()
                    RETURNING contract_id
                    """,
                    {
                        "contract_id": uuid4(),
                        "agent_id": agent_id[:100],
                        "contract_name": cname[:200],
                        "contract_version": c_ver,
                        "direction": direction,
                        "schema_definition": Jsonb(sanitize_payload(dict(raw_ctr))),
                    },
                ).fetchone()
                if row is not None:
                    cid_val = row["contract_id"] if isinstance(row, Mapping) else row[0]
                    contract_ids[cname] = cid_val if isinstance(cid_val, UUID) else UUID(str(cid_val))

            # 3. Register newly created shared components & record component_usage
            comp_tbl = tier2_table("component_registry")
            usage_tbl = tier2_table("component_usage")
            for comp_spec, comp_vec in new_component_embeddings:
                comp_name = str(comp_spec.get("component_name") or comp_spec.get("name") or "").strip()
                if not comp_name:
                    continue
                comp_ver = str(comp_spec.get("component_version") or comp_spec.get("version") or "1.0.0")[:50]
                comp_type = str(comp_spec.get("component_type") or "utility")
                if comp_type not in {
                    "validator",
                    "transformer",
                    "connector",
                    "masker",
                    "quality_rule",
                    "utility",
                    "template",
                }:
                    comp_type = "utility"
                vec_lit = format_vector_literal(comp_vec, expected_dim=embedding_dimension) if comp_vec else None
                conn.execute(
                    f"""
                    INSERT INTO {comp_tbl} (
                        component_id, component_name, component_version, description,
                        component_type, interface_spec, storage_path, embedding, status
                    )
                    VALUES (
                        %(component_id)s, %(component_name)s, %(component_version)s, %(description)s,
                        %(component_type)s, %(interface_spec)s, %(storage_path)s, %(embedding)s::vector, 'active'
                    )
                    ON CONFLICT (component_name, component_version) DO UPDATE
                    SET description = EXCLUDED.description,
                        component_type = EXCLUDED.component_type,
                        interface_spec = EXCLUDED.interface_spec,
                        storage_path = EXCLUDED.storage_path,
                        embedding = COALESCE(EXCLUDED.embedding, {comp_tbl}.embedding),
                        status = 'active',
                        updated_at = clock_timestamp()
                    """,
                    {
                        "component_id": uuid4(),
                        "component_name": comp_name[:200],
                        "component_version": comp_ver,
                        "description": sanitize_text(str(comp_spec.get("description") or comp_name)),
                        "component_type": comp_type,
                        "interface_spec": Jsonb(sanitize_payload(comp_spec.get("interface_spec") or {})),
                        "storage_path": str(comp_spec.get("storage_path") or f"_shared/{comp_name}.py"),
                        "embedding": vec_lit,
                    },
                )

            shared_deps = (package.agent_yaml.get("dependencies") or {}).get("shared_components") or []
            for sc in shared_deps:
                if not isinstance(sc, Mapping) or not sc.get("component_id"):
                    continue
                comp_name = str(sc["component_id"])
                comp_row = conn.execute(
                    f"""
                    SELECT component_id, component_version
                    FROM {comp_tbl}
                    WHERE component_name = %(component_name)s
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 1
                    """,
                    {"component_name": comp_name},
                ).fetchone()
                if comp_row is not None:
                    comp_id = comp_row["component_id"] if isinstance(comp_row, Mapping) else comp_row[0]
                    comp_ver = (
                        comp_row["component_version"] if isinstance(comp_row, Mapping) else comp_row[1]
                    )
                    usage_type = str(sc.get("usage_type") or "import")
                    if usage_type not in {"import", "extend", "wrap"}:
                        usage_type = "import"
                    conn.execute(
                        f"""
                        INSERT INTO {usage_tbl} (
                            usage_id, agent_id, component_id, component_version, usage_type
                        )
                        VALUES (%(usage_id)s, %(agent_id)s, %(component_id)s, %(component_version)s, %(usage_type)s)
                        ON CONFLICT (agent_id, component_id) DO UPDATE
                        SET component_version = EXCLUDED.component_version,
                            usage_type = EXCLUDED.usage_type,
                            last_used_at = clock_timestamp()
                        """,
                        {
                            "usage_id": uuid4(),
                            "agent_id": agent_id[:100],
                            "component_id": comp_id,
                            "component_version": str(sc.get("version") or comp_ver)[:50],
                            "usage_type": usage_type,
                        },
                    )

            # 4. Cross-agent dependencies & contract_lineage (when peer agent is registered)
            dep_tbl = tier2_table("agent_dependency")
            lin_tbl = tier2_table("contract_lineage")
            upstream, downstream = _extract_cross_agent_links(state, package)
            for peer_links, peer_is_upstream in ((upstream, True), (downstream, False)):
                for peer_id, ctr_name in peer_links:
                    if peer_id == agent_id:
                        continue
                    peer_exists = conn.execute(
                        f"SELECT 1 FROM {agent_tbl} WHERE agent_id = %(agent_id)s",
                        {"agent_id": peer_id},
                    ).fetchone()
                    if peer_exists is None:
                        continue
                    local_cid = contract_ids.get(ctr_name or "")
                    upstream_id = peer_id[:100] if peer_is_upstream else agent_id[:100]
                    downstream_id = agent_id[:100] if peer_is_upstream else peer_id[:100]
                    conn.execute(
                        f"""
                        INSERT INTO {dep_tbl} (
                            dependency_id, upstream_agent_id, downstream_agent_id,
                            dependency_type, contract_id, status
                        )
                        VALUES (%(dependency_id)s, %(upstream)s, %(downstream)s, 'data', %(contract_id)s, 'active')
                        ON CONFLICT (upstream_agent_id, downstream_agent_id, dependency_type) DO UPDATE
                        SET contract_id = EXCLUDED.contract_id, status = 'active'
                        """,
                        {
                            "dependency_id": uuid4(),
                            "upstream": upstream_id,
                            "downstream": downstream_id,
                            "contract_id": local_cid,
                        },
                    )
                    if local_cid is not None and ctr_name:
                        peer_row = conn.execute(
                            f"""
                            SELECT contract_id FROM {contract_tbl}
                            WHERE agent_id = %(agent_id)s AND contract_name = %(cname)s
                            ORDER BY updated_at DESC LIMIT 1
                            """,
                            {"agent_id": peer_id, "cname": ctr_name},
                        ).fetchone()
                        if peer_row is not None:
                            peer_cid = (
                                peer_row["contract_id"]
                                if isinstance(peer_row, Mapping)
                                else peer_row[0]
                            )
                            if peer_cid != local_cid:
                                prod_a, prod_c, cons_a, cons_c = (
                                    (peer_id[:100], peer_cid, agent_id[:100], local_cid)
                                    if peer_is_upstream
                                    else (agent_id[:100], local_cid, peer_id[:100], peer_cid)
                                )
                                conn.execute(
                                    f"""
                                    INSERT INTO {lin_tbl} (
                                        lineage_id, producer_agent_id, producer_contract_id,
                                        consumer_agent_id, consumer_contract_id, status
                                    )
                                    VALUES (%(lineage_id)s, %(prod_a)s, %(prod_c)s, %(cons_a)s, %(cons_c)s, 'active')
                                    ON CONFLICT (producer_contract_id, consumer_contract_id) DO UPDATE
                                    SET status = 'active'
                                    """,
                                    {
                                        "lineage_id": uuid4(),
                                        "prod_a": prod_a,
                                        "prod_c": prod_c,
                                        "cons_a": cons_a,
                                        "cons_c": cons_c,
                                    },
                                )

            # 5. Upsert build_summary
            build_tbl = tier2_table("build_summary")
            conn.execute(
                f"""
                INSERT INTO {build_tbl} (
                    build_id, run_id, agent_id, spec_version,
                    clarification_rounds, architecture_revisions,
                    impl_doc_revisions, build_fix_attempts,
                    outcome, completed_at
                )
                VALUES (
                    %(build_id)s, %(run_id)s, %(agent_id)s, %(spec_version)s,
                    %(clar_rounds)s, %(arch_revs)s,
                    %(impl_revs)s, %(fix_attempts)s,
                    'success', %(completed_at)s
                )
                ON CONFLICT (run_id) DO UPDATE
                SET agent_id = EXCLUDED.agent_id,
                    spec_version = EXCLUDED.spec_version,
                    clarification_rounds = EXCLUDED.clarification_rounds,
                    architecture_revisions = EXCLUDED.architecture_revisions,
                    impl_doc_revisions = EXCLUDED.impl_doc_revisions,
                    build_fix_attempts = EXCLUDED.build_fix_attempts,
                    outcome = 'success',
                    completed_at = EXCLUDED.completed_at
                """,
                {
                    "build_id": uuid4(),
                    "run_id": run_uuid,
                    "agent_id": agent_id[:100],
                    "spec_version": str(state.get("spec_version") or agent_version)[:50],
                    "clar_rounds": int(state.get("clarification_round_count", 0)),
                    "arch_revs": int(state.get("architecture_revision_count", 0)),
                    "impl_revs": int(state.get("impl_doc_revision_count", 0)),
                    "fix_attempts": int(state.get("build_fix_attempt_count", 0)),
                    "completed_at": now,
                },
            )

            # 6. Record frozen gate decisions idempotently in stage_gate_record
            gate_tbl = tier2_table("stage_gate_record")
            conn.execute(
                f"DELETE FROM {gate_tbl} WHERE run_id = %(run_id)s",
                {"run_id": run_uuid},
            )
            for raw_gate in state.get("gate_decisions") or []:
                gate = _coerce_model(raw_gate, GateDecisionRecord)
                gate_status = "passed" if gate.decision == "approved" else "failed"
                gate_type = "automated_check" if gate.reviewer == "auto_policy" else "human_approval"
                conn.execute(
                    f"""
                    INSERT INTO {gate_tbl} (
                        gate_id, run_id, agent_id, stage_id,
                        gate_type, gate_status, checked_by,
                        payload_sha256, notes, evaluated_at
                    )
                    VALUES (
                        %(gate_id)s, %(run_id)s, %(agent_id)s, %(stage_id)s,
                        %(gate_type)s, %(gate_status)s, %(checked_by)s,
                        %(payload_sha256)s, %(notes)s, %(evaluated_at)s
                    )
                    """,
                    {
                        "gate_id": uuid4(),
                        "run_id": run_uuid,
                        "agent_id": agent_id[:100],
                        "stage_id": gate.gate_name[:100],
                        "gate_type": gate_type,
                        "gate_status": gate_status,
                        "checked_by": gate.reviewer[:100],
                        "payload_sha256": gate.frozen_pointer_sha256,
                        "notes": sanitize_text(gate.feedback),
                        "evaluated_at": gate.decided_at,
                    },
                )

        if enable_rag:
            docs_tbl = qualified_table(rag_schema, "knowledge_documents")
            chunks_tbl = qualified_table(rag_schema, "knowledge_chunks")
            doc_meta_json = json.dumps(
                {
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "pattern": pattern,
                    "runtime": runtime,
                    "generated_by": "phase_8_delivery",
                }
            )
            doc_row = conn.execute(
                f"""
                INSERT INTO {docs_tbl} (
                    domain, source_path, title, file_sha256,
                    embedding_model, embedding_dimension, chunk_count, metadata
                )
                VALUES (
                    'past_decisions', %(source_path)s, %(title)s, %(file_sha256)s,
                    %(embedding_model)s, %(embedding_dimension)s, %(chunk_count)s, %(metadata)s::jsonb
                )
                ON CONFLICT (domain, source_path) DO UPDATE
                SET title = EXCLUDED.title,
                    file_sha256 = EXCLUDED.file_sha256,
                    embedding_model = EXCLUDED.embedding_model,
                    embedding_dimension = EXCLUDED.embedding_dimension,
                    chunk_count = EXCLUDED.chunk_count,
                    metadata = EXCLUDED.metadata,
                    updated_at = clock_timestamp()
                RETURNING document_id
                """,
                {
                    "source_path": adr_rel_path[:512],
                    "title": adr_title[:256],
                    "file_sha256": adr_sha256,
                    "embedding_model": embedding_model[:128],
                    "embedding_dimension": embedding_dimension,
                    "chunk_count": len(adr_chunks),
                    "metadata": doc_meta_json,
                },
            ).fetchone()
            if doc_row is None:
                raise DeliveryError("Failed to upsert ADR row in rag_knowledge.knowledge_documents.")

            doc_id = doc_row["document_id"] if isinstance(doc_row, Mapping) else doc_row[0]
            conn.execute(
                f"DELETE FROM {chunks_tbl} WHERE document_id = %(document_id)s",
                {"document_id": doc_id},
            )
            if adr_chunks:
                with conn.cursor() as cur:
                    cur.executemany(
                        f"""
                        INSERT INTO {chunks_tbl} (
                            chunk_id, document_id, domain, chunk_index, chunk_sha256,
                            breadcrumb, content, embedding, token_count, metadata
                        )
                        VALUES (
                            %s, %s, 'past_decisions', %s, %s,
                            %s, %s, %s::text::vector, %s, %s::jsonb
                        )
                        """,
                        [
                            (
                                uuid4(),
                                doc_id,
                                chunk.chunk_index,
                                chunk.chunk_sha256,
                                chunk.breadcrumb,
                                chunk.content,
                                format_vector_literal(emb, expected_dim=embedding_dimension),
                                chunk.token_count,
                                json.dumps(chunk.metadata),
                            )
                            for chunk, emb in zip(adr_chunks, adr_embeddings, strict=True)
                        ],
                    )

    return enable_tier2, enable_rag


@dataclass(frozen=True)
class _DeliveryContext:
    """Validated inputs, ADR document, chunks, and target storage configuration for Delivery."""

    active_deps: CoordinatorDeps
    valves: Any
    package: ImplDocPackage
    arch: Optional[ArchitectureProposal]
    code_ptr: ContractPointer
    agent_name: str
    agent_display_name: str
    agent_version: str
    description: str
    owner: str
    pattern: str
    runtime: str
    final_agent_dir: Path
    final_impl_dir: Path
    adr_title: str
    adr_filename: str
    adr_markdown: str
    adr_rel_path: str
    adr_sha256: str
    adr_disk_path: Path
    adr_chunks: list[DocumentChunkDraft]
    embedding_model: str
    embedding_dim: int
    new_comp_specs: list[dict[str, Any]]
    tier2_uses_mock: bool
    rag_uses_mock: bool
    db: Any
    needs_db_rag: bool
    needs_db_tier2: bool


def _prepare_delivery_context(
    state: BlackboardState,
    deps: Optional[CoordinatorDeps],
) -> _DeliveryContext:
    """Validate preconditions, verify Claim Checks, and run Steps 1-2 (ADR + Chunking)."""
    active_deps = deps or CoordinatorDeps()
    valves = active_deps.get_safety_valves()

    if not state.get("impl_docs_approved"):
        raise DeliveryError("Phase 8 Delivery blocked: impl_docs_approved is False.")
    if not state.get("validation_passed"):
        raise DeliveryError("Phase 8 Delivery blocked: validation_passed is False.")

    approved_rev: Optional[ImplDocRevision] = get_approved_impl_docs(state)
    if approved_rev is None:
        raise DeliveryError("Phase 8 Delivery blocked: no approved ImplDocRevision on Blackboard.")
    package = approved_rev.package

    code_ptr = _coerce_optional_pointer(state.get("generated_code_pointer"))
    if code_ptr is None:
        raise DeliveryError("Phase 8 Delivery blocked: generated_code_pointer is missing.")
    try:
        assert_contract_pointer_valid(code_ptr)
        impl_ptr = _coerce_optional_pointer(state.get("impl_docs_pointer"))
        if impl_ptr is not None:
            assert_contract_pointer_valid(impl_ptr)
        tier3_ptr = _coerce_optional_pointer(state.get("tier3_contract_pointer"))
        if tier3_ptr is not None:
            assert_contract_pointer_valid(tier3_ptr)
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Claim Check integrity check failed: {exc}") from exc

    agent_sec = package.agent_yaml.get("agent") or {}
    arch_sec = package.agent_yaml.get("architecture") or {}
    agent_name = str(agent_sec.get("name") or state.get("target_agent_name") or "generated_agent")
    agent_display_name = str(agent_sec.get("display_name") or agent_name)
    agent_version = str(agent_sec.get("version") or "1.0.0")
    description = str(agent_sec.get("description") or "")
    owner = str(agent_sec.get("owner") or active_deps.default_owner)
    pattern = str(arch_sec.get("pattern") or "stateful_pipeline_with_gates")
    runtime = str(arch_sec.get("runtime") or "langgraph")

    arch = get_approved_architecture(state) or get_latest_architecture(state)
    final_agent_dir = (active_deps.resolve_agents_output_dir() / agent_name).resolve()
    final_impl_dir = (final_agent_dir / "impl_docs").resolve()

    # Step 1: Synthesize ADR Markdown Document
    adr_title, adr_filename, adr_markdown = synthesize_adr_markdown(state, package, arch)
    adr_rel_path = f"past_decisions/{adr_filename}"
    adr_sha256 = compute_sha256(adr_markdown)
    adr_disk_path = (active_deps.resolve_knowledge_dir() / "past_decisions" / adr_filename).resolve()

    # Step 2: Structure-Aware Chunking
    adr_chunks = MarkdownChunker().chunk(
        adr_markdown,
        title=adr_title,
        source_path=adr_rel_path,
    )

    studio_emb = active_deps.get_studio_config().rag.embedding
    embedding_model = studio_emb.model
    embedding_dim = int(studio_emb.dimensions)
    new_comp_specs = list(arch.new_components_to_create) if arch else []

    tier2_uses_mock = active_deps.catalog_client is not None and active_deps.metadata_db is None
    rag_uses_mock = active_deps.rag_store is not None and active_deps.metadata_db is None
    need_db_for_tier2 = active_deps.enable_tier2_registration and not tier2_uses_mock
    need_db_for_rag = active_deps.enable_rag_indexing and not rag_uses_mock
    db = (
        active_deps.metadata_db
        if active_deps.metadata_db is not None
        else (active_deps.get_metadata_db() if (need_db_for_tier2 or need_db_for_rag) else None)
    )

    needs_db_rag = active_deps.enable_rag_indexing and (
        db is not None or active_deps.rag_store is not None
    )
    needs_db_tier2 = active_deps.enable_tier2_registration and (
        db is not None or active_deps.catalog_client is not None
    )

    if active_deps.strict_delivery and active_deps.enable_tier2_registration and not needs_db_tier2:
        raise DeliveryError(
            "Tier 2 registration enabled but neither metadata_db/METADATA_DB_URI nor catalog_client is configured."
        )
    if active_deps.strict_delivery and active_deps.enable_rag_indexing and not needs_db_rag:
        raise DeliveryError(
            "RAG ADR indexing enabled but neither metadata_db/RAG_DB_URI nor rag_store is configured."
        )

    return _DeliveryContext(
        active_deps=active_deps,
        valves=valves,
        package=package,
        arch=arch,
        code_ptr=code_ptr,
        agent_name=agent_name,
        agent_display_name=agent_display_name,
        agent_version=agent_version,
        description=description,
        owner=owner,
        pattern=pattern,
        runtime=runtime,
        final_agent_dir=final_agent_dir,
        final_impl_dir=final_impl_dir,
        adr_title=adr_title,
        adr_filename=adr_filename,
        adr_markdown=adr_markdown,
        adr_rel_path=adr_rel_path,
        adr_sha256=adr_sha256,
        adr_disk_path=adr_disk_path,
        adr_chunks=adr_chunks,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        new_comp_specs=new_comp_specs,
        tier2_uses_mock=tier2_uses_mock,
        rag_uses_mock=rag_uses_mock,
        db=db,
        needs_db_rag=needs_db_rag,
        needs_db_tier2=needs_db_tier2,
    )


def _record_tier2_via_mock_clients(
    *,
    ctx: _DeliveryContext,
    state: Mapping[str, Any],
    new_component_embeddings: Sequence[tuple[dict[str, Any], list[float]]],
) -> bool:
    """Register Tier 2 records synchronously through injected mock Catalog/Lineage clients."""
    deps = ctx.active_deps
    if not (ctx.needs_db_tier2 and deps.catalog_client is not None):
        return False

    run_uuid = _coerce_run_uuid(str(state.get("run_id") or uuid4()))
    deps.catalog_client.register_agent(
        agent_id=ctx.agent_name,
        agent_name=ctx.agent_display_name,
        agent_version=ctx.agent_version,
        description=ctx.description,
        architecture_pattern=ctx.pattern,
        runtime=ctx.runtime,
        status="active",
        owner=ctx.owner,
        source_repo=str(ctx.final_agent_dir),
        config_snapshot=ctx.package.config_yaml,
    )
    contract_ids: dict[str, Any] = {}
    for cname, raw_ctr in sorted(ctx.package.contracts.items()):
        cbody = raw_ctr.get("contract") if isinstance(raw_ctr.get("contract"), Mapping) else raw_ctr
        direction = str(cbody.get("direction") or "internal")
        if direction not in {"input", "output", "internal"}:
            direction = "internal"
        rec = deps.catalog_client.register_contract(
            agent_id=ctx.agent_name,
            contract_name=cname,
            contract_version=str(cbody.get("version") or "1.0.0"),
            direction=direction,
            schema_definition=dict(raw_ctr),
            status="active",
        )
        contract_ids[cname] = getattr(rec, "contract_id", None)

    for comp_spec, comp_vec in new_component_embeddings:
        comp_name = str(comp_spec.get("component_name") or comp_spec.get("name") or "").strip()
        if comp_name:
            deps.catalog_client.register_component(
                component_name=comp_name,
                component_version=str(comp_spec.get("component_version") or "1.0.0"),
                description=str(comp_spec.get("description") or comp_name),
                component_type=str(comp_spec.get("component_type") or "utility"),
                storage_path=str(comp_spec.get("storage_path") or f"_shared/{comp_name}.py"),
                interface_spec=comp_spec.get("interface_spec") or {},
                embedding=comp_vec or None,
            )

    if deps.lineage_client is not None:
        upstream, downstream = _extract_cross_agent_links(state, ctx.package)
        for peer_id, ctr_name in upstream:
            if peer_id != ctx.agent_name and deps.catalog_client.get_agent(peer_id) is not None:
                deps.lineage_client.record_agent_dependency(
                    upstream_agent_id=peer_id,
                    downstream_agent_id=ctx.agent_name,
                    dependency_type="data",
                    contract_id=contract_ids.get(ctr_name or ""),
                )
        for peer_id, ctr_name in downstream:
            if peer_id != ctx.agent_name and deps.catalog_client.get_agent(peer_id) is not None:
                deps.lineage_client.record_agent_dependency(
                    upstream_agent_id=ctx.agent_name,
                    downstream_agent_id=peer_id,
                    dependency_type="data",
                    contract_id=contract_ids.get(ctr_name or ""),
                )

    deps.catalog_client.record_build_summary(
        run_id=run_uuid,
        agent_id=ctx.agent_name,
        spec_version=str(state.get("spec_version") or ctx.agent_version),
        outcome="success",
        clarification_rounds=int(state.get("clarification_round_count", 0)),
        architecture_revisions=int(state.get("architecture_revision_count", 0)),
        impl_doc_revisions=int(state.get("impl_doc_revision_count", 0)),
        build_fix_attempts=int(state.get("build_fix_attempt_count", 0)),
    )
    return True


def _embed_batch_sync_first(embedder: Any, texts: list[str]) -> list[list[float]]:
    sync_fn = getattr(embedder, "embed_texts_sync", None)
    if callable(sync_fn):
        return list(sync_fn(texts))
    result = embedder.embed_texts(texts)
    if inspect.isawaitable(result):
        return list(run_coro_sync(result))
    return list(result)


def _compute_step3_embeddings_sync(
    ctx: _DeliveryContext,
) -> tuple[list[list[float]], str, int, list[tuple[dict[str, Any], list[float]]]]:
    """Run Phase 8 Step 3 outbound embeddings synchronously when supported."""
    adr_embeddings: list[list[float]] = []
    embedding_model = ctx.embedding_model
    embedding_dim = ctx.embedding_dim
    try:
        if ctx.needs_db_rag and ctx.adr_chunks:
            embedder = ctx.active_deps.get_embedder()
            embedding_model = getattr(embedder, "model_name", embedding_model)
            embedding_dim = int(getattr(embedder, "dimensions", embedding_dim))
            batch_size = max(1, int(ctx.active_deps.get_studio_config().rag.embedding.batch_size))
            chunk_texts = [c.content for c in ctx.adr_chunks]
            for start in range(0, len(chunk_texts), batch_size):
                batch = chunk_texts[start : start + batch_size]
                adr_embeddings.extend(_embed_batch_sync_first(embedder, batch))
            if len(adr_embeddings) != len(ctx.adr_chunks):
                raise DeliveryError(
                    f"Embedding count mismatch for ADR: {len(adr_embeddings)} vs {len(ctx.adr_chunks)} chunks."
                )

        if ctx.needs_db_tier2 and ctx.new_comp_specs:
            comp_texts = [
                str(c.get("description") or c.get("component_name") or c.get("name") or "")
                for c in ctx.new_comp_specs
            ]
            if any(comp_texts) and (ctx.active_deps.embedder is not None or ctx.needs_db_rag):
                embedder = ctx.active_deps.get_embedder()
                vecs = _embed_batch_sync_first(
                    embedder, [t or "shared component" for t in comp_texts]
                )
                new_comp_embeddings = list(zip(ctx.new_comp_specs, vecs, strict=True))
            else:
                new_comp_embeddings = [(c, []) for c in ctx.new_comp_specs]
        else:
            new_comp_embeddings = [(c, []) for c in ctx.new_comp_specs]
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Step 3 (outbound embeddings) failed: {exc}") from exc

    return adr_embeddings, embedding_model, embedding_dim, new_comp_embeddings


async def _compute_step3_embeddings_async(
    ctx: _DeliveryContext,
) -> tuple[list[list[float]], str, int, list[tuple[dict[str, Any], list[float]]]]:
    """Run Phase 8 Step 3 outbound embeddings asynchronously."""
    adr_embeddings: list[list[float]] = []
    embedding_model = ctx.embedding_model
    embedding_dim = ctx.embedding_dim
    try:
        if ctx.needs_db_rag and ctx.adr_chunks:
            embedder = ctx.active_deps.get_embedder()
            embedding_model = getattr(embedder, "model_name", embedding_model)
            embedding_dim = int(getattr(embedder, "dimensions", embedding_dim))
            batch_size = max(1, int(ctx.active_deps.get_studio_config().rag.embedding.batch_size))
            chunk_texts = [c.content for c in ctx.adr_chunks]
            for start in range(0, len(chunk_texts), batch_size):
                batch = chunk_texts[start : start + batch_size]
                adr_embeddings.extend(await _maybe_await(embedder.embed_texts(batch)))
            if len(adr_embeddings) != len(ctx.adr_chunks):
                raise DeliveryError(
                    f"Embedding count mismatch for ADR: {len(adr_embeddings)} vs {len(ctx.adr_chunks)} chunks."
                )

        if ctx.needs_db_tier2 and ctx.new_comp_specs:
            comp_texts = [
                str(c.get("description") or c.get("component_name") or c.get("name") or "")
                for c in ctx.new_comp_specs
            ]
            if any(comp_texts) and (ctx.active_deps.embedder is not None or ctx.needs_db_rag):
                embedder = ctx.active_deps.get_embedder()
                vecs = await _maybe_await(
                    embedder.embed_texts([t or "shared component" for t in comp_texts])
                )
                new_comp_embeddings = list(zip(ctx.new_comp_specs, vecs, strict=True))
            else:
                new_comp_embeddings = [(c, []) for c in ctx.new_comp_specs]
        else:
            new_comp_embeddings = [(c, []) for c in ctx.new_comp_specs]
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Step 3 (outbound embeddings) failed: {exc}") from exc

    return adr_embeddings, embedding_model, embedding_dim, new_comp_embeddings


def _rag_upsert_payload(
    *,
    ctx: _DeliveryContext,
    adr_embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    embedding_dimension: int,
) -> dict[str, Any]:
    return {
        "domain": "past_decisions",
        "source_path": ctx.adr_rel_path,
        "title": ctx.adr_title,
        "file_sha256": ctx.adr_sha256,
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
        "chunks": ctx.adr_chunks,
        "embeddings": adr_embeddings,
        "metadata": {"agent_id": ctx.agent_name, "agent_version": ctx.agent_version},
    }


def _run_postgres_commit_for_ctx(
    *,
    ctx: _DeliveryContext,
    state: BlackboardState,
    adr_embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    embedding_dimension: int,
    new_comp_embeddings: Sequence[tuple[dict[str, Any], list[float]]],
    enable_tier2: Optional[bool] = None,
    enable_rag: Optional[bool] = None,
) -> tuple[bool, bool]:
    return _execute_atomic_postgres_commit(
        db=ctx.db,
        state=state,
        package=ctx.package,
        arch=ctx.arch,
        agent_id=ctx.agent_name,
        agent_display_name=ctx.agent_display_name,
        agent_version=ctx.agent_version,
        description=ctx.description,
        pattern=ctx.pattern,
        runtime=ctx.runtime,
        owner=ctx.owner,
        source_repo=str(ctx.final_agent_dir),
        adr_rel_path=ctx.adr_rel_path,
        adr_title=ctx.adr_title,
        adr_sha256=ctx.adr_sha256,
        adr_chunks=ctx.adr_chunks,
        adr_embeddings=adr_embeddings,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        new_component_embeddings=new_comp_embeddings,
        enable_tier2=ctx.needs_db_tier2 if enable_tier2 is None else enable_tier2,
        enable_rag=ctx.needs_db_rag if enable_rag is None else enable_rag,
    )


def _execute_step4_sync(
    *,
    ctx: _DeliveryContext,
    state: BlackboardState,
    adr_embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    embedding_dimension: int,
    new_comp_embeddings: Sequence[tuple[dict[str, Any], list[float]]],
) -> tuple[bool, bool]:
    tier2_registered = False
    rag_indexed = False
    db_tier2 = ctx.needs_db_tier2 and not ctx.tier2_uses_mock
    db_rag = ctx.needs_db_rag and not ctx.rag_uses_mock
    try:
        if ctx.db is not None and (db_tier2 or db_rag):
            t2_ok, r_ok = _run_postgres_commit_for_ctx(
                ctx=ctx,
                state=state,
                adr_embeddings=adr_embeddings,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
                new_comp_embeddings=new_comp_embeddings,
                enable_tier2=db_tier2,
                enable_rag=db_rag,
            )
            tier2_registered = tier2_registered or t2_ok
            rag_indexed = rag_indexed or r_ok
        if ctx.tier2_uses_mock and ctx.needs_db_tier2:
            tier2_registered = _record_tier2_via_mock_clients(
                ctx=ctx,
                state=state,
                new_component_embeddings=new_comp_embeddings,
            )
        rag_store = ctx.active_deps.rag_store
        if ctx.rag_uses_mock and ctx.needs_db_rag and rag_store is not None:
            kwargs = _rag_upsert_payload(
                ctx=ctx,
                adr_embeddings=adr_embeddings,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            sync_fn = getattr(rag_store, "upsert_document_and_chunks_sync", None)
            if callable(sync_fn):
                sync_fn(**kwargs)
            else:
                res = rag_store.upsert_document_and_chunks(**kwargs)
                if inspect.isawaitable(res):
                    run_coro_sync(res)
            rag_indexed = True
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Step 4 (atomic database transaction) failed: {exc}") from exc
    return tier2_registered, rag_indexed


async def _execute_step4_async(
    *,
    ctx: _DeliveryContext,
    state: BlackboardState,
    adr_embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    embedding_dimension: int,
    new_comp_embeddings: Sequence[tuple[dict[str, Any], list[float]]],
) -> tuple[bool, bool]:
    tier2_registered = False
    rag_indexed = False
    db_tier2 = ctx.needs_db_tier2 and not ctx.tier2_uses_mock
    db_rag = ctx.needs_db_rag and not ctx.rag_uses_mock
    try:
        if ctx.db is not None and (db_tier2 or db_rag):
            t2_ok, r_ok = await asyncio.to_thread(
                _run_postgres_commit_for_ctx,
                ctx=ctx,
                state=state,
                adr_embeddings=adr_embeddings,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
                new_comp_embeddings=new_comp_embeddings,
                enable_tier2=db_tier2,
                enable_rag=db_rag,
            )
            tier2_registered = tier2_registered or t2_ok
            rag_indexed = rag_indexed or r_ok
        if ctx.tier2_uses_mock and ctx.needs_db_tier2:
            tier2_registered = _record_tier2_via_mock_clients(
                ctx=ctx,
                state=state,
                new_component_embeddings=new_comp_embeddings,
            )
        rag_store = ctx.active_deps.rag_store
        if ctx.rag_uses_mock and ctx.needs_db_rag and rag_store is not None:
            kwargs = _rag_upsert_payload(
                ctx=ctx,
                adr_embeddings=adr_embeddings,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            await _maybe_await(rag_store.upsert_document_and_chunks(**kwargs))
            rag_indexed = True
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Step 4 (atomic database transaction) failed: {exc}") from exc
    return tier2_registered, rag_indexed


def _materialize_and_publish_step5(
    *,
    ctx: _DeliveryContext,
    state: BlackboardState,
    tier2_registered: bool,
    rag_indexed: bool,
) -> dict[str, Any]:
    """Execute Phase 8 Step 5 (Tier 3 migration, workspace sync, ADR write, Git commit)."""
    try:
        staging_build_dir = Path(ctx.code_ptr.storage_uri).resolve()
        _sync_directory_preserving_git(staging_build_dir, ctx.final_agent_dir)

        t3_path = ctx.final_impl_dir / f"tier_3_{ctx.agent_name}.yaml"
        write_impl_docs_package(
            ctx.package,
            ctx.final_impl_dir,
            tier3_contract_path=t3_path if ctx.package.tier3_contract_yaml is not None else None,
        )

        if ctx.package.tier3_contract_yaml is not None and ctx.active_deps.enable_tier3_migration:
            t3_client = ctx.active_deps.get_tier3_client()
            if t3_client is not None:
                t3_client.apply_migration(
                    t3_path,
                    message=f"Provision Tier 3 schema for {ctx.agent_name} v{ctx.agent_version}",
                    agent_name=ctx.agent_name,
                    approve_destructive=not ctx.valves.gates.require_destructive_schema_approval,
                )

        ctx.adr_disk_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.adr_disk_path.write_text(ctx.adr_markdown, encoding="utf-8")

        non_git_files = _list_non_git_relative_files(ctx.final_agent_dir)
        final_code_pointer = build_contract_pointer_from_directory(
            contract_name=f"{ctx.agent_name}_codebase",
            base_dir=ctx.final_agent_dir,
            files=non_git_files,
            contract_version=ctx.agent_version,
            metadata=dict(ctx.code_ptr.metadata),
        )

        git_branch: Optional[str] = None
        git_commit_sha: Optional[str] = None
        branch = (
            f"feat/{ctx.agent_name}-v{ctx.agent_version}"
            if state.get("mode") == "build"
            else f"feat/{ctx.agent_name}-{str(state.get('run_id') or '')[:8]}"
        )
        commit_msg = (
            f"feat({ctx.agent_name}): deliver {ctx.agent_name} v{ctx.agent_version} "
            f"[run:{state.get('run_id')}]"
        )
        if ctx.active_deps.git_publisher is not None:
            pub_res = ctx.active_deps.git_publisher(
                agent_dir=ctx.final_agent_dir,
                branch_name=branch,
                commit_message=commit_msg,
                state=state,
            )
            if isinstance(pub_res, GitDeliveryResult):
                git_branch = pub_res.branch_name
                git_commit_sha = pub_res.commit_sha
            elif isinstance(pub_res, Mapping):
                git_branch = str(pub_res.get("branch_name") or branch)
                git_commit_sha = str(pub_res.get("commit_sha") or "") or None
        elif ctx.active_deps.enable_git_delivery:
            git_res = publish_agent_to_local_git(
                agent_dir=ctx.final_agent_dir,
                branch_name=branch,
                commit_message=commit_msg,
            )
            git_branch = git_res.branch_name
            git_commit_sha = git_res.commit_sha

    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"Phase 8 Step 5 (workspace / Git delivery) failed: {exc}") from exc

    manifest = MetaDeliveryManifest(
        agent_id=ctx.agent_name,
        agent_version=ctx.agent_version,
        workspace_path=str(ctx.final_agent_dir),
        impl_docs_path=str(ctx.final_impl_dir),
        code_pointer=final_code_pointer,
        git_branch=git_branch,
        git_commit_sha=git_commit_sha,
        adr_document_path=str(ctx.adr_disk_path),
        tier2_registered=tier2_registered,
        rag_indexed=rag_indexed,
    )

    return {
        "current_phase": "completed",
        "generated_code_pointer": final_code_pointer,
        "delivery_manifest": manifest,
        "last_error": None,
        "build_logs": [
            BuildLogEntry(
                phase="deliver",
                actor="coordinator",
                action="node_delivery",
                status="completed",
                details={
                    "agent_id": ctx.agent_name,
                    "agent_version": ctx.agent_version,
                    "workspace_path": str(ctx.final_agent_dir),
                    "git_branch": git_branch,
                    "git_commit_sha": git_commit_sha,
                    "tier2_registered": tier2_registered,
                    "rag_indexed": rag_indexed,
                    "adr_document_path": str(ctx.adr_disk_path),
                },
            )
        ],
    }


async def anode_delivery(
    state: BlackboardState,
    *,
    deps: Optional[CoordinatorDeps] = None,
) -> dict[str, Any]:
    """Execute Phase 8 Deterministic Delivery Node asynchronously."""
    ctx = _prepare_delivery_context(state, deps)
    adr_embeddings, embedding_model, embedding_dim, new_comp_embeddings = (
        await _compute_step3_embeddings_async(ctx)
    )
    tier2_registered, rag_indexed = await _execute_step4_async(
        ctx=ctx,
        state=state,
        adr_embeddings=adr_embeddings,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dim,
        new_comp_embeddings=new_comp_embeddings,
    )
    return _materialize_and_publish_step5(
        ctx=ctx,
        state=state,
        tier2_registered=tier2_registered,
        rag_indexed=rag_indexed,
    )


def node_delivery(
    state: BlackboardState,
    *,
    deps: Optional[CoordinatorDeps] = None,
) -> dict[str, Any]:
    """Synchronous entrypoint for Phase 8 Delivery Node (avoids spawning an event loop unnecessarily)."""
    ctx = _prepare_delivery_context(state, deps)
    adr_embeddings, embedding_model, embedding_dim, new_comp_embeddings = (
        _compute_step3_embeddings_sync(ctx)
    )
    tier2_registered, rag_indexed = _execute_step4_sync(
        ctx=ctx,
        state=state,
        adr_embeddings=adr_embeddings,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dim,
        new_comp_embeddings=new_comp_embeddings,
    )
    return _materialize_and_publish_step5(
        ctx=ctx,
        state=state,
        tier2_registered=tier2_registered,
        rag_indexed=rag_indexed,
    )


def make_delivery_node(deps: Optional[CoordinatorDeps] = None) -> Any:
    """Create a dual sync/async LangGraph runnable for Phase 8 Delivery."""
    active_deps = deps or CoordinatorDeps()

    def _sync(state: BlackboardState) -> dict[str, Any]:
        return node_delivery(state, deps=active_deps)

    async def _async(state: BlackboardState) -> dict[str, Any]:
        return await anode_delivery(state, deps=active_deps)

    return make_dual_node(_sync, _async)
