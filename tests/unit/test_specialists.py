"""Zero-infra unit and end-to-end tests for Step 7 specialists and Dual-Tier Builder."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml
from src.core.settings import RuntimeEnvSettings, get_env_settings
from src.specialists import (
    ArchitectSpecialist,
    ArchitectureDesignDraft,
    BuilderSpecialist,
    ChangeAnalysisOutput,
    DDLCIntakeSpec,
    DeterministicScaffoldCompiler,
    DocumentarianSpecialist,
    ImplDocSynthesisOutput,
    ImplDocValidationError,
    LeafCoderPatcher,
    LeafCoderSynthesizer,
    LeafPromptSynthesis,
    LeafSynthesisError,
    LeafToolSynthesis,
    LLMConfigError,
    ResearchAnalysisOutput,
    ResearcherSpecialist,
    SemanticGapAnalysisOutput,
    SpecAnalystSpecialist,
    SpecFieldPatch,
    SpecRefinementOutput,
    ValidatorSpecialist,
    resolve_specialist_model,
    validate_impl_doc_package,
    verify_prompt_template_ast,
    verify_tool_source,
)
from src.specialists.documentarian.models import (
    AgentDependencies,
    AgentManifestArchitecture,
    AgentManifestDoc,
    AgentManifestIdentity,
    AgentMetadataConfig,
    AgentPipelineTopology,
    ConfigBodyDoc,
    ConfigDocument,
    ContractBodyDoc,
    ContractDocument,
    ContractFieldDoc,
    ContractItemsDoc,
    ContractTestCaseDoc,
    ContractTestSuiteDoc,
    CustomTestSpecDoc,
    ErrorScenario,
    LLMConfig,
    PipelineStageEdge,
    SafetyValves,
    SecretDeclaration,
    SharedComponentDependencyRef,
    StageDocument,
    StageHeader,
    StageInput,
    StageOutput,
    StageProcessing,
    TestsBodyDoc,
    TestsDocument,
    ToolConfig,
    ToolDependencyRef,
)
from src.specialists.spec_analyst.specialist import AnswerClassification
from src.state import (
    ArchitectureProposal,
    CatalogContext,
    ChangeRequestContext,
    ClarificationAnswer,
    ClarificationQuestion,
    ImplDocPackage,
    ResearchFindings,
    StageDesignSpec,
    ToolEvaluation,
    initial_blackboard_state,
    project_for_architect,
    project_for_builder,
    project_for_documentarian,
    project_for_leaf_coder,
    project_for_researcher,
    project_for_spec_analyst,
    project_for_validator,
)

# ---------------------------------------------------------------------------
# Deterministic Stub LLM Client & Canonical Test Fixtures
# ---------------------------------------------------------------------------


class _QueueFakeLLMClient:
    """Zero-network StructuredLLMClient returning scripted Pydantic responses or running handlers."""

    def __init__(self, handlers: Optional[dict[type[Any], Any]] = None) -> None:
        self.handlers: dict[type[Any], Any] = dict(handlers or {})
        self.calls: list[dict[str, Any]] = []

    def complete_structured(
        self,
        *,
        specialist_name: str,
        response_model: type[Any],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Any:
        self.calls.append(
            {
                "specialist_name": specialist_name,
                "response_model": response_model.__name__,
                "messages": list(messages),
            }
        )
        handler = self.handlers.get(response_model)
        if handler is None:
            raise AssertionError(f"No stub handler configured for {response_model.__name__}")
        if isinstance(handler, list):
            if not handler:
                raise AssertionError(f"Exhausted stub queue for {response_model.__name__}")
            nxt = handler.pop(0)
            return nxt(messages) if callable(nxt) else nxt
        return handler(messages) if callable(handler) else handler

    async def acomplete_structured(
        self,
        *,
        specialist_name: str,
        response_model: type[Any],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Any:
        return self.complete_structured(
            specialist_name=specialist_name,
            response_model=response_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )


def _canonical_spec_yaml() -> str:
    doc = {
        "agent": {
            "name": "dbt_pipeline_builder_agent",
            "version": "1.0.0",
            "description": "Inspects source schemas, plans dbt models, generates SQL, dry-runs in sandbox, and opens a Git PR.",
            "owner": "analytics-engineering-team",
            "domain": "analytics",
            "tags": ["dbt", "code_generation"],
        },
        "architecture": {
            "autonomy_level": "semi_autonomous",
            "complexity": "complex",
            "runtime_preference": "langgraph",
        },
        "stages": [
            {
                "id": "discover",
                "type": "discovery",
                "description": "Introspect source database schema.",
                "autonomy": "autonomous",
                "input_contract": "source_database_reference",
                "output_contract": "source_schema_profile",
                "tools": ["schema_inspector_v1"],
                "error_strategy": "retry",
                "retry_config": {"max_retries": 3, "backoff_seconds": 30},
            },
            {
                "id": "plan",
                "type": "reasoning",
                "description": "Plan dbt staging and marts DAG.",
                "autonomy": "autonomous",
                "input_contract": "source_schema_profile",
                "output_contract": "transformation_plan",
                "tools": ["sqlglot"],
                "error_strategy": "reflect",
                "retry_config": {"max_retries": 2, "backoff_seconds": 10},
            },
            {
                "id": "generate",
                "type": "code_generation",
                "description": "Generate dbt SQL models and schema YAML.",
                "autonomy": "autonomous",
                "input_contract": "transformation_plan",
                "output_contract": "dbt_artifact_bundle",
                "tools": ["sqlglot"],
                "error_strategy": "reflect",
                "retry_config": {"max_retries": 3, "backoff_seconds": 5},
            },
            {
                "id": "review",
                "type": "human_review",
                "description": "Human approval gate before Git delivery.",
                "autonomy": "human_required",
                "input_contract": "dbt_artifact_bundle",
                "output_contract": "reviewed_artifact_bundle",
                "tools": [],
                "error_strategy": "escalate",
            },
            {
                "id": "deliver",
                "type": "delivery",
                "description": "Open Git PR and register lineage.",
                "autonomy": "autonomous",
                "input_contract": "reviewed_artifact_bundle",
                "output_contract": "delivery_manifest",
                "tools": ["git_deliverer_v1"],
                "error_strategy": "fail_fast",
            },
        ],
        "contracts": [
            {
                "id": "source_database_reference",
                "description": "Input DB reference.",
                "direction": "input",
                "fields": [
                    {"name": "database_name", "type": "string", "description": "Source DB name"},
                    {"name": "schema_names", "type": "array", "items": {"type": "string"}, "description": "Schemas"},
                ],
            },
            {
                "id": "source_schema_profile",
                "description": "Discovered catalog schema.",
                "direction": "internal",
                "fields": [
                    {"name": "database_name", "type": "string", "description": "DB name"},
                    {
                        "name": "tables",
                        "type": "array",
                        "description": "Discovered tables",
                        "items": {
                            "type": "object",
                            "model_name": "TableProfile",
                            "fields": [
                                {"name": "table_name", "type": "string", "description": "Table name"},
                                {"name": "schema_name", "type": "string", "description": "Schema"},
                            ],
                        },
                    },
                ],
            },
            {
                "id": "transformation_plan",
                "description": "Planned dbt models.",
                "direction": "internal",
                "fields": [
                    {"name": "project_name", "type": "string", "description": "dbt project name"},
                    {"name": "target_dialect", "type": "string", "description": "Target SQL dialect"},
                    {"name": "total_models_planned", "type": "integer", "description": "Planned count"},
                ],
            },
            {
                "id": "dbt_artifact_bundle",
                "description": "Claim Check dbt bundle.",
                "direction": "internal",
                "storage_mode": "claim_check",
                "fields": [
                    {"name": "bundle_id", "type": "string", "description": "Bundle UUID"},
                    {"name": "storage_uri", "type": "string", "description": "Bundle path"},
                    {"name": "file_manifest", "type": "array", "items": {"type": "string"}, "description": "Files"},
                    {"name": "artifact_sha256", "type": "string", "description": "SHA-256"},
                ],
            },
            {
                "id": "reviewed_artifact_bundle",
                "description": "Approved artifact pointer.",
                "direction": "internal",
                "fields": [
                    {"name": "bundle_id", "type": "string", "description": "Bundle UUID"},
                    {"name": "approved_by", "type": "string", "description": "Approver"},
                ],
            },
            {
                "id": "delivery_manifest",
                "description": "Git delivery output.",
                "direction": "output",
                "fields": [
                    {"name": "manifest_id", "type": "string", "description": "Manifest ID"},
                    {"name": "run_id", "type": "string", "description": "Run ID"},
                ],
            },
        ],
        "governance": {
            "approval_gates": ["review"],
            "data_classification": "internal",
        },
        "infrastructure": {
            "sources": [
                {
                    "name": "oltp_pg",
                    "system": "postgres",
                    "connection": "config://sources.postgres_oltp",
                    "format": "database_table",
                }
            ],
            "destinations": [
                {
                    "name": "dbt_git",
                    "system": "git_repo",
                    "connection": "config://destinations.git",
                    "format": "git_repo",
                }
            ],
            "scheduling": {"type": "on_demand"},
        },
        "cross_agent": {
            "shared_components": [
                {"name": "schema_inspector_v1", "version": "1.1.0", "usage": "Schema discovery"},
                {"name": "git_deliverer_v1", "version": "1.0.0", "usage": "Git PR delivery"},
            ]
        },
        "constraints": {
            "required_libraries": ["litellm", "instructor", "sqlglot"],
            "avoided_libraries": ["pyspark"],
        },
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _sample_synthesis_output(*, runtime: str = "langgraph", with_custom_tool: bool = True) -> ImplDocSynthesisOutput:
    tools_generate = (
        [
            ToolConfig(
                name="validate_sql_ast",
                type="custom_code",
                handler="tools/sqlglot_verifier.py::validate_sql_ast",
                signature="(sql_content: str, dialect: str = 'snowflake') -> tuple[bool, str]",
                purpose="Verify SQL syntax against dialect AST using SQLGlot.",
                access="read_only",
            )
        ]
        if with_custom_tool
        else []
    )
    return ImplDocSynthesisOutput(
        agent_manifest=AgentManifestDoc(
            agent=AgentManifestIdentity(
                name="dbt_pipeline_builder_agent",
                display_name="dbt Pipeline Builder Agent",
                version="1.0.0",
                description="Builds modular dbt models with SQLGlot checks and human review.",
                owner="analytics-engineering-team",
                tags=["dbt", "analytics"],
            ),
            architecture=AgentManifestArchitecture(
                pattern="stateful_pipeline_with_gates" if runtime == "langgraph" else "lightweight_event_cli_agent",
                runtime=runtime,  # type: ignore[arg-type]
                autonomy_level="semi_autonomous" if runtime == "langgraph" else "autonomous",
            ),
            pipeline=AgentPipelineTopology(
                stages=[
                    PipelineStageEdge(stage_id="plan", next="generate"),
                    PipelineStageEdge(
                        stage_id="generate",
                        next="review" if runtime == "langgraph" else None,
                    ),
                    *(
                        [
                            PipelineStageEdge(
                                stage_id="review",
                                transitions={"on_approved": "deliver", "on_revise": "generate", "on_abort": None},
                            ),
                            PipelineStageEdge(stage_id="deliver", next=None),
                        ]
                        if runtime == "langgraph"
                        else []
                    ),
                ]
            ),
            dependencies=AgentDependencies(
                tools=[
                    ToolDependencyRef(name="sqlglot", version=">=25.0.0", used_by=["generate"]),
                ],
                shared_components=[
                    SharedComponentDependencyRef(
                        component_id="git_deliverer_v1",
                        version="1.0.0",
                        used_by=["deliver"],
                        usage_type="import",
                        description="Git PR delivery helper",
                    )
                ]
                if runtime == "langgraph"
                else [],
            ),
            metadata=AgentMetadataConfig(
                tier_3_schema="ddlc_dbt_pipeline_builder_agent_metadata",
                tier_3_tables=["generated_model_manifest"],
            ),
        ),
        stages=[
            StageDocument(
                stage=StageHeader(
                    id="plan",
                    name="Plan dbt Models",
                    type="reasoning",
                    description="Design dbt staging and marts models from schema profile.",
                    autonomy="autonomous",
                ),
                input=StageInput(
                    contract="source_schema_profile",
                    source={"type": "database", "uri": "${config.sources.postgres_oltp.uri}"},
                ),
                output=StageOutput(contract="transformation_plan"),
                processing=StageProcessing(
                    processing_type="llm_reasoning",
                    llm_config=LLMConfig(
                        model_tier="reasoning",
                        prompt_template_ref="prompts/stage_plan.jinja2",
                        output_schema="transformation_plan",
                        temperature=0.1,
                        max_tokens=4096,
                    ),
                    logic_description="Analyze source_schema_profile and plan staging/marts models.",
                ),
                error_strategy={
                    "on_validation_failure": ErrorScenario(action="retry", max_retries=2)
                },
            ),
            StageDocument(
                stage=StageHeader(
                    id="generate",
                    name="Generate dbt Artifacts",
                    type="code_generation",
                    description="Generate dbt SQL models and verify syntax.",
                    autonomy="autonomous",
                ),
                input=StageInput(contract="transformation_plan"),
                output=StageOutput(contract="dbt_artifact_bundle"),
                processing=StageProcessing(
                    processing_type="llm_reasoning",
                    llm_config=LLMConfig(
                        model_tier="reasoning",
                        prompt_template_ref="prompts/stage_generate.jinja2",
                        output_schema="GeneratedFileSet",
                        temperature=0.0,
                        max_tokens=4096,
                    ),
                    tools=tools_generate,
                    safety_valves=SafetyValves(max_reflection_retries=3, timeout_seconds=180),
                    logic_description="Generate dbt SQL files and validate SQL syntax via validate_sql_ast.",
                ),
                error_strategy={
                    "on_generation_failure": ErrorScenario(action="fail_fast")
                },
            ),
            *(
                [
                    StageDocument(
                        stage=StageHeader(
                            id="review",
                            name="Human Review Gate",
                            type="human_review",
                            description="Pause for engineer sign-off before Git delivery.",
                            autonomy="human_required",
                        ),
                        input=StageInput(contract="dbt_artifact_bundle"),
                        output=StageOutput(contract="reviewed_artifact_bundle"),
                        processing=StageProcessing(processing_type="human_gate"),
                        error_strategy={"on_timeout": ErrorScenario(action="escalate")},
                    ),
                    StageDocument(
                        stage=StageHeader(
                            id="deliver",
                            name="Deliver Git PR",
                            type="delivery",
                            description="Publish PR and register lineage.",
                            autonomy="autonomous",
                        ),
                        input=StageInput(contract="reviewed_artifact_bundle"),
                        output=StageOutput(
                            contract="delivery_manifest",
                            destination={"type": "git", "uri": "${config.destinations.git.repository_url}"},
                        ),
                        processing=StageProcessing(
                            processing_type="deterministic_tool",
                            tools=[
                                ToolConfig(
                                    name="git_deliverer_v1",
                                    type="shared_component",
                                    purpose="Commit and open Pull Request",
                                    access="side_effecting",
                                    requires_approval=True,
                                )
                            ],
                        ),
                        error_strategy={"on_git_error": ErrorScenario(action="fail_fast")},
                    ),
                ]
                if runtime == "langgraph"
                else []
            ),
        ],
        contracts=[
            ContractDocument(
                contract=ContractBodyDoc(
                    name="source_schema_profile",
                    version="1.0.0",
                    description="Discovered source database catalog.",
                    direction="input",
                    fields=[
                        ContractFieldDoc(
                            name="database_name",
                            type="string",
                            nullable=False,
                            description="Source DB identifier",
                            quality_rules=[{"rule": "not_empty", "severity": "error"}],
                        ),
                        ContractFieldDoc(
                            name="tables",
                            type="array",
                            nullable=False,
                            description="Discovered tables",
                            items=ContractItemsDoc(
                                type="object",
                                model_name="TableProfile",
                                fields=[
                                    ContractFieldDoc(
                                        name="table_name",
                                        type="string",
                                        nullable=False,
                                        description="Table name",
                                    ),
                                    ContractFieldDoc(
                                        name="schema_name",
                                        type="string",
                                        nullable=False,
                                        description="Schema name",
                                    ),
                                ],
                            ),
                        ),
                    ],
                )
            ),
            ContractDocument(
                contract=ContractBodyDoc(
                    name="transformation_plan",
                    version="1.0.0",
                    description="Planned dbt transformation DAG.",
                    direction="internal",
                    fields=[
                        ContractFieldDoc(
                            name="project_name",
                            type="string",
                            nullable=False,
                            description="dbt project name",
                            quality_rules=[{"rule": "not_empty", "severity": "error"}],
                        ),
                        ContractFieldDoc(
                            name="target_dialect",
                            type="string",
                            nullable=False,
                            description="Target SQL dialect",
                            quality_rules=[
                                {
                                    "rule": "allowed_values",
                                    "values": ["snowflake", "postgres", "bigquery"],
                                    "severity": "error",
                                }
                            ],
                        ),
                        ContractFieldDoc(
                            name="total_models_planned",
                            type="integer",
                            nullable=False,
                            description="Count of planned models",
                            quality_rules=[{"rule": "range", "min": 1, "max": 100, "severity": "error"}],
                        ),
                    ],
                )
            ),
            ContractDocument(
                contract=ContractBodyDoc(
                    name="dbt_artifact_bundle",
                    version="1.0.0",
                    description="Claim Check bundle of generated dbt files.",
                    direction="internal",
                    storage_mode="claim_check",
                    fields=[
                        ContractFieldDoc(name="bundle_id", type="string", nullable=False, description="Bundle UUID"),
                        ContractFieldDoc(name="storage_uri", type="string", nullable=False, description="Sandbox URI"),
                        ContractFieldDoc(
                            name="file_manifest",
                            type="array",
                            nullable=False,
                            description="Relative paths",
                            items=ContractItemsDoc(type="string"),
                        ),
                        ContractFieldDoc(
                            name="artifact_sha256",
                            type="string",
                            nullable=False,
                            description="Bundle SHA-256",
                            quality_rules=[{"rule": "regex", "pattern": "^[a-f0-9]{64}$", "severity": "error"}],
                        ),
                        ContractFieldDoc(
                            name="generated_at",
                            type="timestamp",
                            nullable=False,
                            default="${now}",
                            description="Generation timestamp",
                        ),
                    ],
                )
            ),
            *(
                [
                    ContractDocument(
                        contract=ContractBodyDoc(
                            name="reviewed_artifact_bundle",
                            version="1.0.0",
                            description="Human-approved bundle sign-off.",
                            direction="internal",
                            fields=[
                                ContractFieldDoc(name="bundle_id", type="string", nullable=False, description="Bundle UUID"),
                                ContractFieldDoc(name="approved_by", type="string", nullable=False, description="Reviewer"),
                                ContractFieldDoc(
                                    name="approval_timestamp",
                                    type="timestamp",
                                    nullable=False,
                                    default="${now}",
                                    description="Approval time",
                                ),
                            ],
                        )
                    ),
                    ContractDocument(
                        contract=ContractBodyDoc(
                            name="delivery_manifest",
                            version="1.0.0",
                            description="Delivered Git PR manifest.",
                            direction="output",
                            fields=[
                                ContractFieldDoc(name="manifest_id", type="string", nullable=False, description="Manifest ID"),
                                ContractFieldDoc(name="run_id", type="string", nullable=False, description="Run ID"),
                                ContractFieldDoc(
                                    name="delivered_at",
                                    type="timestamp",
                                    nullable=False,
                                    default="${now}",
                                    description="Delivery timestamp",
                                ),
                            ],
                        )
                    ),
                ]
                if runtime == "langgraph"
                else []
            ),
        ],
        config_doc=ConfigDocument(
            config=ConfigBodyDoc(
                defaults={
                    "sources": {
                        "postgres_oltp": {
                            "uri": "postgresql://${secrets.source_db_user}:${secrets.source_db_password}@localhost:5432/app",
                            "schema": "public",
                        }
                    },
                    "destinations": {
                        "git": {
                            "repository_url": "https://github.com/org/analytics-dbt.git",
                            "base_branch": "main",
                        }
                    },
                    "dbt": {
                        "sandbox_project_dir": "./sandbox/dbt_dev",
                        "target_dialect": "snowflake",
                        "project_name": "analytics_dbt",
                    },
                    "llm": {
                        "default_provider": "openrouter",
                        "model_tiers": {
                            "speed": "openrouter/meta-llama/llama-3.3-70b-instruct",
                            "reasoning": "openrouter/qwen/qwen3-coder",
                            "heavy": "openrouter/deepseek/deepseek-v3.2",
                        },
                    },
                },
                environments={
                    "development": {
                        "sources": {"postgres_oltp": {"uri": "postgresql://localhost:5432/dev"}},
                    }
                },
                secrets=[
                    SecretDeclaration(name="source_db_user", description="DB user", required=True),
                    SecretDeclaration(name="source_db_password", description="DB pass", required=True),
                ],
            )
        ),
        tests_doc=TestsDocument(
            tests=TestsBodyDoc(
                contract_tests=[
                    ContractTestSuiteDoc(
                        contract="transformation_plan",
                        cases=[
                            ContractTestCaseDoc(
                                test_id="plan_valid",
                                type="schema_validation",
                                description="Verify transformation_plan parses",
                            )
                        ],
                    )
                ],
                custom_tests=[
                    CustomTestSpecDoc(
                        test_id="verify_sqlglot_ast",
                        type="unit",
                        stage="generate",
                        description="Verify SQL AST tool works",
                    )
                ],
            )
        ),
    )


def _valid_sqlglot_tool_bundle() -> LeafToolSynthesis:
    tool_code = '''"""Editable custom SQLGlot AST verification tool."""

from __future__ import annotations

import re
import sqlglot
from sqlglot.errors import ParseError


def validate_sql_ast(sql_content: str, dialect: str = "snowflake") -> tuple[bool, str]:
    """Validate SQL model syntax against target dialect AST using SQLGlot."""
    sanitized = re.sub(r"\\{\\{\\s*(?:ref|source)\\s*\\([^}]+\\)\\s*\\}\\}", "__dbt_table__", sql_content)
    sanitized = re.sub(r"\\{\\{\\s*config\\s*\\([^}]+\\)\\s*\\}\\}", "", sanitized)
    try:
        sqlglot.parse(sanitized, read=dialect)
        return True, ""
    except ParseError as exc:
        return False, str(exc)
'''
    test_code = '''"""Unit tests for validate_sql_ast."""

from src.tools.sqlglot_verifier import validate_sql_ast


def test_validate_sql_ast_valid() -> None:
    ok, err = validate_sql_ast("SELECT id FROM {{ source('raw', 'orders') }}", dialect="snowflake")
    assert ok is True
    assert err == ""


def test_validate_sql_ast_invalid() -> None:
    ok, err = validate_sql_ast("SELECT FROM ((", dialect="snowflake")
    assert ok is False
    assert len(err) > 0
'''
    return LeafToolSynthesis(tool_code=tool_code, unit_test_code=test_code, summary="Generated sqlglot verifier")


def _valid_prompt_plan() -> LeafPromptSynthesis:
    tmpl = """Plan dbt project {{ config.project_name }} (dialect: {{ config.target_dialect }}).
Tables:
{% for t in input.tables %}
- {{ t.table_name }} ({{ t.schema_name }})
{% endfor %}
{% if diagnostics %}
Diagnostics: {{ diagnostics | join("; ") }}
{% endif %}
{% if feedback %}
Feedback: {{ feedback | join("; ") }}
{% endif %}
"""
    return LeafPromptSynthesis(prompt_template=tmpl, summary="Stage plan prompt")


def _valid_prompt_generate() -> LeafPromptSynthesis:
    tmpl = """Generate dbt models for {{ input.project_name }} ({{ config.target_dialect }}).
{% if diagnostics %}
Fix diagnostics: {{ diagnostics | join("; ") }}
{% endif %}
{% if feedback %}
Apply feedback: {{ feedback | join("; ") }}
{% endif %}
"""
    return LeafPromptSynthesis(prompt_template=tmpl, summary="Stage generate prompt")


# ---------------------------------------------------------------------------
# 1. Shared LLM Config & Credential Guard Tests
# ---------------------------------------------------------------------------


class TestSpecialistLLMConfig:
    def test_resolves_configured_tiers_and_fails_fast_without_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved = resolve_specialist_model("architect")
        assert resolved.tier_name == "heavy"
        assert resolved.model.startswith("openrouter/")
        assert resolved.timeout_seconds == 90

        with pytest.raises(LLMConfigError, match="Unknown specialist"):
            resolve_specialist_model("nonexistent_specialist")

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OR_API_KEY", raising=False)
        empty_env = RuntimeEnvSettings(_env_file=tmp_path / ".missing.env")
        monkeypatch.setattr("src.specialists.llm.get_env_settings", lambda: empty_env)

        from src.specialists.llm import ensure_provider_credentials

        with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
            ensure_provider_credentials("openrouter/deepseek/deepseek-v3.2")

        fake_env = tmp_path / ".env"
        fake_env.write_text("OPENROUTER_API_KEY=sk-or-test-key-12345678\n", encoding="utf-8")
        monkeypatch.setattr(
            "src.specialists.llm.get_env_settings",
            lambda: RuntimeEnvSettings(_env_file=fake_env),
        )
        ensure_provider_credentials("openrouter/deepseek/deepseek-v3.2")
        assert os.environ["OPENROUTER_API_KEY"] == "sk-or-test-key-12345678"
        get_env_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. Spec Analyst Tests
# ---------------------------------------------------------------------------


class TestSpecAnalystSpecialist:
    def test_parses_canonical_spec_and_rejects_broken_specs(self) -> None:
        raw_yaml = _canonical_spec_yaml()
        parsed_model = DDLCIntakeSpec.model_validate(yaml.safe_load(raw_yaml))
        assert parsed_model.agent.name == "dbt_pipeline_builder_agent"
        assert len(parsed_model.stages) == 5

        analyst = SpecAnalystSpecialist(
            llm_client=_QueueFakeLLMClient(
                {SemanticGapAnalysisOutput: SemanticGapAnalysisOutput(has_open_gaps=False)}
            )
        )
        state = initial_blackboard_state(run_id="r-spec", raw_spec_yaml=raw_yaml)
        out = analyst.analyze(project_for_spec_analyst(state))
        assert out["spec_validation_status"] == "accepted"
        assert out["has_open_gaps"] is False
        assert out["parsed_spec_summary"].agent_name == "dbt_pipeline_builder_agent"

        # Invalid spec 1: camelCase agent name (field validator)
        bad_doc = yaml.safe_load(raw_yaml)
        bad_doc["agent"]["name"] = "BadCamelCase"
        bad_state = initial_blackboard_state(
            run_id="r-bad-field", raw_spec_yaml=yaml.safe_dump(bad_doc)
        )
        rejected_field = analyst.analyze(project_for_spec_analyst(bad_state))
        assert rejected_field["spec_validation_status"] == "rejected"
        assert any("agent.name" in err for err in rejected_field["spec_rejected_errors"])

        # Invalid spec 2: broken input_contract reference (cross-reference model validator)
        bad_ref_doc = yaml.safe_load(raw_yaml)
        bad_ref_doc["stages"][1]["input_contract"] = "missing_contract"
        bad_ref_state = initial_blackboard_state(
            run_id="r-bad-ref", raw_spec_yaml=yaml.safe_dump(bad_ref_doc)
        )
        rejected_ref = analyst.analyze(project_for_spec_analyst(bad_ref_state))
        assert rejected_ref["spec_validation_status"] == "rejected"
        assert any("missing_contract" in err for err in rejected_ref["spec_rejected_errors"])

    def test_deterministic_gap_detection_dedup_and_answer_incorporation(self) -> None:
        # Spec with confidential classification without audit + unregistered upstream agent
        doc = yaml.safe_load(_canonical_spec_yaml())
        doc["governance"]["data_classification"] = "confidential"
        doc["cross_agent"]["upstream_dependencies"] = [{"agent": "unregistered_extractor"}]

        fake_llm = _QueueFakeLLMClient(
            {
                SemanticGapAnalysisOutput: SemanticGapAnalysisOutput(
                    has_open_gaps=True,
                    questions=[
                        ClarificationQuestion(
                            question_id="llm_q_dialect",
                            category="dialect",
                            field_path="constraints.description",
                            question="Which SQL dialect should be primary?",
                            impact_area="architecture",
                        )
                    ],
                ),
                SpecRefinementOutput: SpecRefinementOutput(
                    patches=[
                        SpecFieldPatch(
                            field_path="governance.audit_requirements",
                            value={"enabled": True, "log_level": "comprehensive"},
                        )
                    ],
                    answer_classifications=[
                        AnswerClassification(
                            question_id="llm_q_dialect",
                            design_impacting=True,
                            resolved_requirement="Target Snowflake dialect exclusively.",
                        ),
                        AnswerClassification(
                            question_id="gap_governance_audit",
                            design_impacting=False,
                        ),
                    ],
                    summary_of_impact="Enabled comprehensive audit and locked Snowflake dialect.",
                ),
            }
        )
        analyst = SpecAnalystSpecialist(llm_client=fake_llm)
        state = initial_blackboard_state(
            run_id="r-gap",
            raw_spec_yaml=yaml.safe_dump(doc),
        )
        state["catalog_context"] = CatalogContext(
            existing_agents=[{"agent_id": "other_agent"}],
            available_components=[
                {"component_id": "schema_inspector_v1", "component_name": "schema_inspector_v1"},
                {"component_id": "git_deliverer_v1", "component_name": "git_deliverer_v1"},
            ],
        )

        out1 = analyst.analyze(project_for_spec_analyst(state))
        assert out1["spec_validation_status"] == "clarify"
        assert out1["has_open_gaps"] is True
        q_ids = {q.question_id for q in out1["clarification_rounds"][0].questions}
        assert {"gap_governance_audit", "gap_upstream_unregistered_extractor", "llm_q_dialect"}.issubset(q_ids)

        # Incorporate answers into round 1
        state.update(out1)
        answers = [
            ClarificationAnswer(question_id="gap_governance_audit", answer="Enable comprehensive audit"),
            ClarificationAnswer(question_id="llm_q_dialect", answer="Snowflake only"),
        ]
        folded = analyst.incorporate_answers(project_for_spec_analyst(state), answers)
        assert folded["spec_validation_status"] == "accepted"
        assert folded["parsed_spec"]["governance"]["audit_requirements"]["enabled"] is True
        # Only design_impacting=True answers projected into enriched_requirements
        assert len(folded["enriched_requirements"]) == 1
        assert "Snowflake" in folded["enriched_requirements"][0].summary

    def test_change_analysis_and_reverse_sync_from_hotfix(self) -> None:
        fake_llm = _QueueFakeLLMClient(
            {
                ChangeAnalysisOutput: ChangeAnalysisOutput(
                    affected_stages=["generate"],
                    affected_contracts=["dbt_artifact_bundle"],
                    requires_dcm_migration=False,
                    changed_impl_files=["stages/stage_generate.yaml"],
                )
            }
        )
        analyst = SpecAnalystSpecialist(llm_client=fake_llm)
        state = initial_blackboard_state(
            run_id="r-chg",
            mode="change",
            target_agent_name="dbt_pipeline_builder_agent",
            change_request=ChangeRequestContext(
                request_text="Support QUALIFY clause in sqlglot verifier",
                agent_id="dbt_pipeline_builder_agent",
            ),
        )
        chg_out = analyst.analyze_change_request(project_for_spec_analyst(state))
        assert chg_out["change_request"].affected_stages == ["generate"]

        # Reverse sync Decision 25
        stage_doc = _sample_synthesis_output().stages[1].model_dump(mode="json")
        hotfixed_src = '''
def validate_sql_ast(sql_content: str, dialect: str = "snowflake", *, strict: bool = True) -> tuple[bool, str]:
    """Validate SQL syntax including QUALIFY clauses."""
    return True, ""
'''
        synced = analyst.sync_stage_doc_from_tool(stage_doc, hotfixed_src, "validate_sql_ast")
        assert synced["processing"]["logic_description"] == "Validate SQL syntax including QUALIFY clauses."
        assert "strict: bool" in synced["processing"]["tools"][0]["signature"]


# ---------------------------------------------------------------------------
# 3. Researcher & Architect Tests
# ---------------------------------------------------------------------------


class TestResearcherAndArchitect:
    def test_researcher_enforces_avoided_library_conflict(self) -> None:
        doc = yaml.safe_load(_canonical_spec_yaml())
        doc["stages"][1]["tools"].append("pyspark")  # conflicts with avoided_libraries!

        fake_llm = _QueueFakeLLMClient(
            {
                ResearchAnalysisOutput: ResearchAnalysisOutput(
                    verdict="feasible",  # deliberately optimistic -> deterministic guard must override
                    summary="Looks okay except pyspark conflict.",
                    tool_evaluations=[ToolEvaluation(tool_name="sqlglot", supported=True)],
                )
            }
        )
        researcher = ResearcherSpecialist(llm_client=fake_llm)
        state = initial_blackboard_state(run_id="r-res", raw_spec_yaml=_canonical_spec_yaml())
        state["parsed_spec"] = doc
        state["catalog_context"] = CatalogContext(
            available_components=[
                {
                    "component_id": "schema_inspector_v1",
                    "component_name": "schema_inspector_v1",
                    "component_type": "connector",
                    "description": "Introspect DB schemas",
                }
            ]
        )

        res_out = researcher.research(project_for_researcher(state))
        assert res_out["feasibility_verdict"] == "not_feasible"
        assert any("pyspark" in c for c in res_out["research_findings"].policy_constraints)
        assert res_out["research_findings"].candidate_shared_components[0]["component_name"] == "schema_inspector_v1"

    def test_architect_enforces_langgraph_for_gates_and_tracks_revisions(self) -> None:
        fake_llm = _QueueFakeLLMClient(
            {
                ArchitectureDesignDraft: ArchitectureDesignDraft(
                    pattern="lightweight_event_cli_agent",  # invalid for human gates -> forced upgrade
                    runtime="python_litellm",
                    autonomy_level="semi_autonomous",
                    summary="Pipeline with human gate.",
                    stages=[
                        StageDesignSpec(stage_id="plan", name="Plan", stage_type="reasoning"),
                        StageDesignSpec(
                            stage_id="review",
                            name="Review",
                            stage_type="human_review",
                            autonomy="human_required",
                        ),
                    ],
                    reused_components=[{"component_id": "git_deliverer_v1"}],
                )
            }
        )
        architect = ArchitectSpecialist(llm_client=fake_llm)
        state = initial_blackboard_state(run_id="r-arch", raw_spec_yaml=_canonical_spec_yaml())
        state["parsed_spec"] = yaml.safe_load(_canonical_spec_yaml())
        state["research_findings"] = ResearchFindings(verdict="feasible", summary="Feasible")
        state["architecture_revision_count"] = 1
        state["architecture_revisions"] = [
            ArchitectureProposal(
                revision_number=1,
                agent_name="dbt_pipeline_builder_agent",
                pattern="stateful_pipeline_with_gates",
                runtime="langgraph",
                summary="v1",
            )
        ]
        state["architecture_feedback"] = "Add explicit gate"

        arch_out = architect.design(project_for_architect(state))
        proposal: ArchitectureProposal = arch_out["architecture_revisions"][0]
        assert proposal.revision_number == 2
        assert proposal.runtime == "langgraph"
        assert proposal.pattern == "stateful_pipeline_with_gates"
        assert proposal.reused_components[0]["usage_type"] == "import"
        assert proposal.revision_feedback_addressed == "Add explicit gate"


# ---------------------------------------------------------------------------
# 4. Documentarian Tests
# ---------------------------------------------------------------------------


class TestDocumentarianSpecialist:
    def test_authors_valid_5file_suite_tier3_contract_and_disk_pointers(self, tmp_path: Path) -> None:
        synthesis = _sample_synthesis_output(runtime="langgraph")
        fake_llm = _QueueFakeLLMClient({ImplDocSynthesisOutput: synthesis})
        doc_specialist = DocumentarianSpecialist(llm_client=fake_llm)

        state = initial_blackboard_state(run_id="r-doc", raw_spec_yaml=_canonical_spec_yaml())
        state["architecture_approved"] = True
        state["approved_architecture_version"] = 1
        state["architecture_revisions"] = [
            ArchitectureProposal(
                revision_number=1,
                agent_name="dbt_pipeline_builder_agent",
                pattern="stateful_pipeline_with_gates",
                runtime="langgraph",
                summary="Approved",
                tier_3_schema_required=True,
                tier_3_tables=["generated_model_manifest"],
                approved=True,
            )
        ]

        out = doc_specialist.author_impl_docs(
            project_for_documentarian(state),
            output_dir=tmp_path / "impl_docs",
        )
        rev = out["impl_doc_revisions"][0]
        assert validate_impl_doc_package(rev.package) == []
        assert rev.package.tier3_contract_yaml is not None
        assert rev.package.tier3_contract_yaml["schema"] == "ddlc_dbt_pipeline_builder_agent_metadata"
        assert out["impl_docs_pointer"] is not None
        assert out["tier3_contract_pointer"] is not None
        assert (tmp_path / "impl_docs" / "agent.yaml").is_file()
        assert (tmp_path / "impl_docs" / "stages" / "stage_generate.yaml").is_file()
        assert (tmp_path / "impl_docs" / "contracts" / "transformation_plan.yaml").is_file()

    def test_cross_document_validator_catches_broken_refs_and_missing_configs(self) -> None:
        synthesis = _sample_synthesis_output(runtime="langgraph")
        pkg = ImplDocPackage(
            agent_yaml=synthesis.agent_manifest.model_dump(mode="json"),
            stages={s.stage.id: s.model_dump(mode="json") for s in synthesis.stages},
            contracts={c.contract.name: c.model_dump(mode="json") for c in synthesis.contracts},
            config_yaml=synthesis.config_doc.model_dump(mode="json"),
            tests_yaml=synthesis.tests_doc.model_dump(mode="json"),
        )
        # Inject broken stage ref, missing config ref, and undeclared secret
        pkg.stages["plan"]["input"]["source"]["uri"] = "${config.sources.nonexistent.uri}"
        pkg.config_yaml["config"]["defaults"]["alerts"] = {"url": "${secrets.undeclared_slack}"}
        pkg.agent_yaml["pipeline"]["stages"][0]["next"] = "ghost_stage"

        errors = validate_impl_doc_package(pkg)
        joined = "\n".join(errors)
        assert "ghost_stage" in joined
        assert "config.sources.nonexistent.uri" in joined
        assert "undeclared_slack" in joined

        # Self-repair test: first LLM output broken, second repaired
        broken = _sample_synthesis_output(runtime="langgraph")
        broken.agent_manifest.pipeline.stages[0].next = "missing_stage"
        repaired = _sample_synthesis_output(runtime="langgraph")
        fake_llm = _QueueFakeLLMClient({ImplDocSynthesisOutput: [broken, repaired]})
        doc_spec = DocumentarianSpecialist(llm_client=fake_llm, max_repair_attempts=1)
        state = initial_blackboard_state(run_id="r-rep", raw_spec_yaml=_canonical_spec_yaml())
        state["architecture_approved"] = True
        state["architecture_revisions"] = [
            ArchitectureProposal(
                revision_number=1,
                agent_name="dbt_pipeline_builder_agent",
                pattern="stateful_pipeline_with_gates",
                runtime="langgraph",
                summary="Approved",
                approved=True,
            )
        ]
        out = doc_spec.author_impl_docs(project_for_documentarian(state))
        assert len(fake_llm.calls) == 2
        assert validate_impl_doc_package(out["impl_doc_revisions"][0].package) == []

        # Exhausted repair raises ImplDocValidationError
        fail_doc = DocumentarianSpecialist(
            llm_client=_QueueFakeLLMClient({ImplDocSynthesisOutput: [broken]}),
            max_repair_attempts=0,
        )
        with pytest.raises(ImplDocValidationError):
            fail_doc.author_impl_docs(project_for_documentarian(state))


# ---------------------------------------------------------------------------
# 5. Builder (Tier 1 Compiler + Tier 2 Leaf Coder) & Validator Tests
# ---------------------------------------------------------------------------


class TestBuilderAndValidator:
    def _build_approved_state(self, *, runtime: str = "langgraph", with_custom_tool: bool = True) -> dict[str, Any]:
        syn = _sample_synthesis_output(runtime=runtime, with_custom_tool=with_custom_tool)
        pkg = ImplDocPackage(
            agent_yaml=syn.agent_manifest.model_dump(mode="json"),
            stages={s.stage.id: s.model_dump(mode="json") for s in syn.stages},
            contracts={c.contract.name: c.model_dump(mode="json") for c in syn.contracts},
            config_yaml=syn.config_doc.model_dump(mode="json"),
            tests_yaml=syn.tests_doc.model_dump(mode="json"),
        )
        from src.state import ImplDocRevision

        state = initial_blackboard_state(
            run_id=f"run-{runtime}",
            target_agent_name="dbt_pipeline_builder_agent",
        )
        state["impl_docs_approved"] = True
        state["approved_impl_doc_version"] = 1
        state["impl_doc_revisions"] = [
            ImplDocRevision(revision_number=1, package=pkg, approved=True)
        ]
        return state

    def test_deterministic_compiler_emits_both_runtimes_zero_llm(self, tmp_path: Path) -> None:
        compiler = DeterministicScaffoldCompiler()

        # 1. LangGraph runtime
        lg_state = self._build_approved_state(runtime="langgraph")
        lg_pkg = lg_state["impl_doc_revisions"][0].package
        lg_dir = tmp_path / "lg_agent"
        lg_res = compiler.compile(lg_pkg, lg_dir)
        assert lg_res.runtime == "langgraph"
        assert (lg_dir / "src" / "graph.py").is_file()
        assert (lg_dir / "src" / "models" / "contracts_pydantic.py").is_file()
        assert (lg_dir / "src" / "core" / "config.py").is_file()
        assert (lg_dir / "tests" / "contracts" / "test_contracts.py").is_file()
        assert len(lg_res.custom_tool_hooks) == 1
        assert len(lg_res.prompt_templates_needed) == 2

        contracts_src = (lg_dir / "src" / "models" / "contracts_pydantic.py").read_text(encoding="utf-8")
        assert "class TableProfile(BaseModel):" in contracts_src
        assert "Literal['snowflake', 'postgres', 'bigquery']" in contracts_src
        assert "ge=1, le=100" in contracts_src
        assert "utcnow" not in contracts_src

        # 2. Python + LiteLLM runtime
        cli_state = self._build_approved_state(runtime="python_litellm", with_custom_tool=False)
        cli_pkg = cli_state["impl_doc_revisions"][0].package
        cli_dir = tmp_path / "cli_agent"
        cli_res = compiler.compile(cli_pkg, cli_dir)
        assert cli_res.runtime == "python_litellm"
        assert (cli_dir / "src" / "pipeline.py").is_file()
        assert (cli_dir / "tests" / "integration" / "test_pipeline.py").is_file()

    def test_leaf_coder_signature_prompt_ast_verifiers_and_patch_hotfix_preservation(
        self, tmp_path: Path
    ) -> None:
        # 1. Signature verifier rejects mismatched parameters, banned imports, utcnow, and bad jinja vars
        bad_tool = "import pyspark\nfrom datetime import datetime\ndef validate_sql_ast(wrong_arg):\n    return datetime.utcnow()\n"
        errs = verify_tool_source(
            bad_tool,
            expected_func_name="validate_sql_ast",
            expected_signature="(sql_content: str, dialect: str = 'snowflake') -> tuple[bool, str]",
        )
        assert any("pyspark" in e for e in errs)
        assert any("utcnow" in e for e in errs)
        assert any("positional parameters" in e for e in errs)

        prompt_errs = verify_prompt_template_ast("stage_plan.jinja2", "Hello {{ illegal_var }}")
        assert len(prompt_errs) == 1
        assert "illegal_var" in prompt_errs[0]

        # 2. Synthesizer self-repairs when first response fails signature check
        bad_bundle = LeafToolSynthesis(tool_code="def wrong_fn(): pass", unit_test_code="def test_x(): pass")
        good_bundle = _valid_sqlglot_tool_bundle()
        state = self._build_approved_state(runtime="langgraph")
        leaf_proj = project_for_leaf_coder(state, stage_id="generate", mode="create")

        synth = LeafCoderSynthesizer(
            llm_client=_QueueFakeLLMClient({LeafToolSynthesis: [bad_bundle, good_bundle]}),
            max_repair_attempts=1,
        )
        out_bundle = synth.create_tool(leaf_proj)
        assert "def validate_sql_ast" in out_bundle.tool_code

        # Exhausting repair raises LeafSynthesisError
        fail_synth = LeafCoderSynthesizer(
            llm_client=_QueueFakeLLMClient({LeafToolSynthesis: [bad_bundle]}),
            max_repair_attempts=0,
        )
        with pytest.raises(LeafSynthesisError):
            fail_synth.create_tool(leaf_proj)

        # 3. Patch mode preserves 2 AM hotfix while updating tool
        hotfix_code = good_bundle.tool_code.replace(
            "try:",
            "# HOTFIX-2AM: preserve QUALIFY keyword handling\n    try:",
        )
        patch_bundle = LeafToolSynthesis(
            tool_code=hotfix_code.replace('dialect: str = "snowflake"', 'dialect: str = "postgres"'),
            unit_test_code=good_bundle.unit_test_code,
        )
        patch_llm = _QueueFakeLLMClient({LeafToolSynthesis: patch_bundle})
        patcher = LeafCoderPatcher(llm_client=patch_llm)
        patch_proj = project_for_leaf_coder(
            state,
            stage_id="generate",
            mode="patch",
            existing_code=hotfix_code,
            existing_tests=good_bundle.unit_test_code,
        )
        patched = patcher.patch_tool(patch_proj)
        assert "HOTFIX-2AM" in patched.tool_code
        assert "existing_code" in patch_llm.calls[0]["messages"][1]["content"]

    def test_full_builder_and_validator_pass_and_route_failures_accurately(self, tmp_path: Path) -> None:
        state = self._build_approved_state(runtime="langgraph")
        fake_llm = _QueueFakeLLMClient(
            {
                LeafToolSynthesis: _valid_sqlglot_tool_bundle(),
                LeafPromptSynthesis: [_valid_prompt_plan(), _valid_prompt_generate()],
            }
        )
        builder = BuilderSpecialist(llm_client=fake_llm)
        agent_dir = tmp_path / "dbt_pipeline_builder_agent"
        build_out = builder.build(project_for_builder(state), output_dir=agent_dir)

        state["generated_code_pointer"] = build_out["generated_code_pointer"]
        validator = ValidatorSpecialist(run_tests=True)
        val_out = validator.validate(project_for_validator(state))
        report = val_out["validation_reports"][0]
        assert report.passed is True, f"Expected validation pass, issues: {report.issues}"
        assert val_out["validation_passed"] is True
        assert report.test_summary.get("returncode") == 0

        # Mutate prompt template with illegal variable + break Claim Check -> Validator catches & routes
        bad_prompt_path = agent_dir / "src" / "prompts" / "stage_plan.jinja2"
        bad_prompt_path.write_text("Use {{ leaked_undeclared_var }}", encoding="utf-8")
        bad_val = validator.validate(project_for_validator(state))
        bad_report = bad_val["validation_reports"][0]
        assert bad_report.passed is False
        check_names = {iss.check_name for iss in bad_report.issues}
        assert "claim_check_integrity" in check_names
        assert "jinja2_prompt_ast" in check_names
        prompt_issue = next(i for i in bad_report.issues if i.check_name == "jinja2_prompt_ast")
        assert prompt_issue.route_target == "leaf_coder"
        assert prompt_issue.target_stage_id == "plan"

    def test_end_to_end_step7_pipeline_from_day0_spec_to_validated_agent(self, tmp_path: Path) -> None:
        """Exercise all 6 specialists sequentially through Blackboard state projections."""
        synthesis = _sample_synthesis_output(runtime="langgraph", with_custom_tool=True)
        fake_llm = _QueueFakeLLMClient(
            {
                SemanticGapAnalysisOutput: SemanticGapAnalysisOutput(has_open_gaps=False),
                ResearchAnalysisOutput: ResearchAnalysisOutput(
                    verdict="feasible",
                    summary="Feasible with sqlglot, instructor, and langgraph.",
                    tool_evaluations=[ToolEvaluation(tool_name="sqlglot", supported=True)],
                ),
                ArchitectureDesignDraft: ArchitectureDesignDraft(
                    pattern="stateful_pipeline_with_gates",
                    runtime="langgraph",
                    autonomy_level="semi_autonomous",
                    summary="Staged pipeline with SQLGlot verification and human gate.",
                    stages=[
                        StageDesignSpec(stage_id="plan", name="Plan", stage_type="reasoning", processing_type="llm_reasoning"),
                        StageDesignSpec(stage_id="generate", name="Generate", stage_type="code_generation", processing_type="llm_reasoning"),
                        StageDesignSpec(stage_id="review", name="Review", stage_type="human_review", processing_type="human_gate", autonomy="human_required"),
                        StageDesignSpec(stage_id="deliver", name="Deliver", stage_type="delivery", processing_type="deterministic_tool"),
                    ],
                    reused_components=[{"component_id": "git_deliverer_v1", "usage_type": "import"}],
                    tier_3_schema_required=True,
                ),
                ImplDocSynthesisOutput: synthesis,
                LeafToolSynthesis: _valid_sqlglot_tool_bundle(),
                LeafPromptSynthesis: [_valid_prompt_plan(), _valid_prompt_generate()],
            }
        )

        state = initial_blackboard_state(
            run_id="e2e-step7",
            raw_spec_yaml=_canonical_spec_yaml(),
        )

        # Phase 2: Spec Analyst
        state.update(SpecAnalystSpecialist(fake_llm).analyze(project_for_spec_analyst(state)))
        assert state["spec_validation_status"] == "accepted"

        # Phase 3: Researcher
        state.update(ResearcherSpecialist(fake_llm).research(project_for_researcher(state)))
        assert state["feasibility_verdict"] == "feasible"

        # Phase 4: Architect + simulated Gate 2 approval
        state.update(ArchitectSpecialist(fake_llm).design(project_for_architect(state)))
        state["architecture_approved"] = True
        state["approved_architecture_version"] = 1

        # Phase 5: Documentarian + simulated Gate 3 approval
        doc_out = DocumentarianSpecialist(fake_llm).author_impl_docs(
            project_for_documentarian(state),
            output_dir=tmp_path / "staged_impl_docs",
        )
        state.update(doc_out)
        state["impl_docs_approved"] = True
        state["approved_impl_doc_version"] = 1

        # Phase 6: Dual-Tier Builder
        build_dir = tmp_path / "built_agent"
        state.update(
            BuilderSpecialist(fake_llm).build(project_for_builder(state), output_dir=build_dir)
        )

        # Phase 7: Validator (including real pytest execution on generated agent!)
        val_out = ValidatorSpecialist(run_tests=True).validate(project_for_validator(state))
        state.update(val_out)

        assert state["validation_passed"] is True
        assert state["validation_reports"][-1].test_summary["returncode"] == 0
        assert (build_dir / "impl_docs" / "agent.yaml").is_file()
        assert (build_dir / "src" / "tools" / "sqlglot_verifier.py").is_file()
        assert (build_dir / "src" / "prompts" / "stage_plan.jinja2").is_file()
        assert (build_dir / "src" / "graph.py").is_file()
