"""Tests for DCM contract compiler — YAML validation, integrity, and SQLGlot DDL compilation."""

from pathlib import Path

import pytest
from src.dcm.compiler import (
    ContractValidationError,
    compile_contract,
    compile_contracts,
    load_contract,
    load_contracts_from_dir,
    resolve_pg_type,
    sort_contracts_by_dependencies,
    validate_contracts_integrity,
)
from src.dcm.models import (
    ColumnSpec,
    DataContractSchema,
    ForeignKeySpec,
    IndexSpec,
    TableSpec,
    TriggerSpec,
    UniqueConstraintSpec,
    parse_index_column,
    parse_reference,
)


class TestResolveType:
    def test_simple_types(self):
        assert resolve_pg_type("UUID") == "UUID"
        assert resolve_pg_type("TEXT") == "TEXT"
        assert resolve_pg_type("INTEGER") == "INTEGER"
        assert resolve_pg_type("BIGINT") == "BIGINT"
        assert resolve_pg_type("TIMESTAMPTZ") == "TIMESTAMPTZ"
        assert resolve_pg_type("JSONB") == "JSONB"
        assert resolve_pg_type("INET") == "INET"
        assert resolve_pg_type("BOOLEAN") == "BOOLEAN"

    def test_parameterized_varchar(self):
        assert resolve_pg_type("VARCHAR(100)") == "VARCHAR(100)"
        assert resolve_pg_type("VARCHAR(255)") == "VARCHAR(255)"

    def test_parameterized_numeric(self):
        assert resolve_pg_type("NUMERIC(10,6)") == "NUMERIC(10, 6)"
        assert resolve_pg_type("NUMERIC(4, 3)") == "NUMERIC(4, 3)"

    def test_parameterized_vector(self):
        assert resolve_pg_type("VECTOR(1536)") == "vector(1536)"

    def test_case_insensitive(self):
        assert resolve_pg_type("uuid") == "UUID"
        assert resolve_pg_type("jsonb") == "JSONB"
        assert resolve_pg_type("varchar(50)") == "VARCHAR(50)"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unrecognized column type"):
            resolve_pg_type("FAKETYPE")


class TestParseReference:
    def test_simple_ref(self):
        schema, table, cols = parse_reference("agent_metadata.agent_run_header(run_id)")
        assert schema == "agent_metadata"
        assert table == "agent_run_header"
        assert cols == ["run_id"]

    def test_composite_ref(self):
        schema, table, cols = parse_reference("agent_metadata.execution_log(run_id, log_id)")
        assert schema == "agent_metadata"
        assert table == "execution_log"
        assert cols == ["run_id", "log_id"]

    def test_invalid_ref_no_dot(self):
        with pytest.raises(ValueError, match="must be in"):
            parse_reference("tablename(col)")

    def test_parse_index_column(self):
        assert parse_index_column("occurred_at DESC") == ("occurred_at", "DESC")
        assert parse_index_column("event_sequence ASC") == ("event_sequence", "ASC")
        assert parse_index_column("started_at", default_desc=True) == ("started_at", "DESC")
        assert parse_index_column("agent_id") == ("agent_id", None)


class TestModels:
    def test_minimal_contract(self):
        contract = DataContractSchema(
            version="1.0",
            schema_name="test_schema",
            tables=[
                TableSpec(
                    name="test_table",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                    ],
                )
            ],
        )
        assert contract.schema_name == "test_schema"
        assert len(contract.tables) == 1

    def test_multiple_pk_raises(self):
        with pytest.raises(ValueError, match="Multiple primary key"):
            TableSpec(
                name="bad_table",
                columns=[
                    ColumnSpec(name="id1", type="UUID", primary_key=True),
                    ColumnSpec(name="id2", type="UUID", primary_key=True),
                ],
            )

    def test_invalid_schema_name_raises(self):
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            DataContractSchema(
                version="1.0",
                schema_name="invalid-schema-name",
                tables=[
                    TableSpec(
                        name="t",
                        columns=[ColumnSpec(name="id", type="UUID")],
                    )
                ],
            )


class TestCompileAndIntegrity:
    @pytest.fixture
    def simple_contract(self) -> DataContractSchema:
        return DataContractSchema(
            version="1.0",
            schema_name="test_schema",
            tables=[
                TableSpec(
                    name="users",
                    description="User accounts.",
                    columns=[
                        ColumnSpec(
                            name="user_id",
                            type="UUID",
                            primary_key=True,
                            nullable=False,
                            default="gen_random_uuid()",
                        ),
                        ColumnSpec(name="email", type="VARCHAR(255)", nullable=False),
                        ColumnSpec(
                            name="status",
                            type="VARCHAR(20)",
                            nullable=False,
                            default="'active'",
                            check="status IN ('active', 'inactive')",
                        ),
                        ColumnSpec(
                            name="created_at",
                            type="TIMESTAMPTZ",
                            nullable=False,
                            default="clock_timestamp()",
                        ),
                    ],
                    indexes=[
                        IndexSpec(name="idx_users_email", columns=["email"], unique=True),
                        IndexSpec(name="idx_users_status", columns=["status", "created_at DESC"]),
                    ],
                )
            ],
        )

    def test_create_schema(self, simple_contract):
        stmts = compile_contract(simple_contract)
        assert stmts[0] == 'CREATE SCHEMA IF NOT EXISTS "test_schema";'

    def test_create_table_present(self, simple_contract):
        stmts = compile_contract(simple_contract)
        create_table = stmts[1]
        assert 'CREATE TABLE IF NOT EXISTS "test_schema"."users"' in create_table
        assert '"user_id" UUID NOT NULL DEFAULT gen_random_uuid()' in create_table
        assert '"email" VARCHAR(255) NOT NULL' in create_table
        assert 'CONSTRAINT "users_pkey" PRIMARY KEY ("user_id")' in create_table

    def test_check_constraint_in_table(self, simple_contract):
        stmts = compile_contract(simple_contract)
        create_table = stmts[1]
        assert "chk_users_status" in create_table
        assert "status IN ('active', 'inactive')" in create_table

    def test_indexes_generated_with_mixed_direction(self, simple_contract):
        stmts = compile_contract(simple_contract)
        idx_stmts = [s for s in stmts if "CREATE" in s and "INDEX" in s]
        assert len(idx_stmts) == 2
        assert any("UNIQUE" in s and "idx_users_email" in s for s in idx_stmts)
        assert any('"status", "created_at" DESC' in s for s in idx_stmts)

    def test_hnsw_vector_index(self):
        contract = DataContractSchema(
            version="1.0",
            schema_name="vec_schema",
            tables=[
                TableSpec(
                    name="items",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="embedding", type="VECTOR(1536)", nullable=True),
                    ],
                    indexes=[
                        IndexSpec(
                            name="idx_items_vec",
                            columns=["embedding"],
                            method="hnsw",
                            opclass="vector_cosine_ops",
                            where="embedding IS NOT NULL",
                        )
                    ],
                )
            ],
        )
        stmts = compile_contract(contract, validate_integrity=True)
        idx_stmt = [s for s in stmts if "idx_items_vec" in s][0]
        assert "USING hnsw" in idx_stmt
        assert '"embedding" vector_cosine_ops' in idx_stmt
        assert "WHERE embedding IS NOT NULL" in idx_stmt

    def test_broken_fk_fails_integrity(self):
        bad = DataContractSchema(
            version="1.0",
            schema_name="broken",
            tables=[
                TableSpec(
                    name="child",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(
                            name="parent_id",
                            type="UUID",
                            references="broken.missing_table(id)",
                        ),
                    ],
                )
            ],
        )
        with pytest.raises(ContractValidationError, match="unknown table"):
            validate_contracts_integrity([bad])

    def test_non_unique_fk_target_fails_integrity(self):
        bad = DataContractSchema(
            version="1.0",
            schema_name="broken",
            tables=[
                TableSpec(
                    name="parent",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="non_unique_col", type="UUID", nullable=False),
                    ],
                ),
                TableSpec(
                    name="child",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(
                            name="ref_col",
                            type="UUID",
                            references="broken.parent(non_unique_col)",
                        ),
                    ],
                ),
            ],
        )
        with pytest.raises(ContractValidationError, match="non-unique target"):
            validate_contracts_integrity([bad])

    def test_topological_sort_orders_parent_schema_first(self):
        child = DataContractSchema(
            version="1.0",
            schema_name="aaa_child",
            tables=[
                TableSpec(
                    name="c",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="p_id", type="UUID", references="zzz_parent.p(id)"),
                    ],
                )
            ],
        )
        parent = DataContractSchema(
            version="1.0",
            schema_name="zzz_parent",
            tables=[
                TableSpec(
                    name="p",
                    columns=[ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False)],
                )
            ],
        )
        ordered = sort_contracts_by_dependencies([child, parent])
        assert [c.schema_name for c in ordered] == ["zzz_parent", "aaa_child"]

    def test_composite_foreign_key(self):
        contract = DataContractSchema(
            version="1.0",
            schema_name="myschema",
            tables=[
                TableSpec(
                    name="parent",
                    columns=[
                        ColumnSpec(name="a", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="b", type="UUID", nullable=False),
                    ],
                    unique_constraints=[
                        UniqueConstraintSpec(name="uq_ab", columns=["a", "b"]),
                    ],
                ),
                TableSpec(
                    name="child",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="ref_a", type="UUID", nullable=True),
                        ColumnSpec(name="ref_b", type="UUID", nullable=True),
                    ],
                    foreign_keys=[
                        ForeignKeySpec(
                            name="fk_child_parent",
                            columns=["ref_a", "ref_b"],
                            references="myschema.parent(a, b)",
                            on_delete="SET NULL",
                        ),
                    ],
                ),
            ],
        )
        stmts = compile_contract(contract, validate_integrity=True)
        fk_stmts = [s for s in stmts if "fk_child_parent" in s]
        assert len(fk_stmts) == 1
        assert 'FOREIGN KEY ("ref_a", "ref_b")' in fk_stmts[0]

    def test_trigger_compilation_idempotent(self):
        contract = DataContractSchema(
            version="1.0",
            schema_name="myschema",
            tables=[
                TableSpec(
                    name="audit",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                    ],
                    triggers=[
                        TriggerSpec(
                            name="trg_immutable",
                            timing="BEFORE",
                            events=["UPDATE", "DELETE"],
                            for_each="ROW",
                            function_name="enforce_immutability",
                            function_body="BEGIN\n    RAISE EXCEPTION 'immutable';\nEND;",
                        ),
                    ],
                ),
            ],
        )
        stmts = compile_contract(contract)
        assert any("CREATE OR REPLACE FUNCTION" in s for s in stmts)
        assert any("CREATE OR REPLACE TRIGGER" in s and "trg_immutable" in s for s in stmts)


class TestLoadRealContracts:
    """Tests that Tier 1 and Tier 2 project contracts validate and compile cleanly."""

    @pytest.fixture
    def contracts_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "contracts"

    def test_tier1_loads(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_1_agent_metadata.yaml")
        assert contract.schema_name == "agent_metadata"
        assert len(contract.tables) == 6

    def test_tier2_loads(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_2_ddlc_shared_metadata.yaml")
        assert contract.schema_name == "ddlc_shared_metadata"
        assert len(contract.tables) == 12

    def test_all_contracts_pass_referential_integrity_and_compile(self, contracts_dir):
        contracts = load_contracts_from_dir(contracts_dir)
        assert [c.schema_name for c in contracts[:2]] == [
            "agent_metadata",
            "ddlc_shared_metadata",
        ]
        validate_contracts_integrity(contracts, strict_external=True)
        stmts = compile_contracts(contracts, validate_integrity=True)
        assert len(stmts) > 40

    def test_tier1_has_immutability_trigger_and_desc_indexes(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_1_agent_metadata.yaml")
        stmts = compile_contract(contract)
        assert any("trg_audit_log_immutable" in s for s in stmts)
        assert any('"actor_id", "occurred_at" DESC' in s for s in stmts)

    def test_tier1_has_composite_fks(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_1_agent_metadata.yaml")
        stmts = compile_contract(contract)
        composite_fks = [s for s in stmts if "fk_exec_parent" in s or "fk_exec_retry" in s]
        assert len(composite_fks) == 2

    def test_tier2_has_vector_column_and_hnsw_index(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_2_ddlc_shared_metadata.yaml")
        stmts = compile_contract(contract)
        comp_table = [s for s in stmts if "component_registry" in s and "CREATE TABLE" in s]
        assert len(comp_table) == 1
        assert "vector(1536)" in comp_table[0]
        assert any("USING hnsw" in s and "vector_cosine_ops" in s for s in stmts)
