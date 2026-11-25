"""Tests for DCM migrator — catalog diffing, type equivalence, Alembic revisions, and SchemaEngine."""

import shutil
from pathlib import Path

import pytest
from src.dcm.compiler import load_contracts_from_dir
from src.dcm.engine import SchemaEngine
from src.dcm.migrator import (
    CatalogSnapshot,
    DestructiveMigrationError,
    IntrospectedColumn,
    IntrospectedTable,
    apply_migrations,
    compute_migration_plan,
    compute_offline_plan,
    create_revision,
    to_sqlalchemy_uri,
    types_equivalent,
)
from src.dcm.models import (
    ColumnSpec,
    DataContractSchema,
    IndexSpec,
    TableSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestUriAndTypeNormalization:
    def test_to_sqlalchemy_uri_normalizes_postgres(self):
        assert (
            to_sqlalchemy_uri("postgresql://u:p@localhost:5434/db")
            == "postgresql+psycopg://u:p@localhost:5434/db"
        )
        assert (
            to_sqlalchemy_uri("postgresql+psycopg://u:p@localhost:5434/db")
            == "postgresql+psycopg://u:p@localhost:5434/db"
        )

    def test_equivalent_postgres_types(self):
        assert types_equivalent("VARCHAR(100)", "character varying(100)")
        assert types_equivalent("TIMESTAMPTZ", "timestamp with time zone")
        assert types_equivalent("NUMERIC(10,6)", "numeric(10, 6)")
        assert types_equivalent("NUMERIC(10,6)", "decimal(10,6)")
        assert types_equivalent("INTEGER", "int4")
        assert types_equivalent("BIGINT", "int8")
        assert types_equivalent("VECTOR(1536)", "vector(1536)")
        assert types_equivalent("BOOLEAN", "bool")
        assert not types_equivalent("VARCHAR(50)", "VARCHAR(100)")
        assert not types_equivalent("INTEGER", "BIGINT")


class TestMigrationDiffing:
    def _sample_contract(self) -> DataContractSchema:
        return DataContractSchema(
            version="1.0",
            schema_name="analytics",
            tables=[
                TableSpec(
                    name="events",
                    columns=[
                        ColumnSpec(
                            name="event_id",
                            type="UUID",
                            primary_key=True,
                            nullable=False,
                            default="gen_random_uuid()",
                        ),
                        ColumnSpec(name="name", type="VARCHAR(100)", nullable=False),
                    ],
                    indexes=[IndexSpec(name="idx_events_name", columns=["name"])],
                )
            ],
        )

    def test_offline_baseline_orders_tables_before_fks_for_real_contracts(self):
        contracts = load_contracts_from_dir(REPO_ROOT / "contracts")
        plan = compute_offline_plan(contracts)
        assert not plan.is_empty
        assert not plan.safety_report().has_destructive

        stmts = plan.upgrade_statements
        last_create_table_idx = max(
            i for i, s in enumerate(stmts) if s.startswith("CREATE TABLE")
        )
        first_fk_idx = min(
            i for i, s in enumerate(stmts) if "FOREIGN KEY" in s
        )
        assert last_create_table_idx < first_fk_idx

    def test_synced_catalog_produces_empty_plan(self):
        contract = self._sample_contract()
        catalog = CatalogSnapshot(
            schemas={"analytics"},
            tables={
                ("analytics", "events"): IntrospectedTable(
                    schema_name="analytics",
                    name="events",
                    columns={
                        "event_id": IntrospectedColumn("event_id", "uuid", nullable=False),
                        "name": IntrospectedColumn(
                            "name", "character varying(100)", nullable=False
                        ),
                    },
                    constraints={"events_pkey"},
                    indexes={"events_pkey", "idx_events_name"},
                    constraint_indexes={"events_pkey"},
                )
            },
        )
        plan = compute_migration_plan([contract], catalog)
        assert plan.is_empty

    def test_detects_added_column_as_non_destructive(self):
        contract = self._sample_contract()
        contract.tables[0].columns.append(
            ColumnSpec(name="source", type="VARCHAR(50)", nullable=True)
        )
        catalog = CatalogSnapshot(
            schemas={"analytics"},
            tables={
                ("analytics", "events"): IntrospectedTable(
                    schema_name="analytics",
                    name="events",
                    columns={
                        "event_id": IntrospectedColumn("event_id", "uuid", nullable=False),
                        "name": IntrospectedColumn(
                            "name", "character varying(100)", nullable=False
                        ),
                    },
                    constraints={"events_pkey"},
                    indexes={"events_pkey", "idx_events_name"},
                    constraint_indexes={"events_pkey"},
                )
            },
        )
        plan = compute_migration_plan([contract], catalog)
        assert any(op.kind == "add_column" for op in plan.ops)
        assert not plan.safety_report().has_destructive

    def test_detects_dropped_and_altered_columns_as_destructive(self):
        contract = self._sample_contract()
        catalog = CatalogSnapshot(
            schemas={"analytics"},
            tables={
                ("analytics", "events"): IntrospectedTable(
                    schema_name="analytics",
                    name="events",
                    columns={
                        "event_id": IntrospectedColumn("event_id", "uuid", nullable=False),
                        "name": IntrospectedColumn(
                            "name", "character varying(250)", nullable=False
                        ),
                        "legacy_col": IntrospectedColumn("legacy_col", "text", nullable=True),
                    },
                    constraints={"events_pkey"},
                    indexes={"events_pkey", "idx_events_name"},
                    constraint_indexes={"events_pkey"},
                )
            },
        )
        plan = compute_migration_plan([contract], catalog)
        kinds = {op.kind for op in plan.ops}
        assert "alter_column_type" in kinds
        assert "drop_column" in kinds
        assert plan.safety_report().has_destructive


class TestAlembicGenerationAndGate:
    @pytest.fixture
    def isolated_project(self, tmp_path: Path) -> Path:
        """Create an isolated temp Alembic workspace copying repo templates."""
        shutil.copy2(REPO_ROOT / "alembic.ini", tmp_path / "alembic.ini")
        shutil.copytree(REPO_ROOT / "alembic", tmp_path / "alembic")
        versions_dir = tmp_path / "alembic" / "versions"
        for existing in versions_dir.glob("*.py"):
            existing.unlink()
        return tmp_path

    def test_create_revision_and_sql_dry_run(self, isolated_project: Path):
        contracts = load_contracts_from_dir(REPO_ROOT / "contracts")
        rev_path = create_revision(
            db_uri="postgresql://u:p@localhost:5434/db",
            message="initial_metadata_framework_baseline",
            contracts=contracts,
            project_root=isolated_project,
            offline=True,
        )
        assert rev_path is not None
        rev_file = Path(rev_path)
        assert rev_file.is_file()

        source = rev_file.read_text(encoding="utf-8")
        compile(source, str(rev_file), "exec")
        assert 'CREATE SCHEMA IF NOT EXISTS "agent_metadata"' in source
        assert "trg_audit_log_immutable" in source

        sql_preview = apply_migrations(
            db_uri="postgresql://u:p@localhost:5434/db",
            project_root=isolated_project,
            sql_only=True,
            current_revision="",
        )
        assert "CREATE TABLE IF NOT EXISTS" in sql_preview
        assert "alembic_version" in sql_preview

    def test_apply_blocks_destructive_revision_until_approved(
        self, isolated_project: Path
    ):
        contract = DataContractSchema(
            version="1.0",
            schema_name="analytics",
            tables=[
                TableSpec(
                    name="events",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False)
                    ],
                )
            ],
        )
        rev_path = create_revision(
            db_uri="postgresql://u:p@localhost:5434/db",
            message="drop_old_col",
            contracts=[contract],
            project_root=isolated_project,
            allow_empty=True,
            offline=True,
        )
        assert rev_path is not None
        rev_file = Path(rev_path)
        rev_file.write_text(
            rev_file.read_text(encoding="utf-8").replace(
                "def upgrade() -> None:",
                'def upgrade() -> None:\n    op.execute(\'ALTER TABLE "analytics"."events" DROP COLUMN "old";\')',
                1,
            ),
            encoding="utf-8",
        )

        with pytest.raises(DestructiveMigrationError):
            apply_migrations(
                db_uri="postgresql://u:p@localhost:5434/db",
                project_root=isolated_project,
                sql_only=True,
                approve_destructive=False,
                current_revision="",
            )

        approved_sql = apply_migrations(
            db_uri="postgresql://u:p@localhost:5434/db",
            project_root=isolated_project,
            sql_only=True,
            approve_destructive=True,
            current_revision="",
        )
        assert 'DROP COLUMN "old"' in approved_sql


class TestSchemaEngine:
    def test_validate_tier2_against_tier1_and_offline_plan(self):
        engine = SchemaEngine(
            db_url="postgresql://u:p@localhost:5434/db",
            project_root=REPO_ROOT,
        )
        tier2 = engine.validate_contract(REPO_ROOT / "contracts" / "tier_2_ddlc_shared_metadata.yaml")
        assert tier2.schema_name == "ddlc_shared_metadata"

        plan = engine.plan(REPO_ROOT / "contracts", offline=True)
        assert not plan.is_empty
        assert plan.schemas == ["agent_metadata", "ddlc_shared_metadata"]
