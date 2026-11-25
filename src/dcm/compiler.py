"""DCM Contract Compiler — deterministic YAML → PostgreSQL DDL via SQLGlot.

Pipeline:
  YAML file → Pydantic DataContractSchema → In-memory FK/Constraint Validation
  → SQLGlot AST Type/DDL Validation → Ordered PostgreSQL DDL strings
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import sqlglot
import yaml
from sqlglot import exp

from src.dcm.models import (
    ColumnSpec,
    DataContractSchema,
    IndexSpec,
    TableSpec,
    TriggerSpec,
    parse_reference,
)


class ContractValidationError(ValueError):
    """Raised when contract cross-references, columns, or ASTs fail validation."""


# ---------------------------------------------------------------------------
# Type mapping & SQLGlot AST validation
# ---------------------------------------------------------------------------

_PARAMETERIZED_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^VARCHAR\((\d+)\)$", re.IGNORECASE), "VARCHAR({0})"),
    (re.compile(r"^NUMERIC\((\d+)\s*,\s*(\d+)\)$", re.IGNORECASE), "NUMERIC({0}, {1})"),
    (re.compile(r"^VECTOR\((\d+)\)$", re.IGNORECASE), "vector({0})"),
]

_SIMPLE_TYPE_MAP: dict[str, str] = {
    "UUID": "UUID",
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "BOOLEAN": "BOOLEAN",
    "TIMESTAMPTZ": "TIMESTAMPTZ",
    "TIMESTAMP": "TIMESTAMP",
    "JSONB": "JSONB",
    "JSON": "JSON",
    "INET": "INET",
    "SERIAL": "SERIAL",
    "BIGSERIAL": "BIGSERIAL",
    "SMALLINT": "SMALLINT",
    "REAL": "REAL",
    "DOUBLE PRECISION": "DOUBLE PRECISION",
    "DATE": "DATE",
    "TIME": "TIME",
    "TIMETZ": "TIMETZ",
    "BYTEA": "BYTEA",
}


def resolve_pg_type(yaml_type: str) -> str:
    """Map a YAML contract type to canonical PostgreSQL type and validate via SQLGlot AST."""
    stripped = yaml_type.strip()
    upper = stripped.upper()

    resolved: Optional[str] = _SIMPLE_TYPE_MAP.get(upper)
    if resolved is None:
        for pattern, template in _PARAMETERIZED_TYPE_PATTERNS:
            m = pattern.match(stripped)
            if m:
                resolved = template.format(*m.groups())
                break

    if resolved is None:
        raise ValueError(
            f"Unrecognized column type: '{yaml_type}'. "
            f"Supported types: {sorted(_SIMPLE_TYPE_MAP.keys())} plus VARCHAR(n), NUMERIC(p,s), VECTOR(n)."
        )

    # Validate dialect safety through SQLGlot DataType AST
    exp.DataType.build(resolved, dialect="postgres")
    return resolved


def validate_sql_ast(statement: str) -> None:
    """Parse a SQL DDL statement with SQLGlot Postgres dialect to verify syntax."""
    sqlglot.parse_one(statement, read="postgres")


# ---------------------------------------------------------------------------
# Contract loading & referential integrity validation
# ---------------------------------------------------------------------------


def load_contract(path: Path) -> DataContractSchema:
    """Load and validate a YAML data contract file against Pydantic schema."""
    if not path.is_file():
        raise FileNotFoundError(f"Contract file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return DataContractSchema.model_validate(raw)


def load_contracts_from_dir(directory: Path) -> list[DataContractSchema]:
    """Load all top-level YAML contract files from a directory in dependency order."""
    if not directory.is_dir():
        raise NotADirectoryError(f"Contracts directory not found: {directory}")

    contracts = [load_contract(path) for path in sorted(directory.glob("*.yaml"))]
    return sort_contracts_by_dependencies(contracts)


def _contract_dependencies(contract: DataContractSchema) -> set[str]:
    """Return external schema names referenced by FKs in this contract."""
    deps: set[str] = set()
    for table in contract.tables:
        for col in table.columns:
            if col.references:
                ref_schema, _, _ = parse_reference(col.references)
                if ref_schema != contract.schema_name:
                    deps.add(ref_schema)
        for fk in table.foreign_keys:
            ref_schema, _, _ = parse_reference(fk.references)
            if ref_schema != contract.schema_name:
                deps.add(ref_schema)
    return deps


def sort_contracts_by_dependencies(
    contracts: list[DataContractSchema],
) -> list[DataContractSchema]:
    """Topologically order contracts so referenced schemas come before dependents."""
    by_schema = {c.schema_name: c for c in contracts}
    deps_map = {c.schema_name: _contract_dependencies(c) for c in contracts}

    ordered: list[DataContractSchema] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(schema: str) -> None:
        if schema in visited:
            return
        if schema in visiting:
            raise ContractValidationError(
                f"Circular cross-schema FK dependency detected involving '{schema}'."
            )
        visiting.add(schema)
        for dep in sorted(deps_map.get(schema, set())):
            if dep in by_schema:
                visit(dep)
        visiting.remove(schema)
        visited.add(schema)
        ordered.append(by_schema[schema])

    for contract in contracts:
        visit(contract.schema_name)
    return ordered


def validate_contracts_integrity(
    contracts: list[DataContractSchema],
    reference_contracts: Optional[list[DataContractSchema]] = None,
    strict_external: bool = True,
) -> None:
    """Validate internal types, column names, unique keys, and FK referential integrity.

    Args:
        contracts: Primary contracts being validated.
        reference_contracts: Optional sibling contracts providing external schema context
            (for example, Tier 1/2 contracts when validating a Tier 3 contract alone).
        strict_external: When True, every FK target schema must exist in the combined set.
    """
    all_contracts = list(contracts)
    if reference_contracts:
        seen_schemas = {c.schema_name for c in all_contracts}
        for ref_c in reference_contracts:
            if ref_c.schema_name not in seen_schemas:
                all_contracts.append(ref_c)

    # Index schemas -> tables -> column sets + key sets
    seen_primary_schemas: set[str] = set()
    table_columns: dict[tuple[str, str], set[str]] = {}
    table_key_groups: dict[tuple[str, str], set[tuple[str, ...]]] = {}

    for contract in contracts:
        if contract.schema_name in seen_primary_schemas:
            raise ContractValidationError(
                f"Duplicate contract definition for schema '{contract.schema_name}'."
            )
        seen_primary_schemas.add(contract.schema_name)

    for contract in all_contracts:
        seen_tables: set[str] = set()
        for table in contract.tables:
            if table.name in seen_tables:
                raise ContractValidationError(
                    f"Duplicate table '{contract.schema_name}.{table.name}'."
                )
            seen_tables.add(table.name)

            col_names: set[str] = set()
            key_groups: set[tuple[str, ...]] = set()
            for col in table.columns:
                if col.name in col_names:
                    raise ContractValidationError(
                        f"Duplicate column '{col.name}' in '{contract.schema_name}.{table.name}'."
                    )
                resolve_pg_type(col.type)
                col_names.add(col.name)
                if col.primary_key:
                    key_groups.add((col.name,))

            for uq in table.unique_constraints:
                key_groups.add(tuple(uq.columns))
            for idx in table.indexes:
                if idx.unique and not idx.where:
                    key_groups.add(tuple(col for col, _ in idx.parsed_columns()))

            key = (contract.schema_name, table.name)
            table_columns[key] = col_names
            table_key_groups[key] = key_groups

    # Validate constraints & references for primary contracts
    known_schemas = {c.schema_name for c in all_contracts}
    for contract in contracts:
        schema = contract.schema_name
        for table in contract.tables:
            cols = table_columns[(schema, table.name)]
            fq_table = f"{schema}.{table.name}"

            # Validate unique constraints & indexes reference local columns
            for uq in table.unique_constraints:
                missing = [c for c in uq.columns if c not in cols]
                if missing:
                    raise ContractValidationError(
                        f"Unique constraint '{uq.name}' on '{fq_table}' references unknown columns: {missing}."
                    )
            for idx in table.indexes:
                parsed_cols = [c for c, _ in idx.parsed_columns()]
                missing = [c for c in parsed_cols if c not in cols]
                if missing:
                    raise ContractValidationError(
                        f"Index '{idx.name}' on '{fq_table}' references unknown columns: {missing}."
                    )

            # Collect all FKs (column-level + table-level)
            fk_checks: list[tuple[str, list[str], str]] = []
            for col in table.columns:
                if col.references:
                    fk_checks.append((f"fk_{table.name}_{col.name}", [col.name], col.references))
            for fk in table.foreign_keys:
                missing = [c for c in fk.columns if c not in cols]
                if missing:
                    raise ContractValidationError(
                        f"Foreign key '{fk.name}' on '{fq_table}' references unknown local columns: {missing}."
                    )
                fk_checks.append((fk.name, fk.columns, fk.references))

            for fk_name, src_cols, target_ref in fk_checks:
                ref_schema, ref_table, ref_cols = parse_reference(target_ref)
                if len(src_cols) != len(ref_cols):
                    raise ContractValidationError(
                        f"Foreign key '{fk_name}' on '{fq_table}' has column count mismatch: "
                        f"{src_cols} -> {ref_cols}."
                    )
                if ref_schema not in known_schemas:
                    if strict_external:
                        raise ContractValidationError(
                            f"Foreign key '{fk_name}' on '{fq_table}' references unknown schema '{ref_schema}'."
                        )
                    continue
                target_key = (ref_schema, ref_table)
                if target_key not in table_columns:
                    raise ContractValidationError(
                        f"Foreign key '{fk_name}' on '{fq_table}' references unknown table '{ref_schema}.{ref_table}'."
                    )
                missing_target_cols = [c for c in ref_cols if c not in table_columns[target_key]]
                if missing_target_cols:
                    raise ContractValidationError(
                        f"Foreign key '{fk_name}' on '{fq_table}' references unknown columns "
                        f"{missing_target_cols} on '{ref_schema}.{ref_table}'."
                    )
                if tuple(ref_cols) not in table_key_groups[target_key]:
                    raise ContractValidationError(
                        f"Foreign key '{fk_name}' on '{fq_table}' references non-unique target "
                        f"'{ref_schema}.{ref_table}({', '.join(ref_cols)})'."
                    )


# ---------------------------------------------------------------------------
# DDL compilation helpers
# ---------------------------------------------------------------------------


def compile_column_definition(col: ColumnSpec) -> str:
    """Compile a single column definition clause (without table context)."""
    pg_type = resolve_pg_type(col.type)
    parts = [f'"{col.name}" {pg_type}']
    if not col.nullable:
        parts.append("NOT NULL")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    return " ".join(parts)


def compile_create_table(table: TableSpec, schema_name: str) -> str:
    """Compile and AST-validate a CREATE TABLE statement."""
    qualified_name = f'"{schema_name}"."{table.name}"'
    col_lines = [f"    {compile_column_definition(col)}" for col in table.columns]
    constraint_lines: list[str] = []

    pk_cols = [col.name for col in table.columns if col.primary_key]
    if pk_cols:
        pk_cols_sql = ", ".join(f'"{c}"' for c in pk_cols)
        constraint_lines.append(
            f'    CONSTRAINT "{table.name}_pkey" PRIMARY KEY ({pk_cols_sql})'
        )

    for col in table.columns:
        if col.check:
            constraint_lines.append(
                f'    CONSTRAINT "chk_{table.name}_{col.name}" CHECK ({col.check})'
            )

    for chk in table.check_constraints:
        constraint_lines.append(
            f'    CONSTRAINT "{chk.name}" CHECK ({chk.expression})'
        )

    for uq in table.unique_constraints:
        col_list = ", ".join(f'"{c}"' for c in uq.columns)
        constraint_lines.append(
            f'    CONSTRAINT "{uq.name}" UNIQUE ({col_list})'
        )

    body = ",\n".join(col_lines + constraint_lines)
    stmt = f"CREATE TABLE IF NOT EXISTS {qualified_name} (\n{body}\n);"
    validate_sql_ast(stmt)
    return stmt


def _compile_fk_constraint(
    name: str,
    src_cols: list[str],
    references: str,
    on_delete: str,
    on_update: str,
    table_name: str,
    schema_name: str,
) -> str:
    """Shared DDL formatter for both single-column and composite foreign keys."""
    ref_schema, ref_table, ref_cols = parse_reference(references)
    src_col_list = ", ".join(f'"{c}"' for c in src_cols)
    ref_col_list = ", ".join(f'"{c}"' for c in ref_cols)
    stmt = (
        f'ALTER TABLE "{schema_name}"."{table_name}" '
        f'ADD CONSTRAINT "{name}" '
        f"FOREIGN KEY ({src_col_list}) "
        f'REFERENCES "{ref_schema}"."{ref_table}" ({ref_col_list}) '
        f"ON DELETE {on_delete} ON UPDATE {on_update};"
    )
    validate_sql_ast(stmt)
    return stmt


def compile_table_foreign_keys(table: TableSpec, schema_name: str) -> list[str]:
    """Compile all column-level and composite FK statements for a table."""
    stmts: list[str] = []
    for col in table.columns:
        if col.references:
            stmts.append(
                _compile_fk_constraint(
                    name=f"fk_{table.name}_{col.name}",
                    src_cols=[col.name],
                    references=col.references,
                    on_delete=col.on_delete or "NO ACTION",
                    on_update=col.on_update or "NO ACTION",
                    table_name=table.name,
                    schema_name=schema_name,
                )
            )
    for fk in table.foreign_keys:
        stmts.append(
            _compile_fk_constraint(
                name=fk.name,
                src_cols=fk.columns,
                references=fk.references,
                on_delete=fk.on_delete,
                on_update=fk.on_update,
                table_name=table.name,
                schema_name=schema_name,
            )
        )
    return stmts


def compile_index(idx: IndexSpec, table_name: str, schema_name: str) -> str:
    """Compile and AST-validate a CREATE INDEX statement."""
    unique = "UNIQUE " if idx.unique else ""
    col_parts: list[str] = []
    for col_name, direction in idx.parsed_columns():
        part = f'"{col_name}"'
        if idx.opclass:
            part += f" {idx.opclass}"
        if direction:
            part += f" {direction}"
        col_parts.append(part)

    using_clause = f" USING {idx.method}" if idx.method else ""
    stmt = (
        f'CREATE {unique}INDEX IF NOT EXISTS "{idx.name}" '
        f'ON "{schema_name}"."{table_name}"{using_clause} ({", ".join(col_parts)})'
    )
    if idx.where:
        stmt += f" WHERE {idx.where}"
    stmt += ";"
    validate_sql_ast(stmt)
    return stmt


def compile_trigger(trigger: TriggerSpec, table_name: str, schema_name: str) -> list[str]:
    """Compile idempotent PL/pgSQL trigger function + CREATE OR REPLACE TRIGGER."""
    fn_name = f'"{schema_name}"."{trigger.function_name}"'
    events = " OR ".join(trigger.events)
    return [
        f"CREATE OR REPLACE FUNCTION {fn_name}()\n"
        f"RETURNS TRIGGER AS $$\n"
        f"{trigger.function_body.rstrip()}\n"
        f"$$ LANGUAGE plpgsql;",
        f'CREATE OR REPLACE TRIGGER "{trigger.name}"\n'
        f'{trigger.timing} {events} ON "{schema_name}"."{table_name}"\n'
        f"FOR EACH {trigger.for_each} EXECUTE FUNCTION {fn_name}();",
    ]


def compile_contract(
    contract: DataContractSchema,
    validate_integrity: bool = False,
) -> list[str]:
    """Compile a single data contract into ordered PostgreSQL DDL statements."""
    if validate_integrity:
        validate_contracts_integrity([contract], strict_external=False)

    schema = contract.schema_name
    stmts: list[str] = [f'CREATE SCHEMA IF NOT EXISTS "{schema}";']
    validate_sql_ast(stmts[0])

    for table in contract.tables:
        stmts.append(compile_create_table(table, schema))

    for table in contract.tables:
        stmts.extend(compile_table_foreign_keys(table, schema))

    for table in contract.tables:
        for idx in table.indexes:
            stmts.append(compile_index(idx, table.name, schema))

    for table in contract.tables:
        for trigger in table.triggers:
            stmts.extend(compile_trigger(trigger, table.name, schema))

    return stmts


def compile_contracts(
    contracts: list[DataContractSchema],
    validate_integrity: bool = False,
) -> list[str]:
    """Compile multiple contracts in topological dependency order."""
    ordered = sort_contracts_by_dependencies(contracts)
    if validate_integrity:
        validate_contracts_integrity(ordered, strict_external=True)

    stmts: list[str] = []
    # Phase 1: Schemas and Tables
    for contract in ordered:
        schema_stmt = f'CREATE SCHEMA IF NOT EXISTS "{contract.schema_name}";'
        validate_sql_ast(schema_stmt)
        stmts.append(schema_stmt)
        for table in contract.tables:
            stmts.append(compile_create_table(table, contract.schema_name))

    # Phase 2: Foreign Keys across all schemas
    for contract in ordered:
        for table in contract.tables:
            stmts.extend(compile_table_foreign_keys(table, contract.schema_name))

    # Phase 3: Indexes
    for contract in ordered:
        for table in contract.tables:
            for idx in table.indexes:
                stmts.append(compile_index(idx, table.name, contract.schema_name))

    # Phase 4: Triggers
    for contract in ordered:
        for table in contract.tables:
            for trigger in table.triggers:
                stmts.extend(compile_trigger(trigger, table.name, contract.schema_name))

    return stmts


def compile_to_sql(contracts: list[DataContractSchema]) -> str:
    """Compile contracts to a single formatted SQL script string."""
    return "\n\n".join(compile_contracts(contracts))
