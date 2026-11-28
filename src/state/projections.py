"""Strict State Projection mappers enforcing the Context Funnel Principle.

Each domain Specialist receives a narrow, immutable, strictly scoped slice of
the Blackboard state containing only the fields authorized for its task.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.state.blackboard import (
    ArchitectureProposal,
    CatalogContext,
    ChangeRequestContext,
    ClarificationRound,
    ClarifiedRequirement,
    ContractPointer,
    ImplDocPackage,
    ParsedSpecSummary,
    ResearchFindings,
    ValidationReport,
    WorkflowMode,
    get_approved_architecture,
    get_approved_impl_docs,
    get_latest_architecture,
    get_latest_impl_docs,
    get_latest_validation_report,
)


class ProjectionError(ValueError):
    """Raised when required upstream Blackboard state is missing for a Specialist slice."""


class _ProjectionModel(BaseModel):
    """Base projection schema: frozen, strictly scoped, rejects leaked keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _deep_clone(value: Any) -> Any:
    """Deep-copy mutable containers or Pydantic models to isolate projections."""
    if isinstance(value, BaseModel):
        return value.model_copy(deep=True)
    return copy.deepcopy(value)


def _coerce_model[T: BaseModel](value: Any, model_cls: type[T]) -> T:
    if isinstance(value, model_cls):
        return value.model_copy(deep=True)
    return model_cls.model_validate(value)


# ---------------------------------------------------------------------------
# Phase-Boundary Compression / Summarization Helpers (architecture.md §5.3)
# ---------------------------------------------------------------------------


def summarize_parsed_spec(parsed_spec: Mapping[str, Any]) -> ParsedSpecSummary:
    """Compress a full parsed YAML spec into a concise ``ParsedSpecSummary``."""
    agent_sec = parsed_spec.get("agent") or {}
    arch_sec = parsed_spec.get("architecture") or {}
    stages_sec = parsed_spec.get("stages") or []
    contracts_sec = parsed_spec.get("contracts") or []
    gov_sec = parsed_spec.get("governance") or {}
    cross_sec = parsed_spec.get("cross_agent") or {}
    constraints_sec = parsed_spec.get("constraints") or {}

    stage_ids: list[str] = []
    stage_summaries: list[dict[str, Any]] = []
    for stg in stages_sec:
        if not isinstance(stg, Mapping):
            continue
        sid = str(stg.get("id") or "").strip()
        if sid:
            stage_ids.append(sid)
        stage_summaries.append(
            {
                "id": sid,
                "type": stg.get("type"),
                "autonomy": stg.get("autonomy"),
                "input_contract": stg.get("input_contract"),
                "output_contract": stg.get("output_contract"),
                "tools": list(stg.get("tools") or []),
                "error_strategy": stg.get("error_strategy"),
            }
        )

    contract_names = [
        str(c.get("id") or c.get("name"))
        for c in contracts_sec
        if isinstance(c, Mapping) and (c.get("id") or c.get("name"))
    ]

    shared_reqs = [
        str(sc.get("name") or sc.get("component_id"))
        for sc in (cross_sec.get("shared_components") or [])
        if isinstance(sc, Mapping) and (sc.get("name") or sc.get("component_id"))
    ]
    upstream = [
        str(dep.get("agent"))
        for dep in (cross_sec.get("upstream_dependencies") or [])
        if isinstance(dep, Mapping) and dep.get("agent")
    ]
    downstream = [
        str(dep.get("agent"))
        for dep in (cross_sec.get("downstream_consumers") or [])
        if isinstance(dep, Mapping) and dep.get("agent")
    ]

    return ParsedSpecSummary(
        agent_name=str(agent_sec.get("name") or "unnamed_agent"),
        agent_version=str(agent_sec.get("version") or "1.0.0"),
        domain=str(agent_sec.get("domain") or "other"),
        description=str(agent_sec.get("description") or "").strip(),
        autonomy_level=str(arch_sec.get("autonomy_level") or "semi_autonomous"),
        complexity=arch_sec.get("complexity"),
        runtime_preference=arch_sec.get("runtime_preference"),
        stage_ids=stage_ids,
        stage_summaries=stage_summaries,
        contract_names=contract_names,
        data_classification=gov_sec.get("data_classification"),
        approval_gates=list(gov_sec.get("approval_gates") or []),
        required_libraries=list(constraints_sec.get("required_libraries") or []),
        avoided_libraries=list(constraints_sec.get("avoided_libraries") or []),
        shared_component_requests=shared_reqs,
        upstream_dependencies=upstream,
        downstream_consumers=downstream,
    )


def extract_design_impacting_requirements(
    rounds: Sequence[ClarificationRound | Mapping[str, Any]],
) -> list[ClarifiedRequirement]:
    """Compress Gate 1 Q&A rounds into design-impacting requirements only.

    Non-design-impacting clarifications (already folded into ``parsed_spec``)
    are intentionally excluded per ``architecture.md`` §5.3.
    """
    extracted: list[ClarifiedRequirement] = []
    for raw_round in rounds:
        rnd = _coerce_model(raw_round, ClarificationRound)
        q_by_id = {q.question_id: q for q in rnd.questions}
        for ans in rnd.answers:
            if not ans.design_impacting:
                continue
            question = q_by_id.get(ans.question_id)
            category = question.category if question else "clarification"
            summary = (ans.resolved_requirement or ans.answer).strip()
            if not summary:
                continue
            affected = [question.impact_area] if (question and question.impact_area) else []
            if question and question.field_path and question.field_path not in affected:
                affected.append(question.field_path)
            extracted.append(
                ClarifiedRequirement(
                    requirement_id=f"r{rnd.round_number}_{ans.question_id}",
                    source_round=rnd.round_number,
                    category=category,
                    summary=summary,
                    affected_sections=affected,
                )
            )
    return extracted


# ---------------------------------------------------------------------------
# Strict Per-Specialist Projection Schemas
# ---------------------------------------------------------------------------


class SpecAnalystProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Spec Analyst specialist."""

    run_id: str
    mode: WorkflowMode
    raw_spec_yaml: Optional[str] = None
    spec_pointer: Optional[ContractPointer] = None
    spec_version: str = "1.0.0"
    parsed_spec: Optional[dict[str, Any]] = None
    clarification_rounds: list[ClarificationRound] = Field(default_factory=list)
    clarification_round_count: int = 0
    known_agent_names: list[str] = Field(default_factory=list)
    known_component_names: list[str] = Field(default_factory=list)
    change_request: Optional[ChangeRequestContext] = None
    active_impl_docs: Optional[ImplDocPackage] = None


class ResearcherProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Researcher specialist (excludes raw spec & Q&A)."""

    run_id: str
    parsed_spec: dict[str, Any]
    parsed_spec_summary: Optional[ParsedSpecSummary] = None
    enriched_requirements: list[ClarifiedRequirement] = Field(default_factory=list)
    available_components: list[dict[str, Any]] = Field(default_factory=list)


class ArchitectProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Architect specialist.

    Receives spec summary, enriched requirements, research findings, Tier 2
    catalog context, and (when revising) only the latest proposal + feedback.
    Excludes raw spec YAML, raw Q&A transcripts, rejected revisions, and code.
    """

    run_id: str
    mode: WorkflowMode
    parsed_spec_summary: ParsedSpecSummary
    enriched_requirements: list[ClarifiedRequirement] = Field(default_factory=list)
    research_findings: Optional[ResearchFindings] = None
    catalog_context: Optional[CatalogContext] = None
    current_architecture: Optional[ArchitectureProposal] = None
    architecture_feedback: Optional[str] = None
    architecture_revision_count: int = 0
    change_request: Optional[ChangeRequestContext] = None


class DocumentarianProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Documentarian specialist.

    Receives ONLY the approved architecture proposal and matching shared
    component interface specs (plus current impl_docs on Gate 3 revision or change flow).
    """

    run_id: str
    mode: WorkflowMode
    approved_architecture: Optional[ArchitectureProposal] = None
    shared_component_specs: list[dict[str, Any]] = Field(default_factory=list)
    current_impl_docs: Optional[ImplDocPackage] = None
    impl_doc_feedback: Optional[str] = None
    impl_doc_revision_count: int = 0
    change_request: Optional[ChangeRequestContext] = None
    contract_validation_feedback: list[str] = Field(default_factory=list)


class BuilderProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Builder specialist.

    The approved ``impl_docs/`` package and referenced shared component sources
    constitute the Builder's entire world; nothing from Part A is projected.
    """

    run_id: str
    mode: WorkflowMode
    target_agent_name: Optional[str] = None
    approved_impl_docs: ImplDocPackage
    impl_docs_pointer: Optional[ContractPointer] = None
    shared_components_source: dict[str, str] = Field(default_factory=dict)
    fix_instructions: list[str] = Field(default_factory=list)
    build_fix_attempt_count: int = 0
    changed_stages: list[str] = Field(default_factory=list)
    changed_contracts: list[str] = Field(default_factory=list)


class LeafCoderProjection(_ProjectionModel):
    """Ultra-narrow projection for LLM Leaf Coder (builder.md §5.3).

    Scoped strictly to one stage and one custom tool hook, supporting ``create``
    (Day-0) and ``patch`` (Day-2 hotfix-preserving) modes.
    """

    run_id: str
    mode: Literal["create", "patch"]
    stage_id: str
    tool_name: Optional[str] = None
    handler: Optional[str] = None
    signature: Optional[str] = None
    logic_description: Optional[str] = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    input_contract: Optional[dict[str, Any]] = None
    output_contract: Optional[dict[str, Any]] = None
    existing_code: Optional[str] = None
    existing_tests: Optional[str] = None
    fix_diagnostics: list[str] = Field(default_factory=list)


class ValidatorProjection(_ProjectionModel):
    """Authorized Blackboard slice for the Validator specialist.

    Receives ONLY approved implementation documents (ground truth) and the
    Claim Check pointer to the generated codebase on disk.
    """

    run_id: str
    approved_impl_docs: ImplDocPackage
    impl_docs_pointer: Optional[ContractPointer] = None
    generated_code_pointer: ContractPointer
    build_fix_attempt_count: int = 0


# Declarative audit map documenting allowed projection fields per specialist.
SPECIALIST_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "spec_analyst": frozenset(SpecAnalystProjection.model_fields.keys()),
    "researcher": frozenset(ResearcherProjection.model_fields.keys()),
    "architect": frozenset(ArchitectProjection.model_fields.keys()),
    "documentarian": frozenset(DocumentarianProjection.model_fields.keys()),
    "builder": frozenset(BuilderProjection.model_fields.keys()),
    "leaf_coder": frozenset(LeafCoderProjection.model_fields.keys()),
    "validator": frozenset(ValidatorProjection.model_fields.keys()),
}


# ---------------------------------------------------------------------------
# Projection Mapper Functions
# ---------------------------------------------------------------------------


def _extract_catalog(state: Mapping[str, Any]) -> Optional[CatalogContext]:
    raw = state.get("catalog_context")
    if raw is None:
        return None
    return _coerce_model(raw, CatalogContext)


def project_for_spec_analyst(state: Mapping[str, Any]) -> SpecAnalystProjection:
    """Project Blackboard state for the Spec Analyst specialist."""
    mode: WorkflowMode = state.get("mode", "build")
    raw_spec_yaml = state.get("raw_spec_yaml")
    spec_pointer_raw = state.get("spec_pointer")
    spec_pointer = (
        _coerce_model(spec_pointer_raw, ContractPointer)
        if spec_pointer_raw is not None
        else None
    )

    change_req_raw = state.get("change_request")
    change_req = (
        _coerce_model(change_req_raw, ChangeRequestContext)
        if change_req_raw is not None
        else None
    )

    if mode == "build" and raw_spec_yaml is None and spec_pointer is None:
        raise ProjectionError("Spec Analyst requires raw_spec_yaml or spec_pointer in build mode.")
    if mode == "change" and change_req is None:
        raise ProjectionError("Spec Analyst requires change_request in change mode.")

    catalog = _extract_catalog(state)
    known_agents = (
        [str(a.get("agent_id") or a.get("agent_name")) for a in catalog.existing_agents if a.get("agent_id") or a.get("agent_name")]
        if catalog is not None
        else []
    )
    known_components = (
        [str(c.get("component_name")) for c in catalog.available_components if c.get("component_name")]
        if catalog is not None
        else []
    )

    rounds = [_coerce_model(r, ClarificationRound) for r in (state.get("clarification_rounds") or [])]

    active_impl_docs: Optional[ImplDocPackage] = None
    if mode == "change":
        latest_impl = get_latest_impl_docs(state)
        if latest_impl is not None:
            active_impl_docs = _deep_clone(latest_impl.package)

    return SpecAnalystProjection(
        run_id=str(state["run_id"]),
        mode=mode,
        raw_spec_yaml=raw_spec_yaml,
        spec_pointer=spec_pointer,
        spec_version=str(state.get("spec_version") or "1.0.0"),
        parsed_spec=_deep_clone(state.get("parsed_spec")),
        clarification_rounds=rounds,
        clarification_round_count=int(state.get("clarification_round_count", len(rounds))),
        known_agent_names=known_agents,
        known_component_names=known_components,
        change_request=change_req,
        active_impl_docs=active_impl_docs,
    )


def project_for_researcher(state: Mapping[str, Any]) -> ResearcherProjection:
    """Project Blackboard state for the Researcher specialist."""
    parsed_spec = state.get("parsed_spec")
    if not parsed_spec:
        raise ProjectionError("Researcher requires parsed_spec on the Blackboard.")

    summary_raw = state.get("parsed_spec_summary")
    summary = (
        _coerce_model(summary_raw, ParsedSpecSummary)
        if summary_raw is not None
        else summarize_parsed_spec(parsed_spec)
    )

    enriched = [
        _coerce_model(req, ClarifiedRequirement)
        for req in (state.get("enriched_requirements") or [])
    ]
    if not enriched and state.get("clarification_rounds"):
        enriched = extract_design_impacting_requirements(state["clarification_rounds"])

    catalog = _extract_catalog(state)
    components = _deep_clone(catalog.available_components) if catalog is not None else []

    return ResearcherProjection(
        run_id=str(state["run_id"]),
        parsed_spec=_deep_clone(parsed_spec),
        parsed_spec_summary=summary,
        enriched_requirements=enriched,
        available_components=components,
    )


def project_for_architect(state: Mapping[str, Any]) -> ArchitectProjection:
    """Project Blackboard state for the Architect specialist."""
    mode: WorkflowMode = state.get("mode", "build")
    summary_raw = state.get("parsed_spec_summary")
    if summary_raw is not None:
        summary = _coerce_model(summary_raw, ParsedSpecSummary)
    elif state.get("parsed_spec"):
        summary = summarize_parsed_spec(state["parsed_spec"])
    elif mode == "change" and (latest_impl := get_latest_impl_docs(state)) is not None:
        agent_meta = latest_impl.package.agent_yaml.get("agent") or {}
        arch_meta = latest_impl.package.agent_yaml.get("architecture") or {}
        summary = ParsedSpecSummary(
            agent_name=str(agent_meta.get("name") or state.get("target_agent_name") or "agent"),
            agent_version=str(agent_meta.get("version") or "1.0.0"),
            description=str(agent_meta.get("description") or ""),
            autonomy_level=str(arch_meta.get("autonomy_level") or "semi_autonomous"),
            runtime_preference=arch_meta.get("runtime"),
            stage_ids=list(latest_impl.package.stages.keys()),
            contract_names=list(latest_impl.package.contracts.keys()),
        )
    else:
        raise ProjectionError("Architect requires parsed_spec_summary or parsed_spec on the Blackboard.")

    research_raw = state.get("research_findings")
    if mode == "build" and research_raw is None:
        raise ProjectionError("Architect requires research_findings before Phase 4 Design.")

    research = _coerce_model(research_raw, ResearchFindings) if research_raw is not None else None
    catalog = _extract_catalog(state)

    # Project only the latest proposal when revising (never rejected history)
    latest_arch = get_latest_architecture(state)
    arch_feedback = state.get("architecture_feedback")
    current_arch = latest_arch.model_copy(deep=True) if (latest_arch is not None and arch_feedback) else None

    enriched = [
        _coerce_model(req, ClarifiedRequirement)
        for req in (state.get("enriched_requirements") or [])
    ]
    if not enriched and state.get("clarification_rounds"):
        enriched = extract_design_impacting_requirements(state["clarification_rounds"])

    change_req_raw = state.get("change_request")
    change_req = (
        _coerce_model(change_req_raw, ChangeRequestContext)
        if change_req_raw is not None
        else None
    )

    return ArchitectProjection(
        run_id=str(state["run_id"]),
        mode=mode,
        parsed_spec_summary=summary,
        enriched_requirements=enriched,
        research_findings=research,
        catalog_context=catalog,
        current_architecture=current_arch,
        architecture_feedback=arch_feedback,
        architecture_revision_count=int(state.get("architecture_revision_count", 0)),
        change_request=change_req,
    )


def project_for_documentarian(state: Mapping[str, Any]) -> DocumentarianProjection:
    """Project Blackboard state for the Documentarian specialist."""
    mode: WorkflowMode = state.get("mode", "build")
    approved_arch = get_approved_architecture(state)
    change_req_raw = state.get("change_request")
    change_req = (
        _coerce_model(change_req_raw, ChangeRequestContext)
        if change_req_raw is not None
        else None
    )

    if mode == "build" and approved_arch is None:
        raise ProjectionError("Documentarian requires an approved architecture proposal.")
    if mode == "change" and change_req is None and approved_arch is None:
        raise ProjectionError("Documentarian requires change_request or approved_architecture in change mode.")

    # Filter Tier 2 component specs to only those referenced in approved_architecture (if present)
    catalog = _extract_catalog(state)
    component_specs: list[dict[str, Any]] = []
    if catalog is not None:
        if approved_arch is not None and approved_arch.reused_components:
            wanted = {
                str(c.get("component_id") or c.get("component_name") or c.get("name"))
                for c in approved_arch.reused_components
                if isinstance(c, Mapping)
            }
            for comp in catalog.available_components:
                cid = str(comp.get("component_id") or "")
                cname = str(comp.get("component_name") or "")
                if cid in wanted or cname in wanted:
                    component_specs.append(_deep_clone(comp))
        else:
            component_specs = _deep_clone(catalog.available_components)

    latest_impl = get_latest_impl_docs(state)
    current_impl_docs: Optional[ImplDocPackage] = None
    if latest_impl is not None and (mode == "change" or state.get("impl_doc_feedback") or state.get("active_fix_instructions")):
        current_impl_docs = _deep_clone(latest_impl.package)

    # Extract only documentarian-targeted validation issues (e.g., contract mismatches)
    contract_feedback: list[str] = []
    latest_report_raw = get_latest_validation_report(state)
    if latest_report_raw is not None:
        latest_report = _coerce_model(latest_report_raw, ValidationReport)
        contract_feedback = [
            f"{issue.check_name}: {issue.message}"
            for issue in latest_report.issues
            if issue.route_target == "documentarian"
        ]

    return DocumentarianProjection(
        run_id=str(state["run_id"]),
        mode=mode,
        approved_architecture=approved_arch.model_copy(deep=True) if approved_arch is not None else None,
        shared_component_specs=component_specs,
        current_impl_docs=current_impl_docs,
        impl_doc_feedback=state.get("impl_doc_feedback"),
        impl_doc_revision_count=int(state.get("impl_doc_revision_count", 0)),
        change_request=change_req,
        contract_validation_feedback=contract_feedback,
    )


def project_for_builder(state: Mapping[str, Any]) -> BuilderProjection:
    """Project Blackboard state for the Builder (Scaffold Compiler + Leaf Coder)."""
    mode: WorkflowMode = state.get("mode", "build")
    approved_rev = get_approved_impl_docs(state)
    if approved_rev is None:
        raise ProjectionError("Builder requires approved implementation documents (Gate 3).")

    impl_ptr_raw = state.get("impl_docs_pointer")
    impl_ptr = (
        _coerce_model(impl_ptr_raw, ContractPointer)
        if impl_ptr_raw is not None
        else None
    )

    change_req_raw = state.get("change_request")
    changed_stages: list[str] = []
    changed_contracts: list[str] = []
    if mode == "change" and change_req_raw is not None:
        change_req = _coerce_model(change_req_raw, ChangeRequestContext)
        changed_stages = list(change_req.affected_stages)
        changed_contracts = list(change_req.affected_contracts)

    return BuilderProjection(
        run_id=str(state["run_id"]),
        mode=mode,
        target_agent_name=state.get("target_agent_name")
        or (approved_rev.package.agent_yaml.get("agent") or {}).get("name"),
        approved_impl_docs=_deep_clone(approved_rev.package),
        impl_docs_pointer=impl_ptr,
        shared_components_source=_deep_clone(state.get("shared_components_source") or {}),
        fix_instructions=list(state.get("active_fix_instructions") or []),
        build_fix_attempt_count=int(state.get("build_fix_attempt_count", 0)),
        changed_stages=changed_stages,
        changed_contracts=changed_contracts,
    )


def project_for_leaf_coder(
    state: Mapping[str, Any],
    *,
    stage_id: str,
    tool_name: Optional[str] = None,
    mode: Literal["create", "patch"] = "create",
    existing_code: Optional[str] = None,
    existing_tests: Optional[str] = None,
) -> LeafCoderProjection:
    """Project a single stage + contract pair for the LLM Leaf Coder (builder.md §5.3)."""
    approved_rev = get_approved_impl_docs(state)
    if approved_rev is None:
        raise ProjectionError("Leaf Coder requires approved implementation documents.")

    pkg = approved_rev.package
    if stage_id not in pkg.stages:
        raise ProjectionError(f"Stage {stage_id!r} not found in approved impl_docs.stages.")

    stage_doc = pkg.stages[stage_id]
    processing = stage_doc.get("processing") or {}
    tools = list(processing.get("tools") or [])

    selected_tool: Optional[Mapping[str, Any]] = None
    if tool_name is not None:
        for t in tools:
            if isinstance(t, Mapping) and t.get("name") == tool_name:
                selected_tool = t
                break
        if selected_tool is None:
            raise ProjectionError(f"Tool {tool_name!r} not found on stage {stage_id!r}.")
    else:
        for t in tools:
            if isinstance(t, Mapping) and t.get("type") == "custom_code" and t.get("handler"):
                selected_tool = t
                break

    in_contract_name = (stage_doc.get("input") or {}).get("contract")
    out_contract_name = (stage_doc.get("output") or {}).get("contract")
    in_contract = pkg.contracts.get(in_contract_name) if in_contract_name else None
    out_contract = pkg.contracts.get(out_contract_name) if out_contract_name else None

    if mode == "patch" and existing_code is None:
        raise ProjectionError("Leaf Coder 'patch' mode requires existing_code to prevent overwriting hotfixes.")

    # Scope diagnostics to issues affecting this stage or general fixes
    fix_diags: list[str] = list(state.get("active_fix_instructions") or [])
    latest_report = get_latest_validation_report(state)
    if latest_report is not None:
        scoped_msgs = [
            f"{iss.check_name} ({iss.file_path or stage_id}): {iss.fix_instruction or iss.message}"
            for iss in latest_report.issues
            if iss.route_target == "leaf_coder"
            and (iss.target_stage_id is None or iss.target_stage_id == stage_id)
        ]
        if scoped_msgs:
            fix_diags = scoped_msgs

    return LeafCoderProjection(
        run_id=str(state["run_id"]),
        mode=mode,
        stage_id=stage_id,
        tool_name=str(selected_tool.get("name")) if selected_tool else tool_name,
        handler=selected_tool.get("handler") if selected_tool else None,
        signature=selected_tool.get("signature") if selected_tool else None,
        logic_description=processing.get("logic_description"),
        tools=_deep_clone(tools),
        input_contract=_deep_clone(in_contract),
        output_contract=_deep_clone(out_contract),
        existing_code=existing_code if mode == "patch" else None,
        existing_tests=existing_tests if mode == "patch" else None,
        fix_diagnostics=fix_diags,
    )


def project_for_validator(state: Mapping[str, Any]) -> ValidatorProjection:
    """Project Blackboard state for the Validator specialist (ground truth + Claim Check code)."""
    approved_rev = get_approved_impl_docs(state)
    if approved_rev is None:
        raise ProjectionError("Validator requires approved implementation documents.")

    code_ptr_raw = state.get("generated_code_pointer")
    if code_ptr_raw is None:
        raise ProjectionError("Validator requires generated_code_pointer from Phase 6 Construction.")

    impl_ptr_raw = state.get("impl_docs_pointer")
    impl_ptr = (
        _coerce_model(impl_ptr_raw, ContractPointer)
        if impl_ptr_raw is not None
        else None
    )

    return ValidatorProjection(
        run_id=str(state["run_id"]),
        approved_impl_docs=_deep_clone(approved_rev.package),
        impl_docs_pointer=impl_ptr,
        generated_code_pointer=_coerce_model(code_ptr_raw, ContractPointer),
        build_fix_attempt_count=int(state.get("build_fix_attempt_count", 0)),
    )


def project_for_specialist(
    specialist_name: str,
    state: Mapping[str, Any],
    **kwargs: Any,
) -> _ProjectionModel:
    """Dispatch state projection uniformly by specialist name."""
    normalized = specialist_name.strip().lower()
    if normalized == "spec_analyst":
        return project_for_spec_analyst(state)
    if normalized == "researcher":
        return project_for_researcher(state)
    if normalized == "architect":
        return project_for_architect(state)
    if normalized == "documentarian":
        return project_for_documentarian(state)
    if normalized == "builder":
        return project_for_builder(state)
    if normalized == "leaf_coder":
        return project_for_leaf_coder(state, **kwargs)
    if normalized == "validator":
        return project_for_validator(state)
    raise ProjectionError(
        f"Unknown specialist {specialist_name!r}. Expected one of: {sorted(SPECIALIST_ALLOWED_FIELDS)}."
    )
