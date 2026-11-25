"""Unit and read-only live tests for src/metadata/schema_engine.py (Tier3SchemaClient)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from src.core.settings import RuntimeEnvSettings, get_env_settings
from src.dcm.compiler import ContractValidationError
from src.metadata.schema_engine import Tier3ContractError, Tier3SchemaClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


class TestTier3NamingConventions:
    def test_expected_schema_name_normalizes_valid_names(self) -> None:
        assert Tier3SchemaClient.expected_schema_name("customer_pipeline") == "ddlc_customer_pipeline_metadata"
        assert Tier3SchemaClient.expected_schema_name("Dbt-Pipeline-Builder") == "ddlc_dbt_pipeline_builder_metadata"

    @pytest.mark.parametrize("bad_name", ["", "123agent", "bad name!", "shared", "SHARED"])
    def test_expected_schema_name_rejects_invalid_or_reserved(self, bad_name: str) -> None:
        with pytest.raises(Tier3ContractError):
            Tier3SchemaClient.expected_schema_name(bad_name)

    @pytest.mark.parametrize(
        "bad_schema",
        [
            "agent_metadata",
            "ddlc_shared_metadata",
            "rag_knowledge",
            "public",
            "customer_metadata",
            "ddlc_customer",
            "ddlc__metadata",
            "ddlc_123_metadata",
        ],
    )
    def test_validate_schema_name_rejects_reserved_and_malformed(self, bad_schema: str) -> None:
        with pytest.raises(Tier3ContractError):
            Tier3SchemaClient.validate_schema_name(bad_schema)

    def test_validate_schema_name_rejects_agent_mismatch(self) -> None:
        with pytest.raises(Tier3ContractError, match="does not match expected schema"):
            Tier3SchemaClient.validate_schema_name(
                "ddlc_orders_metadata",
                agent_name="customers",
            )


class TestTier3ContractValidationAndPlanning:
    def test_validates_tier3_contract_against_tier1_and_tier2_and_plans_offline(
        self, tmp_path: Path
    ) -> None:
        contract_path = _write_yaml(
            tmp_path / "tier_3_orders_agent.yaml",
            """
version: "1.0"
schema: "ddlc_orders_agent_metadata"
description: "Tier 3 operational metadata for orders_agent"
tables:
  - name: "source_profiling_results"
    columns:
      - name: "profile_id"
        type: "UUID"
        primary_key: true
        nullable: false
        default: "gen_random_uuid()"
      - name: "run_id"
        type: "UUID"
        nullable: false
        references: "agent_metadata.agent_run_header(run_id)"
        on_delete: "CASCADE"
      - name: "agent_id"
        type: "VARCHAR(100)"
        nullable: false
        references: "ddlc_shared_metadata.agent_registry(agent_id)"
        on_delete: "CASCADE"
      - name: "null_rate"
        type: "NUMERIC(5,4)"
        nullable: false
    indexes:
      - name: "idx_orders_prof_run"
        columns: ["run_id"]
""",
        )

        client = Tier3SchemaClient()
        validated = client.validate_contract(contract_path, agent_name="orders_agent")
        assert validated.schema_name == "ddlc_orders_agent_metadata"

        plan = client.plan(contract_path, agent_name="orders_agent", offline=True)
        assert not plan.is_empty
        sql_blob = "\n".join(plan.upgrade_statements)
        assert 'CREATE SCHEMA IF NOT EXISTS "ddlc_orders_agent_metadata"' in sql_blob
        assert 'CREATE TABLE IF NOT EXISTS "ddlc_orders_agent_metadata"."source_profiling_results"' in sql_blob
        assert 'REFERENCES "agent_metadata"."agent_run_header"' in sql_blob
        assert 'REFERENCES "ddlc_shared_metadata"."agent_registry"' in sql_blob

    def test_rejects_broken_cross_tier_foreign_key(self, tmp_path: Path) -> None:
        bad_fk_path = _write_yaml(
            tmp_path / "tier_3_bad_fk.yaml",
            """
version: "1.0"
schema: "ddlc_orders_agent_metadata"
tables:
  - name: "staging_state"
    columns:
      - name: "id"
        type: "UUID"
        primary_key: true
        nullable: false
      - name: "run_id"
        type: "UUID"
        nullable: false
        references: "agent_metadata.nonexistent_table(run_id)"
""",
        )
        client = Tier3SchemaClient()
        with pytest.raises(ContractValidationError, match="unknown table"):
            client.validate_contract(bad_fk_path)

    def test_rejects_tier2_schema_name_in_tier3_contract(self, tmp_path: Path) -> None:
        reserved_path = _write_yaml(
            tmp_path / "tier_3_reserved.yaml",
            """
version: "1.0"
schema: "ddlc_shared_metadata"
tables:
  - name: "rogue_table"
    columns:
      - name: "id"
        type: "UUID"
        primary_key: true
        nullable: false
""",
        )
        client = Tier3SchemaClient()
        with pytest.raises(Tier3ContractError, match="reserved platform schema"):
            client.validate_contract(reserved_path)

    def test_project_root_resolves_custom_contracts_dir_and_validation_blocks_migrate(
        self, tmp_path: Path
    ) -> None:
        custom_root = tmp_path / "studio_copy"
        custom_contracts = custom_root / "contracts"
        custom_contracts.mkdir(parents=True)
        client = Tier3SchemaClient(project_root=custom_root)
        assert client.contracts_dir == custom_contracts

        bad_path = _write_yaml(
            tmp_path / "tier_3_bad_name.yaml",
            """
version: "1.0"
schema: "invalid_schema_name"
tables:
  - name: "t"
    columns:
      - name: "id"
        type: "UUID"
        primary_key: true
        nullable: false
""",
        )
        mock_engine = MagicMock()
        failing_client = Tier3SchemaClient(contracts_dir=REPO_ROOT / "contracts", engine=mock_engine)
        with pytest.raises(Tier3ContractError):
            failing_client.apply_migration(bad_path, message="should fail")
        mock_engine.migrate.assert_not_called()

    def test_apply_migration_delegates_after_validation_and_checks_lazy_uri(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contract_path = _write_yaml(
            tmp_path / "tier_3_billing.yaml",
            """
version: "1.0"
schema: "ddlc_billing_metadata"
tables:
  - name: "watermarks"
    columns:
      - name: "watermark_id"
        type: "UUID"
        primary_key: true
        nullable: false
""",
        )

        mock_engine = MagicMock()
        mock_engine.migrate.return_value = "rev_abc123"
        mock_engine.status.return_value = {"current_revision": "rev_abc123"}

        client = Tier3SchemaClient(contracts_dir=REPO_ROOT / "contracts", engine=mock_engine)
        rev = client.apply_migration(
            contract_path,
            message="add billing watermarks",
            agent_name="billing",
            approve_destructive=False,
        )
        assert rev == "rev_abc123"
        mock_engine.migrate.assert_called_once_with(
            contract_path,
            message="add billing watermarks",
            approve_destructive=False,
        )
        assert client.status() == {"current_revision": "rev_abc123"}

        # Verify lazy engine raises ValueError when DCM_MIGRATION_DB_URI is absent
        monkeypatch.setattr(
            "src.metadata.schema_engine.get_env_settings",
            lambda: RuntimeEnvSettings(dcm_migration_db_uri=None),
        )
        unconfigured = Tier3SchemaClient(contracts_dir=REPO_ROOT / "contracts")
        with pytest.raises(ValueError, match="DCM_MIGRATION_DB_URI"):
            _ = unconfigured.engine

    def test_live_readonly_tier3_plan_introspection(self, tmp_path: Path) -> None:
        uri = get_env_settings().dcm_migration_db_uri or get_env_settings().metadata_db_uri
        if not uri:
            pytest.skip("Database URI is not configured.")
        try:
            probe = psycopg.connect(uri, connect_timeout=2)
            probe.close()
        except psycopg.OperationalError as exc:
            pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")

        contract_path = _write_yaml(
            tmp_path / "tier_3_live_probe.yaml",
            """
version: "1.0"
schema: "ddlc_live_probe_metadata"
tables:
  - name: "probe_metrics"
    columns:
      - name: "metric_id"
        type: "UUID"
        primary_key: true
        nullable: false
      - name: "run_id"
        type: "UUID"
        nullable: false
        references: "agent_metadata.agent_run_header(run_id)"
""",
        )
        client = Tier3SchemaClient(db_uri=uri, contracts_dir=REPO_ROOT / "contracts")
        plan = client.plan(contract_path, agent_name="live_probe", offline=False)
        assert not plan.is_empty
        assert any(op.kind == "create_schema" for op in plan.ops)
        assert any(op.kind == "create_table" for op in plan.ops)
