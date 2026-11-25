"""Unit and live PostgreSQL integration tests for CatalogClient and LineageClient."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg
import pytest
from pgvector import Vector
from pydantic import ValidationError
from src.core.settings import get_env_settings, get_studio_config
from src.metadata.callbacks import MetadataCallbackHandler
from src.metadata.catalog import CatalogClient, ComponentRecord
from src.metadata.db import MetadataDatabase
from src.metadata.lineage import LineageClient


class _MockCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> Optional[dict[str, Any]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _MockConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rolled_back = False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise

    def execute(self, sql: str, params: Optional[dict[str, Any]] = None) -> _MockCursor:
        self.calls.append((sql, params or {}))
        return _MockCursor(self.rows)


class _MockPool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[Any]:
        yield self._conn


def _unit_embedding(dim_index: int = 0) -> list[float]:
    dims = get_studio_config().rag.embedding.dimensions
    vec = [0.0] * dims
    vec[dim_index] = 1.0
    return vec


def _open_live_db_or_skip() -> str:
    uri = get_env_settings().metadata_db_uri
    if not uri:
        pytest.skip("METADATA_DB_URI is not configured.")
    try:
        probe = psycopg.connect(uri, connect_timeout=2)
        probe.close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")
    return uri


class TestCatalogAndLineageValidationUnit:
    def test_catalog_fail_fast_propagates_db_errors_and_rolls_back(self) -> None:
        class _ExplodingConn(_MockConnection):
            def execute(self, sql: str, params: Optional[dict[str, Any]] = None) -> _MockCursor:
                raise psycopg.IntegrityError("duplicate key or fk violation")

        conn = _ExplodingConn()
        client = CatalogClient(db=MetadataDatabase(pool=_MockPool(conn)))
        with pytest.raises(psycopg.IntegrityError):
            client.register_agent(
                agent_id="bad-agent",
                agent_name="Bad Agent",
                agent_version="0.1.0",
                architecture_pattern="staged_gated_pipeline",
                runtime="langgraph",
                owner="platform",
            )
        assert conn.rolled_back is True

    def test_catalog_pydantic_validation_constraints(self) -> None:
        client = CatalogClient(db=MetadataDatabase(pool=_MockPool(_MockConnection())))

        # Invalid agent status
        with pytest.raises(ValidationError):
            client.register_agent(
                agent_id="a1",
                agent_name="A1",
                agent_version="1.0.0",
                architecture_pattern="staged_gated_pipeline",
                runtime="langgraph",
                owner="team",
                status="unknown_status",  # type: ignore[arg-type]
            )

        # Invalid embedding dimensions (!= 1536)
        with pytest.raises(ValidationError, match="dimension mismatch"):
            client.register_component(
                component_name="pii_masker",
                component_version="1.0.0",
                description="Masks fields",
                component_type="masker",
                storage_path="_shared/masker.py",
                embedding=[0.1, 0.2, 0.3],
            )

        # Decision 26 stage_type validation: reject legacy 'ingestion'
        with pytest.raises(ValidationError):
            client.record_stage_execution(
                run_id=uuid4(),
                agent_id="a1",
                stage_id="s1",
                stage_name="Ingest",
                stage_type="ingestion",  # type: ignore[arg-type]
            )

        # Quality score outside [0, 1]
        with pytest.raises(ValidationError):
            client.record_data_quality_summary(
                run_id=uuid4(),
                agent_id="a1",
                stage_id="s1",
                assertions_evaluated=10,
                assertions_passed=10,
                assertions_failed=0,
                quality_score=1.25,
            )

        # Oversized payload_sha256 (> 64 chars)
        with pytest.raises(ValidationError):
            client.record_stage_gate(
                run_id=uuid4(),
                agent_id="a1",
                stage_id="review",
                gate_type="human_approval",
                gate_status="passed",
                payload_sha256="x" * 65,
            )

    def test_component_record_coerces_pgvector_Vector(self) -> None:
        dims = get_studio_config().rag.embedding.dimensions
        vec = Vector([0.5] * dims)
        record = ComponentRecord.model_validate(
            {
                "component_id": uuid4(),
                "component_name": "comp",
                "component_version": "1.0.0",
                "description": "test",
                "component_type": "utility",
                "interface_spec": {},
                "storage_path": "_shared/comp.py",
                "embedding": vec,
                "status": "active",
                "created_at": "2026-11-25T12:00:00Z",
                "updated_at": "2026-11-25T12:00:00Z",
            }
        )
        assert isinstance(record.embedding, list)
        assert len(record.embedding) == dims

    def test_search_components_semantic_uses_cosine_distance_sql(self) -> None:
        conn = _MockConnection(rows=[])
        client = CatalogClient(db=MetadataDatabase(pool=_MockPool(conn)))
        results = client.search_components_semantic(
            _unit_embedding(0),
            top_k=3,
            component_type="masker",
            status="active",
        )
        assert results == []
        sql, params = conn.calls[0]
        assert "<=>" in sql
        assert "component_type = %(component_type)s" in sql
        assert "status = %(status)s" in sql
        assert params["top_k"] == 3

        with pytest.raises(ValueError, match="top_k"):
            client.search_components_semantic(_unit_embedding(0), top_k=0)

    def test_lineage_validation_rejects_self_loops_and_bad_depth(self) -> None:
        lineage = LineageClient(db=MetadataDatabase(pool=_MockPool(_MockConnection())))
        same_cid = uuid4()
        with pytest.raises(ValueError, match="identical"):
            lineage.record_contract_lineage(
                producer_agent_id="a",
                producer_contract_id=same_cid,
                consumer_agent_id="b",
                consumer_contract_id=same_cid,
            )
        with pytest.raises(ValueError, match="identical"):
            lineage.record_agent_dependency(
                upstream_agent_id="a",
                downstream_agent_id="a",
                dependency_type="data",
            )
        with pytest.raises(ValueError, match="max_depth"):
            lineage.get_downstream_impact("a", max_depth=0)


class TestCatalogAndLineageLivePostgresIntegration:
    def test_tier2_catalog_vector_search_lineage_and_bom_roundtrip(self) -> None:
        uri = _open_live_db_or_skip()
        suffix = uuid4().hex[:8]
        agent_a = f"test-agent-a-{suffix}"
        agent_b = f"test-agent-b-{suffix}"
        agent_c = f"test-agent-c-{suffix}"
        comp_masker_name = f"test_masker_{suffix}"
        comp_validator_name = f"test_validator_{suffix}"

        with MetadataDatabase(db_uri=uri) as db:
            catalog = CatalogClient(db=db)
            lineage = LineageClient(db=db)
            callbacks = MetadataCallbackHandler(
                db=db,
                agent_id=agent_a,
                agent_version="1.0.0",
                initiator_id="pytest@ddlc.local",
                initiator_type="system",
                trigger_type="manual",
            )

            run_id: Optional[UUID] = None
            artifact_id: Optional[UUID] = None
            comp_ids: list[UUID] = []

            try:
                # 1. Register agents (verify config_snapshot secret redaction)
                rec_a = catalog.register_agent(
                    agent_id=agent_a,
                    agent_name="Upstream Agent",
                    agent_version="1.0.0",
                    architecture_pattern="staged_gated_pipeline",
                    runtime="langgraph",
                    owner="data-eng",
                    status="active",
                    config_snapshot={"api_key": "sk-agentSecretKey1234567890", "retries": 2},
                )
                assert rec_a.config_snapshot["api_key"] == "[REDACTED]"

                catalog.register_agent(
                    agent_id=agent_b,
                    agent_name="Mid Agent",
                    agent_version="1.0.0",
                    architecture_pattern="staged_gated_pipeline",
                    runtime="langgraph",
                    owner="data-eng",
                    status="active",
                )
                catalog.register_agent(
                    agent_id=agent_c,
                    agent_name="Downstream Agent",
                    agent_version="1.0.0",
                    architecture_pattern="python_litellm",
                    runtime="python_litellm",
                    owner="analytics",
                    status="active",
                )
                assert catalog.get_agent(agent_a) is not None
                assert any(a.agent_id == agent_b for a in catalog.list_agents(status="active"))

                # 2. Register components with pgvector embeddings & run semantic search
                comp_masker = catalog.register_component(
                    component_name=comp_masker_name,
                    component_version="1.0.0",
                    description="Enterprise classification tagger & PII masker",
                    component_type="masker",
                    storage_path="_shared/masker.py",
                    interface_spec={"input": "dict", "output": "dict"},
                    embedding=_unit_embedding(0),
                    status="active",
                )
                comp_ids.append(comp_masker.component_id)
                comp_validator = catalog.register_component(
                    component_name=comp_validator_name,
                    component_version="1.0.0",
                    description="SQLGlot syntax validator",
                    component_type="validator",
                    storage_path="_shared/validator.py",
                    embedding=_unit_embedding(1),
                    status="active",
                )
                comp_ids.append(comp_validator.component_id)

                # Verify ON CONFLICT DO UPDATE preserves existing embedding via COALESCE
                updated_masker = catalog.register_component(
                    component_name=comp_masker_name,
                    component_version="1.0.0",
                    description="Updated description preserving embedding",
                    component_type="masker",
                    storage_path="_shared/masker_v2.py",
                    embedding=None,
                    status="active",
                )
                assert updated_masker.component_id == comp_masker.component_id
                assert updated_masker.embedding is not None

                fetched_by_name = catalog.get_component(
                    component_name=comp_masker_name,
                    component_version="1.0.0",
                )
                assert fetched_by_name is not None
                assert fetched_by_name.storage_path == "_shared/masker_v2.py"

                fetched_comp = catalog.get_component(comp_masker.component_id)
                assert fetched_comp is not None
                assert fetched_comp.component_name == comp_masker_name

                matches = catalog.search_components_semantic(
                    _unit_embedding(0),
                    top_k=2,
                    component_type="masker",
                    status="active",
                )
                assert len(matches) >= 1
                assert matches[0].component_id == comp_masker.component_id
                assert matches[0].similarity > 0.99

                # 3. Register contracts
                c_out = catalog.register_contract(
                    agent_id=agent_a,
                    contract_name="curated_orders",
                    contract_version="1.0.0",
                    direction="output",
                    schema_definition={"columns": [{"name": "order_id", "type": "UUID"}]},
                )
                c_in = catalog.register_contract(
                    agent_id=agent_b,
                    contract_name="raw_orders_input",
                    contract_version="1.0.0",
                    direction="input",
                    schema_definition={"columns": [{"name": "order_id", "type": "UUID"}]},
                )
                assert catalog.get_contract(c_out.contract_id) is not None
                assert catalog.get_contract(
                    agent_id=agent_a,
                    contract_name="curated_orders",
                    contract_version="1.0.0",
                ) is not None
                assert len(catalog.list_contracts(agent_id=agent_a)) == 1

                # 4. Lineage: contract_lineage, agent_dependency with cycle A->B->C->A, component_usage
                edge_cl = lineage.record_contract_lineage(
                    producer_agent_id=agent_a,
                    producer_contract_id=c_out.contract_id,
                    consumer_agent_id=agent_b,
                    consumer_contract_id=c_in.contract_id,
                )
                assert edge_cl.status == "active"

                lineage.record_agent_dependency(
                    upstream_agent_id=agent_a,
                    downstream_agent_id=agent_b,
                    dependency_type="data",
                    contract_id=c_out.contract_id,
                )
                lineage.record_agent_dependency(
                    upstream_agent_id=agent_b,
                    downstream_agent_id=agent_c,
                    dependency_type="data",
                )
                # Deliberate cycle back to A to verify cycle protection
                lineage.record_agent_dependency(
                    upstream_agent_id=agent_c,
                    downstream_agent_id=agent_a,
                    dependency_type="config",
                )

                impact = lineage.get_downstream_impact(agent_a, max_depth=10)
                impacted_ids = [(n.downstream_agent_id, n.depth) for n in impact]
                assert (agent_b, 1) in impacted_ids
                assert (agent_c, 2) in impacted_ids
                # Cycle to root A must be stopped (A never appears as downstream target in path)
                assert all(n.downstream_agent_id != agent_a for n in impact)

                upstream = lineage.get_upstream_dependencies(agent_b)
                assert len(upstream) == 1
                assert upstream[0].upstream_agent_id == agent_a
                assert upstream[0].contract_name == "curated_orders"

                lineage.record_component_usage(
                    agent_id=agent_b,
                    component_id=comp_masker.component_id,
                    component_version="1.0.0",
                    usage_type="import",
                )
                consumers = lineage.get_component_consumers(comp_masker.component_id)
                assert len(consumers) == 1
                assert consumers[0].agent_id == agent_b
                assert consumers[0].agent_name == "Mid Agent"

                # 5. Tier 1 Run + Artifact + Tier 2 Operational/Audit records
                run_id = callbacks.start_run()
                assert run_id is not None
                sha = "f" * 64
                artifact_id = callbacks.record_artifact(
                    artifact_name="dbt_bundle.tar.gz",
                    artifact_kind="code",
                    storage_uri="s3://ddlc-artifacts/test/dbt_bundle.tar.gz",
                    checksum_sha256=sha,
                    byte_size=2048,
                    produced_run_id=run_id,
                )
                assert artifact_id is not None
                callbacks.link_artifact_manifest(
                    artifact_id=artifact_id,
                    role="output",
                    associated_phase="delivery",
                )

                bom = lineage.get_run_artifact_manifest(run_id, role="output")
                assert len(bom) == 1
                assert bom[0].artifact_id == artifact_id
                assert bom[0].checksum_sha256 == sha
                assert lineage.get_run_artifact_manifest(run_id, role="input") == []

                build = catalog.record_build_summary(
                    run_id=run_id,
                    agent_id=agent_a,
                    spec_version="1.0.0",
                    clarification_rounds=1,
                    architecture_revisions=1,
                    impl_doc_revisions=0,
                    build_fix_attempts=0,
                    total_duration_ms=4200,
                    outcome="success",
                )
                assert build.outcome == "success"

                stage = catalog.record_stage_execution(
                    run_id=run_id,
                    agent_id=agent_a,
                    stage_id="stage_generate",
                    stage_name="Generate dbt Models",
                    stage_type="code_generation",
                    status="completed",
                    input_artifact_ref=artifact_id,
                    output_artifact_ref=artifact_id,
                    tokens_used=210,
                    cost_usd=Decimal("0.001500"),
                )
                assert stage.status == "completed"

                dq = catalog.record_data_quality_summary(
                    run_id=run_id,
                    agent_id=agent_a,
                    stage_id="stage_review",
                    contract_ref=c_out.contract_id,
                    assertions_evaluated=10,
                    assertions_passed=10,
                    assertions_failed=0,
                    quality_score=Decimal("1.0000"),
                )
                assert dq.quality_score == Decimal("1.0000")

                cfg = catalog.record_config_snapshot(
                    run_id=run_id,
                    agent_id=agent_a,
                    config_type="model_tiers",
                    config_body={"tier": "balanced", "secret": "Bearer abcSecretToken12345"},
                )
                assert cfg.config_body["secret"] == "[REDACTED]"

                gov = catalog.record_governance_audit(
                    run_id=run_id,
                    agent_id=agent_a,
                    stage_id="stage_review",
                    rule_id="GOV-01",
                    rule_name="Classification Tag Policy",
                    rule_type="classification_tagging",
                    evaluation_result="pass",
                    payload_sha256=sha,
                    details={"oversized": "z" * 70_000},
                )
                assert gov.payload_sha256 == sha
                assert gov.details["truncated"] is True

                gate_pending = catalog.record_stage_gate(
                    run_id=run_id,
                    agent_id=agent_a,
                    stage_id="stage_review",
                    gate_type="human_approval",
                    gate_status="pending",
                    payload_sha256=sha,
                )
                gate_passed = catalog.record_stage_gate(
                    gate_id=gate_pending.gate_id,
                    run_id=run_id,
                    agent_id=agent_a,
                    stage_id="stage_review",
                    gate_type="human_approval",
                    gate_status="passed",
                    checked_by="lead-architect@ddlc.local",
                    payload_sha256=sha,
                    notes="Claim Check hash verified.",
                )
                assert gate_passed.gate_status == "passed"
                assert gate_passed.payload_sha256 == sha

                callbacks.complete_run("completed")
            finally:
                with db.transaction() as conn:
                    if run_id is not None:
                        conn.execute(
                            "DELETE FROM agent_metadata.artifact_manifest WHERE run_id = %s",
                            (run_id,),
                        )
                    if artifact_id is not None:
                        conn.execute(
                            "DELETE FROM ddlc_shared_metadata.stage_execution WHERE run_id = %s",
                            (run_id,),
                        )
                        conn.execute(
                            "DELETE FROM agent_metadata.artifact_registry WHERE artifact_id = %s",
                            (artifact_id,),
                        )
                    if run_id is not None:
                        conn.execute(
                            "DELETE FROM agent_metadata.agent_run_header WHERE run_id = %s",
                            (run_id,),
                        )
                    for cid in comp_ids:
                        conn.execute(
                            "DELETE FROM ddlc_shared_metadata.component_registry WHERE component_id = %s",
                            (cid,),
                        )
                    conn.execute(
                        "DELETE FROM ddlc_shared_metadata.agent_registry WHERE agent_id = ANY(%s)",
                        ([agent_a, agent_b, agent_c],),
                    )
