"""Authoritative Pydantic v2 schemas for the 5-file impl_docs/ suite (impl_doc_schema.md)."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_HANDLER_RE = re.compile(r"^tools/[a-zA-Z0-9_/]+\.py::[a-zA-Z_][a-zA-Z0-9_]*$")

ALLOWED_BUILTIN_OUTPUT_SCHEMAS: frozenset[str] = frozenset({"GeneratedFileSet"})

PatternEnum = Literal[
    "stateful_pipeline_with_gates",
    "react_tool_agent",
    "multi_step_planner_agent",
    "lightweight_event_cli_agent",
    "multi_source_lineage_inspector",
]
RuntimeEnum = Literal["langgraph", "python_litellm"]
AutonomyEnum = Literal["autonomous", "semi_autonomous", "human_required"]
StageTypeEnum = Literal[
    "discovery",
    "reasoning",
    "code_generation",
    "sandbox_testing",
    "human_review",
    "delivery",
    "custom",
]
ProcessingTypeEnum = Literal["llm_reasoning", "deterministic_tool", "human_gate"]
ToolCategoryEnum = Literal["builtin", "custom_code", "shared_component"]
UsageTypeEnum = Literal["import", "extend", "wrap"]
ErrorActionEnum = Literal["retry", "fail_fast", "skip", "escalate", "reflect"]
ContractFieldTypeEnum = Literal[
    "string",
    "integer",
    "float",
    "boolean",
    "timestamp",
    "datetime",
    "date",
    "json",
    "binary",
    "array",
    "object",
]
ClassificationEnum = Literal["public", "internal", "confidential", "restricted"]


# ---------------------------------------------------------------------------
# 1. agent.yaml
# ---------------------------------------------------------------------------


class AgentManifestIdentity(BaseModel):
    name: str
    display_name: str = Field(..., min_length=1)
    version: str
    description: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"agent.name '{v}' must be snake_case.")
        return cleaned

    @field_validator("version")
    @classmethod
    def _val_ver(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SEMVER_RE.match(cleaned):
            raise ValueError(f"agent.version '{v}' must follow semver (x.y.z).")
        return cleaned


class AgentManifestArchitecture(BaseModel):
    pattern: PatternEnum
    runtime: RuntimeEnum
    autonomy_level: AutonomyEnum


class PipelineStageEdge(BaseModel):
    stage_id: str
    next: Optional[str] = None
    on_error: Optional[str] = None
    transitions: Optional[dict[str, Optional[str]]] = None

    @field_validator("stage_id")
    @classmethod
    def _val_stage_id(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"pipeline stage_id '{v}' must be snake_case.")
        return cleaned


class AgentPipelineTopology(BaseModel):
    stages: list[PipelineStageEdge] = Field(..., min_length=1)


class ToolDependencyRef(BaseModel):
    name: str = Field(..., min_length=1)
    version: Optional[str] = None
    used_by: list[str] = Field(default_factory=list)
    description: Optional[str] = None


class SharedComponentDependencyRef(BaseModel):
    component_id: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    used_by: list[str] = Field(default_factory=list)
    usage_type: UsageTypeEnum = "import"
    description: Optional[str] = None


class AgentDependencies(BaseModel):
    tools: list[ToolDependencyRef] = Field(default_factory=list)
    shared_components: list[SharedComponentDependencyRef] = Field(default_factory=list)


class AgentMetadataConfig(BaseModel):
    tier_3_schema: Optional[str] = None
    tier_3_tables: list[str] = Field(default_factory=list)
    writes_to: dict[str, list[str]] = Field(default_factory=dict)


class AgentScheduleConfig(BaseModel):
    type: Literal["on_demand", "cron", "event_driven"] = "on_demand"
    schedule: Optional[str] = None
    timezone: Optional[str] = "UTC"
    description: Optional[str] = None


class AgentManifestDoc(BaseModel):
    """Canonical schema for impl_docs/agent.yaml."""

    agent: AgentManifestIdentity
    architecture: AgentManifestArchitecture
    pipeline: AgentPipelineTopology
    dependencies: AgentDependencies = Field(default_factory=AgentDependencies)
    metadata: Optional[AgentMetadataConfig] = None
    schedule: Optional[AgentScheduleConfig] = None


# ---------------------------------------------------------------------------
# 2. stages/stage_<id>.yaml
# ---------------------------------------------------------------------------


class StageHeader(BaseModel):
    id: str
    name: str = Field(..., min_length=1)
    type: StageTypeEnum
    description: str = Field(..., min_length=1)
    autonomy: AutonomyEnum = "autonomous"

    @field_validator("id")
    @classmethod
    def _val_id(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"stage.id '{v}' must be snake_case.")
        return cleaned


class StageInput(BaseModel):
    contract: Optional[str] = None
    source: Optional[dict[str, Any]] = None


class StageOutput(BaseModel):
    contract: Optional[str] = None
    destination: Optional[dict[str, Any]] = None


class ToolConfig(BaseModel):
    name: str = Field(..., min_length=1)
    type: ToolCategoryEnum = "builtin"
    handler: Optional[str] = None
    signature: Optional[str] = None
    purpose: str = Field(..., min_length=1)
    access: Literal["read_only", "side_effecting"] = "read_only"
    requires_approval: bool = False
    configuration: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_custom_hook(self) -> "ToolConfig":
        if self.type == "custom_code":
            if not self.handler or not _HANDLER_RE.match(self.handler.strip()):
                raise ValueError(
                    f"Tool '{self.name}' with type='custom_code' must declare handler "
                    "matching 'tools/<module>.py::<func>'."
                )
            if not self.signature or not self.signature.strip():
                raise ValueError(
                    f"Tool '{self.name}' with type='custom_code' must declare a typed signature."
                )
        return self


class LLMConfig(BaseModel):
    model_tier: Literal["speed", "reasoning", "heavy"] = "reasoning"
    prompt_template_ref: Optional[str] = None
    system_prompt: Optional[str] = None
    output_schema: Optional[str] = None
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, gt=0)
    cost_cap_usd: Optional[float] = Field(default=None, gt=0)


class SafetyValves(BaseModel):
    max_tool_iterations: Optional[int] = Field(default=5, ge=1)
    max_reflection_retries: Optional[int] = Field(default=3, ge=0)
    timeout_seconds: Optional[int] = Field(default=300, ge=1)


class StageProcessing(BaseModel):
    processing_type: ProcessingTypeEnum = "deterministic_tool"
    llm_config: Optional[LLMConfig] = None
    tools: list[ToolConfig] = Field(default_factory=list)
    safety_valves: Optional[SafetyValves] = Field(default_factory=SafetyValves)
    logic_description: Optional[str] = None

    @model_validator(mode="after")
    def _validate_llm_and_hooks(self) -> "StageProcessing":
        if self.processing_type == "llm_reasoning" and self.llm_config is None:
            raise ValueError("Stage with processing_type='llm_reasoning' requires llm_config.")
        has_custom_handler = any(t.type == "custom_code" and t.handler for t in self.tools)
        if has_custom_handler and not (self.logic_description and self.logic_description.strip()):
            raise ValueError(
                "Stages declaring custom_code tool handlers must provide a non-empty logic_description."
            )
        return self


class ErrorScenario(BaseModel):
    action: ErrorActionEnum
    max_retries: Optional[int] = Field(default=3, ge=0)
    retry_delay_seconds: Optional[int] = Field(default=30, ge=0)
    escalation: Optional[Literal["fail_fast", "escalate", "skip"]] = "fail_fast"
    log_level: Optional[str] = "INFO"
    details: Optional[str] = None

    @field_validator("action", "escalation", mode="before")
    @classmethod
    def _normalize_fail_alias(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() == "fail":
            return "fail_fast"
        return v


class StagePerformance(BaseModel):
    expected_duration_seconds: Optional[int] = Field(default=None, gt=0)
    timeout_seconds: Optional[int] = Field(default=None, gt=0)


class StageDocument(BaseModel):
    """Canonical schema for impl_docs/stages/stage_<id>.yaml."""

    stage: StageHeader
    input: StageInput = Field(default_factory=StageInput)
    output: StageOutput = Field(default_factory=StageOutput)
    processing: StageProcessing = Field(default_factory=StageProcessing)
    error_strategy: dict[str, ErrorScenario] = Field(..., min_length=1)
    performance: Optional[StagePerformance] = None


# ---------------------------------------------------------------------------
# 3. contracts/<name>.yaml
# ---------------------------------------------------------------------------


class ContractItemsDoc(BaseModel):
    type: ContractFieldTypeEnum
    model_name: Optional[str] = None
    description: Optional[str] = None
    fields: list["ContractFieldDoc"] = Field(default_factory=list)


class ContractFieldDoc(BaseModel):
    name: str
    type: ContractFieldTypeEnum
    nullable: bool  # Explicitly required per impl_doc_schema.md §10.4
    default: Any = None
    description: str = Field(..., min_length=1)
    pii: bool = False
    pii_category: Optional[str] = None
    items: Optional[ContractItemsDoc] = None
    fields: list["ContractFieldDoc"] = Field(default_factory=list)
    quality_rules: list[Any] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"Contract field '{v}' must be snake_case.")
        return cleaned


ContractItemsDoc.model_rebuild()
ContractFieldDoc.model_rebuild()


class ContractBodyDoc(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str = Field(..., min_length=1)
    direction: Literal["input", "internal", "output"]
    storage_mode: Literal["inline", "claim_check"] = "inline"
    owner: Optional[str] = None
    classification: ClassificationEnum = "internal"
    tags: list[str] = Field(default_factory=list)
    fields: list[ContractFieldDoc] = Field(..., min_length=1)
    sla: Optional[dict[str, Any]] = None

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"contract.name '{v}' must be snake_case.")
        return cleaned

    @field_validator("version")
    @classmethod
    def _val_ver(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SEMVER_RE.match(cleaned):
            raise ValueError(f"contract.version '{v}' must follow semver.")
        return cleaned


class ContractDocument(BaseModel):
    """Canonical schema for impl_docs/contracts/<name>.yaml."""

    contract: ContractBodyDoc


# ---------------------------------------------------------------------------
# 4. config.yaml
# ---------------------------------------------------------------------------


class SecretDeclaration(BaseModel):
    name: str
    description: str = Field(..., min_length=1)
    required: bool = True

    @field_validator("name")
    @classmethod
    def _val_secret_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SNAKE_CASE_RE.match(cleaned):
            raise ValueError(f"Secret name '{v}' must be lowercase snake_case.")
        return cleaned


class ConfigBodyDoc(BaseModel):
    defaults: dict[str, Any] = Field(default_factory=dict)
    environments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    secrets: list[SecretDeclaration] = Field(default_factory=list)


class ConfigDocument(BaseModel):
    """Canonical schema for impl_docs/config.yaml."""

    config: ConfigBodyDoc


# ---------------------------------------------------------------------------
# 5. tests.yaml
# ---------------------------------------------------------------------------


class ContractTestCaseDoc(BaseModel):
    test_id: str = Field(..., min_length=1)
    type: Literal["schema_validation", "field_quality"]
    description: str = Field(..., min_length=1)
    field: Optional[str] = None
    rule: Optional[str] = None
    auto_generate: bool = True


class ContractTestSuiteDoc(BaseModel):
    contract: str = Field(..., min_length=1)
    description: Optional[str] = None
    cases: list[ContractTestCaseDoc] = Field(default_factory=list)


class CustomTestSpecDoc(BaseModel):
    test_id: str = Field(..., min_length=1)
    type: Literal["integration", "end_to_end", "unit"]
    description: str = Field(..., min_length=1)
    stage: Optional[str] = None
    setup: Optional[str] = None
    expected_behavior: Optional[str] = None


class TestsBodyDoc(BaseModel):
    contract_tests: list[ContractTestSuiteDoc] = Field(default_factory=list)
    custom_tests: list[CustomTestSpecDoc] = Field(default_factory=list)


class TestsDocument(BaseModel):
    """Canonical schema for impl_docs/tests.yaml."""

    tests: TestsBodyDoc


TestsBodyDoc.__test__ = False  # type: ignore[attr-defined]
TestsDocument.__test__ = False  # type: ignore[attr-defined]
