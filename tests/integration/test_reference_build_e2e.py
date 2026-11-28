"""End-to-end Day-0 and Day-2 reference build tests for ``dbt_pipeline_builder_agent``."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import psycopg
import pytest
import yaml
from pydantic import BaseModel
from src.coordinator.dependencies import CoordinatorDeps
from src.coordinator.graph import load_impl_docs_package_from_dir
from src.core.settings import DEFAULT_PROJECT_ROOT, get_env_settings
from src.metadata.callbacks import MetadataCallbackHandler
from src.metadata.db import MetadataDatabase
from src.rag.store.pgvector_store import PgVectorRAGStore, create_rag_pool
from src.specialists.architect.specialist import ArchitectSpecialist, ArchitectureDesignDraft
from src.specialists.builder.leaf_coder.patcher import LeafToolSynthesis as PatchLeafToolSynthesis
from src.specialists.builder.leaf_coder.synthesizer import (
    LeafPromptSynthesis,
    LeafToolSynthesis,
)
from src.specialists.builder.specialist import BuilderSpecialist
from src.specialists.documentarian.models import (
    AgentDependencies,
    AgentManifestArchitecture,
    AgentManifestDoc,
    AgentManifestIdentity,
    AgentMetadataConfig,
    AgentPipelineTopology,
    AgentScheduleConfig,
    ConfigBodyDoc,
    ConfigDocument,
    ContractBodyDoc,
    ContractDocument,
    ContractFieldDoc,
    ErrorScenario,
    LLMConfig,
    PipelineStageEdge,
    SafetyValves,
    SharedComponentDependencyRef,
    StageDocument,
    StageHeader,
    StageInput,
    StageOutput,
    StagePerformance,
    StageProcessing,
    TestsBodyDoc,
    TestsDocument,
    ToolConfig,
)
from src.specialists.documentarian.specialist import (
    DocumentarianSpecialist,
    ImplDocSynthesisOutput,
)
from src.specialists.researcher.specialist import ResearchAnalysisOutput, ResearcherSpecialist
from src.specialists.spec_analyst.models import DDLCIntakeSpec
from src.specialists.spec_analyst.specialist import (
    AnswerClassification,
    ChangeAnalysisOutput,
    SemanticGapAnalysisOutput,
    SpecAnalystSpecialist,
    SpecRefinementOutput,
)
from src.specialists.validator.specialist import ValidatorSpecialist
from src.state.blackboard import (
    ChangeRequestContext,
    ClarificationQuestion,
    StageDesignSpec,
    initial_blackboard_state,
)
from src.state.checkpointer import (
    arecover_thread_state,
    create_memory_checkpointer,
    ensure_checkpointer_setup,
    make_resume_command,
    open_async_checkpointer,
    run_async_selector,
)
from src.ui.app import drive_coordinator_until_pause_or_done, load_canonical_reference_spec


def _init_temp_git_repo(repo_root: Path) -> None:
    """Initialize a clean Git repository inside ``repo_root`` with an initial baseline commit."""
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "ddlc-test@example.com"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "DDLC Reference Test"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    (repo_root / "README.md").write_text("# Temporary DDLC Reference Workspace\n", encoding="utf-8")
    (repo_root / "knowledge" / "past_decisions").mkdir(parents=True, exist_ok=True)
    (repo_root / "agents").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial studio workspace"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )


def _current_git_sha(repo_root: Path, ref: str = "HEAD") -> str:
    res = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def _git_commit_count(repo_root: Path, ref: str = "HEAD") -> int:
    res = subprocess.run(
        ["git", "rev-list", "--count", ref],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return int(res.stdout.strip())


def _build_contract_docs() -> dict[str, ContractDocument]:
    specs = [
        (
            "source_database_reference",
            "input",
            [("database_name", "string"), ("schema_names", "json"), ("connection_ref", "string")],
        ),
        ("source_schema_profile", "internal", [("database_name", "string"), ("tables", "json")]),
        ("transformation_plan", "internal", [("models", "json"), ("target_dialect", "string")]),
        ("dbt_artifact_bundle", "internal", [("bundle_uri", "string"), ("model_files", "json")]),
        ("compilation_report", "internal", [("compiled_ok", "boolean"), ("diagnostics", "json")]),
        ("reviewed_artifact_bundle", "internal", [("approved_by", "string"), ("bundle_uri", "string")]),
        (
            "delivery_manifest",
            "output",
            [("git_branch", "string"), ("commit_sha", "string"), ("pull_request_url", "string")],
        ),
    ]
    contracts: dict[str, ContractDocument] = {}
    for name, direction, fields in specs:
        contracts[name] = ContractDocument(
            contract=ContractBodyDoc(
                name=name,
                version="1.0.0",
                description=f"Contract {name} for dbt_pipeline_builder_agent.",
                direction=direction,  # type: ignore[arg-type]
                storage_mode="inline",
                owner="analytics-engineering-team",
                classification="internal",
                fields=[
                    ContractFieldDoc(name=fname, type=ftype, nullable=False, description=f"Field {fname}")  # type: ignore[arg-type]
                    for fname, ftype in fields
                ],
            )
        )
    return contracts


class _ReferenceSpecLLMClient:
    """Deterministic StructuredLLMClient powering all real DDLC specialists in E2E tests."""

    def __init__(self, *, target_agent_name: str = "dbt_pipeline_builder_agent") -> None:
        self.target_agent_name = target_agent_name
        self.calls: dict[str, int] = {}

    def complete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        model_name = response_model.__name__
        self.calls[model_name] = self.calls.get(model_name, 0) + 1
        joined_prompts = "\n".join(str(m.get("content") or "") for m in messages)

        if model_name == "SemanticGapAnalysisOutput":
            is_first = self.calls[model_name] == 1
            return SemanticGapAnalysisOutput(
                has_open_gaps=is_first,
                questions=[
                    ClarificationQuestion(
                        question_id="q_audit",
                        category="governance",
                        field_path="governance.audit_requirements.log_level",
                        question="Enforce comprehensive Tier 1 audit logging on dbt generation?",
                        suggested_default="comprehensive",
                        impact_area="Compliance and telemetry",
                    )
                ]
                if is_first
                else [],
                ambiguities_found=[],
                inferred_complexity="complex",
                summary="Canonical 6-stage dbt pipeline builder spec parsed cleanly.",
            )  # type: ignore[return-value]

        if model_name == "SpecRefinementOutput":
            return SpecRefinementOutput(
                patches=[],
                answer_classifications=[
                    AnswerClassification(
                        question_id="q_audit",
                        design_impacting=False,
                        resolved_requirement="Enforce comprehensive Tier 1 audit logging.",
                    )
                ],
                summary_of_impact="Confirmed comprehensive audit logging.",
            )  # type: ignore[return-value]

        if model_name == "ChangeAnalysisOutput":
            return ChangeAnalysisOutput(
                affected_stages=["dry_run"],
                affected_contracts=[],
                requires_dcm_migration=False,
                changed_impl_files=["stages.yaml"],
                summary="Update dry_run stage to enforce not_null model assertions.",
            )  # type: ignore[return-value]

        if model_name == "ResearchAnalysisOutput":
            return ResearchAnalysisOutput(
                verdict="feasible",
                summary="Required stack (litellm, instructor, sqlglot, dbt-core) verified feasible.",
                tool_evaluations=[],
                policy_constraints=[],
                risks=[],
            )  # type: ignore[return-value]

        if model_name == "ArchitectureDesignDraft":
            return ArchitectureDesignDraft(
                pattern="stateful_pipeline_with_gates",
                runtime="langgraph",
                autonomy_level="semi_autonomous",
                summary="6-stage stateful LangGraph dbt pipeline builder with review gate.",
                stages=[
                    StageDesignSpec(
                        stage_id="discover",
                        name="Discover Source Schema",
                        stage_type="discovery",
                        processing_type="deterministic_tool",
                        autonomy="autonomous",
                        input_contract="source_database_reference",
                        output_contract="source_schema_profile",
                        tools=["schema_inspector_v1", "inspect_information_schema"],
                        shared_components=[{"component_id": "schema_inspector_v1", "usage_type": "import"}],
                    ),
                    StageDesignSpec(
                        stage_id="plan",
                        name="Plan Transformation DAG",
                        stage_type="reasoning",
                        processing_type="llm_reasoning",
                        autonomy="autonomous",
                        input_contract="source_schema_profile",
                        output_contract="transformation_plan",
                    ),
                    StageDesignSpec(
                        stage_id="generate",
                        name="Generate dbt Models",
                        stage_type="code_generation",
                        processing_type="llm_reasoning",
                        autonomy="autonomous",
                        input_contract="transformation_plan",
                        output_contract="dbt_artifact_bundle",
                    ),
                    StageDesignSpec(
                        stage_id="dry_run",
                        name="Dry Run & SQLGlot Validate",
                        stage_type="sandbox_testing",
                        processing_type="deterministic_tool",
                        autonomy="autonomous",
                        input_contract="dbt_artifact_bundle",
                        output_contract="compilation_report",
                        tools=["run_sqlglot_validate"],
                    ),
                    StageDesignSpec(
                        stage_id="review",
                        name="Human Diff Review",
                        stage_type="human_review",
                        processing_type="human_gate",
                        autonomy="human_required",
                        input_contract="compilation_report",
                        output_contract="reviewed_artifact_bundle",
                    ),
                    StageDesignSpec(
                        stage_id="deliver",
                        name="Deliver PR & Manifest",
                        stage_type="delivery",
                        processing_type="deterministic_tool",
                        autonomy="autonomous",
                        input_contract="reviewed_artifact_bundle",
                        output_contract="delivery_manifest",
                        tools=["git_deliverer_v1"],
                        shared_components=[{"component_id": "git_deliverer_v1", "usage_type": "import"}],
                    ),
                ],
                contracts_to_define=list(_build_contract_docs().keys()),
                reused_components=[
                    {
                        "component_id": "schema_inspector_v1",
                        "component_name": "schema_inspector_v1",
                        "usage_type": "import",
                    },
                    {
                        "component_id": "git_deliverer_v1",
                        "component_name": "git_deliverer_v1",
                        "usage_type": "import",
                    },
                    {
                        "component_id": "metadata_telemetry_callback",
                        "component_name": "metadata_telemetry_callback",
                        "usage_type": "import",
                    },
                ],
                tier_3_schema_required=False,
                tier_3_tables=[],
            )  # type: ignore[return-value]

        if model_name == "ImplDocSynthesisOutput":
            is_day2_change = "non_null" in joined_prompts.lower() or "mode: change" in joined_prompts.lower()
            dry_run_desc = (
                "Enforce not_null schema assertion checks and SQLGlot AST validation across generated models."
                if is_day2_change
                else "Validate SQLGlot AST syntax and compile dbt bundle inside isolated sandbox."
            )
            contracts = _build_contract_docs()
            manifest = AgentManifestDoc(
                agent=AgentManifestIdentity(
                    name=self.target_agent_name,
                    display_name="dbt Pipeline Builder Agent",
                    version="1.0.1" if is_day2_change else "1.0.0",
                    description="Generates, validates, reviews, and commits modular dbt pipelines.",
                    owner="analytics-engineering-team",
                    tags=["dbt", "analytics", "langgraph"],
                ),
                architecture=AgentManifestArchitecture(
                    pattern="stateful_pipeline_with_gates",
                    runtime="langgraph",
                    autonomy_level="semi_autonomous",
                ),
                pipeline=AgentPipelineTopology(
                    stages=[
                        PipelineStageEdge(stage_id="discover", next="plan"),
                        PipelineStageEdge(stage_id="plan", next="generate"),
                        PipelineStageEdge(stage_id="generate", next="dry_run"),
                        PipelineStageEdge(stage_id="dry_run", next="review"),
                        PipelineStageEdge(stage_id="review", next="deliver"),
                        PipelineStageEdge(stage_id="deliver", next=None),
                    ]
                ),
                dependencies=AgentDependencies(
                    tools=[],
                    shared_components=[
                        SharedComponentDependencyRef(
                            component_id="schema_inspector_v1",
                            version="1.0.0",
                            used_by=["discover"],
                            usage_type="import",
                            description="Shared OLTP database schema inspector",
                        ),
                        SharedComponentDependencyRef(
                            component_id="git_deliverer_v1",
                            version="2.1.0",
                            used_by=["deliver"],
                            usage_type="import",
                            description="Shared Git branch and PR automation wrapper",
                        ),
                        SharedComponentDependencyRef(
                            component_id="metadata_telemetry_callback",
                            version="1.0.0",
                            used_by=["discover", "deliver"],
                            usage_type="import",
                            description="Tier 1 operational telemetry callback",
                        ),
                    ],
                ),
                metadata=AgentMetadataConfig(tier_3_schema=None, tier_3_tables=[], writes_to={}),
                schedule=AgentScheduleConfig(type="on_demand", description="Triggered on demand"),
            )
            default_err = {"default": ErrorScenario(action="retry", max_retries=2, retry_delay_seconds=5)}
            stages = {
                "discover": StageDocument(
                    stage=StageHeader(
                        id="discover",
                        name="Discover Source Schema",
                        type="discovery",
                        description="Introspect OLTP schema metadata.",
                        autonomy="autonomous",
                    ),
                    input=StageInput(contract="source_database_reference", source={"origin": "user_input"}),
                    output=StageOutput(contract="source_schema_profile", destination={"next_stage": "plan"}),
                    processing=StageProcessing(
                        processing_type="deterministic_tool",
                        tools=[
                            ToolConfig(
                                name="schema_inspector_v1",
                                type="shared_component",
                                purpose="Resolve shared schema inspector connector",
                                access="read_only",
                                configuration={"component_id": "schema_inspector_v1"},
                            ),
                            ToolConfig(
                                name="inspect_information_schema",
                                type="custom_code",
                                handler="tools/discover_tools.py::inspect_information_schema",
                                signature="(source_database_reference: dict[str, Any]) -> dict[str, Any]",
                                purpose="Build source schema profile from database reference",
                                access="read_only",
                            ),
                        ],
                        safety_valves=SafetyValves(),
                        logic_description="Inspect source database schema cleanly.",
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=30, timeout_seconds=120),
                ),
                "plan": StageDocument(
                    stage=StageHeader(
                        id="plan",
                        name="Plan Transformation DAG",
                        type="reasoning",
                        description="Plan dbt model tiers and join DAG.",
                        autonomy="autonomous",
                    ),
                    input=StageInput(contract="source_schema_profile", source={"stage": "discover"}),
                    output=StageOutput(contract="transformation_plan", destination={"next_stage": "generate"}),
                    processing=StageProcessing(
                        processing_type="llm_reasoning",
                        llm_config=LLMConfig(
                            model_tier="reasoning",
                            prompt_template_ref="prompts/plan_models.jinja2",
                            system_prompt="Plan modular staging/intermediate/marts dbt DAG.",
                            output_schema="transformation_plan",
                        ),
                        safety_valves=SafetyValves(),
                        logic_description="Design dbt DAG models.",
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=30, timeout_seconds=120),
                ),
                "generate": StageDocument(
                    stage=StageHeader(
                        id="generate",
                        name="Generate dbt Models",
                        type="code_generation",
                        description="Generate SQL models and schema.yml tests.",
                        autonomy="autonomous",
                    ),
                    input=StageInput(contract="transformation_plan", source={"stage": "plan"}),
                    output=StageOutput(contract="dbt_artifact_bundle", destination={"next_stage": "dry_run"}),
                    processing=StageProcessing(
                        processing_type="llm_reasoning",
                        llm_config=LLMConfig(
                            model_tier="reasoning",
                            prompt_template_ref="prompts/generate_dbt_models.jinja2",
                            system_prompt="Generate valid dbt Jinja-SQL models and tests.",
                            output_schema="dbt_artifact_bundle",
                        ),
                        safety_valves=SafetyValves(),
                        logic_description="Render dbt SQL and schema.yml bundle.",
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=30, timeout_seconds=120),
                ),
                "dry_run": StageDocument(
                    stage=StageHeader(
                        id="dry_run",
                        name="Dry Run & Validate",
                        type="sandbox_testing",
                        description=dry_run_desc,
                        autonomy="autonomous",
                    ),
                    input=StageInput(contract="dbt_artifact_bundle", source={"stage": "generate"}),
                    output=StageOutput(contract="compilation_report", destination={"next_stage": "review"}),
                    processing=StageProcessing(
                        processing_type="deterministic_tool",
                        tools=[
                            ToolConfig(
                                name="run_sqlglot_validate",
                                type="custom_code",
                                handler="tools/dry_run_tools.py::run_sqlglot_validate",
                                signature="(dbt_artifact_bundle: dict[str, Any]) -> dict[str, Any]",
                                purpose="Validate SQLGlot AST and dbt model tests",
                                access="read_only",
                            )
                        ],
                        safety_valves=SafetyValves(),
                        logic_description=dry_run_desc,
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=30, timeout_seconds=120),
                ),
                "review": StageDocument(
                    stage=StageHeader(
                        id="review",
                        name="Human Review Gate",
                        type="human_review",
                        description="Pause via LangGraph interrupt() for engineer sign-off.",
                        autonomy="human_required",
                    ),
                    input=StageInput(contract="compilation_report", source={"stage": "dry_run"}),
                    output=StageOutput(contract="reviewed_artifact_bundle", destination={"next_stage": "deliver"}),
                    processing=StageProcessing(
                        processing_type="human_gate",
                        safety_valves=SafetyValves(),
                        logic_description="Await human approval before Git delivery.",
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=60, timeout_seconds=3600),
                ),
                "deliver": StageDocument(
                    stage=StageHeader(
                        id="deliver",
                        name="Deliver Git PR",
                        type="delivery",
                        description="Commit validated models and open Pull Request.",
                        autonomy="autonomous",
                    ),
                    input=StageInput(contract="reviewed_artifact_bundle", source={"stage": "review"}),
                    output=StageOutput(contract="delivery_manifest", destination={"target": "git_repo"}),
                    processing=StageProcessing(
                        processing_type="deterministic_tool",
                        tools=[
                            ToolConfig(
                                name="git_deliverer_v1",
                                type="shared_component",
                                purpose="Create Git branch and commit bundle",
                                access="side_effecting",
                                requires_approval=True,
                                configuration={"component_id": "git_deliverer_v1"},
                            )
                        ],
                        safety_valves=SafetyValves(),
                        logic_description="Deliver reviewed dbt artifacts to Git.",
                    ),
                    error_strategy=default_err,
                    performance=StagePerformance(expected_duration_seconds=30, timeout_seconds=120),
                ),
            }
            return ImplDocSynthesisOutput(
                agent_manifest=manifest,
                stages=list(stages.values()),
                contracts=list(contracts.values()),
                config_doc=ConfigDocument(
                    config=ConfigBodyDoc(
                        defaults={"dbt_target_dialect": "postgres", "sandbox_mode": "duckdb"},
                        environments={"dev": {"sandbox_mode": "duckdb"}},
                        secrets=[],
                    )
                ),
                tests_doc=TestsDocument(tests=TestsBodyDoc(contract_tests=[], custom_tests=[])),
                tier3_contract_yaml=None,
                summary="Synthesized full 6-stage impl_docs package.",
            )  # type: ignore[return-value]

        if response_model in {LeafToolSynthesis, PatchLeafToolSynthesis}:
            if "inspect_information_schema" in joined_prompts:
                tool_code = (
                    '"""Discovery leaf tool for source schema introspection."""\n\n'
                    "from __future__ import annotations\n\n"
                    "from typing import Any\n\n\n"
                    "def inspect_information_schema(source_database_reference: dict[str, Any]) -> dict[str, Any]:\n"
                    '    """Return structured schema profile dictionary for downstream dbt planning."""\n'
                    '    db_name = str(source_database_reference.get("database_name") or "default_db")\n'
                    '    return {"database_name": db_name, "tables": [{"table_name": "orders", "schema_name": "public", "columns": []}]}\n'
                )
                unit_test_code = (
                    '"""Unit tests for inspect_information_schema."""\n\n'
                    "from src.tools.discover_tools import inspect_information_schema\n\n\n"
                    "def test_inspect_information_schema_basic() -> None:\n"
                    '    out = inspect_information_schema({"database_name": "prod_oltp", "schema_names": ["public"], "connection_ref": "config://db"})\n'
                    '    assert out["database_name"] == "prod_oltp"\n'
                    '    assert len(out["tables"]) == 1\n'
                )
                return response_model(tool_code=tool_code, unit_test_code=unit_test_code, summary="Discover tool synthesized")  # type: ignore[call-arg]

            is_patch = "patch" in joined_prompts.lower() or "non_null" in joined_prompts.lower()
            extra_field = '    out["non_null_enforced"] = True\n' if is_patch else ""
            tool_code = (
                '"""Dry-run SQLGlot & dbt sandbox validation leaf tool."""\n\n'
                "from __future__ import annotations\n\n"
                "from typing import Any\n\n\n"
                "def run_sqlglot_validate(dbt_artifact_bundle: dict[str, Any]) -> dict[str, Any]:\n"
                '    """Validate generated SQL artifact bundle cleanly."""\n'
                '    out: dict[str, Any] = {"compiled_ok": True, "diagnostics": [], "bundle": dbt_artifact_bundle}\n'
                f"{extra_field}"
                "    return out\n"
            )
            assert_extra = '    assert out["non_null_enforced"] is True\n' if is_patch else ""
            unit_test_code = (
                '"""Unit tests for run_sqlglot_validate."""\n\n'
                "from src.tools.dry_run_tools import run_sqlglot_validate\n\n\n"
                "def test_run_sqlglot_validate_basic() -> None:\n"
                '    out = run_sqlglot_validate({"bundle_uri": "memory://bundle", "model_files": ["stg_orders.sql"]})\n'
                '    assert out["compiled_ok"] is True\n'
                f"{assert_extra}"
            )
            return response_model(tool_code=tool_code, unit_test_code=unit_test_code, summary="Dry-run tool synthesized")  # type: ignore[call-arg]

        if model_name == "LeafPromptSynthesis":
            return LeafPromptSynthesis(
                prompt_template="Role: Analytics Engineer\nStage Task: Execute dbt step safely on {{ input }}.\n",
                summary="Prompt template synthesized.",
            )  # type: ignore[return-value]

        raise AssertionError(f"Unexpected structured completion request: {specialist_name} -> {model_name}")

    async def acomplete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        return self.complete_structured(
            specialist_name=specialist_name,
            response_model=response_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )


def _make_real_deps(
    *,
    llm_client: _ReferenceSpecLLMClient,
    workspace_root: Path,
    enable_tier2: bool = False,
    enable_rag_indexing: bool = False,
    embedder: Optional[Any] = None,
) -> CoordinatorDeps:
    return CoordinatorDeps(
        spec_analyst=SpecAnalystSpecialist(llm_client=llm_client, run_llm_semantic_pass=True),
        researcher=ResearcherSpecialist(llm_client=llm_client),
        architect=ArchitectSpecialist(llm_client=llm_client),
        documentarian=DocumentarianSpecialist(llm_client=llm_client),
        builder=BuilderSpecialist(llm_client=llm_client),
        validator=ValidatorSpecialist(llm_client=llm_client, run_tests=True, test_timeout_seconds=60),
        workspace_root=workspace_root,
        agents_output_dir=workspace_root / "agents",
        knowledge_dir=workspace_root / "knowledge",
        staging_root=workspace_root / "sandbox" / "runs",
        enable_tier2_registration=enable_tier2,
        enable_rag_indexing=enable_rag_indexing,
        enable_rag_retrieval=False,
        enable_git_delivery=True,
        enable_tier3_migration=False,
        strict_delivery=True,
        embedder=embedder,
    )


def test_canonical_reference_spec_schema_validation() -> None:
    """Canonical dbt_pipeline_builder_agent.yaml validates cleanly against DDLCIntakeSpec."""
    raw_yaml, parsed = load_canonical_reference_spec()
    assert raw_yaml.strip()
    validated = DDLCIntakeSpec.model_validate(parsed)

    assert validated.agent.name == "dbt_pipeline_builder_agent"
    assert validated.architecture.runtime_preference == "langgraph"
    assert validated.architecture.autonomy_level == "semi_autonomous"
    assert [s.id for s in validated.stages] == ["discover", "plan", "generate", "dry_run", "review", "deliver"]
    assert len(validated.contracts) == 7
    assert validated.governance.approval_gates == ["review"]
    shared_names = {sc.name for sc in validated.cross_agent.shared_components}
    assert {"metadata_telemetry_callback", "git_deliverer_v1"}.issubset(shared_names)


@pytest.mark.asyncio
async def test_day0_full_8_phases_and_day2_hotfix_preservation_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execute full 8-phase Day-0 build and Day-2 change flow verifying hotfix & Git commits."""
    _init_temp_git_repo(tmp_path)
    raw_yaml, parsed = load_canonical_reference_spec()
    llm_client = _ReferenceSpecLLMClient(target_agent_name="dbt_pipeline_builder_agent")
    deps = _make_real_deps(llm_client=llm_client, workspace_root=tmp_path)
    monkeypatch.setattr(deps, "get_metadata_db", lambda: None)

    cp = create_memory_checkpointer()
    day0_gates: list[str] = []

    async def _approve_gates(payload: Mapping[str, Any], *, reviewer: str = "e2e-reviewer") -> dict[str, Any]:
        gate = str(payload.get("gate"))
        day0_gates.append(gate)
        if gate == "clarification":
            return {"decision": "approved", "answers": {"q_audit": "comprehensive"}, "reviewer": reviewer}
        return {"decision": "approved", "reviewer": reviewer, "feedback": None}

    init_day0 = initial_blackboard_state(
        run_id="run-ref-day0",
        thread_id="thread-ref-day0",
        mode="build",
        raw_spec_yaml=raw_yaml,
        spec_version=str(parsed["agent"]["version"]),
        target_agent_name="dbt_pipeline_builder_agent",
    )
    day0_res = await drive_coordinator_until_pause_or_done(
        initial_or_command=init_day0,
        thread_id="thread-ref-day0",
        run_id="run-ref-day0",
        deps=deps,
        checkpointer=cp,
        reviewer="e2e-reviewer",
        gate_handler=_approve_gates,
        stream_to_chainlit=False,
        notify_fn=lambda _: asyncio.sleep(0),
    )

    assert day0_res.status == "completed", day0_res.error
    assert day0_gates == ["clarification", "architecture_review", "impl_doc_review"]

    manifest = day0_res.final_state["delivery_manifest"]
    manifest_dict = manifest.model_dump() if hasattr(manifest, "model_dump") else dict(manifest)
    agent_dir = tmp_path / "agents" / "dbt_pipeline_builder_agent"
    branch_1 = str(manifest_dict["git_branch"])
    commit_1 = str(manifest_dict["git_commit_sha"])
    assert (agent_dir / ".git").exists()
    assert commit_1 and commit_1 == _current_git_sha(agent_dir, branch_1)
    assert _git_commit_count(agent_dir, branch_1) >= 1

    assert (agent_dir / "impl_docs" / "agent.yaml").is_file()
    assert (agent_dir / "src" / "graph.py").is_file()
    assert (agent_dir / "src" / "tools" / "discover_tools.py").is_file()
    assert (agent_dir / "src" / "tools" / "dry_run_tools.py").is_file()
    assert list((tmp_path / "knowledge" / "past_decisions").glob("ADR-*.md"))

    # Verify generated graph references _shared.resolve_component and imports cleanly in isolation.
    graph_src = (agent_dir / "src" / "graph.py").read_text(encoding="utf-8")
    assert "resolve_component" in graph_src and "schema_inspector_v1" in graph_src
    run_env = dict(os.environ)
    run_env["PYTHONPATH"] = os.pathsep.join([str(agent_dir.resolve()), str(DEFAULT_PROJECT_ROOT.resolve())])
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from _shared import resolve_component; "
                "from src.graph import build_graph, invoke_shared_component, run_agent; "
                "schema_res = resolve_component('schema_inspector_v1'); "
                "git_res = resolve_component('git_deliverer_v1', run_id='probe1234'); "
                "assert schema_res['tables'] and 'branch_name' in git_res; "
                "assert callable(build_graph) and callable(run_agent); "
                "comp_res = invoke_shared_component('schema_inspector_v1', contracts={'source_database_reference': {'database_name': 'demo', 'schema_names': ['public']}}); "
                "assert comp_res['database_name'] == 'demo' and comp_res['tables']"
            ),
        ],
        cwd=str(agent_dir),
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"

    # Inject a 2 AM emergency human hotfix into untouched discover leaf module before Day-2 change!
    discover_tool_path = agent_dir / "src" / "tools" / "discover_tools.py"
    hotfix_marker = "# 2AM_HUMAN_HOTFIX_MARKER: emergency OLTP read-replica timeout safeguard"
    discover_tool_path.write_text(
        discover_tool_path.read_text(encoding="utf-8") + f"\n{hotfix_marker}\n",
        encoding="utf-8",
    )

    baseline_pkg = load_impl_docs_package_from_dir(agent_dir / "impl_docs")
    day2_gates: list[str] = []

    async def _approve_day2(payload: Mapping[str, Any], *, reviewer: str = "e2e-reviewer") -> dict[str, Any]:
        day2_gates.append(str(payload.get("gate")))
        return {"decision": "approved", "reviewer": reviewer, "feedback": None}

    init_day2 = initial_blackboard_state(
        run_id="run-ref-day2",
        thread_id="thread-ref-day2",
        mode="change",
        target_agent_name="dbt_pipeline_builder_agent",
        change_request=ChangeRequestContext(
            request_text="Add non_null assertion verification in dry_run stage",
            intake_source="chainlit_chat",
            agent_id="dbt_pipeline_builder_agent",
        ),
        baseline_impl_docs=baseline_pkg,
    )
    day2_res = await drive_coordinator_until_pause_or_done(
        initial_or_command=init_day2,
        thread_id="thread-ref-day2",
        run_id="run-ref-day2",
        deps=deps,
        checkpointer=cp,
        mode_label="change",
        reviewer="e2e-reviewer",
        gate_handler=_approve_day2,
        stream_to_chainlit=False,
        notify_fn=lambda _: asyncio.sleep(0),
    )

    assert day2_res.status == "completed", day2_res.error
    assert "impl_doc_review" in day2_gates
    assert hotfix_marker in discover_tool_path.read_text(encoding="utf-8")
    assert "non_null_enforced" in (agent_dir / "src" / "tools" / "dry_run_tools.py").read_text(encoding="utf-8")

    manifest_2 = day2_res.final_state["delivery_manifest"]
    manifest_2_dict = manifest_2.model_dump() if hasattr(manifest_2, "model_dump") else dict(manifest_2)
    branch_2 = str(manifest_2_dict["git_branch"])
    commit_2 = str(manifest_2_dict["git_commit_sha"])
    assert commit_2 and commit_2 != commit_1
    assert commit_2 == _current_git_sha(agent_dir, branch_2)
    assert _git_commit_count(agent_dir, branch_2) >= 2


class _DeterministicEmbedder:
    """Zero-network 1536-dim embedder implementing LiteLLMEmbedder's async/sync methods."""

    dimensions = 1536

    def embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for idx, ch in enumerate(text.encode("utf-8")):
            vec[idx % self.dimensions] += ((ch % 19) + 1) / 100.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [round(v / norm, 6) for v in vec]

    def embed_texts_sync(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return self.embed_text(query)

    async def aembed_text(self, text: str) -> list[float]:
        return self.embed_text(text)

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]


def _check_live_postgres_available() -> tuple[bool, Optional[str], Optional[str]]:
    env = get_env_settings()
    cp_uri = env.checkpointer_db_uri
    meta_uri = env.metadata_db_uri
    if not cp_uri or not meta_uri:
        return False, None, None
    try:
        norm_cp = cp_uri.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(norm_cp, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True, cp_uri, meta_uri
    except Exception:
        return False, None, None


def test_live_postgres_day0_checkpointer_telemetry_catalog_and_rag_e2e(
    tmp_path: Path,
) -> None:
    """Run Day-0 reference workflow against live PostgresSaver, Tier 1 telemetry, Tier 2, and RAG."""
    available, cp_uri, meta_uri = _check_live_postgres_available()
    if not available or cp_uri is None or meta_uri is None:
        pytest.skip("Live PostgreSQL checkpointer/metadata database not configured or unreachable")

    _init_temp_git_repo(tmp_path)
    uid = uuid4().hex[:8]
    agent_id = f"dbt_ref_it_{uid}"
    thread_id = f"thread-ref-it-{uid}"
    run_id = str(uuid4())

    _, parsed = load_canonical_reference_spec()
    parsed["agent"]["name"] = agent_id
    custom_yaml = yaml.safe_dump(parsed, sort_keys=False)

    ensure_checkpointer_setup(cp_uri)
    llm_client = _ReferenceSpecLLMClient(target_agent_name=agent_id)
    deps = _make_real_deps(
        llm_client=llm_client,
        workspace_root=tmp_path,
        enable_tier2=True,
        enable_rag_indexing=True,
        embedder=_DeterministicEmbedder(),
    )
    meta_db = MetadataDatabase(db_uri=meta_uri)
    telemetry = MetadataCallbackHandler(
        run_id=run_id,
        agent_id="meta-agent",
        agent_version="0.1.0",
        operation_kind="meta_build",
        initiator_id="it-suite",
        initiator_type="system",
        trigger_type="manual",
        db=meta_db,
        enabled=True,
    )

    async def _run_live() -> None:
        rag_pool = await create_rag_pool(db_uri=meta_uri)
        deps.rag_store = PgVectorRAGStore(pool=rag_pool)
        try:
            async with open_async_checkpointer(cp_uri, verify_setup=True) as cp:
                async def _timeout_gate(_: Any, **__: Any) -> None:
                    return None

                paused_res = await drive_coordinator_until_pause_or_done(
                    initial_or_command=initial_blackboard_state(
                        run_id=run_id,
                        thread_id=thread_id,
                        mode="build",
                        raw_spec_yaml=custom_yaml,
                        spec_version="1.0.0",
                        target_agent_name=agent_id,
                    ),
                    thread_id=thread_id,
                    run_id=run_id,
                    deps=deps,
                    checkpointer=cp,
                    telemetry_handler=telemetry,
                    reviewer="it-suite",
                    gate_handler=_timeout_gate,
                    stream_to_chainlit=False,
                    notify_fn=lambda _: asyncio.sleep(0),
                )
                assert paused_res.status == "interrupted", paused_res.error

                snap = await arecover_thread_state(cp, thread_id)
                assert snap is not None and snap.pending_interrupts

                async def _approve_all(payload: Mapping[str, Any], *, reviewer: str = "it-suite") -> dict[str, Any]:
                    if payload.get("gate") == "clarification":
                        return {"decision": "approved", "answers": {"q_audit": "comprehensive"}, "reviewer": reviewer}
                    return {"decision": "approved", "reviewer": reviewer, "feedback": None}

                resumed_res = await drive_coordinator_until_pause_or_done(
                    initial_or_command=make_resume_command(
                        await _approve_all(paused_res.interrupt_payload or {}, reviewer="it-suite")
                    ),
                    thread_id=thread_id,
                    run_id=run_id,
                    deps=deps,
                    checkpointer=cp,
                    telemetry_handler=telemetry,
                    reviewer="it-suite",
                    is_resume=True,
                    resume_decision={"decision": "approved", "reviewer": "it-suite"},
                    gate_handler=_approve_all,
                    stream_to_chainlit=False,
                    notify_fn=lambda _: asyncio.sleep(0),
                )
                assert resumed_res.status == "completed", resumed_res.error

                manifest = resumed_res.final_state["delivery_manifest"]
                manifest_dict = manifest.model_dump() if hasattr(manifest, "model_dump") else dict(manifest)
                assert manifest_dict["agent_id"] == agent_id
                assert manifest_dict["tier2_registered"] is True
                assert manifest_dict["rag_indexed"] is True
        finally:
            await rag_pool.close()
            await deps.aclose()

    try:
        run_async_selector(_run_live())

        # Verify Tier 1 + Tier 2 + RAG rows actually landed in PostgreSQL.
        norm_meta = meta_uri.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(norm_meta) as conn:
            run_row = conn.execute(
                "SELECT status FROM agent_metadata.agent_run_header WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            assert run_row is not None and run_row[0] == "completed"

            tier2_row = conn.execute(
                "SELECT agent_id FROM ddlc_shared_metadata.agent_registry WHERE agent_id = %s",
                (agent_id,),
            ).fetchone()
            assert tier2_row is not None

            rag_row = conn.execute(
                "SELECT document_id FROM rag_knowledge.knowledge_documents WHERE title LIKE %s",
                (f"%{agent_id}%",),
            ).fetchone()
            assert rag_row is not None
    finally:
        meta_db.close()
        norm_cp = cp_uri.replace("postgresql+psycopg://", "postgresql://", 1)
        norm_meta = meta_uri.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(norm_cp) as conn:
            conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
            conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
            conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
            conn.commit()
        with psycopg.connect(norm_meta) as conn:
            conn.execute(
                "DELETE FROM rag_knowledge.knowledge_documents WHERE title LIKE %s",
                (f"%{agent_id}%",),
            )
            conn.execute(
                "DELETE FROM ddlc_shared_metadata.build_summary WHERE agent_id = %s",
                (agent_id,),
            )
            conn.execute(
                "DELETE FROM ddlc_shared_metadata.contract_lineage WHERE producer_agent_id = %s OR consumer_agent_id = %s",
                (agent_id, agent_id),
            )
            conn.execute(
                "DELETE FROM ddlc_shared_metadata.agent_registry WHERE agent_id = %s",
                (agent_id,),
            )
            conn.execute("DELETE FROM agent_metadata.artifact_manifest WHERE run_id = %s", (run_id,))
            conn.execute("DELETE FROM agent_metadata.artifact_registry WHERE produced_run_id = %s", (run_id,))
            conn.execute("DELETE FROM agent_metadata.decision_log WHERE run_id = %s", (run_id,))
            conn.execute("DELETE FROM agent_metadata.execution_log WHERE run_id = %s", (run_id,))
            conn.commit()
            try:
                conn.execute("DELETE FROM agent_metadata.agent_run_header WHERE run_id = %s", (run_id,))
                conn.commit()
            except psycopg.Error:
                # Immutable audit_log entries legally retain their parent agent_run_header via RESTRICT FK.
                conn.rollback()
