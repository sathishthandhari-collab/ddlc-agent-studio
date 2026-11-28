"""Spec Analyst specialist — Day-0 YAML validation, gap detection, Gate 1 refinement, and Day-2 change intake."""

from __future__ import annotations

import ast
import copy
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.specialists.spec_analyst.models import ComplexityEnum, DDLCIntakeSpec
from src.specialists.spec_analyst.prompts import (
    build_change_analysis_messages,
    build_gap_analysis_messages,
    build_refinement_messages,
)
from src.state.blackboard import (
    BuildLogEntry,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    ContractPointer,
    assert_contract_pointer_valid,
)
from src.state.projections import (
    SpecAnalystProjection,
    extract_design_impacting_requirements,
    summarize_parsed_spec,
)

# ---------------------------------------------------------------------------
# Instructor Structured Outputs (internal to Spec Analyst)
# ---------------------------------------------------------------------------


class SemanticGapAnalysisOutput(BaseModel):
    """Structured LLM output for Pass 2 semantic gap analysis."""

    has_open_gaps: bool = False
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    ambiguities_found: list[str] = Field(default_factory=list)
    inferred_complexity: Optional[ComplexityEnum] = None
    summary: str = ""


class SpecFieldPatch(BaseModel):
    """Targeted dotted-path update folded into parsed_spec."""

    field_path: str = Field(..., min_length=1)
    value: Any
    reason: str = ""


class AnswerClassification(BaseModel):
    """Impact classification for a single Gate 1 answer."""

    question_id: str = Field(..., min_length=1)
    design_impacting: bool = True
    resolved_requirement: Optional[str] = None


class SpecRefinementOutput(BaseModel):
    """Structured LLM output folding Gate 1 answers into parsed_spec."""

    patches: list[SpecFieldPatch] = Field(default_factory=list)
    answer_classifications: list[AnswerClassification] = Field(default_factory=list)
    summary_of_impact: str = ""


class ChangeAnalysisOutput(BaseModel):
    """Structured LLM output for Day-2 maintenance change intake."""

    affected_stages: list[str] = Field(default_factory=list)
    affected_contracts: list[str] = Field(default_factory=list)
    requires_dcm_migration: bool = False
    changed_impl_files: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Deterministic Helpers
# ---------------------------------------------------------------------------

_PATH_TOKEN_RE = re.compile(r"([^[.\]]+)|\[(\d+)\]")


def _apply_field_patch(spec_dict: dict[str, Any], field_path: str, value: Any) -> bool:
    """Safely mutate a nested dict/list path like 'governance.audit_requirements.enabled'."""
    tokens = _PATH_TOKEN_RE.findall(field_path.strip())
    if not tokens:
        return False

    cursor: Any = spec_dict
    parsed_steps: list[str | int] = []
    for key_part, idx_part in tokens:
        if key_part:
            parsed_steps.append(key_part)
        else:
            parsed_steps.append(int(idx_part))

    for step in parsed_steps[:-1]:
        if isinstance(step, str):
            if not isinstance(cursor, dict):
                return False
            if step not in cursor or cursor[step] is None:
                cursor[step] = {}
            cursor = cursor[step]
        else:
            if not isinstance(cursor, list) or step >= len(cursor):
                return False
            cursor = cursor[step]

    last = parsed_steps[-1]
    if isinstance(last, str) and isinstance(cursor, dict):
        cursor[last] = value
        return True
    if isinstance(last, int) and isinstance(cursor, list) and 0 <= last < len(cursor):
        cursor[last] = value
        return True
    return False


def infer_complexity(spec: DDLCIntakeSpec) -> ComplexityEnum:
    """Deterministically infer complexity when omitted from spec.architecture."""
    if spec.architecture.complexity is not None:
        return spec.architecture.complexity
    has_reflect = any(s.error_strategy == "reflect" for s in spec.stages)
    has_human_gate = bool(spec.governance.approval_gates) or any(
        s.autonomy == "human_required" for s in spec.stages
    )
    if len(spec.stages) >= 5 or (has_reflect and has_human_gate):
        return "complex"
    if len(spec.stages) >= 3 or has_reflect or has_human_gate:
        return "moderate"
    return "simple"


def detect_deterministic_gaps(
    spec: DDLCIntakeSpec,
    *,
    known_agent_names: Sequence[str] = (),
    known_component_names: Sequence[str] = (),
) -> tuple[list[ClarificationQuestion], list[str]]:
    """Run deterministic semantic checks from spec_template.md §6.2."""
    questions: list[ClarificationQuestion] = []
    findings: list[str] = []

    # 1. Contract chain continuity
    for idx in range(len(spec.stages) - 1):
        curr_stg = spec.stages[idx]
        next_stg = spec.stages[idx + 1]
        if (
            curr_stg.output_contract
            and next_stg.input_contract
            and curr_stg.output_contract != next_stg.input_contract
        ):
            msg = (
                f"Stage '{curr_stg.id}' outputs '{curr_stg.output_contract}' while "
                f"subsequent stage '{next_stg.id}' expects '{next_stg.input_contract}'."
            )
            findings.append(msg)
            questions.append(
                ClarificationQuestion(
                    question_id=f"gap_chain_{curr_stg.id}_{next_stg.id}",
                    category="contract_continuity",
                    field_path=f"stages[{idx + 1}].input_contract",
                    question=(
                        f"{msg} Is there an intermediate stage or should '{next_stg.id}' "
                        f"consume '{curr_stg.output_contract}'?"
                    ),
                    suggested_default=curr_stg.output_contract,
                    impact_area="architecture",
                )
            )

    # 2. Discovery / delivery infrastructure alignment
    if any(s.type == "discovery" for s in spec.stages) and not spec.infrastructure.sources:
        msg = "Discovery stage declared without any source in infrastructure.sources."
        findings.append(msg)
        questions.append(
            ClarificationQuestion(
                question_id="gap_infra_sources",
                category="infrastructure",
                field_path="infrastructure.sources",
                question="A discovery stage is defined, but infrastructure.sources is empty. What source system and connection reference should it inspect?",
                impact_area="infrastructure",
            )
        )

    if any(s.type == "delivery" for s in spec.stages) and not spec.infrastructure.destinations:
        msg = "Delivery stage declared without any target in infrastructure.destinations."
        findings.append(msg)
        questions.append(
            ClarificationQuestion(
                question_id="gap_infra_destinations",
                category="infrastructure",
                field_path="infrastructure.destinations",
                question="A delivery stage is defined, but infrastructure.destinations is empty. Where should generated artifacts be delivered?",
                impact_area="infrastructure",
            )
        )

    # 3. Governance completeness for confidential/restricted data
    if spec.governance.data_classification in {"confidential", "restricted"}:
        audit = spec.governance.audit_requirements
        if audit is None or not audit.enabled:
            msg = (
                f"Data classification is '{spec.governance.data_classification}', "
                "but governance.audit_requirements.enabled is false/unset."
            )
            findings.append(msg)
            questions.append(
                ClarificationQuestion(
                    question_id="gap_governance_audit",
                    category="governance",
                    field_path="governance.audit_requirements.enabled",
                    question=(
                        f"Data classification is '{spec.governance.data_classification}'. "
                        "Should comprehensive audit logging be enabled?"
                    ),
                    suggested_default="true (comprehensive)",
                    impact_area="governance",
                )
            )

    # 4. Event-driven scheduling trigger consistency
    sched = spec.infrastructure.scheduling
    if sched is not None and sched.type == "event_driven":
        has_event_source = any(
            s.system in {"git_repo", "http_api"} for s in spec.infrastructure.sources
        ) or bool(sched.description and len(sched.description.strip()) >= 15)
        if not has_event_source:
            msg = "Scheduling is event_driven without an explicit webhook/event trigger description."
            findings.append(msg)
            questions.append(
                ClarificationQuestion(
                    question_id="gap_event_trigger",
                    category="scheduling",
                    field_path="infrastructure.scheduling.description",
                    question="Scheduling is event_driven. What external event or webhook triggers this agent?",
                    impact_area="infrastructure",
                )
            )

    # 5. Cross-agent references against Tier 2 catalog (when catalog is populated)
    known_agents_set = {a.strip() for a in known_agent_names if a.strip()}
    if known_agents_set:
        for dep in spec.cross_agent.upstream_dependencies:
            if dep.agent not in known_agents_set:
                msg = f"Upstream agent '{dep.agent}' is not registered in Tier 2 agent_registry."
                findings.append(msg)
                questions.append(
                    ClarificationQuestion(
                        question_id=f"gap_upstream_{dep.agent}",
                        category="cross_agent",
                        field_path="cross_agent.upstream_dependencies",
                        question=(
                            f"Upstream agent '{dep.agent}' was not found in the Tier 2 catalog. "
                            "Is it already deployed under another name or planned?"
                        ),
                        impact_area="architecture",
                    )
                )
        for dep in spec.cross_agent.downstream_consumers:
            if dep.agent not in known_agents_set:
                msg = f"Downstream consumer '{dep.agent}' is not registered in Tier 2 agent_registry."
                findings.append(msg)
                questions.append(
                    ClarificationQuestion(
                        question_id=f"gap_downstream_{dep.agent}",
                        category="cross_agent",
                        field_path="cross_agent.downstream_consumers",
                        question=(
                            f"Downstream consumer '{dep.agent}' was not found in the Tier 2 catalog. "
                            "Should this consumer be registered or treated as external?"
                        ),
                        impact_area="architecture",
                    )
                )

    # 6. Shared component references against Tier 2 catalog
    known_comps_set = {c.strip() for c in known_component_names if c.strip()}
    if known_comps_set:
        for comp in spec.cross_agent.shared_components:
            if comp.name not in known_comps_set:
                msg = f"Shared component '{comp.name}' is not registered in Tier 2 component_registry."
                findings.append(msg)
                questions.append(
                    ClarificationQuestion(
                        question_id=f"gap_component_{comp.name}",
                        category="shared_component",
                        field_path="cross_agent.shared_components",
                        question=(
                            f"Shared component '{comp.name}' is not in the Tier 2 catalog. "
                            "Should the Builder create it as a new shared component or use an existing alternative?"
                        ),
                        impact_area="architecture",
                    )
                )

    return questions, findings


def _load_raw_spec_text(projection: SpecAnalystProjection) -> str:
    if projection.raw_spec_yaml is not None and projection.raw_spec_yaml.strip():
        return projection.raw_spec_yaml
    if projection.spec_pointer is not None:
        assert_contract_pointer_valid(projection.spec_pointer)
        uri = projection.spec_pointer.storage_uri
        if uri.startswith("file://"):
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            path = Path(url2pathname(unquote(urlparse(uri).path)))
        else:
            path = Path(uri)
        return path.read_text(encoding="utf-8")
    raise ValueError("SpecAnalystProjection contains neither raw_spec_yaml nor spec_pointer.")


def _format_validation_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "validation error")
        errors.append(f"{loc}: {msg}" if loc else msg)
    return errors


def _previously_asked_keys(rounds: Sequence[ClarificationRound]) -> tuple[set[str], set[str]]:
    asked_ids: set[str] = set()
    asked_paths: set[str] = set()
    for rnd in rounds:
        for q in rnd.questions:
            asked_ids.add(q.question_id)
            if q.field_path:
                asked_paths.add(q.field_path)
    return asked_ids, asked_paths


def _deduplicate_questions(
    candidates: Sequence[ClarificationQuestion],
    prior_rounds: Sequence[ClarificationRound],
) -> list[ClarificationQuestion]:
    asked_ids, asked_paths = _previously_asked_keys(prior_rounds)
    unique: list[ClarificationQuestion] = []
    seen_ids = set(asked_ids)
    for q in candidates:
        if q.question_id in seen_ids:
            continue
        if q.field_path and q.field_path in asked_paths:
            continue
        seen_ids.add(q.question_id)
        unique.append(q)
    return unique


# ---------------------------------------------------------------------------
# Spec Analyst Specialist
# ---------------------------------------------------------------------------


class SpecAnalystSpecialist:
    """Validates Day-0 YAML specs, detects gaps, folds Gate 1 answers, and analyzes Day-2 changes."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        *,
        run_llm_semantic_pass: bool = True,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.run_llm_semantic_pass = run_llm_semantic_pass

    def validate_and_parse(
        self,
        raw_yaml: str,
    ) -> tuple[Optional[DDLCIntakeSpec], Optional[dict[str, Any]], list[str]]:
        """Parse YAML text deterministically against DDLCIntakeSpec."""
        try:
            loaded = yaml.safe_load(raw_yaml)
        except yaml.YAMLError as exc:
            return None, None, [f"YAML syntax error: {exc}"]

        if not isinstance(loaded, Mapping):
            return None, None, ["Specification root must be a YAML mapping/object."]

        try:
            model = DDLCIntakeSpec.model_validate(dict(loaded))
        except ValidationError as exc:
            return None, None, _format_validation_errors(exc)

        normalized_dict = model.model_dump(mode="json")
        if normalized_dict["architecture"].get("complexity") is None:
            normalized_dict["architecture"]["complexity"] = infer_complexity(model)
        return model, normalized_dict, []

    def analyze(self, projection: SpecAnalystProjection) -> dict[str, Any]:
        """Run Pass 1 deterministic validation and Pass 2 gap analysis on a Day-0 spec."""
        if projection.parsed_spec is not None:
            try:
                spec_model = DDLCIntakeSpec.model_validate(projection.parsed_spec)
                parsed_dict = spec_model.model_dump(mode="json")
                if parsed_dict["architecture"].get("complexity") is None:
                    parsed_dict["architecture"]["complexity"] = infer_complexity(spec_model)
                errors: list[str] = []
            except ValidationError as exc:
                spec_model = None
                parsed_dict = None
                errors = _format_validation_errors(exc)
        else:
            raw_text = _load_raw_spec_text(projection)
            spec_model, parsed_dict, errors = self.validate_and_parse(raw_text)

        if spec_model is None or parsed_dict is None:
            return {
                "current_phase": "understand",
                "spec_validation_status": "rejected",
                "spec_rejected_errors": errors,
                "has_open_gaps": False,
                "build_logs": [
                    BuildLogEntry(
                        phase="understand",
                        actor="spec_analyst",
                        action="validate_spec",
                        status="failed",
                        details={"errors": errors},
                    )
                ],
            }

        det_questions, det_findings = detect_deterministic_gaps(
            spec_model,
            known_agent_names=projection.known_agent_names,
            known_component_names=projection.known_component_names,
        )

        llm_questions: list[ClarificationQuestion] = []
        llm_summary = ""
        if self.run_llm_semantic_pass:
            messages = build_gap_analysis_messages(projection, parsed_dict, det_findings)
            semantic_out = self.llm_client.complete_structured(
                specialist_name="spec_analyst",
                response_model=SemanticGapAnalysisOutput,
                messages=messages,
            )
            llm_questions = list(semantic_out.questions)
            llm_summary = semantic_out.summary
            if (
                semantic_out.inferred_complexity is not None
                and spec_model.architecture.complexity is None
            ):
                parsed_dict["architecture"]["complexity"] = semantic_out.inferred_complexity

        merged_questions = _deduplicate_questions(
            [*det_questions, *llm_questions],
            projection.clarification_rounds,
        )
        summary = summarize_parsed_spec(parsed_dict)

        if merged_questions:
            next_round_num = (
                max((r.round_number for r in projection.clarification_rounds), default=0) + 1
            )
            new_round = ClarificationRound(
                round_number=next_round_num,
                questions=merged_questions,
                resolved=False,
            )
            return {
                "current_phase": "understand",
                "target_agent_name": spec_model.agent.name,
                "spec_version": spec_model.agent.version,
                "spec_validation_status": "clarify",
                "spec_rejected_errors": [],
                "parsed_spec": parsed_dict,
                "parsed_spec_summary": summary,
                "has_open_gaps": True,
                "clarification_rounds": [new_round],
                "clarification_round_count": next_round_num,
                "build_logs": [
                    BuildLogEntry(
                        phase="understand",
                        actor="spec_analyst",
                        action="identify_gaps",
                        status="completed",
                        details={
                            "round": next_round_num,
                            "question_count": len(merged_questions),
                            "summary": llm_summary,
                        },
                    )
                ],
            }

        return {
            "current_phase": "understand",
            "target_agent_name": spec_model.agent.name,
            "spec_version": spec_model.agent.version,
            "spec_validation_status": "accepted",
            "spec_rejected_errors": [],
            "parsed_spec": parsed_dict,
            "parsed_spec_summary": summary,
            "has_open_gaps": False,
            "build_logs": [
                BuildLogEntry(
                    phase="understand",
                    actor="spec_analyst",
                    action="accept_spec",
                    status="completed",
                    details={"agent_name": spec_model.agent.name},
                )
            ],
        }

    def incorporate_answers(
        self,
        projection: SpecAnalystProjection,
        answers: Sequence[ClarificationAnswer],
    ) -> dict[str, Any]:
        """Fold Gate 1 user answers into parsed_spec and classify design-impacting updates."""
        if projection.parsed_spec is None:
            raise ValueError("Cannot incorporate clarification answers before parsed_spec exists.")
        if not projection.clarification_rounds:
            raise ValueError("No clarification_rounds exist on projection to answer.")

        latest_round = projection.clarification_rounds[-1]
        updated_spec = copy.deepcopy(projection.parsed_spec)
        answer_list = list(answers)

        if self.run_llm_semantic_pass:
            messages = build_refinement_messages(updated_spec, latest_round, answer_list)
            refined = self.llm_client.complete_structured(
                specialist_name="spec_analyst",
                response_model=SpecRefinementOutput,
                messages=messages,
            )
            for patch in refined.patches:
                _apply_field_patch(updated_spec, patch.field_path, patch.value)

            cls_map = {c.question_id: c for c in refined.answer_classifications}
            enriched_answers: list[ClarificationAnswer] = []
            for ans in answer_list:
                cls_entry = cls_map.get(ans.question_id)
                if cls_entry is not None:
                    enriched_answers.append(
                        ans.model_copy(
                            update={
                                "design_impacting": cls_entry.design_impacting,
                                "resolved_requirement": cls_entry.resolved_requirement
                                or ans.resolved_requirement,
                            }
                        )
                    )
                else:
                    enriched_answers.append(ans)
            summary_of_impact = refined.summary_of_impact
        else:
            enriched_answers = answer_list
            summary_of_impact = "Applied user clarification answers."

        resolved_round = latest_round.model_copy(
            update={
                "answers": enriched_answers,
                "resolved": True,
                "summary_of_impact": summary_of_impact,
            }
        )

        # Re-validate updated spec deterministically; fall back to prior parsed_spec if LLM patch broke schema
        try:
            validated = DDLCIntakeSpec.model_validate(updated_spec)
            final_spec_dict = validated.model_dump(mode="json")
        except ValidationError:
            validated = DDLCIntakeSpec.model_validate(projection.parsed_spec)
            final_spec_dict = validated.model_dump(mode="json")

        enriched_reqs = extract_design_impacting_requirements([resolved_round])
        summary = summarize_parsed_spec(final_spec_dict)

        return {
            "current_phase": "understand",
            "parsed_spec": final_spec_dict,
            "parsed_spec_summary": summary,
            "clarification_rounds": [resolved_round],
            "enriched_requirements": enriched_reqs,
            "has_open_gaps": False,
            "spec_validation_status": "accepted",
            "build_logs": [
                BuildLogEntry(
                    phase="understand",
                    actor="spec_analyst",
                    action="incorporate_answers",
                    status="completed",
                    details={
                        "round": resolved_round.round_number,
                        "answers_count": len(enriched_answers),
                        "design_impacting_count": len(enriched_reqs),
                    },
                )
            ],
        }

    def analyze_change_request(self, projection: SpecAnalystProjection) -> dict[str, Any]:
        """Analyze a Day-2 natural-language or PR change request against active impl_docs/."""
        if projection.change_request is None:
            raise ValueError("change_request is required in change mode.")

        req = projection.change_request
        if self.run_llm_semantic_pass and not (
            req.affected_stages or req.affected_contracts or req.changed_impl_files
        ):
            messages = build_change_analysis_messages(projection)
            out = self.llm_client.complete_structured(
                specialist_name="spec_analyst",
                response_model=ChangeAnalysisOutput,
                messages=messages,
            )
            updated_req = req.model_copy(
                update={
                    "affected_stages": out.affected_stages,
                    "affected_contracts": out.affected_contracts,
                    "requires_dcm_migration": out.requires_dcm_migration,
                    "changed_impl_files": out.changed_impl_files,
                }
            )
        else:
            updated_req = req

        return {
            "current_phase": "change_analysis",
            "change_request": updated_req,
            "build_logs": [
                BuildLogEntry(
                    phase="change_analysis",
                    actor="spec_analyst",
                    action="analyze_change_request",
                    status="completed",
                    details={
                        "agent_id": updated_req.agent_id,
                        "affected_stages": updated_req.affected_stages,
                        "affected_contracts": updated_req.affected_contracts,
                    },
                )
            ],
        }

    @staticmethod
    def sync_stage_doc_from_tool(
        stage_doc: Mapping[str, Any],
        tool_source: str,
        function_name: str,
    ) -> dict[str, Any]:
        """Deterministically reverse-sync a 2 AM tool hotfix docstring/signature into stage YAML (Decision 25)."""
        tree = ast.parse(tool_source)
        target_fn: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                target_fn = node
                break

        if target_fn is None:
            raise ValueError(f"Function '{function_name}' not found in provided tool source.")

        docstring = ast.get_docstring(target_fn) or ""
        args_parts: list[str] = []
        for arg in target_fn.args.args:
            ann = ast.unparse(arg.annotation) if arg.annotation is not None else "Any"
            args_parts.append(f"{arg.arg}: {ann}")
        for kwarg in target_fn.args.kwonlyargs:
            ann = ast.unparse(kwarg.annotation) if kwarg.annotation is not None else "Any"
            args_parts.append(f"{kwarg.arg}: {ann}")
        ret = ast.unparse(target_fn.returns) if target_fn.returns is not None else "Any"
        inferred_sig = f"({', '.join(args_parts)}) -> {ret}"

        updated = copy.deepcopy(dict(stage_doc))
        processing = updated.setdefault("processing", {})
        for tool in processing.get("tools", []):
            if isinstance(tool, dict) and tool.get("handler", "").endswith(f"::{function_name}"):
                tool["signature"] = inferred_sig
        if docstring.strip():
            processing["logic_description"] = docstring.strip()
        return updated


def create_spec_pointer_from_file(spec_path: Path, version: str = "1.0.0") -> ContractPointer:
    """Helper constructing a tamper-evident Claim Check pointer for a Day-0 spec YAML file."""
    from src.state.blackboard import build_contract_pointer_from_bytes

    content = spec_path.read_bytes()
    return build_contract_pointer_from_bytes(
        contract_name="day0_spec_yaml",
        content=content,
        storage_uri=spec_path.resolve().as_uri(),
        contract_version=version,
        file_manifest=[spec_path.name],
    )
