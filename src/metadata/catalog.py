"""Tier 2 transactional fail-fast client for ddlc_shared_metadata catalog, operational, and audit tables."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pgvector import Vector
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, field_validator

from src.core.settings import get_studio_config
from src.metadata.db import (
    MetadataDatabase,
    guard_inline_jsonb,
    sanitize_payload,
    sanitize_text,
    tier2_table,
)

AgentStatus = Literal["draft", "building", "active", "deprecated", "archived"]
LifecycleStatus = Literal["draft", "active", "deprecated", "archived"]
ComponentType = Literal[
    "validator",
    "transformer",
    "connector",
    "masker",
    "quality_rule",
    "utility",
    "template",
]
ContractDirection = Literal["input", "output", "internal"]
BuildOutcome = Literal["success", "partial", "failed", "cancelled"]
# Decision 26 Layer 2 agentic stage types
StageType = Literal[
    "discovery",
    "reasoning",
    "code_generation",
    "sandbox_testing",
    "human_review",
    "delivery",
    "custom",
]
StageStatus = Literal["running", "completed", "failed", "skipped"]
ConfigType = Literal[
    "model_tiers",
    "safety_valves",
    "pipeline_settings",
    "prompt_templates",
    "full",
]
GovernanceRuleType = Literal[
    "classification_tagging",
    "access_control",
    "retention",
    "contract_policy",
    "custom",
]
GovernanceEvalResult = Literal["pass", "fail", "warning", "skip"]
GateType = Literal["human_approval", "automated_check", "quality_gate"]
GateStatus = Literal["passed", "failed", "overridden", "pending"]


def _coerce_uuid(value: Optional[UUID | str]) -> Optional[UUID]:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _coerce_embedding(value: Any) -> Optional[list[float]]:
    """Normalize pgvector.Vector, ndarray, text, or float sequences into list[float]."""
    if value is None:
        return None
    if isinstance(value, Vector):
        floats = value.to_list()
    elif isinstance(value, str):
        floats = Vector.from_text(value).to_list()
    elif hasattr(value, "tolist"):
        floats = [float(x) for x in value.tolist()]
    elif isinstance(value, Sequence):
        floats = [float(x) for x in value]
    else:
        raise ValueError(f"Unsupported embedding value type: {type(value).__name__}")

    expected_dim = get_studio_config().rag.embedding.dimensions
    if len(floats) != expected_dim:
        raise ValueError(
            f"Embedding dimension mismatch: expected {expected_dim}, got {len(floats)}."
        )
    if not all(math.isfinite(x) for x in floats):
        raise ValueError("Embedding vector must contain only finite float values.")
    return floats


# ---------------------------------------------------------------------------
# Pydantic Models (Tier 2 Input & Output Contracts)
# ---------------------------------------------------------------------------


class AgentRegistration(BaseModel):
    """Input model for ddlc_shared_metadata.agent_registry."""

    agent_id: str = Field(..., min_length=1, max_length=100)
    agent_name: str = Field(..., min_length=1, max_length=200)
    agent_version: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None
    architecture_pattern: str = Field(..., min_length=1, max_length=50)
    runtime: str = Field(..., min_length=1, max_length=50)
    status: AgentStatus = "draft"
    owner: str = Field(..., min_length=1, max_length=100)
    source_repo: Optional[str] = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


class AgentRecord(AgentRegistration):
    """Persisted record from ddlc_shared_metadata.agent_registry."""

    created_at: datetime
    updated_at: datetime


class ComponentRegistration(BaseModel):
    """Input model for ddlc_shared_metadata.component_registry."""

    component_id: Optional[UUID] = None
    component_name: str = Field(..., min_length=1, max_length=200)
    component_version: str = Field(..., min_length=1, max_length=50)
    description: str = Field(..., min_length=1)
    component_type: ComponentType
    interface_spec: dict[str, Any] = Field(default_factory=dict)
    storage_path: str = Field(..., min_length=1)
    embedding: Optional[list[float]] = None
    status: LifecycleStatus = "active"

    @field_validator("embedding", mode="before")
    @classmethod
    def _validate_embedding(cls, value: Any) -> Optional[list[float]]:
        return _coerce_embedding(value)


class ComponentRecord(ComponentRegistration):
    """Persisted record from ddlc_shared_metadata.component_registry."""

    component_id: UUID
    created_at: datetime
    updated_at: datetime


class ComponentSearchResult(ComponentRecord):
    """Semantic search match from ddlc_shared_metadata.component_registry."""

    distance: float
    similarity: float


class ContractRegistration(BaseModel):
    """Input model for ddlc_shared_metadata.contract_registry."""

    contract_id: Optional[UUID] = None
    agent_id: str = Field(..., min_length=1, max_length=100)
    contract_name: str = Field(..., min_length=1, max_length=200)
    contract_version: str = Field(..., min_length=1, max_length=50)
    direction: ContractDirection
    schema_definition: dict[str, Any]
    status: LifecycleStatus = "active"


class ContractRecord(ContractRegistration):
    """Persisted record from ddlc_shared_metadata.contract_registry."""

    contract_id: UUID
    registered_at: datetime
    updated_at: datetime


class BuildSummaryInput(BaseModel):
    """Input model for ddlc_shared_metadata.build_summary."""

    build_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    spec_version: str = Field(..., min_length=1, max_length=50)
    clarification_rounds: int = Field(0, ge=0)
    architecture_revisions: int = Field(0, ge=0)
    impl_doc_revisions: int = Field(0, ge=0)
    build_fix_attempts: int = Field(0, ge=0)
    total_duration_ms: Optional[int] = Field(None, ge=0)
    outcome: BuildOutcome
    completed_at: Optional[datetime] = None


class BuildSummaryRecord(BuildSummaryInput):
    """Persisted record from ddlc_shared_metadata.build_summary."""

    build_id: UUID


class StageExecutionInput(BaseModel):
    """Input model for ddlc_shared_metadata.stage_execution."""

    stage_exec_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    stage_id: str = Field(..., min_length=1, max_length=100)
    stage_name: str = Field(..., min_length=1, max_length=200)
    stage_type: StageType
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: StageStatus = "running"
    input_artifact_ref: Optional[UUID] = None
    output_artifact_ref: Optional[UUID] = None
    tokens_used: int = Field(0, ge=0)
    cost_usd: Decimal = Field(Decimal("0.000000"), ge=Decimal("0"))
    error_details: dict[str, Any] = Field(default_factory=dict)


class StageExecutionRecord(StageExecutionInput):
    """Persisted record from ddlc_shared_metadata.stage_execution."""

    stage_exec_id: UUID
    started_at: datetime


class DataQualitySummaryInput(BaseModel):
    """Input model for ddlc_shared_metadata.data_quality_summary."""

    quality_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    stage_id: str = Field(..., min_length=1, max_length=100)
    contract_ref: Optional[UUID] = None
    assertions_evaluated: int = Field(0, ge=0)
    assertions_passed: int = Field(0, ge=0)
    assertions_failed: int = Field(0, ge=0)
    quality_score: Optional[Decimal] = Field(
        None,
        ge=Decimal("0.0000"),
        le=Decimal("1.0000"),
    )
    failure_details: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: Optional[datetime] = None


class DataQualitySummaryRecord(DataQualitySummaryInput):
    """Persisted record from ddlc_shared_metadata.data_quality_summary."""

    quality_id: UUID
    evaluated_at: datetime


class ConfigSnapshotInput(BaseModel):
    """Input model for ddlc_shared_metadata.config_snapshot."""

    snapshot_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    config_type: ConfigType
    config_body: dict[str, Any]


class ConfigSnapshotRecord(ConfigSnapshotInput):
    """Persisted record from ddlc_shared_metadata.config_snapshot."""

    snapshot_id: UUID
    captured_at: datetime


class GovernanceAuditInput(BaseModel):
    """Input model for ddlc_shared_metadata.governance_audit."""

    gov_audit_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    stage_id: str = Field(..., min_length=1, max_length=100)
    rule_id: str = Field(..., min_length=1, max_length=100)
    rule_name: str = Field(..., min_length=1, max_length=200)
    rule_type: GovernanceRuleType
    evaluation_result: GovernanceEvalResult
    payload_sha256: Optional[str] = Field(None, min_length=1, max_length=64)
    details: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: Optional[datetime] = None


class GovernanceAuditRecord(GovernanceAuditInput):
    """Persisted record from ddlc_shared_metadata.governance_audit."""

    gov_audit_id: UUID
    evaluated_at: datetime


class StageGateInput(BaseModel):
    """Input model for ddlc_shared_metadata.stage_gate_record."""

    gate_id: Optional[UUID] = None
    run_id: UUID
    agent_id: str = Field(..., min_length=1, max_length=100)
    stage_id: str = Field(..., min_length=1, max_length=100)
    gate_type: GateType
    gate_status: GateStatus
    checked_by: Optional[str] = Field(None, max_length=100)
    payload_sha256: Optional[str] = Field(None, min_length=1, max_length=64)
    notes: Optional[str] = None
    evaluated_at: Optional[datetime] = None


class StageGateRecord(StageGateInput):
    """Persisted record from ddlc_shared_metadata.stage_gate_record."""

    gate_id: UUID
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# Transactional Fail-Fast Tier 2 Catalog Client
# ---------------------------------------------------------------------------


class CatalogClient:
    """Transactional fail-fast client for ddlc_shared_metadata (Tier 2).

    Per Decision 30, mutations raise immediately and roll back on validation
    or database failures so registry/audit errors never fail silently.
    """

    def __init__(
        self,
        *,
        db: Optional[MetadataDatabase] = None,
        db_uri: Optional[str] = None,
        pool: Optional[ConnectionPool] = None,
    ) -> None:
        self._db = db if db is not None else MetadataDatabase(db_uri=db_uri, pool=pool)

    # -----------------------------------------------------------------------
    # Agent Registry
    # -----------------------------------------------------------------------

    def register_agent(
        self,
        *,
        agent_id: str,
        agent_name: str,
        agent_version: str,
        architecture_pattern: str,
        runtime: str,
        owner: str,
        description: Optional[str] = None,
        status: AgentStatus = "draft",
        source_repo: Optional[str] = None,
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> AgentRecord:
        """Insert or update an agent record in ddlc_shared_metadata.agent_registry."""
        model = AgentRegistration(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_version=agent_version,
            description=sanitize_text(description),
            architecture_pattern=architecture_pattern,
            runtime=runtime,
            status=status,
            owner=owner,
            source_repo=sanitize_text(source_repo),
            config_snapshot=sanitize_payload(config_snapshot or {}),
        )
        tbl = tier2_table("agent_registry")
        sql = f"""
            INSERT INTO {tbl} (
                agent_id, agent_name, agent_version, description,
                architecture_pattern, runtime, status, owner,
                source_repo, config_snapshot
            )
            VALUES (
                %(agent_id)s, %(agent_name)s, %(agent_version)s, %(description)s,
                %(architecture_pattern)s, %(runtime)s, %(status)s, %(owner)s,
                %(source_repo)s, %(config_snapshot)s
            )
            ON CONFLICT (agent_id) DO UPDATE
            SET agent_name = EXCLUDED.agent_name,
                agent_version = EXCLUDED.agent_version,
                description = EXCLUDED.description,
                architecture_pattern = EXCLUDED.architecture_pattern,
                runtime = EXCLUDED.runtime,
                status = EXCLUDED.status,
                owner = EXCLUDED.owner,
                source_repo = EXCLUDED.source_repo,
                config_snapshot = EXCLUDED.config_snapshot,
                updated_at = clock_timestamp()
            RETURNING agent_id, agent_name, agent_version, description,
                      architecture_pattern, runtime, status, owner,
                      source_repo, config_snapshot, created_at, updated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "agent_id": model.agent_id,
                    "agent_name": model.agent_name,
                    "agent_version": model.agent_version,
                    "description": model.description,
                    "architecture_pattern": model.architecture_pattern,
                    "runtime": model.runtime,
                    "status": model.status,
                    "owner": model.owner,
                    "source_repo": model.source_repo,
                    "config_snapshot": Jsonb(model.config_snapshot),
                },
            ).fetchone()
        return AgentRecord.model_validate(row)

    def get_agent(self, agent_id: str) -> Optional[AgentRecord]:
        """Fetch an agent by agent_id from ddlc_shared_metadata.agent_registry."""
        sql = f"""
            SELECT agent_id, agent_name, agent_version, description,
                   architecture_pattern, runtime, status, owner,
                   source_repo, config_snapshot, created_at, updated_at
            FROM {tier2_table("agent_registry")}
            WHERE agent_id = %(agent_id)s
        """
        with self._db.connection() as conn:
            row = conn.execute(sql, {"agent_id": agent_id}).fetchone()
        return AgentRecord.model_validate(row) if row is not None else None

    def list_agents(
        self,
        *,
        status: Optional[AgentStatus] = None,
        runtime: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> list[AgentRecord]:
        """List registered DDLC agents with optional status/runtime/owner filters."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status
        if runtime is not None:
            clauses.append("runtime = %(runtime)s")
            params["runtime"] = runtime
        if owner is not None:
            clauses.append("owner = %(owner)s")
            params["owner"] = owner

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT agent_id, agent_name, agent_version, description,
                   architecture_pattern, runtime, status, owner,
                   source_repo, config_snapshot, created_at, updated_at
            FROM {tier2_table("agent_registry")}
            {where_sql}
            ORDER BY created_at DESC, agent_id ASC
        """
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [AgentRecord.model_validate(row) for row in rows]

    # -----------------------------------------------------------------------
    # Component Registry & Semantic Vector Search
    # -----------------------------------------------------------------------

    def register_component(
        self,
        *,
        component_name: str,
        component_version: str,
        description: str,
        component_type: ComponentType,
        storage_path: str,
        interface_spec: Optional[dict[str, Any]] = None,
        embedding: Optional[Sequence[float]] = None,
        status: LifecycleStatus = "active",
        component_id: Optional[UUID | str] = None,
    ) -> ComponentRecord:
        """Insert or update a reusable component in ddlc_shared_metadata.component_registry."""
        model = ComponentRegistration(
            component_id=_coerce_uuid(component_id),
            component_name=component_name,
            component_version=component_version,
            description=sanitize_text(description),
            component_type=component_type,
            interface_spec=sanitize_payload(interface_spec or {}),
            storage_path=sanitize_text(storage_path),
            embedding=list(embedding) if embedding is not None else None,
            status=status,
        )
        resolved_id = model.component_id or uuid4()
        vec_param = Vector(model.embedding) if model.embedding is not None else None

        tbl = tier2_table("component_registry")
        sql = f"""
            INSERT INTO {tbl} (
                component_id, component_name, component_version, description,
                component_type, interface_spec, storage_path, embedding, status
            )
            VALUES (
                %(component_id)s, %(component_name)s, %(component_version)s, %(description)s,
                %(component_type)s, %(interface_spec)s, %(storage_path)s, %(embedding)s, %(status)s
            )
            ON CONFLICT (component_name, component_version) DO UPDATE
            SET description = EXCLUDED.description,
                component_type = EXCLUDED.component_type,
                interface_spec = EXCLUDED.interface_spec,
                storage_path = EXCLUDED.storage_path,
                embedding = COALESCE(EXCLUDED.embedding, {tbl}.embedding),
                status = EXCLUDED.status,
                updated_at = clock_timestamp()
            RETURNING component_id, component_name, component_version, description,
                      component_type, interface_spec, storage_path, embedding,
                      status, created_at, updated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "component_id": resolved_id,
                    "component_name": model.component_name,
                    "component_version": model.component_version,
                    "description": model.description,
                    "component_type": model.component_type,
                    "interface_spec": Jsonb(model.interface_spec),
                    "storage_path": model.storage_path,
                    "embedding": vec_param,
                    "status": model.status,
                },
            ).fetchone()
        return ComponentRecord.model_validate(row)

    def get_component(
        self,
        component_id: Optional[UUID | str] = None,
        *,
        component_name: Optional[str] = None,
        component_version: Optional[str] = None,
    ) -> Optional[ComponentRecord]:
        """Fetch a component by component_id or (component_name, optional component_version)."""
        if component_id is None and component_name is None:
            raise ValueError("Provide component_id or component_name to get_component().")

        tbl = tier2_table("component_registry")
        if component_id is not None:
            sql = f"""
                SELECT component_id, component_name, component_version, description,
                       component_type, interface_spec, storage_path, embedding,
                       status, created_at, updated_at
                FROM {tbl}
                WHERE component_id = %(component_id)s
            """
            params: dict[str, Any] = {"component_id": _coerce_uuid(component_id)}
        elif component_version is not None:
            sql = f"""
                SELECT component_id, component_name, component_version, description,
                       component_type, interface_spec, storage_path, embedding,
                       status, created_at, updated_at
                FROM {tbl}
                WHERE component_name = %(component_name)s
                  AND component_version = %(component_version)s
            """
            params = {
                "component_name": component_name,
                "component_version": component_version,
            }
        else:
            sql = f"""
                SELECT component_id, component_name, component_version, description,
                       component_type, interface_spec, storage_path, embedding,
                       status, created_at, updated_at
                FROM {tbl}
                WHERE component_name = %(component_name)s
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            """
            params = {"component_name": component_name}

        with self._db.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return ComponentRecord.model_validate(row) if row is not None else None

    def search_components_semantic(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
        component_type: Optional[ComponentType] = None,
        status: Optional[LifecycleStatus] = "active",
    ) -> list[ComponentSearchResult]:
        """Search component_registry by pgvector cosine distance (<=>)."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1.")
        validated_embedding = _coerce_embedding(query_embedding)
        assert validated_embedding is not None
        query_vec = Vector(validated_embedding)

        clauses = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {
            "query_embedding": query_vec,
            "top_k": top_k,
        }
        if component_type is not None:
            clauses.append("component_type = %(component_type)s")
            params["component_type"] = component_type
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status

        where_sql = " AND ".join(clauses)
        sql = f"""
            SELECT component_id, component_name, component_version, description,
                   component_type, interface_spec, storage_path, embedding,
                   status, created_at, updated_at,
                   (embedding <=> %(query_embedding)s::vector) AS distance,
                   (1.0 - (embedding <=> %(query_embedding)s::vector)) AS similarity
            FROM {tier2_table("component_registry")}
            WHERE {where_sql}
            ORDER BY embedding <=> %(query_embedding)s::vector ASC
            LIMIT %(top_k)s
        """
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ComponentSearchResult.model_validate(row) for row in rows]

    # -----------------------------------------------------------------------
    # Contract Registry
    # -----------------------------------------------------------------------

    def register_contract(
        self,
        *,
        agent_id: str,
        contract_name: str,
        contract_version: str,
        direction: ContractDirection,
        schema_definition: dict[str, Any],
        status: LifecycleStatus = "active",
        contract_id: Optional[UUID | str] = None,
    ) -> ContractRecord:
        """Insert or update a data contract in ddlc_shared_metadata.contract_registry."""
        model = ContractRegistration(
            contract_id=_coerce_uuid(contract_id),
            agent_id=agent_id,
            contract_name=contract_name,
            contract_version=contract_version,
            direction=direction,
            schema_definition=sanitize_payload(schema_definition),
            status=status,
        )
        resolved_id = model.contract_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("contract_registry")} (
                contract_id, agent_id, contract_name, contract_version,
                direction, schema_definition, status
            )
            VALUES (
                %(contract_id)s, %(agent_id)s, %(contract_name)s, %(contract_version)s,
                %(direction)s, %(schema_definition)s, %(status)s
            )
            ON CONFLICT (agent_id, contract_name, contract_version) DO UPDATE
            SET direction = EXCLUDED.direction,
                schema_definition = EXCLUDED.schema_definition,
                status = EXCLUDED.status,
                updated_at = clock_timestamp()
            RETURNING contract_id, agent_id, contract_name, contract_version,
                      direction, schema_definition, status, registered_at, updated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "contract_id": resolved_id,
                    "agent_id": model.agent_id,
                    "contract_name": model.contract_name,
                    "contract_version": model.contract_version,
                    "direction": model.direction,
                    "schema_definition": Jsonb(model.schema_definition),
                    "status": model.status,
                },
            ).fetchone()
        return ContractRecord.model_validate(row)

    def get_contract(
        self,
        contract_id: Optional[UUID | str] = None,
        *,
        agent_id: Optional[str] = None,
        contract_name: Optional[str] = None,
        contract_version: Optional[str] = None,
    ) -> Optional[ContractRecord]:
        """Fetch a contract by contract_id or (agent_id, contract_name, contract_version)."""
        if contract_id is None and (agent_id is None or contract_name is None):
            raise ValueError(
                "Provide contract_id, or agent_id + contract_name to get_contract()."
            )
        tbl = tier2_table("contract_registry")
        if contract_id is not None:
            sql = f"""
                SELECT contract_id, agent_id, contract_name, contract_version,
                       direction, schema_definition, status, registered_at, updated_at
                FROM {tbl}
                WHERE contract_id = %(contract_id)s
            """
            params: dict[str, Any] = {"contract_id": _coerce_uuid(contract_id)}
        elif contract_version is not None:
            sql = f"""
                SELECT contract_id, agent_id, contract_name, contract_version,
                       direction, schema_definition, status, registered_at, updated_at
                FROM {tbl}
                WHERE agent_id = %(agent_id)s
                  AND contract_name = %(contract_name)s
                  AND contract_version = %(contract_version)s
            """
            params = {
                "agent_id": agent_id,
                "contract_name": contract_name,
                "contract_version": contract_version,
            }
        else:
            sql = f"""
                SELECT contract_id, agent_id, contract_name, contract_version,
                       direction, schema_definition, status, registered_at, updated_at
                FROM {tbl}
                WHERE agent_id = %(agent_id)s
                  AND contract_name = %(contract_name)s
                ORDER BY updated_at DESC, registered_at DESC
                LIMIT 1
            """
            params = {
                "agent_id": agent_id,
                "contract_name": contract_name,
            }

        with self._db.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return ContractRecord.model_validate(row) if row is not None else None

    def list_contracts(
        self,
        *,
        agent_id: Optional[str] = None,
        direction: Optional[ContractDirection] = None,
        status: Optional[LifecycleStatus] = None,
    ) -> list[ContractRecord]:
        """List registered data contracts filtered by agent, direction, or status."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if agent_id is not None:
            clauses.append("agent_id = %(agent_id)s")
            params["agent_id"] = agent_id
        if direction is not None:
            clauses.append("direction = %(direction)s")
            params["direction"] = direction
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT contract_id, agent_id, contract_name, contract_version,
                   direction, schema_definition, status, registered_at, updated_at
            FROM {tier2_table("contract_registry")}
            {where_sql}
            ORDER BY registered_at DESC, contract_name ASC
        """
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ContractRecord.model_validate(row) for row in rows]

    # -----------------------------------------------------------------------
    # Operational & Audit Records
    # -----------------------------------------------------------------------

    def record_build_summary(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        spec_version: str,
        outcome: BuildOutcome,
        clarification_rounds: int = 0,
        architecture_revisions: int = 0,
        impl_doc_revisions: int = 0,
        build_fix_attempts: int = 0,
        total_duration_ms: Optional[int] = None,
        completed_at: Optional[datetime] = None,
        build_id: Optional[UUID | str] = None,
    ) -> BuildSummaryRecord:
        """Record or update build metrics in ddlc_shared_metadata.build_summary."""
        model = BuildSummaryInput(
            build_id=_coerce_uuid(build_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            spec_version=spec_version,
            clarification_rounds=clarification_rounds,
            architecture_revisions=architecture_revisions,
            impl_doc_revisions=impl_doc_revisions,
            build_fix_attempts=build_fix_attempts,
            total_duration_ms=total_duration_ms,
            outcome=outcome,
            completed_at=completed_at or datetime.now(timezone.utc),
        )
        resolved_id = model.build_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("build_summary")} (
                build_id, run_id, agent_id, spec_version,
                clarification_rounds, architecture_revisions,
                impl_doc_revisions, build_fix_attempts,
                total_duration_ms, outcome, completed_at
            )
            VALUES (
                %(build_id)s, %(run_id)s, %(agent_id)s, %(spec_version)s,
                %(clarification_rounds)s, %(architecture_revisions)s,
                %(impl_doc_revisions)s, %(build_fix_attempts)s,
                %(total_duration_ms)s, %(outcome)s, %(completed_at)s
            )
            ON CONFLICT (run_id) DO UPDATE
            SET agent_id = EXCLUDED.agent_id,
                spec_version = EXCLUDED.spec_version,
                clarification_rounds = EXCLUDED.clarification_rounds,
                architecture_revisions = EXCLUDED.architecture_revisions,
                impl_doc_revisions = EXCLUDED.impl_doc_revisions,
                build_fix_attempts = EXCLUDED.build_fix_attempts,
                total_duration_ms = EXCLUDED.total_duration_ms,
                outcome = EXCLUDED.outcome,
                completed_at = EXCLUDED.completed_at
            RETURNING build_id, run_id, agent_id, spec_version,
                      clarification_rounds, architecture_revisions,
                      impl_doc_revisions, build_fix_attempts,
                      total_duration_ms, outcome, completed_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "build_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "spec_version": model.spec_version,
                    "clarification_rounds": model.clarification_rounds,
                    "architecture_revisions": model.architecture_revisions,
                    "impl_doc_revisions": model.impl_doc_revisions,
                    "build_fix_attempts": model.build_fix_attempts,
                    "total_duration_ms": model.total_duration_ms,
                    "outcome": model.outcome,
                    "completed_at": model.completed_at,
                },
            ).fetchone()
        return BuildSummaryRecord.model_validate(row)

    def record_stage_execution(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        stage_id: str,
        stage_name: str,
        stage_type: StageType,
        status: StageStatus = "running",
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        input_artifact_ref: Optional[UUID | str] = None,
        output_artifact_ref: Optional[UUID | str] = None,
        tokens_used: int = 0,
        cost_usd: Decimal | float = Decimal("0.000000"),
        error_details: Optional[dict[str, Any]] = None,
        stage_exec_id: Optional[UUID | str] = None,
    ) -> StageExecutionRecord:
        """Insert or update a stage execution record in ddlc_shared_metadata.stage_execution."""
        eff_started = started_at or datetime.now(timezone.utc)
        eff_completed = completed_at
        if status != "running" and eff_completed is None:
            eff_completed = datetime.now(timezone.utc)

        guarded_err = guard_inline_jsonb(error_details or {})
        if not isinstance(guarded_err, dict):
            guarded_err = {"value": guarded_err}

        model = StageExecutionInput(
            stage_exec_id=_coerce_uuid(stage_exec_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            stage_id=stage_id,
            stage_name=stage_name,
            stage_type=stage_type,
            started_at=eff_started,
            completed_at=eff_completed,
            status=status,
            input_artifact_ref=_coerce_uuid(input_artifact_ref),
            output_artifact_ref=_coerce_uuid(output_artifact_ref),
            tokens_used=tokens_used,
            cost_usd=Decimal(str(cost_usd)),
            error_details=guarded_err,
        )
        resolved_id = model.stage_exec_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("stage_execution")} (
                stage_exec_id, run_id, agent_id, stage_id, stage_name, stage_type,
                started_at, completed_at, status,
                input_artifact_ref, output_artifact_ref,
                tokens_used, cost_usd, error_details
            )
            VALUES (
                %(stage_exec_id)s, %(run_id)s, %(agent_id)s, %(stage_id)s, %(stage_name)s, %(stage_type)s,
                %(started_at)s, %(completed_at)s, %(status)s,
                %(input_artifact_ref)s, %(output_artifact_ref)s,
                %(tokens_used)s, %(cost_usd)s, %(error_details)s
            )
            ON CONFLICT (stage_exec_id) DO UPDATE
            SET completed_at = COALESCE(EXCLUDED.completed_at, {tier2_table("stage_execution")}.completed_at),
                status = EXCLUDED.status,
                input_artifact_ref = COALESCE(EXCLUDED.input_artifact_ref, {tier2_table("stage_execution")}.input_artifact_ref),
                output_artifact_ref = COALESCE(EXCLUDED.output_artifact_ref, {tier2_table("stage_execution")}.output_artifact_ref),
                tokens_used = EXCLUDED.tokens_used,
                cost_usd = EXCLUDED.cost_usd,
                error_details = EXCLUDED.error_details
            RETURNING stage_exec_id, run_id, agent_id, stage_id, stage_name, stage_type,
                      started_at, completed_at, status,
                      input_artifact_ref, output_artifact_ref,
                      tokens_used, cost_usd, error_details
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "stage_exec_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "stage_id": model.stage_id,
                    "stage_name": model.stage_name,
                    "stage_type": model.stage_type,
                    "started_at": model.started_at,
                    "completed_at": model.completed_at,
                    "status": model.status,
                    "input_artifact_ref": model.input_artifact_ref,
                    "output_artifact_ref": model.output_artifact_ref,
                    "tokens_used": model.tokens_used,
                    "cost_usd": model.cost_usd,
                    "error_details": Jsonb(model.error_details),
                },
            ).fetchone()
        return StageExecutionRecord.model_validate(row)

    def record_data_quality_summary(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        stage_id: str,
        assertions_evaluated: int,
        assertions_passed: int,
        assertions_failed: int,
        contract_ref: Optional[UUID | str] = None,
        quality_score: Optional[Decimal | float] = None,
        failure_details: Optional[dict[str, Any]] = None,
        evaluated_at: Optional[datetime] = None,
        quality_id: Optional[UUID | str] = None,
    ) -> DataQualitySummaryRecord:
        """Insert a quality evaluation summary in ddlc_shared_metadata.data_quality_summary."""
        guarded_failures = guard_inline_jsonb(failure_details or {})
        if not isinstance(guarded_failures, dict):
            guarded_failures = {"value": guarded_failures}

        model = DataQualitySummaryInput(
            quality_id=_coerce_uuid(quality_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            stage_id=stage_id,
            contract_ref=_coerce_uuid(contract_ref),
            assertions_evaluated=assertions_evaluated,
            assertions_passed=assertions_passed,
            assertions_failed=assertions_failed,
            quality_score=Decimal(str(quality_score)) if quality_score is not None else None,
            failure_details=guarded_failures,
            evaluated_at=evaluated_at or datetime.now(timezone.utc),
        )
        resolved_id = model.quality_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("data_quality_summary")} (
                quality_id, run_id, agent_id, stage_id, contract_ref,
                assertions_evaluated, assertions_passed, assertions_failed,
                quality_score, failure_details, evaluated_at
            )
            VALUES (
                %(quality_id)s, %(run_id)s, %(agent_id)s, %(stage_id)s, %(contract_ref)s,
                %(assertions_evaluated)s, %(assertions_passed)s, %(assertions_failed)s,
                %(quality_score)s, %(failure_details)s, %(evaluated_at)s
            )
            RETURNING quality_id, run_id, agent_id, stage_id, contract_ref,
                      assertions_evaluated, assertions_passed, assertions_failed,
                      quality_score, failure_details, evaluated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "quality_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "stage_id": model.stage_id,
                    "contract_ref": model.contract_ref,
                    "assertions_evaluated": model.assertions_evaluated,
                    "assertions_passed": model.assertions_passed,
                    "assertions_failed": model.assertions_failed,
                    "quality_score": model.quality_score,
                    "failure_details": Jsonb(model.failure_details),
                    "evaluated_at": model.evaluated_at,
                },
            ).fetchone()
        return DataQualitySummaryRecord.model_validate(row)

    def record_config_snapshot(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        config_type: ConfigType,
        config_body: dict[str, Any],
        snapshot_id: Optional[UUID | str] = None,
    ) -> ConfigSnapshotRecord:
        """Persist or update a sanitized configuration snapshot for reproducibility."""
        model = ConfigSnapshotInput(
            snapshot_id=_coerce_uuid(snapshot_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            config_type=config_type,
            config_body=sanitize_payload(config_body),
        )
        resolved_id = model.snapshot_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("config_snapshot")} (
                snapshot_id, run_id, agent_id, config_type, config_body
            )
            VALUES (
                %(snapshot_id)s, %(run_id)s, %(agent_id)s, %(config_type)s, %(config_body)s
            )
            ON CONFLICT (run_id, config_type) DO UPDATE
            SET agent_id = EXCLUDED.agent_id,
                config_body = EXCLUDED.config_body,
                captured_at = clock_timestamp()
            RETURNING snapshot_id, run_id, agent_id, config_type, config_body, captured_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "snapshot_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "config_type": model.config_type,
                    "config_body": Jsonb(model.config_body),
                },
            ).fetchone()
        return ConfigSnapshotRecord.model_validate(row)

    def record_governance_audit(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        stage_id: str,
        rule_id: str,
        rule_name: str,
        rule_type: GovernanceRuleType,
        evaluation_result: GovernanceEvalResult,
        payload_sha256: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        evaluated_at: Optional[datetime] = None,
        gov_audit_id: Optional[UUID | str] = None,
    ) -> GovernanceAuditRecord:
        """Record a governance rule evaluation in ddlc_shared_metadata.governance_audit."""
        guarded_details = guard_inline_jsonb(details or {})
        if not isinstance(guarded_details, dict):
            guarded_details = {"value": guarded_details}

        model = GovernanceAuditInput(
            gov_audit_id=_coerce_uuid(gov_audit_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            stage_id=stage_id,
            rule_id=rule_id,
            rule_name=rule_name,
            rule_type=rule_type,
            evaluation_result=evaluation_result,
            payload_sha256=payload_sha256,
            details=guarded_details,
            evaluated_at=evaluated_at or datetime.now(timezone.utc),
        )
        resolved_id = model.gov_audit_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("governance_audit")} (
                gov_audit_id, run_id, agent_id, stage_id,
                rule_id, rule_name, rule_type, evaluation_result,
                payload_sha256, details, evaluated_at
            )
            VALUES (
                %(gov_audit_id)s, %(run_id)s, %(agent_id)s, %(stage_id)s,
                %(rule_id)s, %(rule_name)s, %(rule_type)s, %(evaluation_result)s,
                %(payload_sha256)s, %(details)s, %(evaluated_at)s
            )
            RETURNING gov_audit_id, run_id, agent_id, stage_id,
                      rule_id, rule_name, rule_type, evaluation_result,
                      payload_sha256, details, evaluated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "gov_audit_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "stage_id": model.stage_id,
                    "rule_id": model.rule_id,
                    "rule_name": model.rule_name,
                    "rule_type": model.rule_type,
                    "evaluation_result": model.evaluation_result,
                    "payload_sha256": model.payload_sha256,
                    "details": Jsonb(model.details),
                    "evaluated_at": model.evaluated_at,
                },
            ).fetchone()
        return GovernanceAuditRecord.model_validate(row)

    def record_stage_gate(
        self,
        *,
        run_id: UUID | str,
        agent_id: str,
        stage_id: str,
        gate_type: GateType,
        gate_status: GateStatus,
        checked_by: Optional[str] = None,
        payload_sha256: Optional[str] = None,
        notes: Optional[str] = None,
        evaluated_at: Optional[datetime] = None,
        gate_id: Optional[UUID | str] = None,
    ) -> StageGateRecord:
        """Record or resolve a stage boundary gate check with optional Claim Check SHA-256 freeze."""
        model = StageGateInput(
            gate_id=_coerce_uuid(gate_id),
            run_id=_coerce_uuid(run_id),
            agent_id=agent_id,
            stage_id=stage_id,
            gate_type=gate_type,
            gate_status=gate_status,
            checked_by=checked_by,
            payload_sha256=payload_sha256,
            notes=sanitize_text(notes),
            evaluated_at=evaluated_at or datetime.now(timezone.utc),
        )
        resolved_id = model.gate_id or uuid4()
        tbl = tier2_table("stage_gate_record")
        sql = f"""
            INSERT INTO {tbl} (
                gate_id, run_id, agent_id, stage_id,
                gate_type, gate_status, checked_by,
                payload_sha256, notes, evaluated_at
            )
            VALUES (
                %(gate_id)s, %(run_id)s, %(agent_id)s, %(stage_id)s,
                %(gate_type)s, %(gate_status)s, %(checked_by)s,
                %(payload_sha256)s, %(notes)s, %(evaluated_at)s
            )
            ON CONFLICT (gate_id) DO UPDATE
            SET gate_status = EXCLUDED.gate_status,
                checked_by = COALESCE(EXCLUDED.checked_by, {tbl}.checked_by),
                payload_sha256 = COALESCE(EXCLUDED.payload_sha256, {tbl}.payload_sha256),
                notes = COALESCE(EXCLUDED.notes, {tbl}.notes),
                evaluated_at = EXCLUDED.evaluated_at
            RETURNING gate_id, run_id, agent_id, stage_id,
                      gate_type, gate_status, checked_by,
                      payload_sha256, notes, evaluated_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "gate_id": resolved_id,
                    "run_id": model.run_id,
                    "agent_id": model.agent_id,
                    "stage_id": model.stage_id,
                    "gate_type": model.gate_type,
                    "gate_status": model.gate_status,
                    "checked_by": model.checked_by,
                    "payload_sha256": model.payload_sha256,
                    "notes": model.notes,
                    "evaluated_at": model.evaluated_at,
                },
            ).fetchone()
        return StageGateRecord.model_validate(row)
