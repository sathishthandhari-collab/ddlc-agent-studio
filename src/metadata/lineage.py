"""Tier 2 & Tier 1 transactional lineage, dependency graph, and Bill-of-Materials client."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from src.metadata.db import MetadataDatabase, tier1_table, tier2_table

LineageStatus = Literal["active", "deprecated", "proposed"]
DependencyType = Literal["data", "component", "config"]
UsageType = Literal["import", "extend", "wrap"]
ManifestRole = Literal["input", "output", "intermediate"]


def _coerce_uuid(value: Optional[UUID | str]) -> Optional[UUID]:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class ContractLineageInput(BaseModel):
    """Input model for ddlc_shared_metadata.contract_lineage."""

    lineage_id: Optional[UUID] = None
    producer_agent_id: str = Field(..., min_length=1, max_length=100)
    producer_contract_id: UUID
    consumer_agent_id: str = Field(..., min_length=1, max_length=100)
    consumer_contract_id: UUID
    status: LineageStatus = "active"


class ContractLineageRecord(ContractLineageInput):
    """Persisted record from ddlc_shared_metadata.contract_lineage."""

    lineage_id: UUID
    established_at: datetime


class AgentDependencyInput(BaseModel):
    """Input model for ddlc_shared_metadata.agent_dependency."""

    dependency_id: Optional[UUID] = None
    upstream_agent_id: str = Field(..., min_length=1, max_length=100)
    downstream_agent_id: str = Field(..., min_length=1, max_length=100)
    dependency_type: DependencyType
    contract_id: Optional[UUID] = None
    status: LineageStatus = "active"


class AgentDependencyRecord(AgentDependencyInput):
    """Persisted record from ddlc_shared_metadata.agent_dependency."""

    dependency_id: UUID
    established_at: datetime


class ComponentUsageInput(BaseModel):
    """Input model for ddlc_shared_metadata.component_usage."""

    usage_id: Optional[UUID] = None
    agent_id: str = Field(..., min_length=1, max_length=100)
    component_id: UUID
    component_version: str = Field(..., min_length=1, max_length=50)
    usage_type: UsageType


class ComponentUsageRecord(ComponentUsageInput):
    """Persisted record from ddlc_shared_metadata.component_usage."""

    usage_id: UUID
    first_used_at: datetime
    last_used_at: datetime


class DownstreamImpactNode(BaseModel):
    """Single reachable downstream node returned by recursive impact traversal."""

    upstream_agent_id: str
    downstream_agent_id: str
    dependency_type: DependencyType
    contract_id: Optional[UUID] = None
    status: LineageStatus
    depth: int
    path: list[str]
    downstream_agent_name: Optional[str] = None
    downstream_agent_version: Optional[str] = None
    downstream_agent_status: Optional[str] = None


class UpstreamDependencyRecord(BaseModel):
    """Direct upstream dependency enriched with upstream agent and contract metadata."""

    dependency_id: UUID
    upstream_agent_id: str
    downstream_agent_id: str
    dependency_type: DependencyType
    contract_id: Optional[UUID] = None
    status: LineageStatus
    established_at: datetime
    upstream_agent_name: Optional[str] = None
    upstream_agent_version: Optional[str] = None
    upstream_agent_status: Optional[str] = None
    contract_name: Optional[str] = None
    contract_version: Optional[str] = None


class ComponentConsumerRecord(BaseModel):
    """Agent consuming a shared component from component_usage joined with agent_registry."""

    usage_id: UUID
    agent_id: str
    component_id: UUID
    component_version: str
    usage_type: UsageType
    first_used_at: datetime
    last_used_at: datetime
    agent_name: Optional[str] = None
    agent_version: Optional[str] = None
    agent_status: Optional[str] = None


class RunArtifactManifestEntry(BaseModel):
    """Joined Tier 1 Bill-of-Materials row (artifact_manifest + artifact_registry)."""

    manifest_id: UUID
    run_id: UUID
    artifact_id: UUID
    role: ManifestRole
    associated_log_id: Optional[UUID] = None
    associated_phase: Optional[str] = None
    associated_at: datetime
    artifact_name: str
    artifact_kind: str
    artifact_version: str
    storage_uri: str
    checksum_sha256: str
    byte_size: Optional[int] = None
    mime_type: Optional[str] = None
    produced_run_id: Optional[UUID] = None
    produced_log_id: Optional[UUID] = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transactional Lineage Client
# ---------------------------------------------------------------------------


class LineageClient:
    """Transactional fail-fast client for Tier 2 lineage and Tier 1 Bill-of-Materials queries."""

    def __init__(
        self,
        *,
        db: Optional[MetadataDatabase] = None,
        db_uri: Optional[str] = None,
        pool: Optional[ConnectionPool] = None,
    ) -> None:
        self._db = db if db is not None else MetadataDatabase(db_uri=db_uri, pool=pool)

    def record_contract_lineage(
        self,
        *,
        producer_agent_id: str,
        producer_contract_id: UUID | str,
        consumer_agent_id: str,
        consumer_contract_id: UUID | str,
        status: LineageStatus = "active",
        lineage_id: Optional[UUID | str] = None,
    ) -> ContractLineageRecord:
        """Record or update a producer -> consumer contract edge in contract_lineage."""
        prod_cid = _coerce_uuid(producer_contract_id)
        cons_cid = _coerce_uuid(consumer_contract_id)
        if prod_cid == cons_cid:
            raise ValueError("producer_contract_id and consumer_contract_id cannot be identical.")

        model = ContractLineageInput(
            lineage_id=_coerce_uuid(lineage_id),
            producer_agent_id=producer_agent_id,
            producer_contract_id=prod_cid,
            consumer_agent_id=consumer_agent_id,
            consumer_contract_id=cons_cid,
            status=status,
        )
        resolved_id = model.lineage_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("contract_lineage")} (
                lineage_id, producer_agent_id, producer_contract_id,
                consumer_agent_id, consumer_contract_id, status
            )
            VALUES (
                %(lineage_id)s, %(producer_agent_id)s, %(producer_contract_id)s,
                %(consumer_agent_id)s, %(consumer_contract_id)s, %(status)s
            )
            ON CONFLICT (producer_contract_id, consumer_contract_id) DO UPDATE
            SET producer_agent_id = EXCLUDED.producer_agent_id,
                consumer_agent_id = EXCLUDED.consumer_agent_id,
                status = EXCLUDED.status
            RETURNING lineage_id, producer_agent_id, producer_contract_id,
                      consumer_agent_id, consumer_contract_id, status, established_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "lineage_id": resolved_id,
                    "producer_agent_id": model.producer_agent_id,
                    "producer_contract_id": model.producer_contract_id,
                    "consumer_agent_id": model.consumer_agent_id,
                    "consumer_contract_id": model.consumer_contract_id,
                    "status": model.status,
                },
            ).fetchone()
        return ContractLineageRecord.model_validate(row)

    def record_agent_dependency(
        self,
        *,
        upstream_agent_id: str,
        downstream_agent_id: str,
        dependency_type: DependencyType,
        contract_id: Optional[UUID | str] = None,
        status: LineageStatus = "active",
        dependency_id: Optional[UUID | str] = None,
    ) -> AgentDependencyRecord:
        """Record or update an upstream -> downstream agent edge in agent_dependency."""
        if upstream_agent_id == downstream_agent_id:
            raise ValueError("upstream_agent_id and downstream_agent_id cannot be identical.")

        model = AgentDependencyInput(
            dependency_id=_coerce_uuid(dependency_id),
            upstream_agent_id=upstream_agent_id,
            downstream_agent_id=downstream_agent_id,
            dependency_type=dependency_type,
            contract_id=_coerce_uuid(contract_id),
            status=status,
        )
        resolved_id = model.dependency_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("agent_dependency")} (
                dependency_id, upstream_agent_id, downstream_agent_id,
                dependency_type, contract_id, status
            )
            VALUES (
                %(dependency_id)s, %(upstream_agent_id)s, %(downstream_agent_id)s,
                %(dependency_type)s, %(contract_id)s, %(status)s
            )
            ON CONFLICT (upstream_agent_id, downstream_agent_id, dependency_type) DO UPDATE
            SET contract_id = EXCLUDED.contract_id,
                status = EXCLUDED.status
            RETURNING dependency_id, upstream_agent_id, downstream_agent_id,
                      dependency_type, contract_id, status, established_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "dependency_id": resolved_id,
                    "upstream_agent_id": model.upstream_agent_id,
                    "downstream_agent_id": model.downstream_agent_id,
                    "dependency_type": model.dependency_type,
                    "contract_id": model.contract_id,
                    "status": model.status,
                },
            ).fetchone()
        return AgentDependencyRecord.model_validate(row)

    def record_component_usage(
        self,
        *,
        agent_id: str,
        component_id: UUID | str,
        component_version: str,
        usage_type: UsageType,
        usage_id: Optional[UUID | str] = None,
    ) -> ComponentUsageRecord:
        """Record or update agent usage of a shared component in component_usage."""
        model = ComponentUsageInput(
            usage_id=_coerce_uuid(usage_id),
            agent_id=agent_id,
            component_id=_coerce_uuid(component_id),
            component_version=component_version,
            usage_type=usage_type,
        )
        resolved_id = model.usage_id or uuid4()
        sql = f"""
            INSERT INTO {tier2_table("component_usage")} (
                usage_id, agent_id, component_id, component_version, usage_type
            )
            VALUES (
                %(usage_id)s, %(agent_id)s, %(component_id)s, %(component_version)s, %(usage_type)s
            )
            ON CONFLICT (agent_id, component_id) DO UPDATE
            SET component_version = EXCLUDED.component_version,
                usage_type = EXCLUDED.usage_type,
                last_used_at = clock_timestamp()
            RETURNING usage_id, agent_id, component_id, component_version,
                      usage_type, first_used_at, last_used_at
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                sql,
                {
                    "usage_id": resolved_id,
                    "agent_id": model.agent_id,
                    "component_id": model.component_id,
                    "component_version": model.component_version,
                    "usage_type": model.usage_type,
                },
            ).fetchone()
        return ComponentUsageRecord.model_validate(row)

    def get_downstream_impact(
        self,
        agent_id: str,
        max_depth: int = 10,
        *,
        statuses: Sequence[LineageStatus] = ("active",),
    ) -> list[DownstreamImpactNode]:
        """Traverse agent_dependency recursively with cycle protection."""
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1.")
        status_list = list(statuses)
        if not status_list:
            return []

        dep_tbl = tier2_table("agent_dependency")
        reg_tbl = tier2_table("agent_registry")
        sql = f"""
            WITH RECURSIVE impact_tree AS (
                SELECT
                    d.upstream_agent_id,
                    d.downstream_agent_id,
                    d.dependency_type,
                    d.contract_id,
                    d.status,
                    1 AS depth,
                    ARRAY[d.upstream_agent_id, d.downstream_agent_id]::TEXT[] AS path
                FROM {dep_tbl} AS d
                WHERE d.upstream_agent_id = %(agent_id)s
                  AND d.status = ANY(%(statuses)s)

                UNION ALL

                SELECT
                    d.upstream_agent_id,
                    d.downstream_agent_id,
                    d.dependency_type,
                    d.contract_id,
                    d.status,
                    t.depth + 1 AS depth,
                    t.path || d.downstream_agent_id::TEXT
                FROM {dep_tbl} AS d
                JOIN impact_tree AS t
                  ON d.upstream_agent_id = t.downstream_agent_id
                WHERE t.depth < %(max_depth)s
                  AND d.status = ANY(%(statuses)s)
                  AND NOT (d.downstream_agent_id::TEXT = ANY(t.path))
            )
            SELECT
                it.upstream_agent_id,
                it.downstream_agent_id,
                it.dependency_type,
                it.contract_id,
                it.status,
                it.depth,
                it.path,
                ar.agent_name AS downstream_agent_name,
                ar.agent_version AS downstream_agent_version,
                ar.status AS downstream_agent_status
            FROM impact_tree AS it
            LEFT JOIN {reg_tbl} AS ar
              ON ar.agent_id = it.downstream_agent_id
            ORDER BY it.depth ASC, it.downstream_agent_id ASC
        """
        with self._db.connection() as conn:
            rows = conn.execute(
                sql,
                {
                    "agent_id": agent_id,
                    "max_depth": max_depth,
                    "statuses": status_list,
                },
            ).fetchall()
        return [DownstreamImpactNode.model_validate(row) for row in rows]

    def get_upstream_dependencies(
        self,
        agent_id: str,
        *,
        statuses: Sequence[LineageStatus] = ("active",),
    ) -> list[UpstreamDependencyRecord]:
        """List upstream agents and contracts that an agent depends on."""
        status_list = list(statuses)
        if not status_list:
            return []

        dep_tbl = tier2_table("agent_dependency")
        reg_tbl = tier2_table("agent_registry")
        con_tbl = tier2_table("contract_registry")
        sql = f"""
            SELECT
                d.dependency_id,
                d.upstream_agent_id,
                d.downstream_agent_id,
                d.dependency_type,
                d.contract_id,
                d.status,
                d.established_at,
                ar.agent_name AS upstream_agent_name,
                ar.agent_version AS upstream_agent_version,
                ar.status AS upstream_agent_status,
                cr.contract_name AS contract_name,
                cr.contract_version AS contract_version
            FROM {dep_tbl} AS d
            LEFT JOIN {reg_tbl} AS ar
              ON ar.agent_id = d.upstream_agent_id
            LEFT JOIN {con_tbl} AS cr
              ON cr.contract_id = d.contract_id
            WHERE d.downstream_agent_id = %(agent_id)s
              AND d.status = ANY(%(statuses)s)
            ORDER BY d.established_at DESC, d.upstream_agent_id ASC
        """
        with self._db.connection() as conn:
            rows = conn.execute(
                sql,
                {"agent_id": agent_id, "statuses": status_list},
            ).fetchall()
        return [UpstreamDependencyRecord.model_validate(row) for row in rows]

    def get_component_consumers(
        self,
        component_id: UUID | str,
    ) -> list[ComponentConsumerRecord]:
        """List all agents using a given shared component."""
        cid = _coerce_uuid(component_id)
        usage_tbl = tier2_table("component_usage")
        reg_tbl = tier2_table("agent_registry")
        sql = f"""
            SELECT
                cu.usage_id,
                cu.agent_id,
                cu.component_id,
                cu.component_version,
                cu.usage_type,
                cu.first_used_at,
                cu.last_used_at,
                ar.agent_name AS agent_name,
                ar.agent_version AS agent_version,
                ar.status AS agent_status
            FROM {usage_tbl} AS cu
            LEFT JOIN {reg_tbl} AS ar
              ON ar.agent_id = cu.agent_id
            WHERE cu.component_id = %(component_id)s
            ORDER BY cu.last_used_at DESC, cu.agent_id ASC
        """
        with self._db.connection() as conn:
            rows = conn.execute(sql, {"component_id": cid}).fetchall()
        return [ComponentConsumerRecord.model_validate(row) for row in rows]

    def get_run_artifact_manifest(
        self,
        run_id: UUID | str,
        *,
        role: Optional[ManifestRole] = None,
    ) -> list[RunArtifactManifestEntry]:
        """Return a run's Bill of Materials by joining Tier 1 artifact_manifest and artifact_registry."""
        rid = _coerce_uuid(run_id)
        manifest_tbl = tier1_table("artifact_manifest")
        artifact_tbl = tier1_table("artifact_registry")
        role_clause = "AND m.role = %(role)s" if role is not None else ""
        sql = f"""
            SELECT
                m.manifest_id,
                m.run_id,
                m.artifact_id,
                m.role,
                m.associated_log_id,
                m.associated_phase,
                m.associated_at,
                a.artifact_name,
                a.artifact_kind,
                a.artifact_version,
                a.storage_uri,
                a.checksum_sha256,
                a.byte_size,
                a.mime_type,
                a.produced_run_id,
                a.produced_log_id,
                a.created_at,
                a.metadata
            FROM {manifest_tbl} AS m
            JOIN {artifact_tbl} AS a
              ON a.artifact_id = m.artifact_id
            WHERE m.run_id = %(run_id)s
              {role_clause}
            ORDER BY m.associated_at ASC, m.manifest_id ASC
        """
        params: dict[str, Any] = {"run_id": rid}
        if role is not None:
            params["role"] = role
        with self._db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [RunArtifactManifestEntry.model_validate(row) for row in rows]
