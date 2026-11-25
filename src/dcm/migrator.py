"""DCM Migrator — PostgreSQL catalog introspection, schema diffing, and Alembic execution."""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.dcm.compiler import (
    compile_column_definition,
    compile_create_table,
    compile_index,
    compile_table_foreign_keys,
    compile_trigger,
    resolve_pg_type,
    sort_contracts_by_dependencies,
    validate_sql_ast,
)
from src.dcm.models import DataContractSchema, parse_reference
from src.dcm.safety import SafetyReport, analyze_migration, gate_check


class DestructiveMigrationError(RuntimeError):
    """Raised when a migration containing destructive DDL lacks explicit approval."""

    def __init__(self, report: SafetyReport) -> None:
        self.report = report
        super().__init__(
            "Execution halted by Human Gate: destructive DDL detected.\n"
            + report.summary()
        )


def to_sqlalchemy_uri(db_uri: str) -> str:
    """Normalize postgresql:// URIs to postgresql+psycopg:// for psycopg v3."""
    if db_uri.startswith("postgres://"):
        return db_uri.replace("postgres://", "postgresql+psycopg://", 1)
    if db_uri.startswith("postgresql://"):
        return db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_uri


# ---------------------------------------------------------------------------
# Catalog Snapshot Data Structures & Type Normalization
# ---------------------------------------------------------------------------


@dataclass
class IntrospectedColumn:
    """Live column definition introspected from PostgreSQL."""

    name: str
    pg_type: str
    nullable: bool
    default: Optional[str] = None


@dataclass
class IntrospectedTable:
    """Live table structure introspected from PostgreSQL."""

    schema_name: str
    name: str
    columns: dict[str, IntrospectedColumn] = field(default_factory=dict)
    constraints: set[str] = field(default_factory=set)
    indexes: set[str] = field(default_factory=set)
    constraint_indexes: set[str] = field(default_factory=set)
    triggers: set[str] = field(default_factory=set)


@dataclass
class CatalogSnapshot:
    """Snapshot of relevant database schemas and tables."""

    schemas: set[str] = field(default_factory=set)
    tables: dict[tuple[str, str], IntrospectedTable] = field(default_factory=dict)


_TYPE_ALIASES: dict[str, str] = {
    "CHARACTER VARYING": "VARCHAR",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
    "TIME WITHOUT TIME ZONE": "TIME",
    "TIME WITH TIME ZONE": "TIMETZ",
    "INT4": "INTEGER",
    "INT": "INTEGER",
    "INT8": "BIGINT",
    "INT2": "SMALLINT",
    "BOOL": "BOOLEAN",
    "FLOAT4": "REAL",
    "FLOAT8": "DOUBLE PRECISION",
    "DECIMAL": "NUMERIC",
}


def normalize_pg_type(type_str: str) -> str:
    """Normalize PostgreSQL type strings for equivalent schema comparison."""
    cleaned = re.sub(r"\s+", " ", type_str.strip()).upper()
    cleaned = re.sub(r"\s*,\s*", ",", cleaned)
    cleaned = re.sub(r"\s*\(\s*", "(", cleaned)

    if "(" in cleaned:
        base, rest = cleaned.split("(", 1)
        base = _TYPE_ALIASES.get(base.strip(), base.strip())
        return f"{base}({rest}"
    return _TYPE_ALIASES.get(cleaned, cleaned)


def types_equivalent(contract_type: str, db_type: str) -> bool:
    """Return True when a contract type and live PG type represent the same type."""
    return normalize_pg_type(contract_type) == normalize_pg_type(db_type)


def introspect_database(
    db_uri: str,
    target_schemas: list[str],
    engine: Optional[Engine] = None,
) -> CatalogSnapshot:
    """Introspect PostgreSQL catalog for the given target schemas."""
    owns_engine = engine is None
    db_engine = engine or create_engine(to_sqlalchemy_uri(db_uri))
    snapshot = CatalogSnapshot()

    try:
        with db_engine.connect() as conn:
            schema_rows = conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = ANY(:schemas)"
                ),
                {"schemas": target_schemas},
            ).fetchall()
            snapshot.schemas = {row[0] for row in schema_rows}

            if not snapshot.schemas:
                return snapshot

            existing = sorted(snapshot.schemas)

            # Tables
            table_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p')"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name in table_rows:
                snapshot.tables[(schema_name, table_name)] = IntrospectedTable(
                    schema_name=schema_name,
                    name=table_name,
                )

            if not snapshot.tables:
                return snapshot

            # Columns (using format_type for exact TIMESTAMPTZ/vector(N) precision)
            col_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname, a.attname, "
                    "       pg_catalog.format_type(a.atttypid, a.atttypmod) AS pg_type, "
                    "       NOT a.attnotnull AS is_nullable, "
                    "       pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr "
                    "FROM pg_catalog.pg_attribute a "
                    "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_attrdef d "
                    "       ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                    "WHERE n.nspname = ANY(:schemas) "
                    "  AND c.relkind IN ('r', 'p') "
                    "  AND a.attnum > 0 AND NOT a.attisdropped "
                    "ORDER BY n.nspname, c.relname, a.attnum"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, col_name, pg_type, is_nullable, default_expr in col_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.columns[col_name] = IntrospectedColumn(
                        name=col_name,
                        pg_type=pg_type,
                        nullable=bool(is_nullable),
                        default=default_expr,
                    )

            # Constraints & constraint-backing indexes
            con_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname, con.conname, ic.relname AS idx_name "
                    "FROM pg_catalog.pg_constraint con "
                    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_class ic ON ic.oid = con.conindid "
                    "WHERE n.nspname = ANY(:schemas)"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, con_name, idx_name in con_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.constraints.add(con_name)
                    if idx_name:
                        tbl.constraint_indexes.add(idx_name)

            # Indexes
            idx_rows = conn.execute(
                text(
                    "SELECT schemaname, tablename, indexname "
                    "FROM pg_catalog.pg_indexes "
                    "WHERE schemaname = ANY(:schemas)"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, idx_name in idx_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.indexes.add(idx_name)

            # User triggers
            trg_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname, t.tgname "
                    "FROM pg_catalog.pg_trigger t "
                    "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = ANY(:schemas) AND NOT t.tgisinternal"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, tg_name in trg_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.triggers.add(tg_name)
    finally:
        if owns_engine:
            db_engine.dispose()

    return snapshot


# ---------------------------------------------------------------------------
# Diff & Migration Plan Generation
# ---------------------------------------------------------------------------


@dataclass
class DiffOp:
    """A single logical schema change operation."""

    kind: str
    schema: str
    table: Optional[str]
    description: str
    upgrade_sql: list[str] = field(default_factory=list)
    downgrade_sql: list[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    """Computed migration plan between YAML contracts and target catalog."""

    ops: list[DiffOp] = field(default_factory=list)
    schemas: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.ops) == 0

    @property
    def upgrade_statements(self) -> list[str]:
        stmts: list[str] = []
        for op in self.ops:
            stmts.extend(op.upgrade_sql)
        return stmts

    @property
    def downgrade_statements(self) -> list[str]:
        stmts: list[str] = []
        for op in reversed(self.ops):
            stmts.extend(op.downgrade_sql)
        return stmts

    def safety_report(self) -> SafetyReport:
        return analyze_migration(self.upgrade_statements)


def _expected_constraints(table) -> list[tuple[str, str]]:
    """Return [(constraint_name, clause)] for CHECK & UNIQUE constraints on a table."""
    items: list[tuple[str, str]] = []
    for col in table.columns:
        if col.check:
            cname = f"chk_{table.name}_{col.name}"
            items.append((cname, f'CONSTRAINT "{cname}" CHECK ({col.check})'))
    for chk in table.check_constraints:
        items.append((chk.name, f'CONSTRAINT "{chk.name}" CHECK ({chk.expression})'))
    for uq in table.unique_constraints:
        cols_sql = ", ".join(f'"{c}"' for c in uq.columns)
        items.append((uq.name, f'CONSTRAINT "{uq.name}" UNIQUE ({cols_sql})'))
    return items


def _expected_fks(table, schema: str) -> list[tuple[str, str]]:
    """Return [(fk_name, alter_table_sql)] for a table."""
    stmts = compile_table_foreign_keys(table, schema)
    names: list[str] = []
    for col in table.columns:
        if col.references:
            parse_reference(col.references)
            names.append(f"fk_{table.name}_{col.name}")
    for fk in table.foreign_keys:
        names.append(fk.name)
    return list(zip(names, stmts))


def compute_migration_plan(
    contracts: list[DataContractSchema],
    catalog: CatalogSnapshot,
) -> MigrationPlan:
    """Compare validated YAML contracts against an introspected CatalogSnapshot."""
    ordered = sort_contracts_by_dependencies(contracts)
    plan = MigrationPlan(schemas=[c.schema_name for c in ordered])

    deferred_fks: list[DiffOp] = []
    deferred_indexes_triggers: list[DiffOp] = []
    deferred_drops: list[DiffOp] = []

    for contract in ordered:
        schema = contract.schema_name
        if schema not in catalog.schemas:
            plan.ops.append(
                DiffOp(
                    kind="create_schema",
                    schema=schema,
                    table=None,
                    description=f"+ CREATE SCHEMA {schema}",
                    upgrade_sql=[f'CREATE SCHEMA IF NOT EXISTS "{schema}";'],
                    downgrade_sql=[],
                )
            )

        desired_tables = {t.name: t for t in contract.tables}

        for table in contract.tables:
            existing = catalog.tables.get((schema, table.name))
            fq = f'"{schema}"."{table.name}"'

            if existing is None:
                create_stmt = compile_create_table(table, schema)
                plan.ops.append(
                    DiffOp(
                        kind="create_table",
                        schema=schema,
                        table=table.name,
                        description=f"+ CREATE TABLE {schema}.{table.name}",
                        upgrade_sql=[create_stmt],
                        downgrade_sql=[f"DROP TABLE IF EXISTS {fq} CASCADE;"],
                    )
                )

                for fk_name, fk_sql in _expected_fks(table, schema):
                    deferred_fks.append(
                        DiffOp(
                            kind="add_fk",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD FK {schema}.{table.name}.{fk_name}",
                            upgrade_sql=[fk_sql],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{fk_name}";'
                            ],
                        )
                    )

                for idx in table.indexes:
                    idx_sql = compile_index(idx, table.name, schema)
                    deferred_indexes_triggers.append(
                        DiffOp(
                            kind="create_index",
                            schema=schema,
                            table=table.name,
                            description=f"+ CREATE INDEX {schema}.{idx.name}",
                            upgrade_sql=[idx_sql],
                            downgrade_sql=[f'DROP INDEX IF EXISTS "{schema}"."{idx.name}";'],
                        )
                    )

                for trg in table.triggers:
                    trg_stmts = compile_trigger(trg, table.name, schema)
                    deferred_indexes_triggers.append(
                        DiffOp(
                            kind="create_trigger",
                            schema=schema,
                            table=table.name,
                            description=f"+ CREATE TRIGGER {schema}.{table.name}.{trg.name}",
                            upgrade_sql=trg_stmts,
                            downgrade_sql=[
                                f'DROP TRIGGER IF EXISTS "{trg.name}" ON {fq};',
                                f'DROP FUNCTION IF EXISTS "{schema}"."{trg.function_name}"();',
                            ],
                        )
                    )
                continue

            # Existing table: diff columns
            desired_cols = {c.name: c for c in table.columns}
            for col in table.columns:
                live_col = existing.columns.get(col.name)
                if live_col is None:
                    add_stmt = f"ALTER TABLE {fq} ADD COLUMN {compile_column_definition(col)};"
                    validate_sql_ast(add_stmt)
                    plan.ops.append(
                        DiffOp(
                            kind="add_column",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD COLUMN {schema}.{table.name}.{col.name}",
                            upgrade_sql=[add_stmt],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP COLUMN IF EXISTS "{col.name}";'
                            ],
                        )
                    )
                    continue

                if not types_equivalent(col.type, live_col.pg_type):
                    target_pg = resolve_pg_type(col.type)
                    alter_stmt = (
                        f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" '
                        f'TYPE {target_pg} USING "{col.name}"::{target_pg};'
                    )
                    plan.ops.append(
                        DiffOp(
                            kind="alter_column_type",
                            schema=schema,
                            table=table.name,
                            description=(
                                f"~ ALTER COLUMN {schema}.{table.name}.{col.name} "
                                f"TYPE {live_col.pg_type} -> {target_pg}"
                            ),
                            upgrade_sql=[alter_stmt],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" '
                                f'TYPE {live_col.pg_type} USING "{col.name}"::{live_col.pg_type};'
                            ],
                        )
                    )

                if col.nullable != live_col.nullable:
                    action = "DROP NOT NULL" if col.nullable else "SET NOT NULL"
                    reverse_action = "SET NOT NULL" if col.nullable else "DROP NOT NULL"
                    null_stmt = f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" {action};'
                    validate_sql_ast(null_stmt)
                    plan.ops.append(
                        DiffOp(
                            kind="alter_nullable",
                            schema=schema,
                            table=table.name,
                            description=(
                                f"~ ALTER COLUMN {schema}.{table.name}.{col.name} {action}"
                            ),
                            upgrade_sql=[null_stmt],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" {reverse_action};'
                            ],
                        )
                    )

            for live_col_name in sorted(existing.columns.keys()):
                if live_col_name not in desired_cols:
                    drop_stmt = f'ALTER TABLE {fq} DROP COLUMN "{live_col_name}";'
                    validate_sql_ast(drop_stmt)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_column",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP COLUMN {schema}.{table.name}.{live_col_name}",
                            upgrade_sql=[drop_stmt],
                            downgrade_sql=[],
                        )
                    )

            # Missing CHECK / UNIQUE constraints on existing table
            for con_name, con_clause in _expected_constraints(table):
                if con_name not in existing.constraints:
                    con_stmt = f"ALTER TABLE {fq} ADD {con_clause};"
                    validate_sql_ast(con_stmt)
                    plan.ops.append(
                        DiffOp(
                            kind="add_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD CONSTRAINT {schema}.{table.name}.{con_name}",
                            upgrade_sql=[con_stmt],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{con_name}";'
                            ],
                        )
                    )

            # Missing FKs on existing table
            for fk_name, fk_sql in _expected_fks(table, schema):
                if fk_name not in existing.constraints:
                    deferred_fks.append(
                        DiffOp(
                            kind="add_fk",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD FK {schema}.{table.name}.{fk_name}",
                            upgrade_sql=[fk_sql],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{fk_name}";'
                            ],
                        )
                    )

            # Indexes on existing table
            desired_idx_names = {idx.name for idx in table.indexes}
            for idx in table.indexes:
                if idx.name not in existing.indexes:
                    idx_sql = compile_index(idx, table.name, schema)
                    deferred_indexes_triggers.append(
                        DiffOp(
                            kind="create_index",
                            schema=schema,
                            table=table.name,
                            description=f"+ CREATE INDEX {schema}.{idx.name}",
                            upgrade_sql=[idx_sql],
                            downgrade_sql=[f'DROP INDEX IF EXISTS "{schema}"."{idx.name}";'],
                        )
                    )

            for live_idx in sorted(existing.indexes):
                if live_idx not in desired_idx_names and live_idx not in existing.constraint_indexes:
                    drop_idx = f'DROP INDEX IF EXISTS "{schema}"."{live_idx}";'
                    validate_sql_ast(drop_idx)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_index",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP INDEX {schema}.{live_idx}",
                            upgrade_sql=[drop_idx],
                            downgrade_sql=[],
                        )
                    )

            # Triggers on existing table
            for trg in table.triggers:
                if trg.name not in existing.triggers:
                    trg_stmts = compile_trigger(trg, table.name, schema)
                    deferred_indexes_triggers.append(
                        DiffOp(
                            kind="create_trigger",
                            schema=schema,
                            table=table.name,
                            description=f"+ CREATE TRIGGER {schema}.{table.name}.{trg.name}",
                            upgrade_sql=trg_stmts,
                            downgrade_sql=[
                                f'DROP TRIGGER IF EXISTS "{trg.name}" ON {fq};',
                                f'DROP FUNCTION IF EXISTS "{schema}"."{trg.function_name}"();',
                            ],
                        )
                    )

        # Tables in DB inside this managed schema that were removed from contract
        for tbl_schema, tbl_name in sorted(catalog.tables.keys()):
            if tbl_schema == schema and tbl_name not in desired_tables:
                drop_tbl = f'DROP TABLE "{schema}"."{tbl_name}";'
                validate_sql_ast(drop_tbl)
                deferred_drops.append(
                    DiffOp(
                        kind="drop_table",
                        schema=schema,
                        table=tbl_name,
                        description=f"- DROP TABLE {schema}.{tbl_name}",
                        upgrade_sql=[drop_tbl],
                        downgrade_sql=[],
                    )
                )

    plan.ops.extend(deferred_fks)
    plan.ops.extend(deferred_indexes_triggers)
    plan.ops.extend(deferred_drops)
    return plan


def compute_offline_plan(contracts: list[DataContractSchema]) -> MigrationPlan:
    """Compute a baseline MigrationPlan against an empty database catalog."""
    return compute_migration_plan(contracts, CatalogSnapshot())


# ---------------------------------------------------------------------------
# Alembic Execution & Revision Script Generation
# ---------------------------------------------------------------------------


def _get_alembic_config(
    db_uri: str,
    project_root: Optional[Path] = None,
    stdout: Optional[TextIO] = None,
) -> AlembicConfig:
    """Build an Alembic Config object pointing to the project's alembic directory."""
    root = project_root or Path(__file__).resolve().parents[2]
    ini_path = root / "alembic.ini"

    if not ini_path.is_file():
        raise FileNotFoundError(
            f"alembic.ini not found at {ini_path}. "
            "Run the project setup or create the Alembic configuration."
        )

    cfg = AlembicConfig(str(ini_path), stdout=stdout) if stdout is not None else AlembicConfig(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", to_sqlalchemy_uri(db_uri))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def _format_exec_lines(statements: list[str]) -> str:
    """Format SQL statements into valid indented Python `op.execute(...)` calls."""
    if not statements:
        return "    pass"
    lines: list[str] = []
    for stmt in statements:
        escaped = stmt.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        if "\n" in escaped:
            lines.append(f'    op.execute("""\n{escaped}\n    """)')
        else:
            lines.append(f"    op.execute({stmt!r})")
    return "\n".join(lines)


def _populate_revision_file(
    script_path: Path,
    upgrade_stmts: list[str],
    downgrade_stmts: list[str],
) -> None:
    """Inject compiled DDL statements into the Alembic revision's upgrade/downgrade."""
    content = script_path.read_text(encoding="utf-8")
    upgrade_body = _format_exec_lines(upgrade_stmts)
    downgrade_body = _format_exec_lines(downgrade_stmts)

    content = content.replace(
        "def upgrade() -> None:\n    pass",
        f"def upgrade() -> None:\n{upgrade_body}",
        1,
    )
    content = content.replace(
        "def downgrade() -> None:\n    pass",
        f"def downgrade() -> None:\n{downgrade_body}",
        1,
    )
    script_path.write_text(content, encoding="utf-8")


def create_revision(
    db_uri: str,
    message: str,
    contracts: Optional[list[DataContractSchema]] = None,
    project_root: Optional[Path] = None,
    allow_empty: bool = False,
    offline: bool = False,
) -> Optional[str]:
    """Generate a populated Alembic revision from the contract-vs-DB diff."""
    plan: Optional[MigrationPlan] = None
    if contracts:
        if offline:
            plan = compute_offline_plan(contracts)
        else:
            schemas = [c.schema_name for c in contracts]
            catalog = introspect_database(db_uri, schemas)
            plan = compute_migration_plan(contracts, catalog)

        if plan.is_empty and not allow_empty:
            return None

    cfg = _get_alembic_config(db_uri, project_root)
    script = command.revision(cfg, message=message, autogenerate=False)
    if script is None:
        raise RuntimeError("Alembic revision returned None — no migration generated.")

    script_path = Path(script.path)
    if plan is not None and not plan.is_empty:
        _populate_revision_file(
            script_path,
            plan.upgrade_statements,
            plan.downgrade_statements,
        )
    return str(script_path)


def _split_sql_statements(sql_blob: str) -> list[str]:
    """Split an Alembic SQL script on semicolons while respecting PL/pgSQL $$ blocks."""
    stmts: list[str] = []
    current: list[str] = []
    in_dollar_quote = False
    i = 0
    n = len(sql_blob)
    while i < n:
        if sql_blob[i : i + 2] == "$$":
            in_dollar_quote = not in_dollar_quote
            current.append("$$")
            i += 2
            continue
        ch = sql_blob[i]
        if ch == ";" and not in_dollar_quote:
            candidate = "".join(current).strip()
            if candidate:
                stmts.append(candidate + ";")
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _filter_user_ddl(statements: list[str]) -> list[str]:
    """Strip Alembic internal bookkeeping SQL before safety classification."""
    ignored_prefixes = (
        "BEGIN",
        "COMMIT",
        "CREATE TABLE alembic_version",
        "INSERT INTO alembic_version",
        "UPDATE alembic_version",
        "DELETE FROM alembic_version",
    )
    filtered: list[str] = []
    for stmt in statements:
        lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
        body = "\n".join(lines).strip()
        if not body:
            continue
        upper = body.upper()
        if any(upper.startswith(p.upper()) for p in ignored_prefixes):
            continue
        filtered.append(body)
    return filtered


def preview_pending_sql(
    db_uri: str,
    revision: str = "head",
    project_root: Optional[Path] = None,
    current_revision: Optional[str] = None,
) -> str:
    """Capture the exact SQL that Alembic `upgrade` will run for pending revisions."""
    buf = io.StringIO()
    cfg = _get_alembic_config(db_uri, project_root, stdout=buf)
    script_dir = ScriptDirectory.from_config(cfg)
    head = script_dir.get_current_head()
    if head is None:
        return ""

    current = (
        current_revision
        if current_revision is not None
        else get_current_revision(db_uri, project_root)
    )
    if current == head and revision == "head":
        return ""

    rev_range = f"{current}:{revision}" if current else revision
    with redirect_stdout(buf):
        command.upgrade(cfg, rev_range, sql=True)
    return buf.getvalue()


def apply_migrations(
    db_uri: str,
    revision: str = "head",
    project_root: Optional[Path] = None,
    sql_only: bool = False,
    approve_destructive: bool = False,
    current_revision: Optional[str] = None,
) -> str:
    """Inspect pending Alembic SQL through the safety gate and execute migrations."""
    pending_sql = preview_pending_sql(
        db_uri,
        revision=revision,
        project_root=project_root,
        current_revision=current_revision,
    )
    user_stmts = _filter_user_ddl(_split_sql_statements(pending_sql))
    report = analyze_migration(user_stmts)

    if not gate_check(report, approve_destructive=approve_destructive):
        raise DestructiveMigrationError(report)

    if sql_only:
        return pending_sql

    cfg = _get_alembic_config(db_uri, project_root)
    command.upgrade(cfg, revision)
    return pending_sql


def get_current_revision(
    db_uri: str,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """Get the current Alembic migration revision from public.alembic_version."""
    engine = create_engine(to_sqlalchemy_uri(db_uri))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT version_num FROM public.alembic_version LIMIT 1")
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        engine.dispose()


def get_migration_status(
    db_uri: str,
    contracts: Optional[list[DataContractSchema]] = None,
    project_root: Optional[Path] = None,
) -> dict:
    """Get comprehensive migration and per-schema sync status."""
    cfg = _get_alembic_config(db_uri, project_root)
    script_dir = ScriptDirectory.from_config(cfg)
    head = script_dir.get_current_head()
    current = get_current_revision(db_uri, project_root)

    schema_sync: dict[str, bool] = {}
    if contracts:
        schemas = [c.schema_name for c in contracts]
        catalog = introspect_database(db_uri, schemas)
        for contract in contracts:
            sub_plan = compute_migration_plan([contract], catalog)
            schema_sync[contract.schema_name] = sub_plan.is_empty

    return {
        "current_revision": current,
        "head_revision": head,
        "is_up_to_date": current == head,
        "pending_migrations": current != head,
        "schemas": schema_sync,
    }
