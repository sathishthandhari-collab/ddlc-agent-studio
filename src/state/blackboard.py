"""Master append-only Blackboard state schema and Claim Check primitives.

Implements the Blackboard pattern (PAT-04 deterministic-control variant) and
Claim Check pointers (Decision 22) for the Layer 1 DDLC Meta-Agent.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

WorkflowMode = Literal["build", "change"]
BlackboardPhase = Literal[
    "setup",
    "understand",
    "research",
    "design",
    "document",
    "construct",
    "validate",
    "deliver",
    "change_analysis",
    "impact_analysis",
    "completed",
    "failed",
    "aborted",
]
FeasibilityStatus = Literal["feasible", "feasible_with_caveats", "not_feasible"]
GateDecisionStatus = Literal["approved", "revise", "abort", "escalate"]
SpecValidationStatus = Literal["pending", "rejected", "clarify", "accepted"]
SeverityLevel = Literal["info", "warning", "error", "critical"]

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class StaleStateError(RuntimeError):
    """Raised when a Claim Check artifact fails integrity verification on resume (FM-06)."""


def append_only[T](
    existing: Optional[Sequence[T]],
    incoming: Optional[Sequence[T] | T],
) -> list[T]:
    """Append-only LangGraph state reducer preserving full historical sequence.

    Lists accumulate across graph steps; empty updates or ``None`` never erase
    prior entries, and input sequences are shallow-copied to avoid aliasing.
    """
    result = list(existing) if existing is not None else []
    if incoming is None:
        return result
    if isinstance(incoming, (list, tuple)):
        result.extend(incoming)
    else:
        result.append(incoming)
    return result


# ---------------------------------------------------------------------------
# Claim Check Pointers & Cryptographic Verification (Decision 22 & 31)
# ---------------------------------------------------------------------------


def compute_bytes_sha256(data: bytes) -> str:
    """Return lowercase hex SHA-256 digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _resolve_local_storage_path(storage_uri: str) -> Path:
    """Resolve a local filesystem path or file:// URI safely."""
    raw = storage_uri.strip()
    if not raw:
        raise ValueError("storage_uri cannot be empty.")

    if raw.startswith("file://"):
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raise ValueError(f"Unsupported remote file URI host in {storage_uri!r}.")
        local_path = url2pathname(unquote(parsed.path))
        return Path(local_path)

    if "://" in raw:
        scheme = raw.split("://", 1)[0]
        raise ValueError(
            f"Unsupported non-local storage_uri scheme {scheme!r} without an explicit base_dir."
        )

    return Path(raw)


def compute_directory_sha256(
    base_dir: Path,
    files: Optional[Sequence[str]] = None,
) -> tuple[str, list[str], int]:
    """Deterministically hash files inside ``base_dir`` with path-traversal protection.

    Returns:
        ``(sha256_hex, sorted_relative_posix_paths, total_bytes)``
    """
    resolved_base = Path(base_dir).resolve()
    if not resolved_base.is_dir():
        raise FileNotFoundError(f"Artifact directory does not exist: {resolved_base}")

    candidate_paths: list[tuple[str, Path]] = []
    if files is not None:
        if len(files) == 0:
            raise ValueError("files cannot be empty; pass None to hash all files in base_dir.")
        for rel in files:
            if not rel or rel.strip() in {"", "."}:
                raise ValueError(f"Invalid empty or current-directory manifest entry: {rel!r}")
            rel_obj = Path(rel)
            if rel_obj.is_absolute():
                raise ValueError(f"Path traversal or absolute path disallowed: {rel!r}")
            target = (resolved_base / rel_obj).resolve()
            if not target.is_relative_to(resolved_base):
                raise ValueError(f"Path traversal detected outside base_dir: {rel!r}")
            if not target.is_file():
                raise FileNotFoundError(f"Manifest file missing on disk: {target}")
            norm_rel = target.relative_to(resolved_base).as_posix()
            candidate_paths.append((norm_rel, target))
    else:
        for item in resolved_base.rglob("*"):
            if item.is_file():
                resolved_item = item.resolve()
                if not resolved_item.is_relative_to(resolved_base):
                    raise ValueError(f"Symlink escape detected outside base_dir: {item}")
                norm_rel = resolved_item.relative_to(resolved_base).as_posix()
                candidate_paths.append((norm_rel, resolved_item))

    # Deduplicate and sort deterministically by relative POSIX path
    unique_sorted = sorted(dict(candidate_paths).items(), key=lambda pair: pair[0])
    hasher = hashlib.sha256()
    manifest: list[str] = []
    total_bytes = 0

    for rel_posix, file_path in unique_sorted:
        content = file_path.read_bytes()
        manifest.append(rel_posix)
        total_bytes += len(content)
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(content)

    return hasher.hexdigest(), manifest, total_bytes


class ContractPointer(BaseModel):
    """Lightweight Claim Check reference stored in LangGraph state (Decision 22)."""

    pointer_id: str = Field(default_factory=lambda: str(uuid4()))
    contract_name: str = Field(..., min_length=1)
    contract_version: str = Field(default="1.0.0", min_length=1)
    storage_uri: str = Field(..., min_length=1)
    file_manifest: list[str] = Field(default_factory=list)
    item_count: int = Field(default=0, ge=0)
    byte_size: int = Field(default=0, ge=0)
    payload_sha256: str = Field(..., min_length=64, max_length=64)
    frozen: bool = False
    frozen_by: Optional[str] = None
    frozen_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        lowered = value.strip().lower()
        if not _SHA256_RE.match(lowered):
            raise ValueError(f"Invalid SHA-256 hex digest: {value!r}")
        return lowered


def build_contract_pointer_from_directory(
    *,
    contract_name: str,
    base_dir: Path,
    files: Optional[Sequence[str]] = None,
    contract_version: str = "1.0.0",
    storage_uri: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> ContractPointer:
    """Create a tamper-evident Claim Check pointer for an artifact directory."""
    resolved_dir = Path(base_dir).resolve()
    digest, manifest, total_bytes = compute_directory_sha256(resolved_dir, files=files)
    return ContractPointer(
        contract_name=contract_name,
        contract_version=contract_version,
        storage_uri=storage_uri or str(resolved_dir),
        file_manifest=manifest,
        item_count=len(manifest),
        byte_size=total_bytes,
        payload_sha256=digest,
        metadata=dict(metadata or {}),
    )


def build_contract_pointer_from_bytes(
    *,
    contract_name: str,
    content: bytes,
    storage_uri: str,
    contract_version: str = "1.0.0",
    item_count: int = 1,
    file_manifest: Optional[Sequence[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> ContractPointer:
    """Create a Claim Check pointer for an externally persisted single payload."""
    return ContractPointer(
        contract_name=contract_name,
        contract_version=contract_version,
        storage_uri=storage_uri,
        file_manifest=list(file_manifest or []),
        item_count=item_count,
        byte_size=len(content),
        payload_sha256=compute_bytes_sha256(content),
        metadata=dict(metadata or {}),
    )


def verify_contract_pointer(
    pointer: ContractPointer,
    *,
    base_dir: Optional[Path] = None,
    raise_on_missing: bool = False,
) -> bool:
    """Verify that external artifact bytes match ``pointer.payload_sha256``.

    Raises ``ValueError`` immediately on path traversal or unsupported URI schemes,
    and returns ``False`` (or raises ``FileNotFoundError`` when ``raise_on_missing=True``)
    if target files are missing or modified.
    """
    target_path = Path(base_dir).resolve() if base_dir is not None else _resolve_local_storage_path(
        pointer.storage_uri
    ).resolve()

    if not target_path.exists():
        if raise_on_missing:
            raise FileNotFoundError(f"Claim Check target missing: {target_path}")
        return False

    if target_path.is_file():
        digest = compute_bytes_sha256(target_path.read_bytes())
        return digest == pointer.payload_sha256

    if target_path.is_dir():
        try:
            digest, manifest, _ = compute_directory_sha256(
                target_path,
                files=pointer.file_manifest if pointer.file_manifest else None,
            )
        except FileNotFoundError:
            if raise_on_missing:
                raise
            return False
        if pointer.file_manifest and sorted(pointer.file_manifest) != sorted(manifest):
            return False
        return digest == pointer.payload_sha256

    return False


def assert_contract_pointer_valid(
    pointer: ContractPointer,
    *,
    base_dir: Optional[Path] = None,
) -> None:
    """Verify a Claim Check pointer and raise ``StaleStateError`` on tampering/drift."""
    if not verify_contract_pointer(pointer, base_dir=base_dir, raise_on_missing=False):
        raise StaleStateError(
            f"Claim Check verification failed for contract '{pointer.contract_name}' "
            f"(pointer_id={pointer.pointer_id}, uri={pointer.storage_uri})."
        )


def freeze_contract_pointer(
    pointer: ContractPointer,
    *,
    approved_by: str,
    frozen_at: Optional[datetime] = None,
) -> ContractPointer:
    """Return an immutable frozen copy of ``pointer`` upon human gate approval."""
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by must be non-empty when freezing a Claim Check pointer.")
    return pointer.model_copy(
        update={
            "frozen": True,
            "frozen_by": approved_by.strip(),
            "frozen_at": frozen_at or datetime.now(timezone.utc),
        }
    )


def verify_state_pointers(state: Mapping[str, Any]) -> dict[str, bool]:
    """Verify all populated ``ContractPointer`` fields present in a Blackboard state."""
    pointer_fields = (
        "spec_pointer",
        "impl_docs_pointer",
        "tier3_contract_pointer",
        "generated_code_pointer",
    )
    results: dict[str, bool] = {}
    for field_name in pointer_fields:
        raw_ptr = state.get(field_name)
        if raw_ptr is None:
            continue
        ptr = raw_ptr if isinstance(raw_ptr, ContractPointer) else ContractPointer.model_validate(raw_ptr)
        results[field_name] = verify_contract_pointer(ptr, raise_on_missing=False)
    return results


# ---------------------------------------------------------------------------
# Phase-Scoped Blackboard Domain Models
# ---------------------------------------------------------------------------


class ClarificationQuestion(BaseModel):
    """Single clarifying question surfaced by the Spec Analyst."""

    question_id: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    field_path: Optional[str] = None
    question: str = Field(..., min_length=1)
    suggested_default: Optional[str] = None
    impact_area: Optional[str] = None


class ClarificationAnswer(BaseModel):
    """User answer to a clarifying question at Gate 1."""

    question_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    answered_by: str = Field(default="human", min_length=1)
    design_impacting: bool = True
    resolved_requirement: Optional[str] = None


class ClarificationRound(BaseModel):
    """Append-only record of one Gate 1 clarification cycle."""

    round_number: int = Field(..., ge=1)
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    answers: list[ClarificationAnswer] = Field(default_factory=list)
    summary_of_impact: Optional[str] = None
    resolved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ClarifiedRequirement(BaseModel):
    """Compressed, design-impacting requirement projected forward across Phase 2 -> 3/4."""

    requirement_id: str = Field(..., min_length=1)
    source_round: int = Field(..., ge=1)
    category: str = Field(default="clarification")
    summary: str = Field(..., min_length=1)
    affected_sections: list[str] = Field(default_factory=list)


class ParsedSpecSummary(BaseModel):
    """Compressed summary of a parsed spec projected to the Architect."""

    agent_name: str = Field(..., min_length=1)
    agent_version: str = Field(..., min_length=1)
    domain: str = Field(default="other")
    description: str = Field(default="")
    autonomy_level: str = Field(default="semi_autonomous")
    complexity: Optional[str] = None
    runtime_preference: Optional[str] = None
    stage_ids: list[str] = Field(default_factory=list)
    stage_summaries: list[dict[str, Any]] = Field(default_factory=list)
    contract_names: list[str] = Field(default_factory=list)
    data_classification: Optional[str] = None
    approval_gates: list[str] = Field(default_factory=list)
    required_libraries: list[str] = Field(default_factory=list)
    avoided_libraries: list[str] = Field(default_factory=list)
    shared_component_requests: list[str] = Field(default_factory=list)
    upstream_dependencies: list[str] = Field(default_factory=list)
    downstream_consumers: list[str] = Field(default_factory=list)


class CatalogContext(BaseModel):
    """Tier 2 portfolio context loaded during Phase 1 Setup."""

    existing_agents: list[dict[str, Any]] = Field(default_factory=list)
    available_components: list[dict[str, Any]] = Field(default_factory=list)
    known_contracts: list[dict[str, Any]] = Field(default_factory=list)
    cross_agent_dependencies: list[dict[str, Any]] = Field(default_factory=list)
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolEvaluation(BaseModel):
    """Feasibility evaluation of a single tool or library from the Researcher."""

    tool_name: str = Field(..., min_length=1)
    supported: bool
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    compatibility_notes: Optional[str] = None
    recommended_alternative: Optional[str] = None


class ResearchFindings(BaseModel):
    """Phase 3 Researcher feasibility verdict and discovered constraints."""

    verdict: FeasibilityStatus
    summary: str = Field(..., min_length=1)
    tool_evaluations: list[ToolEvaluation] = Field(default_factory=list)
    policy_constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    candidate_shared_components: list[dict[str, Any]] = Field(default_factory=list)
    rag_sources: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StageDesignSpec(BaseModel):
    """Architectural stage plan emitted by the Architect specialist."""

    stage_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    stage_type: str = Field(..., min_length=1)
    processing_type: str = Field(default="deterministic_tool")
    autonomy: str = Field(default="autonomous")
    input_contract: Optional[str] = None
    output_contract: Optional[str] = None
    tools: list[str] = Field(default_factory=list)
    shared_components: list[dict[str, Any]] = Field(default_factory=list)
    notes: Optional[str] = None


class ArchitectureProposal(BaseModel):
    """Append-only Phase 4 architecture proposal revision."""

    revision_number: int = Field(..., ge=1)
    agent_name: str = Field(..., min_length=1)
    agent_version: str = Field(default="1.0.0", min_length=1)
    pattern: str = Field(..., min_length=1)
    runtime: Literal["langgraph", "python_litellm"]
    autonomy_level: str = Field(default="semi_autonomous")
    summary: str = Field(..., min_length=1)
    stages: list[StageDesignSpec] = Field(default_factory=list)
    contracts_to_define: list[str] = Field(default_factory=list)
    reused_components: list[dict[str, Any]] = Field(default_factory=list)
    new_components_to_create: list[dict[str, Any]] = Field(default_factory=list)
    risks_and_tradeoffs: list[str] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    tier_3_schema_required: bool = False
    tier_3_tables: list[str] = Field(default_factory=list)
    revision_feedback_addressed: Optional[str] = None
    approved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ImplDocPackage(BaseModel):
    """5-file runtime-agnostic implementation document suite (Single Source of Truth)."""

    agent_yaml: dict[str, Any]
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    config_yaml: dict[str, Any] = Field(default_factory=dict)
    tests_yaml: dict[str, Any] = Field(default_factory=dict)
    tier3_contract_yaml: Optional[dict[str, Any]] = None
    pointer: Optional[ContractPointer] = None


class ImplDocRevision(BaseModel):
    """Append-only Phase 5 implementation document revision."""

    revision_number: int = Field(..., ge=1)
    package: ImplDocPackage
    summary: str = Field(default="")
    revision_feedback_addressed: Optional[str] = None
    approved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ValidationIssue(BaseModel):
    """Single deterministic or test verification failure from Phase 7 Validator."""

    check_name: str = Field(..., min_length=1)
    severity: SeverityLevel = "error"
    file_path: Optional[str] = None
    target_stage_id: Optional[str] = None
    route_target: Literal["leaf_coder", "compiler", "documentarian", "human"] = "leaf_coder"
    message: str = Field(..., min_length=1)
    fix_instruction: Optional[str] = None


class ValidationReport(BaseModel):
    """Append-only Phase 7 validation execution report."""

    attempt_number: int = Field(..., ge=1)
    passed: bool
    checks_run: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    test_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BuildLogEntry(BaseModel):
    """Append-only operational breadcrumb recorded on the Blackboard."""

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    phase: str = Field(..., min_length=1)
    actor: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1)
    status: Literal["started", "completed", "failed", "skipped"] = "completed"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GateDecisionRecord(BaseModel):
    """Append-only summary of human gate outcomes preserved in the Blackboard."""

    gate_name: Literal[
        "clarification",
        "architecture_review",
        "impl_doc_review",
        "build_escalation",
    ]
    round_or_revision: int = Field(default=1, ge=1)
    decision: GateDecisionStatus
    reviewer: str = Field(default="human", min_length=1)
    feedback: Optional[str] = None
    frozen_pointer_sha256: Optional[str] = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChangeRequestContext(BaseModel):
    """Day-2 maintenance / enhancement context operating directly on impl_docs/."""

    request_text: str = Field(..., min_length=1)
    intake_source: Literal["chainlit_chat", "git_pr", "cli"] = "chainlit_chat"
    agent_id: str = Field(..., min_length=1)
    affected_stages: list[str] = Field(default_factory=list)
    affected_contracts: list[str] = Field(default_factory=list)
    downstream_impacted_agents: list[str] = Field(default_factory=list)
    requires_dcm_migration: bool = False
    changed_impl_files: list[str] = Field(default_factory=list)


class MetaDeliveryManifest(BaseModel):
    """Terminal Phase 8 Delivery output produced by deterministic node_delivery."""

    agent_id: str = Field(..., min_length=1)
    agent_version: str = Field(..., min_length=1)
    workspace_path: str = Field(..., min_length=1)
    impl_docs_path: str = Field(..., min_length=1)
    code_pointer: Optional[ContractPointer] = None
    git_branch: Optional[str] = None
    git_commit_sha: Optional[str] = None
    adr_document_path: Optional[str] = None
    tier2_registered: bool = False
    rag_indexed: bool = False
    delivered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Master LangGraph Blackboard State
# ---------------------------------------------------------------------------


class BlackboardState(TypedDict):
    """Master LangGraph Blackboard state shared across Coordinator and Specialists."""

    # Identity & workflow routing
    run_id: str
    thread_id: str
    mode: WorkflowMode
    current_phase: BlackboardPhase
    target_agent_name: Optional[str]

    # Phase 1 Setup (Tier 2 catalog snapshot)
    catalog_context: Optional[CatalogContext]

    # Phase 2 Understanding (Spec Analyst + Gate 1 Clarification)
    raw_spec_yaml: Optional[str]
    spec_pointer: Optional[ContractPointer]
    spec_version: str
    spec_validation_status: SpecValidationStatus
    spec_rejected_errors: list[str]
    parsed_spec: Optional[dict[str, Any]]
    parsed_spec_summary: Optional[ParsedSpecSummary]
    has_open_gaps: bool
    clarification_round_count: int
    clarification_rounds: Annotated[list[ClarificationRound], append_only]
    enriched_requirements: Annotated[list[ClarifiedRequirement], append_only]

    # Phase 3 Research (Researcher)
    research_findings: Optional[ResearchFindings]
    feasibility_verdict: Optional[FeasibilityStatus]

    # Phase 4 Design (Architect + Gate 2 Architecture Review)
    architecture_revisions: Annotated[list[ArchitectureProposal], append_only]
    architecture_revision_count: int
    architecture_decision: Optional[GateDecisionStatus]
    architecture_approved: bool
    approved_architecture_version: Optional[int]
    architecture_feedback: Optional[str]

    # Phase 5 Documentation (Documentarian + Gate 3 Impl Doc Review)
    impl_doc_revisions: Annotated[list[ImplDocRevision], append_only]
    impl_doc_revision_count: int
    impl_doc_decision: Optional[GateDecisionStatus]
    impl_docs_approved: bool
    approved_impl_doc_version: Optional[int]
    impl_docs_pointer: Optional[ContractPointer]
    tier3_contract_pointer: Optional[ContractPointer]
    impl_doc_feedback: Optional[str]

    # Phase 6 Construction & Phase 7 Validation (Claim Check + Fix Loop + Gate 4)
    generated_code_pointer: Optional[ContractPointer]
    shared_components_source: dict[str, str]
    validation_reports: Annotated[list[ValidationReport], append_only]
    validation_passed: bool
    build_fix_attempt_count: int
    active_fix_instructions: list[str]

    # Day-2 Change Flow
    change_request: Optional[ChangeRequestContext]

    # Audit history, Safety Valves, Logs & Phase 8 Delivery
    gate_decisions: Annotated[list[GateDecisionRecord], append_only]
    build_logs: Annotated[list[BuildLogEntry], append_only]
    escalation_required: bool
    escalation_reason: Optional[str]
    human_escalated_decision: Optional[str]
    delivery_manifest: Optional[MetaDeliveryManifest]
    last_error: Optional[str]


def initial_blackboard_state(
    *,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    mode: WorkflowMode = "build",
    raw_spec_yaml: Optional[str] = None,
    spec_pointer: Optional[ContractPointer] = None,
    spec_version: str = "1.0.0",
    target_agent_name: Optional[str] = None,
    change_request: Optional[ChangeRequestContext] = None,
    baseline_impl_docs: Optional[ImplDocPackage] = None,
) -> BlackboardState:
    """Create a complete initial ``BlackboardState`` for a build or change run."""
    resolved_run_id = run_id or str(uuid4())
    resolved_thread_id = thread_id or resolved_run_id
    initial_phase: BlackboardPhase = "setup"
    initial_impl_revisions: list[ImplDocRevision] = []
    if baseline_impl_docs is not None:
        initial_impl_revisions.append(
            ImplDocRevision(
                revision_number=1,
                package=baseline_impl_docs,
                summary="Loaded baseline impl_docs for Day-2 change analysis.",
                approved=False,
            )
        )

    return {
        "run_id": resolved_run_id,
        "thread_id": resolved_thread_id,
        "mode": mode,
        "current_phase": initial_phase,
        "target_agent_name": target_agent_name,
        "catalog_context": None,
        "raw_spec_yaml": raw_spec_yaml,
        "spec_pointer": spec_pointer,
        "spec_version": spec_version,
        "spec_validation_status": "pending",
        "spec_rejected_errors": [],
        "parsed_spec": None,
        "parsed_spec_summary": None,
        "has_open_gaps": False,
        "clarification_round_count": 0,
        "clarification_rounds": [],
        "enriched_requirements": [],
        "research_findings": None,
        "feasibility_verdict": None,
        "architecture_revisions": [],
        "architecture_revision_count": 0,
        "architecture_decision": None,
        "architecture_approved": False,
        "approved_architecture_version": None,
        "architecture_feedback": None,
        "impl_doc_revisions": initial_impl_revisions,
        "impl_doc_revision_count": len(initial_impl_revisions),
        "impl_doc_decision": None,
        "impl_docs_approved": False,
        "approved_impl_doc_version": None,
        "impl_docs_pointer": None,
        "tier3_contract_pointer": None,
        "impl_doc_feedback": None,
        "generated_code_pointer": None,
        "shared_components_source": {},
        "validation_reports": [],
        "validation_passed": False,
        "build_fix_attempt_count": 0,
        "active_fix_instructions": [],
        "change_request": change_request,
        "gate_decisions": [],
        "build_logs": [],
        "escalation_required": False,
        "escalation_reason": None,
        "human_escalated_decision": None,
        "delivery_manifest": None,
        "last_error": None,
    }


# ---------------------------------------------------------------------------
# Deterministic Blackboard Accessors
# ---------------------------------------------------------------------------


def _coerce_model[T: BaseModel](item: Any, model_cls: type[T]) -> T:
    if isinstance(item, model_cls):
        return item
    return model_cls.model_validate(item)


def get_latest_architecture(state: Mapping[str, Any]) -> Optional[ArchitectureProposal]:
    """Return the most recent architecture proposal revision, if any."""
    revisions = state.get("architecture_revisions") or []
    if not revisions:
        return None
    return _coerce_model(revisions[-1], ArchitectureProposal)


def get_approved_architecture(state: Mapping[str, Any]) -> Optional[ArchitectureProposal]:
    """Return the explicitly approved architecture proposal version, if approved."""
    if not state.get("architecture_approved"):
        return None
    revisions = [_coerce_model(r, ArchitectureProposal) for r in (state.get("architecture_revisions") or [])]
    if not revisions:
        return None

    target_version = state.get("approved_architecture_version")
    if target_version is not None:
        for rev in reversed(revisions):
            if rev.revision_number == target_version:
                return rev
        return None
    for rev in reversed(revisions):
        if rev.approved:
            return rev
    return revisions[-1]


def get_latest_impl_docs(state: Mapping[str, Any]) -> Optional[ImplDocRevision]:
    """Return the most recent implementation document revision, if any."""
    revisions = state.get("impl_doc_revisions") or []
    if not revisions:
        return None
    return _coerce_model(revisions[-1], ImplDocRevision)


def get_approved_impl_docs(state: Mapping[str, Any]) -> Optional[ImplDocRevision]:
    """Return the explicitly approved implementation document revision, if approved."""
    if not state.get("impl_docs_approved"):
        return None
    revisions = [_coerce_model(r, ImplDocRevision) for r in (state.get("impl_doc_revisions") or [])]
    if not revisions:
        return None

    target_version = state.get("approved_impl_doc_version")
    if target_version is not None:
        for rev in reversed(revisions):
            if rev.revision_number == target_version:
                return rev
        return None
    for rev in reversed(revisions):
        if rev.approved:
            return rev
    return revisions[-1]


def get_latest_validation_report(state: Mapping[str, Any]) -> Optional[ValidationReport]:
    """Return the most recent Phase 7 validation report, if any."""
    reports = state.get("validation_reports") or []
    if not reports:
        return None
    return _coerce_model(reports[-1], ValidationReport)


def get_open_clarification_questions(state: Mapping[str, Any]) -> list[ClarificationQuestion]:
    """Return unanswered questions from the latest unresolved clarification round."""
    rounds = [_coerce_model(r, ClarificationRound) for r in (state.get("clarification_rounds") or [])]
    if not rounds:
        return []
    latest = rounds[-1]
    if latest.resolved:
        return []
    answered_ids = {ans.question_id for ans in latest.answers}
    return [q for q in latest.questions if q.question_id not in answered_ids]


# Register all Blackboard Pydantic models for LangGraph strict msgpack serialization.
_BLACKBOARD_MODELS: tuple[type[BaseModel], ...] = tuple(
    obj
    for obj in globals().values()
    if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
)

BLACKBOARD_MSGPACK_ALLOWLIST: tuple[tuple[str, str], ...] = tuple(
    sorted((cls.__module__, cls.__name__) for cls in _BLACKBOARD_MODELS)
)
