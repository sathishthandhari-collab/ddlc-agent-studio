"""Tests for DCM migrator — deep catalog diffing, Alembic migrations, safety gate, and live DB checks."""

import shutil
from pathlib import Path

import psycopg
import pytest
from src.core.settings import get_env_settings
from src.dcm.compiler import load_contracts_from_dir, types_equivalent
from src.dcm.engine import SchemaEngine
from src.dcm.migrator import (
    CatalogSnapshot,
    DestructiveMigrationError,
    IntrospectedColumn,
    IntrospectedTable,
    IntrospectedTrigger,
    _filter_user_ddl,
    _split_sql_statements,
    apply_migrations,
    compute_migration_plan,
    compute_offline_plan,
    create_revision,
    get_current_revision,
    get_migration_status,
    introspect_database,
    to_sqlalchemy_uri,
)
from src.dcm.models import (
    CheckConstraintSpec,
    ColumnSpec,
    DataContractSchema,
    IndexSpec,
    TableSpec,
    TriggerSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_admin_uri_or_skip() -> str:
    uri = get_env_settings().dcm_migration_db_uri
    if not uri:
        pytest.skip("DCM_MIGRATION_DB_URI is not configured.")
    try:
        conn = psycopg.connect(uri, connect_timeout=2)
        conn.close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")
    return uri


class TestUriAndTypeNormalization:
    def test_to_sqlalchemy_uri_normalizes_postgres(self):
        assert (
            to_sqlalchemy_uri("postgresql://u:p@localhost:5434/db")
            == "postgresql+psycopg://u:p@localhost:5434/db"
        )
        assert (
            to_sqlalchemy_uri("postgres://u:p@localhost:5434/db")
            == "postgresql+psycopg://u:p@localhost:5434/db"
        )

    def test_equivalent_postgres_types_including_serial(self):
        assert types_equivalent("VARCHAR(100)", "character varying(100)")
        assert types_equivalent("TIMESTAMPTZ", "timestamp with time zone")
        assert types_equivalent("NUMERIC(10,6)", "numeric(10, 6)")
        assert types_equivalent("NUMERIC(10,6)", "decimal(10,6)")
        assert types_equivalent("INTEGER", "int4")
        assert types_equivalent("SERIAL", "integer")
        assert types_equivalent("BIGSERIAL", "bigint")
        assert types_equivalent("BIGINT", "int8")
        assert types_equivalent("VECTOR(1536)", "vector(1536)")
        assert types_equivalent("BOOLEAN", "bool")
        assert not types_equivalent("VARCHAR(50)", "VARCHAR(100)")
        assert not types_equivalent("INTEGER", "BIGINT")


class TestSqlSplitterAndAlembicFilter:
    def test_split_preserves_semicolons_in_strings_and_tagged_dollar_blocks(self):
        sql = (
            "CREATE TABLE public.alembic_version (version_num VARCHAR(32));\n"
            "INSERT INTO public.alembic_version VALUES ('abc');\n"
            "CREATE OR REPLACE FUNCTION \"s\".\"f\"() RETURNS TRIGGER AS $fn$\n"
            "BEGIN RAISE EXCEPTION 'bad;msg'; END;\n"
            "$fn$ LANGUAGE plpgsql;\n"
            'CREATE TABLE "s"."t" ("note" TEXT DEFAULT \'a;b\');'
        )
        stmts = _split_sql_statements(sql)
        assert len(stmts) == 4
        assert "bad;msg" in stmts[2]
        assert "'a;b'" in stmts[3]

        filtered = _filter_user_ddl(stmts)
        assert len(filtered) == 2
        assert all("alembic_version" not in s for s in filtered)


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
                        ColumnSpec(
                            name="name",
                            type="VARCHAR(100)",
                            nullable=False,
                            default="'unknown'",
                            check="name IN ('click', 'view', 'unknown')",
                        ),
                    ],
                    indexes=[IndexSpec(name="idx_events_name", columns=["name"])],
                )
            ],
        )

    def _synced_catalog(self) -> CatalogSnapshot:
        return CatalogSnapshot(
            schemas={"analytics"},
            tables={
                ("analytics", "events"): IntrospectedTable(
                    schema_name="analytics",
                    name="events",
                    columns={
                        "event_id": IntrospectedColumn(
                            "event_id", "uuid", nullable=False, default="gen_random_uuid()"
                        ),
                        "name": IntrospectedColumn(
                            "name",
                            "character varying(100)",
                            nullable=False,
                            default="'unknown'::character varying",
                        ),
                    },
                    constraints={
                        "events_pkey": ("p", "PRIMARY KEY (event_id)"),
                        "chk_events_name": (
                            "c",
                            "CHECK (name::text = ANY (ARRAY['click'::character varying, 'view'::character varying, 'unknown'::character varying]::text[]))",
                        ),
                    },
                    indexes={
                        "events_pkey": "CREATE UNIQUE INDEX events_pkey ON analytics.events USING btree (event_id)",
                        "idx_events_name": "CREATE INDEX idx_events_name ON analytics.events USING btree (name)",
                    },
                    constraint_indexes={"events_pkey"},
                )
            },
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
        first_fk_idx = min(i for i, s in enumerate(stmts) if "FOREIGN KEY" in s)
        assert last_create_table_idx < first_fk_idx

    def test_synced_catalog_produces_empty_plan(self):
        plan = compute_migration_plan([self._sample_contract()], self._synced_catalog())
        assert plan.is_empty

    def test_detects_default_nullability_check_index_and_trigger_drift(self):
        contract = self._sample_contract()
        contract.tables[0].columns[1].default = "'default_v2'"
        contract.tables[0].columns[1].nullable = True
        contract.tables[0].columns[1].check = "name IN ('click', 'purchase')"
        contract.tables[0].indexes[0] = IndexSpec(
            name="idx_events_name", columns=["name DESC"], unique=True
        )
        contract.tables[0].triggers.append(
            TriggerSpec(
                name="trg_events_ro",
                timing="BEFORE",
                events=["DELETE"],
                for_each="ROW",
                function_name="block_delete",
                function_body="BEGIN RAISE EXCEPTION 'no delete'; END;",
            )
        )

        catalog = self._synced_catalog()
        catalog.tables[("analytics", "events")].triggers["trg_events_ro"] = IntrospectedTrigger(
            name="trg_events_ro",
            timing="BEFORE",
            events=("UPDATE",),
            for_each="ROW",
            function_schema="analytics",
            function_name="block_delete",
            function_body="BEGIN RAISE EXCEPTION 'old body'; END;",
        )

        plan = compute_migration_plan([contract], catalog)
        kinds = {op.kind for op in plan.ops}
        assert "alter_default" in kinds
        assert "alter_nullable" in kinds
        assert "alter_constraint" in kinds
        assert "alter_index" in kinds
        assert "alter_trigger" in kinds
        assert plan.safety_report().has_destructive

    def test_detects_dropped_index_constraint_table_with_downgrades(self):
        contract = self._sample_contract()
        contract.tables[0].indexes.clear()
        catalog = self._synced_catalog()
        catalog.tables[("analytics", "legacy_tbl")] = IntrospectedTable(
            schema_name="analytics",
            name="legacy_tbl",
            columns={"id": IntrospectedColumn("id", "uuid", nullable=False)},
        )
        catalog.tables[("analytics", "events")].constraints["chk_extra"] = (
            "c",
            "CHECK ((1 = 1))",
        )

        plan = compute_migration_plan([contract], catalog)
        kinds = {op.kind for op in plan.ops}
        assert "drop_index" in kinds
        assert "drop_constraint" in kinds
        assert "drop_table" in kinds
        assert any("CASCADE" in s for s in plan.upgrade_statements)
        assert any("CREATE INDEX idx_events_name" in s for s in plan.downgrade_statements)
        assert any("ADD CONSTRAINT" in s for s in plan.downgrade_statements)

    def test_detects_added_column_as_non_destructive(self):
        contract = self._sample_contract()
        contract.tables[0].columns.append(
            ColumnSpec(name="source", type="VARCHAR(50)", nullable=True)
        )
        plan = compute_migration_plan([contract], self._synced_catalog())
        assert any(op.kind == "add_column" for op in plan.ops)
        assert not plan.safety_report().has_destructive

    def test_detects_dropped_and_altered_columns_as_destructive(self):
        contract = self._sample_contract()
        catalog = self._synced_catalog()
        catalog.tables[("analytics", "events")].columns["name"].pg_type = "character varying(250)"
        catalog.tables[("analytics", "events")].columns["legacy_col"] = IntrospectedColumn(
            "legacy_col", "text", nullable=True
        )
        plan = compute_migration_plan([contract], catalog)
        kinds = {op.kind for op in plan.ops}
        assert "alter_column_type" in kinds
        assert "drop_column" in kinds
        assert plan.safety_report().has_destructive


class TestAlembicGenerationAndGate:
    @pytest.fixture
    def isolated_project(self, tmp_path: Path) -> Path:
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
                    check_constraints=[CheckConstraintSpec(name="chk_ok", expression="id IS NOT NULL")],
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
                'def upgrade() -> None:\n    op.execute(\'ALTER TABLE "analytics"."events" RENAME TO "events_v2";\')',
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
        assert 'RENAME TO "events_v2"' in approved_sql

    def test_schema_engine_migrate_does_not_create_dangling_file_when_blocked(
        self, isolated_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        contract_file = isolated_project / "analytics.yaml"
        contract_file.write_text(
            """
version: "1.0"
schema: "analytics"
tables:
  - name: "events"
    columns:
      - name: "id"
        type: "UUID"
        primary_key: true
        nullable: false
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "src.dcm.engine.introspect_database",
            lambda uri, schemas: CatalogSnapshot(
                schemas={"analytics"},
                tables={
                    ("analytics", "events"): IntrospectedTable(
                        schema_name="analytics",
                        name="events",
                        columns={
                            "id": IntrospectedColumn("id", "uuid", nullable=False),
                            "old_col": IntrospectedColumn("old_col", "text", nullable=True),
                        },
                        constraints={"events_pkey": ("p", "PRIMARY KEY (id)")},
                    )
                },
            ),
        )
        engine = SchemaEngine(
            db_url="postgresql://u:p@localhost:5434/db",
            project_root=isolated_project,
        )
        with pytest.raises(DestructiveMigrationError):
            engine.migrate(contract_file, message="drop_old_col", approve_destructive=False)

        assert list((isolated_project / "alembic" / "versions").glob("*.py")) == []


class TestSchemaEngineUnit:
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

    def test_get_current_revision_propagates_connection_failure(self):
        with pytest.raises(Exception):
            get_current_revision("postgresql://bad:bad@127.0.0.1:59999/nope")


class TestLivePostgresProvisioningIntegration:
    def test_live_introspection_status_privileges_triggers_and_checkpointer(self):
        admin_uri = _live_admin_uri_or_skip()
        app_uri = get_env_settings().checkpointer_db_uri
        assert app_uri

        contracts = load_contracts_from_dir(REPO_ROOT / "contracts")
        catalog = introspect_database(admin_uri, [c.schema_name for c in contracts])
        assert {"agent_metadata", "ddlc_shared_metadata"}.issubset(catalog.schemas)
        assert len([k for k in catalog.tables if k[0] == "agent_metadata"]) == 6
        assert len([k for k in catalog.tables if k[0] == "ddlc_shared_metadata"]) == 12

        status = get_migration_status(admin_uri, contracts=contracts, project_root=REPO_ROOT)
        assert status["current_revision"] == status["head_revision"]
        assert status["current_revision"] is not None
        assert status["schemas"] == {"agent_metadata": True, "ddlc_shared_metadata": True}

        engine = SchemaEngine(db_url=admin_uri, project_root=REPO_ROOT)
        assert engine.plan(REPO_ROOT / "contracts").is_empty

        # Verify LangGraph checkpoint tables, app role least-privilege, and audit_log UPDATE+DELETE block
        with psycopg.connect(app_uri) as app_conn:
            cp_tables = {
                row[0]
                for row in app_conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'checkpoint%'"
                ).fetchall()
            }
            assert {
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "checkpoint_migrations",
            }.issubset(cp_tables)

            can_create = app_conn.execute(
                "SELECT has_schema_privilege('agent_metadata', 'CREATE'), "
                "has_schema_privilege('ddlc_shared_metadata', 'CREATE')"
            ).fetchone()
            assert can_create == (False, False)

            can_truncate = app_conn.execute(
                "SELECT has_table_privilege('agent_metadata.audit_log', 'TRUNCATE')"
            ).fetchone()
            assert can_truncate == (False,)

            # Inside an uncommitted transaction with savepoints, verify BOTH UPDATE and DELETE fail
            app_conn.execute("SAVEPOINT sp_audit_insert")
            app_conn.execute(
                "INSERT INTO agent_metadata.audit_log (action, actor_id, actor_type, target_entity_type) "
                "VALUES ('qa_test', 'qa_agent', 'system', 'schema')"
            )
            app_conn.execute("SAVEPOINT sp_before_update")
            with pytest.raises(psycopg.Error, match="Compliance Rule SEC-01"):
                app_conn.execute("UPDATE agent_metadata.audit_log SET action = 'tamper'")
            app_conn.execute("ROLLBACK TO SAVEPOINT sp_before_update")

            app_conn.execute("SAVEPOINT sp_before_delete")
            with pytest.raises(psycopg.Error, match="Compliance Rule SEC-01"):
                app_conn.execute("DELETE FROM agent_metadata.audit_log WHERE action = 'qa_test'")
            app_conn.execute("ROLLBACK TO SAVEPOINT sp_before_delete")
            app_conn.rollback()

        # Verify LangGraph PostgresSaver runtime read/write/cleanup with least-privilege role
        from langgraph.checkpoint.postgres import PostgresSaver

        test_thread = "qa-step3-checkpointer-smoke"
        with PostgresSaver.from_conn_string(app_uri) as saver:
            cfg = {"configurable": {"thread_id": test_thread, "checkpoint_ns": ""}}
            checkpoint = {
                "v": 1,
                "id": "00000000-0000-0000-0000-000000000001",
                "ts": "2026-11-26T00:00:00+00:00",
                "channel_values": {"ping": "pong"},
                "channel_versions": {"ping": "1"},
                "versions_seen": {},
            }
            saver.put(cfg, checkpoint, {"source": "qa", "step": 1, "writes": {}}, {})
            loaded = saver.get_tuple({"configurable": {"thread_id": test_thread}})
            assert loaded is not None
            assert loaded.checkpoint["channel_values"]["ping"] == "pong"
        with psycopg.connect(app_uri, autocommit=True) as cleanup_conn:
            for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cleanup_conn.execute(
                    f"DELETE FROM public.{tbl} WHERE thread_id = %s",
                    (test_thread,),
                )
