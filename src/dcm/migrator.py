"""DCM Migrator — PostgreSQL catalog introspection, deep schema diffing, and Alembic execution."""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO

import psycopg
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .compiler import (
    compile_column_definition,
    compile_create_table,
    compile_fk_constraint,
    compile_index,
    compile_trigger,
    iter_table_foreign_keys,
    resolve_pg_type,
    sort_contracts_by_dependencies,
    types_equivalent,
    validate_sql_ast,
)
from .models import DataContractSchema, IndexSpec, TableSpec, parse_reference
from .safety import SafetyReport, analyze_migration, gate_check


class DestructiveMigrationError(RuntimeError):
    """Raised when a migration containing destructive DDL lacks explicit approval."""

    def __init__(self, report: SafetyReport) -> None:
        self.report = report
        super().__init__(
            "Execution halted by Human Gate: destructive DDL detected.\n"
            + report.summary()
        )


def to_sqlalchemy_uri(db_uri: str) -> str:
    """Normalize postgres:// and postgresql:// URIs to postgresql+psycopg://."""
    if db_uri.startswith("postgres://"):
        return db_uri.replace("postgres://", "postgresql+psycopg://", 1)
    if db_uri.startswith("postgresql://"):
        return db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_uri


# ---------------------------------------------------------------------------
# Catalog Snapshot & PostgreSQL Normalization
# ---------------------------------------------------------------------------


@dataclass
class IntrospectedColumn:
    """Live column definition introspected from PostgreSQL."""

    name: str
    pg_type: str
    nullable: bool
    default: Optional[str] = None


@dataclass
class IntrospectedTrigger:
    """Live trigger metadata introspected from PostgreSQL."""

    name: str
    timing: str
    events: tuple[str, ...]
    for_each: str
    function_schema: str
    function_name: str
    function_body: str


@dataclass
class IntrospectedTable:
    """Live table structure introspected from PostgreSQL."""

    schema_name: str
    name: str
    columns: dict[str, IntrospectedColumn] = field(default_factory=dict)
    constraints: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (contype, def)
    indexes: dict[str, str] = field(default_factory=dict)  # name -> indexdef
    constraint_indexes: set[str] = field(default_factory=set)
    triggers: dict[str, IntrospectedTrigger] = field(default_factory=dict)


@dataclass
class CatalogSnapshot:
    """Snapshot of relevant database schemas and tables."""

    schemas: set[str] = field(default_factory=set)
    tables: dict[tuple[str, str], IntrospectedTable] = field(default_factory=dict)


_PG_CAST_RE = re.compile(
    r"::(?:character varying|varchar(?:\(\d+\))?|text|numeric(?:\(\d+,\d+\))?|"
    r"integer|int4|bigint|int8|smallint|int2|jsonb|json|uuid|boolean|bool|"
    r"double precision|real|inet|vector(?:\(\d+\))?|"
    r"timestamp with time zone|timestamp without time zone|"
    r"timestamptz|timestamp)(?:\[\])?",
    re.IGNORECASE,
)


def normalize_default_expr(expr: Optional[str]) -> Optional[str]:
    """Normalize SQL DEFAULT expression for drift comparison."""
    if expr is None:
        return None
    stripped = expr.strip()
    if re.match(r"^NULL(?:::.+)?$", stripped, re.IGNORECASE):
        return None
    stripped = _PG_CAST_RE.sub("", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped.upper() in {"NULL", ""}:
        return None
    return stripped


def _strip_outer_parens(expr: str) -> str:
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        wrapped = True
        for idx, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx < len(s) - 1:
                    wrapped = False
                    break
        if not wrapped or depth != 0:
            break
        s = s[1:-1].strip()
    return s


def normalize_check_expr(expr: str) -> str:
    """Normalize CHECK constraint expressions across PostgreSQL rewrite forms."""
    s = expr.strip()
    if s.upper().startswith("CHECK"):
        s = s[5:].strip()
    s = _strip_outer_parens(s)
    s = _PG_CAST_RE.sub("", s)
    s = s.replace("<>", "!=")
    s = re.sub(
        r"\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*ANY\s*\(\s*ARRAY\[([^\]]+)\]\s*\)\s*\)?",
        r"\1 IN (\2)",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*!=\s*ALL\s*\(\s*ARRAY\[([^\]]+)\]\s*\)\s*\)?",
        r"\1 NOT IN (\2)",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"([A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+([^\s)]+)\s+AND\s+([^\s)]+)",
        r"(\1 >= \2 AND \1 <= \3)",
        s,
        flags=re.IGNORECASE,
    )
    # Remove parens and spaces uniformly so PG parenthesization matches YAML input
    s = s.replace("(", " ").replace(")", " ")
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _normalize_unique_cols(condef: str) -> tuple[str, ...]:
    m = re.match(r"^\s*UNIQUE\s*\(([^)]+)\)", condef, re.IGNORECASE)
    if not m:
        return ()
    return tuple(c.strip().strip('"') for c in m.group(1).split(","))


def _normalize_fk_def(
    condef: str, default_schema: str
) -> Optional[tuple[tuple[str, ...], str, str, tuple[str, ...], str, str]]:
    """Parse PostgreSQL pg_get_constraintdef FOREIGN KEY into comparable tuple."""
    m = re.match(
        r"^\s*FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+([^(]+)\(([^)]+)\)(.*)$",
        condef.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    src_cols = tuple(c.strip().strip('"') for c in m.group(1).split(","))
    target_tbl = m.group(2).strip().replace('"', "")
    if "." in target_tbl:
        ref_schema, ref_table = target_tbl.split(".", 1)
    else:
        ref_schema, ref_table = default_schema, target_tbl
    ref_cols = tuple(c.strip().strip('"') for c in m.group(3).split(","))
    tail = m.group(4)
    on_del_m = re.search(
        r"ON\s+DELETE\s+(CASCADE|SET NULL|RESTRICT|NO ACTION|SET DEFAULT)",
        tail,
        re.IGNORECASE,
    )
    on_upd_m = re.search(
        r"ON\s+UPDATE\s+(CASCADE|SET NULL|RESTRICT|NO ACTION|SET DEFAULT)",
        tail,
        re.IGNORECASE,
    )
    on_delete = on_del_m.group(1).upper() if on_del_m else "NO ACTION"
    on_update = on_upd_m.group(1).upper() if on_upd_m else "NO ACTION"
    return (src_cols, ref_schema, ref_table, ref_cols, on_delete, on_update)


def _normalize_where_clause(where: Optional[str]) -> str:
    if not where:
        return ""
    s = _PG_CAST_RE.sub("", where.strip())
    s = s.replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def _parse_pg_indexdef(
    indexdef: str,
) -> tuple[bool, str, tuple[tuple[str, Optional[str], str], ...], str]:
    """Parse PostgreSQL pg_indexes.indexdef into (unique, method, cols, where_norm)."""
    m = re.match(
        r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+\S+\s+ON\s+\S+\s+USING\s+(\w+)\s+\((.+?)\)(?:\s+WHERE\s+(.+))?$",
        indexdef.strip(),
        re.IGNORECASE,
    )
    if not m:
        return (False, "btree", (), "")
    is_unique = bool(m.group(1))
    method = m.group(2).lower()
    raw_cols = m.group(3)
    where_norm = _normalize_where_clause(m.group(4))

    cols: list[tuple[str, Optional[str], str]] = []
    for entry in raw_cols.split(","):
        tokens = entry.strip().replace('"', "").split()
        if not tokens:
            continue
        col_name = tokens[0]
        direction = "ASC"
        opclass: Optional[str] = None
        for tok in tokens[1:]:
            up = tok.upper()
            if up in {"ASC", "DESC"}:
                direction = up
            elif up not in {"NULLS", "FIRST", "LAST"}:
                opclass = tok.lower()
        cols.append((col_name, opclass, direction))
    return (is_unique, method, tuple(cols), where_norm)


def _expected_index_signature(
    idx: IndexSpec,
) -> tuple[bool, str, tuple[tuple[str, Optional[str], str], ...], str]:
    cols = tuple(
        (
            col_name,
            idx.opclass.lower() if idx.opclass else None,
            direction or "ASC",
        )
        for col_name, direction in idx.parsed_columns()
    )
    return (
        idx.unique,
        (idx.method or "btree").lower(),
        cols,
        _normalize_where_clause(idx.where),
    )


def _decode_trigger_events(tgtype: int) -> tuple[str, tuple[str, ...], str]:
    """Decode pg_trigger.tgtype bitmask into (timing, sorted_events, for_each)."""
    for_each = "ROW" if (tgtype & 1) else "STATEMENT"
    if tgtype & 2:
        timing = "BEFORE"
    elif tgtype & 64:
        timing = "INSTEAD OF"
    else:
        timing = "AFTER"
    events: list[str] = []
    if tgtype & 4:
        events.append("INSERT")
    if tgtype & 8:
        events.append("DELETE")
    if tgtype & 16:
        events.append("UPDATE")
    if tgtype & 32:
        events.append("TRUNCATE")
    return timing, tuple(sorted(events)), for_each


def _normalize_plpgsql_body(body: str) -> str:
    return re.sub(r"\s+", " ", body.strip()).strip()


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

            con_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname, con.conname, con.contype::text, "
                    "       pg_catalog.pg_get_constraintdef(con.oid, true) AS condef, "
                    "       ic.relname AS idx_name "
                    "FROM pg_catalog.pg_constraint con "
                    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_class ic ON ic.oid = con.conindid "
                    "WHERE n.nspname = ANY(:schemas)"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, con_name, contype, condef, idx_name in con_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.constraints[con_name] = (contype, condef)
                    if idx_name:
                        tbl.constraint_indexes.add(idx_name)

            idx_rows = conn.execute(
                text(
                    "SELECT schemaname, tablename, indexname, indexdef "
                    "FROM pg_catalog.pg_indexes "
                    "WHERE schemaname = ANY(:schemas)"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, idx_name, indexdef in idx_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    tbl.indexes[idx_name] = indexdef

            trg_rows = conn.execute(
                text(
                    "SELECT n.nspname, c.relname, t.tgname, t.tgtype, "
                    "       pn.nspname AS fn_schema, p.proname AS fn_name, p.prosrc AS fn_body "
                    "FROM pg_catalog.pg_trigger t "
                    "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid "
                    "JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace "
                    "WHERE n.nspname = ANY(:schemas) AND NOT t.tgisinternal"
                ),
                {"schemas": existing},
            ).fetchall()
            for schema_name, table_name, tg_name, tgtype, fn_schema, fn_name, fn_body in trg_rows:
                tbl = snapshot.tables.get((schema_name, table_name))
                if tbl is not None:
                    timing, events, for_each = _decode_trigger_events(int(tgtype))
                    tbl.triggers[tg_name] = IntrospectedTrigger(
                        name=tg_name,
                        timing=timing,
                        events=events,
                        for_each=for_each,
                        function_schema=fn_schema,
                        function_name=fn_name,
                        function_body=fn_body or "",
                    )
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


def _expected_checks(table: TableSpec) -> dict[str, tuple[str, str]]:
    """Return {constraint_name: (add_clause, raw_expr)} for CHECK constraints."""
    out: dict[str, tuple[str, str]] = {}
    for col in table.columns:
        if col.check:
            cname = f"chk_{table.name}_{col.name}"
            out[cname] = (f'CONSTRAINT "{cname}" CHECK ({col.check})', col.check)
    for chk in table.check_constraints:
        out[chk.name] = (
            f'CONSTRAINT "{chk.name}" CHECK ({chk.expression})',
            chk.expression,
        )
    return out


def _expected_uniques(table: TableSpec) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Return {constraint_name: (add_clause, cols_tuple)} for UNIQUE constraints."""
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for uq in table.unique_constraints:
        cols_sql = ", ".join(f'"{c}"' for c in uq.columns)
        out[uq.name] = (
            f'CONSTRAINT "{uq.name}" UNIQUE ({cols_sql})',
            tuple(uq.columns),
        )
    return out


def _expected_fks(table: TableSpec, schema: str) -> dict[str, tuple[tuple, str]]:
    """Return {fk_name: (normalized_tuple, alter_table_sql)} for a table."""
    out: dict[str, tuple[tuple, str]] = {}
    for fk_name, src_cols, target_ref, on_delete, on_update in iter_table_foreign_keys(table):
        ref_schema, ref_table, ref_cols = parse_reference(target_ref)
        fk_sql = compile_fk_constraint(
            name=fk_name,
            src_cols=src_cols,
            references=target_ref,
            on_delete=on_delete,
            on_update=on_update,
            table_name=table.name,
            schema_name=schema,
        )
        sig = (
            tuple(src_cols),
            ref_schema,
            ref_table,
            tuple(ref_cols),
            on_delete.upper(),
            on_update.upper(),
        )
        out[fk_name] = (sig, fk_sql)
    return out


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

                for fk_name, (_, fk_sql) in _expected_fks(table, schema).items():
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

            # Existing table: diff columns (type, nullable, default)
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
                    validate_sql_ast(alter_stmt)
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

                # Column DEFAULT drift check (ignore implicit nextval sequence defaults on *SERIAL)
                is_serial = col.type.strip().upper() in {"SERIAL", "BIGSERIAL", "SMALLSERIAL"}
                desired_default = normalize_default_expr(col.default)
                live_default = normalize_default_expr(live_col.default)
                if not (is_serial and col.default is None and live_default and live_default.startswith("nextval(")):
                    if desired_default != live_default:
                        if desired_default is None:
                            def_stmt = f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" DROP DEFAULT;'
                        else:
                            def_stmt = (
                                f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" '
                                f"SET DEFAULT {col.default};"
                            )
                        if live_default is None:
                            rev_def = f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" DROP DEFAULT;'
                        else:
                            rev_def = (
                                f'ALTER TABLE {fq} ALTER COLUMN "{col.name}" '
                                f"SET DEFAULT {live_col.default};"
                            )
                        validate_sql_ast(def_stmt)
                        plan.ops.append(
                            DiffOp(
                                kind="alter_default",
                                schema=schema,
                                table=table.name,
                                description=(
                                    f"~ ALTER COLUMN {schema}.{table.name}.{col.name} "
                                    f"DEFAULT {live_default} -> {desired_default}"
                                ),
                                upgrade_sql=[def_stmt],
                                downgrade_sql=[rev_def],
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

            # Diff CHECK constraints (missing, modified, or removed)
            desired_checks = _expected_checks(table)
            for chk_name, (chk_clause, raw_expr) in desired_checks.items():
                live_entry = existing.constraints.get(chk_name)
                if live_entry is None:
                    add_chk = f"ALTER TABLE {fq} ADD {chk_clause};"
                    validate_sql_ast(add_chk)
                    plan.ops.append(
                        DiffOp(
                            kind="add_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD CHECK {schema}.{table.name}.{chk_name}",
                            upgrade_sql=[add_chk],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{chk_name}";'
                            ],
                        )
                    )
                elif normalize_check_expr(raw_expr) != normalize_check_expr(live_entry[1]):
                    drop_old = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{chk_name}";'
                    add_new = f"ALTER TABLE {fq} ADD {chk_clause};"
                    validate_sql_ast(drop_old)
                    validate_sql_ast(add_new)
                    plan.ops.append(
                        DiffOp(
                            kind="alter_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"~ REPLACE CHECK {schema}.{table.name}.{chk_name}",
                            upgrade_sql=[drop_old, add_new],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{chk_name}";',
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{chk_name}" {live_entry[1]};',
                            ],
                        )
                    )

            # Diff UNIQUE constraints (missing, modified, or removed)
            desired_uniques = _expected_uniques(table)
            for uq_name, (uq_clause, uq_cols) in desired_uniques.items():
                live_entry = existing.constraints.get(uq_name)
                if live_entry is None:
                    add_uq = f"ALTER TABLE {fq} ADD {uq_clause};"
                    validate_sql_ast(add_uq)
                    plan.ops.append(
                        DiffOp(
                            kind="add_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"+ ADD UNIQUE {schema}.{table.name}.{uq_name}",
                            upgrade_sql=[add_uq],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{uq_name}";'
                            ],
                        )
                    )
                elif uq_cols != _normalize_unique_cols(live_entry[1]):
                    drop_old = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{uq_name}";'
                    add_new = f"ALTER TABLE {fq} ADD {uq_clause};"
                    validate_sql_ast(drop_old)
                    validate_sql_ast(add_new)
                    plan.ops.append(
                        DiffOp(
                            kind="alter_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"~ REPLACE UNIQUE {schema}.{table.name}.{uq_name}",
                            upgrade_sql=[drop_old, add_new],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{uq_name}";',
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{uq_name}" {live_entry[1]};',
                            ],
                        )
                    )

            # Dropped CHECK or UNIQUE constraints
            for con_name, (contype, condef) in sorted(existing.constraints.items()):
                if contype == "c" and con_name not in desired_checks:
                    drop_c = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{con_name}";'
                    validate_sql_ast(drop_c)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP CHECK {schema}.{table.name}.{con_name}",
                            upgrade_sql=[drop_c],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{con_name}" {condef};'
                            ],
                        )
                    )
                elif contype == "u" and con_name not in desired_uniques:
                    drop_u = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{con_name}";'
                    validate_sql_ast(drop_u)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP UNIQUE {schema}.{table.name}.{con_name}",
                            upgrade_sql=[drop_u],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{con_name}" {condef};'
                            ],
                        )
                    )

            # Diff FKs (missing, modified, or removed)
            desired_fks = _expected_fks(table, schema)
            for fk_name, (fk_sig, fk_sql) in desired_fks.items():
                live_entry = existing.constraints.get(fk_name)
                if live_entry is None:
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
                elif fk_sig != _normalize_fk_def(live_entry[1], schema):
                    drop_fk = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{fk_name}";'
                    validate_sql_ast(drop_fk)
                    deferred_fks.append(
                        DiffOp(
                            kind="alter_fk",
                            schema=schema,
                            table=table.name,
                            description=f"~ REPLACE FK {schema}.{table.name}.{fk_name}",
                            upgrade_sql=[drop_fk, fk_sql],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{fk_name}";',
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{fk_name}" {live_entry[1]};',
                            ],
                        )
                    )

            for con_name, (contype, condef) in sorted(existing.constraints.items()):
                if contype == "f" and con_name not in desired_fks:
                    drop_fk = f'ALTER TABLE {fq} DROP CONSTRAINT IF EXISTS "{con_name}";'
                    validate_sql_ast(drop_fk)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_constraint",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP FK {schema}.{table.name}.{con_name}",
                            upgrade_sql=[drop_fk],
                            downgrade_sql=[
                                f'ALTER TABLE {fq} ADD CONSTRAINT "{con_name}" {condef};'
                            ],
                        )
                    )

            # Diff Indexes (missing, modified, or removed standalone indexes)
            desired_idx_map = {idx.name: idx for idx in table.indexes}
            for idx in table.indexes:
                live_idxdef = existing.indexes.get(idx.name)
                idx_sql = compile_index(idx, table.name, schema)
                if live_idxdef is None:
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
                elif _expected_index_signature(idx) != _parse_pg_indexdef(live_idxdef):
                    drop_old = f'DROP INDEX IF EXISTS "{schema}"."{idx.name}";'
                    validate_sql_ast(drop_old)
                    deferred_indexes_triggers.append(
                        DiffOp(
                            kind="alter_index",
                            schema=schema,
                            table=table.name,
                            description=f"~ REPLACE INDEX {schema}.{idx.name}",
                            upgrade_sql=[drop_old, idx_sql],
                            downgrade_sql=[
                                f'DROP INDEX IF EXISTS "{schema}"."{idx.name}";',
                                f"{live_idxdef};",
                            ],
                        )
                    )

            for live_idx, live_idxdef in sorted(existing.indexes.items()):
                if live_idx not in desired_idx_map and live_idx not in existing.constraint_indexes:
                    drop_idx = f'DROP INDEX IF EXISTS "{schema}"."{live_idx}";'
                    validate_sql_ast(drop_idx)
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_index",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP INDEX {schema}.{live_idx}",
                            upgrade_sql=[drop_idx],
                            downgrade_sql=[f"{live_idxdef};"],
                        )
                    )

            # Diff Triggers (missing, changed definition/body, or removed)
            desired_triggers = {t.name: t for t in table.triggers}
            for trg in table.triggers:
                live_trg = existing.triggers.get(trg.name)
                trg_stmts = compile_trigger(trg, table.name, schema)
                if live_trg is None:
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
                else:
                    trg_changed = (
                        trg.timing != live_trg.timing
                        or tuple(sorted(trg.events)) != live_trg.events
                        or trg.for_each != live_trg.for_each
                        or trg.function_name != live_trg.function_name
                        or _normalize_plpgsql_body(trg.function_body)
                        != _normalize_plpgsql_body(live_trg.function_body)
                    )
                    if trg_changed:
                        deferred_indexes_triggers.append(
                            DiffOp(
                                kind="alter_trigger",
                                schema=schema,
                                table=table.name,
                                description=f"~ UPDATE TRIGGER {schema}.{table.name}.{trg.name}",
                                upgrade_sql=trg_stmts,
                                downgrade_sql=[],
                            )
                        )

            for live_trg_name, live_trg in sorted(existing.triggers.items()):
                if live_trg_name not in desired_triggers:
                    deferred_drops.append(
                        DiffOp(
                            kind="drop_trigger",
                            schema=schema,
                            table=table.name,
                            description=f"- DROP TRIGGER {schema}.{table.name}.{live_trg_name}",
                            upgrade_sql=[f'DROP TRIGGER IF EXISTS "{live_trg_name}" ON {fq};'],
                            downgrade_sql=[],
                        )
                    )

        # Tables removed from contract (ordered reversed from contract declaration, CASCADE safe)
        for tbl_schema, tbl_name in reversed(sorted(catalog.tables.keys())):
            if tbl_schema == schema and tbl_name not in desired_tables:
                drop_tbl = f'DROP TABLE IF EXISTS "{schema}"."{tbl_name}" CASCADE;'
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

    cfg = (
        AlembicConfig(str(ini_path), stdout=stdout)
        if stdout is not None
        else AlembicConfig(str(ini_path))
    )
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


_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _split_sql_statements(sql_blob: str) -> list[str]:
    """Split an Alembic SQL script on semicolons while respecting quotes, comments, and $tag$ blocks."""
    stmts: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql_blob)

    while i < n:
        ch = sql_blob[i]
        # Line comment -- ...
        if ch == "-" and i + 1 < n and sql_blob[i + 1] == "-":
            end = sql_blob.find("\n", i + 2)
            chunk = sql_blob[i:] if end == -1 else sql_blob[i : end + 1]
            current.append(chunk)
            i = n if end == -1 else end + 1
            continue
        # Block comment /* ... */
        if ch == "/" and i + 1 < n and sql_blob[i + 1] == "*":
            end = sql_blob.find("*/", i + 2)
            chunk = sql_blob[i:] if end == -1 else sql_blob[i : end + 2]
            current.append(chunk)
            i = n if end == -1 else end + 2
            continue
        # Single-quoted string literal
        if ch == "'":
            start = i
            i += 1
            while i < n:
                if sql_blob[i] == "'":
                    if i + 1 < n and sql_blob[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            current.append(sql_blob[start:i])
            continue
        # Double-quoted identifier
        if ch == '"':
            start = i
            i += 1
            while i < n:
                if sql_blob[i] == '"':
                    if i + 1 < n and sql_blob[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            current.append(sql_blob[start:i])
            continue
        # Dollar-quoted body ($tag$ ... $tag$)
        if ch == "$":
            m = _TAG_RE.match(sql_blob, i)
            if m:
                tag = m.group(0)
                end = sql_blob.find(tag, m.end())
                if end != -1:
                    current.append(sql_blob[i : end + len(tag)])
                    i = end + len(tag)
                    continue
        if ch == ";":
            candidate = "".join(current).strip()
            if candidate:
                stmts.append(candidate + ";")
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    tail = "".join(current).strip()
    if tail:
        stmts.append(tail)
    return stmts


_ALEMBIC_BOOKKEEPING_RE = re.compile(
    r"^(?:BEGIN\b|COMMIT\b|"
    r"(?:CREATE\s+TABLE|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r'(?:"?public"?\.)?"?alembic_version"?\b)',
    re.IGNORECASE,
)


def _filter_user_ddl(statements: list[str]) -> list[str]:
    """Strip Alembic internal bookkeeping SQL before safety classification."""
    filtered: list[str] = []
    for stmt in statements:
        lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
        body = "\n".join(lines).strip()
        if not body:
            continue
        if _ALEMBIC_BOOKKEEPING_RE.match(body):
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
    """Get the current Alembic migration revision from public.alembic_version.

    Returns None when the database is reachable but public.alembic_version
    has not been created yet; propagates connection errors.
    """
    engine = create_engine(to_sqlalchemy_uri(db_uri))
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT version_num FROM public.alembic_version LIMIT 1")
            )
            row = result.fetchone()
            return row[0] if row else None
    except SQLAlchemyError as exc:
        orig = getattr(exc, "orig", None)
        if isinstance(orig, psycopg.errors.UndefinedTable):
            return None
        raise
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
