"""Pydantic v2 schema for the Day-0 DDLC Agent Intake Specification (spec_template.md)."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

DomainEnum = Literal[
    "customer",
    "finance",
    "product",
    "marketing",
    "operations",
    "supply_chain",
    "hr",
    "compliance",
    "analytics",
    "infrastructure",
    "other",
]
AutonomyEnum = Literal["autonomous", "semi_autonomous", "human_required"]
ComplexityEnum = Literal["simple", "moderate", "complex"]
RuntimePrefEnum = Literal["langgraph", "python_litellm", "no_preference"]
StageTypeEnum = Literal[
    "discovery",
    "reasoning",
    "code_generation",
    "sandbox_testing",
    "human_review",
    "delivery",
    "custom",
]
ErrorStrategyEnum = Literal["fail_fast", "retry", "reflect", "skip", "escalate"]
ContractDirectionEnum = Literal["input", "output", "internal"]
StorageModeEnum = Literal["inline", "claim_check"]
FieldTypeEnum = Literal[
    "string",
    "integer",
    "float",
    "boolean",
    "date",
    "datetime",
    "timestamp",
    "json",
    "array",
    "object",
    "binary",
]
PiiStrategyEnum = Literal[
    "mask",
    "hash",
    "encrypt",
    "redact",
    "tokenize",
    "none_warehouse_managed",
]
AuditLogLevelEnum = Literal["minimal", "standard", "comprehensive"]
DataClassificationEnum = Literal["public", "internal", "confidential", "restricted"]
InfraSystemEnum = Literal[
    "git_repo",
    "data_catalog",
    "dbt_project",
    "relational_db",
    "postgres",
    "mysql",
    "snowflake",
    "bigquery",
    "redshift",
    "s3",
    "gcs",
    "azure_blob",
    "sftp",
    "http_api",
    "local_filesystem",
    "other",
]
InfraFormatEnum = Literal[
    "csv",
    "json",
    "jsonl",
    "parquet",
    "avro",
    "orc",
    "xml",
    "database_table",
    "api_response",
    "git_repo",
    "other",
]
ScheduleTypeEnum = Literal["on_demand", "cron", "event_driven"]


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _require_non_empty(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be empty.")
    return cleaned


def _require_snake_case(value: str, label: str) -> str:
    cleaned = value.strip()
    if not _SNAKE_CASE_RE.match(cleaned):
        raise ValueError(f"{label} '{value}' must be lowercase snake_case (^[a-z][a-z0-9_]*$).")
    return cleaned


class AgentIdentitySection(BaseModel):
    name: str
    version: str
    description: str
    owner: str
    domain: DomainEnum
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        return _require_snake_case(v, "agent.name")

    @field_validator("version")
    @classmethod
    def _val_version(cls, v: str) -> str:
        cleaned = v.strip()
        if not _SEMVER_RE.match(cleaned):
            raise ValueError(f"agent.version '{v}' must follow semantic versioning (e.g. '1.0.0').")
        return cleaned

    @field_validator("description", "owner")
    @classmethod
    def _val_non_empty(cls, v: str, info: Any) -> str:
        return _require_non_empty(v, f"agent.{info.field_name}")


class ArchitecturePreferenceSection(BaseModel):
    autonomy_level: AutonomyEnum
    complexity: Optional[ComplexityEnum] = None
    runtime_preference: RuntimePrefEnum = "no_preference"
    description: Optional[str] = None

    @field_validator("complexity", "description", mode="before")
    @classmethod
    def _blank_optional(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("runtime_preference", mode="before")
    @classmethod
    def _default_runtime_pref(cls, v: Any) -> Any:
        cleaned = _blank_to_none(v)
        return "no_preference" if cleaned is None else cleaned


class RetryConfigSpec(BaseModel):
    max_retries: int = Field(..., gt=0)
    backoff_seconds: int = Field(..., gt=0)


class StageIntakeSpec(BaseModel):
    id: str
    type: StageTypeEnum
    description: str
    autonomy: AutonomyEnum
    input_contract: Optional[str] = None
    output_contract: Optional[str] = None
    tools: list[str] = Field(default_factory=list)
    error_strategy: ErrorStrategyEnum = "fail_fast"
    retry_config: Optional[RetryConfigSpec] = None
    description_details: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _val_id(cls, v: str) -> str:
        return _require_snake_case(v, "stages[].id")

    @field_validator("description")
    @classmethod
    def _val_desc(cls, v: str) -> str:
        return _require_non_empty(v, "stages[].description")

    @field_validator(
        "input_contract",
        "output_contract",
        "description_details",
        mode="before",
    )
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("error_strategy", mode="before")
    @classmethod
    def _default_err(cls, v: Any) -> Any:
        cleaned = _blank_to_none(v)
        return "fail_fast" if cleaned is None else cleaned

    @model_validator(mode="after")
    def _check_retry_config(self) -> "StageIntakeSpec":
        if self.error_strategy in {"retry", "reflect"} and self.retry_config is None:
            raise ValueError(
                f"Stage '{self.id}' uses error_strategy='{self.error_strategy}' "
                "and must provide retry_config with positive max_retries and backoff_seconds."
            )
        return self


class ContractItemSpec(BaseModel):
    type: FieldTypeEnum
    model_name: Optional[str] = None
    description: Optional[str] = None
    fields: list["ContractFieldSpec"] = Field(default_factory=list)

    @field_validator("model_name", "description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


class ContractFieldSpec(BaseModel):
    name: str
    type: FieldTypeEnum
    nullable: bool = False
    description: str
    default: Any = None
    items: Optional[ContractItemSpec] = None
    fields: list["ContractFieldSpec"] = Field(default_factory=list)
    quality_rules: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        return _require_snake_case(v, "contracts[].fields[].name")

    @field_validator("description")
    @classmethod
    def _val_desc(cls, v: str) -> str:
        return _require_non_empty(v, "contracts[].fields[].description")


ContractItemSpec.model_rebuild()
ContractFieldSpec.model_rebuild()


class ContractSlaSpec(BaseModel):
    max_items: Optional[int] = Field(default=None, gt=0)
    min_items: Optional[int] = Field(default=None, ge=0)
    freshness_hours: Optional[int] = Field(default=None, gt=0)


class ContractIntakeSpec(BaseModel):
    id: str
    description: str
    direction: ContractDirectionEnum
    storage_mode: StorageModeEnum = "inline"
    fields: list[ContractFieldSpec] = Field(..., min_length=1)
    sla: Optional[ContractSlaSpec] = None

    @field_validator("id")
    @classmethod
    def _val_id(cls, v: str) -> str:
        return _require_snake_case(v, "contracts[].id")

    @field_validator("description")
    @classmethod
    def _val_desc(cls, v: str) -> str:
        return _require_non_empty(v, "contracts[].description")

    @field_validator("storage_mode", mode="before")
    @classmethod
    def _default_storage(cls, v: Any) -> Any:
        cleaned = _blank_to_none(v)
        return "inline" if cleaned is None else cleaned


class AuditRequirementsSpec(BaseModel):
    enabled: bool = False
    log_level: Optional[AuditLogLevelEnum] = None
    retention_days: Optional[int] = Field(default=None, gt=0)

    @field_validator("log_level", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


class GovernanceIntakeSection(BaseModel):
    pii_fields: list[str] = Field(default_factory=list)
    pii_strategy: Optional[PiiStrategyEnum] = None
    audit_requirements: Optional[AuditRequirementsSpec] = None
    approval_gates: list[str] = Field(default_factory=list)
    compliance_rules: list[str] = Field(default_factory=list)
    data_classification: Optional[DataClassificationEnum] = None
    description: Optional[str] = None

    @field_validator("pii_strategy", "data_classification", "description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @model_validator(mode="after")
    def _check_pii_strategy(self) -> "GovernanceIntakeSection":
        if self.pii_fields and self.pii_strategy is None:
            raise ValueError(
                "governance.pii_strategy is required when governance.pii_fields is non-empty."
            )
        return self


class InfraEndpointSpec(BaseModel):
    name: str
    system: InfraSystemEnum
    connection: str
    format: Optional[InfraFormatEnum] = None
    description: Optional[str] = None

    @field_validator("name", "connection")
    @classmethod
    def _val_req(cls, v: str, info: Any) -> str:
        return _require_non_empty(v, f"infrastructure.{info.field_name}")

    @field_validator("format", "description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


class SchedulingSpec(BaseModel):
    type: ScheduleTypeEnum = "on_demand"
    schedule: Optional[str] = None
    timezone: Optional[str] = None
    description: Optional[str] = None

    @field_validator("schedule", "timezone", "description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("type", mode="before")
    @classmethod
    def _default_type(cls, v: Any) -> Any:
        cleaned = _blank_to_none(v)
        return "on_demand" if cleaned is None else cleaned


class ResourceConstraintsSpec(BaseModel):
    max_memory_gb: Optional[float] = Field(default=None, gt=0)
    max_cpu_cores: Optional[int] = Field(default=None, gt=0)
    max_execution_minutes: Optional[int] = Field(default=None, gt=0)
    description: Optional[str] = None

    @field_validator("description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


class InfrastructureIntakeSection(BaseModel):
    sources: list[InfraEndpointSpec] = Field(default_factory=list)
    destinations: list[InfraEndpointSpec] = Field(default_factory=list)
    scheduling: Optional[SchedulingSpec] = None
    resource_constraints: Optional[ResourceConstraintsSpec] = None


class AgentDependencyIntakeSpec(BaseModel):
    agent: str
    contract: Optional[str] = None
    description: Optional[str] = None

    @field_validator("agent")
    @classmethod
    def _val_agent(cls, v: str) -> str:
        return _require_non_empty(v, "cross_agent.dependency.agent")


class SharedComponentRequestSpec(BaseModel):
    name: str
    version: Optional[str] = None
    usage: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _val_name(cls, v: str) -> str:
        return _require_non_empty(v, "cross_agent.shared_components[].name")


class CrossAgentSection(BaseModel):
    upstream_dependencies: list[AgentDependencyIntakeSpec] = Field(default_factory=list)
    downstream_consumers: list[AgentDependencyIntakeSpec] = Field(default_factory=list)
    shared_components: list[SharedComponentRequestSpec] = Field(default_factory=list)
    description: Optional[str] = None

    @field_validator("description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


class PerformanceConstraintSpec(BaseModel):
    max_latency_seconds: Optional[int] = Field(default=None, gt=0)
    max_cost_per_run_usd: Optional[float] = Field(default=None, gt=0)


class SLAConstraintSpec(BaseModel):
    availability_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    max_downtime_minutes: Optional[int] = Field(default=None, gt=0)
    recovery_time_minutes: Optional[int] = Field(default=None, gt=0)


class ConstraintsSection(BaseModel):
    required_libraries: list[str] = Field(default_factory=list)
    avoided_libraries: list[str] = Field(default_factory=list)
    performance: Optional[PerformanceConstraintSpec] = None
    sla: Optional[SLAConstraintSpec] = None
    description: Optional[str] = None

    @field_validator("description", mode="before")
    @classmethod
    def _blank_opt(cls, v: Any) -> Any:
        return _blank_to_none(v)


def _collect_field_names(fields: list[ContractFieldSpec]) -> set[str]:
    names: set[str] = set()
    for fld in fields:
        names.add(fld.name)
        if fld.fields:
            names.update(_collect_field_names(fld.fields))
        if fld.items and fld.items.fields:
            names.update(_collect_field_names(fld.items.fields))
    return names


class DDLCIntakeSpec(BaseModel):
    """Authoritative Pydantic v2 model for Day-0 DDLC agent YAML specifications."""

    agent: AgentIdentitySection
    architecture: ArchitecturePreferenceSection
    stages: list[StageIntakeSpec] = Field(..., min_length=1)
    contracts: list[ContractIntakeSpec] = Field(..., min_length=1)
    governance: GovernanceIntakeSection = Field(default_factory=GovernanceIntakeSection)
    infrastructure: InfrastructureIntakeSection = Field(
        default_factory=InfrastructureIntakeSection
    )
    cross_agent: CrossAgentSection = Field(default_factory=CrossAgentSection)
    constraints: ConstraintsSection = Field(default_factory=ConstraintsSection)

    @model_validator(mode="after")
    def _validate_cross_references(self) -> "DDLCIntakeSpec":
        stage_ids: list[str] = []
        seen_stages: set[str] = set()
        for idx, stg in enumerate(self.stages):
            if stg.id in seen_stages:
                raise ValueError(f"Duplicate stage id '{stg.id}' in stages.")
            seen_stages.add(stg.id)
            stage_ids.append(stg.id)

            if len(self.stages) > 1:
                if idx > 0 and not stg.input_contract:
                    raise ValueError(
                        f"Non-initial stage '{stg.id}' must declare input_contract."
                    )
                if idx < len(self.stages) - 1 and not stg.output_contract:
                    raise ValueError(
                        f"Non-terminal stage '{stg.id}' must declare output_contract."
                    )

        seen_contracts: set[str] = set()
        all_field_names: set[str] = set()
        for ctr in self.contracts:
            if ctr.id in seen_contracts:
                raise ValueError(f"Duplicate contract id '{ctr.id}' in contracts.")
            seen_contracts.add(ctr.id)
            all_field_names.update(_collect_field_names(ctr.fields))

        for stg in self.stages:
            if stg.input_contract and stg.input_contract not in seen_contracts:
                raise ValueError(
                    f"Stage '{stg.id}' references undefined input_contract '{stg.input_contract}'."
                )
            if stg.output_contract and stg.output_contract not in seen_contracts:
                raise ValueError(
                    f"Stage '{stg.id}' references undefined output_contract '{stg.output_contract}'."
                )

        for pii_field in self.governance.pii_fields:
            leaf_name = pii_field.split(".")[-1]
            if pii_field not in all_field_names and leaf_name not in all_field_names:
                raise ValueError(
                    f"governance.pii_fields references '{pii_field}', "
                    "which does not exist in any contract."
                )

        for gate_stage in self.governance.approval_gates:
            if gate_stage not in seen_stages:
                raise ValueError(
                    f"governance.approval_gates references unknown stage '{gate_stage}'."
                )

        return self
