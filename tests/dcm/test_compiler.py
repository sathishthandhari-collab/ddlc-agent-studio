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
    validate_sql_ast,
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
        assert resolve_pg_type("SERIAL") == "SERIAL"
        assert resolve_pg_type("BIGSERIAL") == "BIGSERIAL"
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


class TestValidateSqlAst:
    def test_rejects_multi_statement_block(self):
        with pytest.raises(ContractValidationError, match="Expected exactly 1 DDL statement"):
            validate_sql_ast('CREATE TABLE "s"."t" ("a" INT); DROP TABLE users;')

    def test_rejects_malformed_ddl(self):
        with pytest.raises(ContractValidationError, match="Invalid PostgreSQL DDL AST"):
            validate_sql_ast("ALTER TABLE s.t DROP COLUMN ;")
        with pytest.raises(ContractValidationError, match="Invalid PostgreSQL DDL AST"):
            validate_sql_ast("CREATE TABLE s.t ();")
        with pytest.raises(ContractValidationError, match="DDL statement"):
            validate_sql_ast("SELECT 1;")


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

    @pytest.mark.parametrize("bad_ident", ['bad"name', "bad;drop", "1starts_num", "has space"])
    def test_invalid_sql_identifiers_rejected(self, bad_ident: str):
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            ColumnSpec(name=bad_ident, type="UUID")
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            TableSpec(name=bad_ident, columns=[ColumnSpec(name="id", type="UUID")])


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

    def test_fk_to_standalone_unique_index_rejected(self):
        bad = DataContractSchema(
            version="1.0",
            schema_name="broken",
            tables=[
                TableSpec(
                    name="parent",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="code", type="VARCHAR(50)", nullable=False),
                    ],
                    indexes=[IndexSpec(name="idx_parent_code_uq", columns=["code"], unique=True)],
                ),
                TableSpec(
                    name="child",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(
                            name="code",
                            type="VARCHAR(50)",
                            references="broken.parent(code)",
                        ),
                    ],
                ),
            ],
        )
        with pytest.raises(ContractValidationError, match="must match PK or UNIQUE constraint"):
            validate_contracts_integrity([bad])

    def test_fk_arity_and_type_mismatch_rejected(self):
        arity_bad = DataContractSchema(
            version="1.0",
            schema_name="broken",
            tables=[
                TableSpec(
                    name="p",
                    columns=[
                        ColumnSpec(name="a", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(name="b", type="UUID", nullable=False),
                    ],
                    unique_constraints=[UniqueConstraintSpec(name="uq_ab", columns=["a", "b"])],
                ),
                TableSpec(
                    name="c",
                    columns=[ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False)],
                    foreign_keys=[
                        ForeignKeySpec(
                            name="fk_bad_arity",
                            columns=["id"],
                            references="broken.p(a, b)",
                        )
                    ],
                ),
            ],
        )
        with pytest.raises(ContractValidationError, match="column count mismatch"):
            validate_contracts_integrity([arity_bad])

        type_bad = DataContractSchema(
            version="1.0",
            schema_name="broken",
            tables=[
                TableSpec(
                    name="p",
                    columns=[ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False)],
                ),
                TableSpec(
                    name="c",
                    columns=[
                        ColumnSpec(name="id", type="UUID", primary_key=True, nullable=False),
                        ColumnSpec(
                            name="p_id",
                            type="VARCHAR(50)",
                            references="broken.p(id)",
                        ),
                    ],
                ),
            ],
        )
        with pytest.raises(ContractValidationError, match="incompatible types"):
            validate_contracts_integrity([type_bad])

    def test_topological_sort_orders_parent_schema_first_and_detects_cycles(self):
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

        parent.tables[0].columns.append(
            ColumnSpec(name="c_id", type="UUID", references="aaa_child.c(id)")
        )
        with pytest.raises(ContractValidationError, match="Circular cross-schema FK"):
            sort_contracts_by_dependencies([child, parent])

    def test_explicit_fk_name_and_referential_actions(self):
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
                        ColumnSpec(
                            name="p_id",
                            type="UUID",
                            references="myschema.parent(a)",
                            fk_name="fk_custom_p",
                            on_delete="RESTRICT",
                            on_update="CASCADE",
                        ),
                        ColumnSpec(name="ref_b", type="UUID", nullable=True),
                    ],
                    foreign_keys=[
                        ForeignKeySpec(
                            name="fk_child_parent",
                            columns=["p_id", "ref_b"],
                            references="myschema.parent(a, b)",
                            on_delete="SET NULL",
                        ),
                    ],
                ),
            ],
        )
        stmts = compile_contract(contract, validate_integrity=True)
        fk_stmts = [s for s in stmts if "FOREIGN KEY" in s]
        assert any(
            'CONSTRAINT "fk_custom_p"' in s and "ON DELETE RESTRICT ON UPDATE CASCADE" in s
            for s in fk_stmts
        )
        assert any('CONSTRAINT "fk_child_parent"' in s and "ON DELETE SET NULL" in s for s in fk_stmts)

    def test_trigger_compilation_idempotent_and_rejects_raw_dollar_delimiters(self):
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

        contract.tables[0].triggers[0].function_body = "BEGIN RAISE EXCEPTION $$oops$$; END;"
        with pytest.raises(ContractValidationError, match=r"\$\$"):
            compile_contract(contract)


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

    def test_tier1_has_explicit_fk_names_immutability_and_desc_indexes(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_1_agent_metadata.yaml")
        stmts = compile_contract(contract)
        sql = "\n".join(stmts)
        for fk in [
            "fk_run_parent",
            "fk_exec_run",
            "fk_exec_parent",
            "fk_exec_retry",
            "fk_artifact_run",
            "fk_artifact_step",
            "fk_manifest_run",
            "fk_manifest_artifact",
            "fk_manifest_step",
            "fk_audit_run",
            "fk_decision_run",
            "fk_decision_step",
        ]:
            assert f'"{fk}"' in sql
        assert "trg_audit_log_immutable" in sql
        assert '"actor_id", "occurred_at" DESC' in sql

    def test_tier2_has_decision26_stages_vector_column_and_hnsw_index(self, contracts_dir):
        contract = load_contract(contracts_dir / "tier_2_ddlc_shared_metadata.yaml")
        stmts = compile_contract(contract)
        sql = "\n".join(stmts)
        assert "vector(1536)" in sql
        assert "USING hnsw" in sql and "vector_cosine_ops" in sql
        assert "'code_generation'" in sql
        assert "'sandbox_testing'" in sql
