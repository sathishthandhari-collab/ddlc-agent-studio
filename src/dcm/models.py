"""Pydantic v2 schemas for DCM YAML Data Contract validation.

These models define the structure that YAML contract files must conform to.
The DCM compiler ingests validated contracts and emits deterministic PostgreSQL DDL.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def parse_reference(ref: str) -> tuple[str, str, list[str]]:
    """Parse a 'schema.table(col1, col2)' reference string.

    Returns:
        (schema, table, [columns])
    """
    if "(" not in ref or not ref.endswith(")"):
        raise ValueError(
            f"Reference '{ref}' must be in 'schema.table(col)' format."
        )
    paren_idx = ref.index("(")
    close_idx = ref.rindex(")")
    schema_table = ref[:paren_idx].strip()
    cols_str = ref[paren_idx + 1 : close_idx].strip()

    parts = schema_table.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Reference '{ref}' must be in 'schema.table(col)' format."
        )
    schema, table = parts[0].strip(), parts[1].strip()
    cols = [c.strip() for c in cols_str.split(",") if c.strip()]
    if not cols:
        raise ValueError(f"Reference '{ref}' must name at least one column.")
    return schema, table, cols


def parse_index_column(spec: str, default_desc: bool = False) -> tuple[str, Optional[str]]:
    """Parse an index column entry like 'occurred_at DESC' into (name, direction)."""
    tokens = spec.strip().split()
    if not tokens:
        raise ValueError("Index column entry cannot be empty.")
    if len(tokens) == 2 and tokens[1].upper() in {"ASC", "DESC"}:
        return tokens[0], tokens[1].upper()
    if len(tokens) == 1:
        return tokens[0], "DESC" if default_desc else None
    raise ValueError(
        f"Invalid index column specification '{spec}'. Expected 'col' or 'col ASC|DESC'."
    )


class TriggerSpec(BaseModel):
    """Database trigger specification."""

    name: str = Field(..., description="Trigger name.")
    timing: Literal["BEFORE", "AFTER", "INSTEAD OF"] = Field(
        ..., description="Trigger timing."
    )
    events: list[Literal["INSERT", "UPDATE", "DELETE", "TRUNCATE"]] = Field(
        ..., min_length=1, description="Events that fire the trigger."
    )
    for_each: Literal["ROW", "STATEMENT"] = Field(
        "ROW", description="Trigger granularity."
    )
    function_name: str = Field(..., description="Name of the trigger function.")
    function_body: str = Field(
        ..., description="PL/pgSQL function body (BEGIN...END block)."
    )


class IndexSpec(BaseModel):
    """Table index specification."""

    name: str = Field(..., description="Index name.")
    columns: list[str] = Field(
        ..., min_length=1, description="Indexed columns (optionally suffixed with ASC/DESC)."
    )
    unique: bool = Field(False, description="Whether the index is unique.")
    descending: bool = Field(
        False, description="Default DESC sort order for columns without explicit direction."
    )
    method: Optional[Literal["btree", "gin", "gist", "hnsw", "ivfflat"]] = Field(
        None, description="PostgreSQL index access method (e.g., btree, hnsw, gin)."
    )
    opclass: Optional[str] = Field(
        None, description="Operator class for specialized indexes (e.g., vector_cosine_ops)."
    )
    where: Optional[str] = Field(
        None, description="Partial index WHERE predicate."
    )

    def parsed_columns(self) -> list[tuple[str, Optional[str]]]:
        """Return [(column_name, 'ASC'|'DESC'|None), ...] for each column entry."""
        return [parse_index_column(c, default_desc=self.descending) for c in self.columns]


class ForeignKeySpec(BaseModel):
    """Composite or simple foreign key constraint specification."""

    name: str = Field(..., description="Constraint name.")
    columns: list[str] = Field(
        ..., min_length=1, description="Source columns in this table."
    )
    references: str = Field(
        ...,
        description="Target in 'schema.table(col1, col2)' format.",
    )
    on_delete: Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"] = Field(
        "NO ACTION", description="Referential action on delete."
    )
    on_update: Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"] = Field(
        "NO ACTION", description="Referential action on update."
    )

    @field_validator("references")
    @classmethod
    def validate_reference_format(cls, v: str) -> str:
        parse_reference(v)
        return v


class CheckConstraintSpec(BaseModel):
    """Named CHECK constraint specification."""

    name: str = Field(..., description="Constraint name.")
    expression: str = Field(..., description="SQL CHECK expression.")


class UniqueConstraintSpec(BaseModel):
    """Named UNIQUE constraint specification."""

    name: str = Field(..., description="Constraint name.")
    columns: list[str] = Field(
        ..., min_length=1, description="Columns forming the unique group."
    )


class ColumnSpec(BaseModel):
    """Column specification within a table."""

    name: str = Field(..., description="Column name.")
    type: str = Field(
        ..., description="SQL data type (e.g., UUID, VARCHAR(100), JSONB, VECTOR(1536))."
    )
    nullable: bool = Field(True, description="Whether the column accepts NULL.")
    primary_key: bool = Field(False, description="Whether this column is the primary key.")
    default: Optional[str] = Field(None, description="SQL DEFAULT expression (as string).")
    check: Optional[str] = Field(None, description="Inline CHECK constraint expression.")
    description: Optional[str] = Field(None, description="Human-readable column description.")

    references: Optional[str] = Field(
        None,
        description="Single-column FK in 'schema.table(column)' format.",
    )
    on_delete: Optional[
        Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"]
    ] = Field(None, description="Referential action on delete for simple FK.")
    on_update: Optional[
        Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"]
    ] = Field(None, description="Referential action on update for simple FK.")

    @field_validator("references")
    @classmethod
    def validate_reference_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_reference(v)
        return v


class TableSpec(BaseModel):
    """Full table specification within a data contract."""

    name: str = Field(..., description="Table name.")
    description: Optional[str] = Field(None, description="Table purpose and scope.")
    columns: list[ColumnSpec] = Field(..., min_length=1, description="Column definitions.")

    check_constraints: list[CheckConstraintSpec] = Field(
        default_factory=list, description="Named CHECK constraints."
    )
    unique_constraints: list[UniqueConstraintSpec] = Field(
        default_factory=list, description="Named UNIQUE constraints."
    )
    foreign_keys: list[ForeignKeySpec] = Field(
        default_factory=list, description="Composite or named foreign keys."
    )
    indexes: list[IndexSpec] = Field(
        default_factory=list, description="Table indexes."
    )
    triggers: list[TriggerSpec] = Field(
        default_factory=list, description="Database triggers."
    )

    @field_validator("columns")
    @classmethod
    def validate_single_primary_key(cls, columns: list[ColumnSpec]) -> list[ColumnSpec]:
        pk_cols = [c.name for c in columns if c.primary_key]
        if len(pk_cols) > 1:
            raise ValueError(
                f"Multiple primary key columns found: {pk_cols}. "
                "Use a composite PK via unique_constraints instead."
            )
        return columns


class DataContractSchema(BaseModel):
    """Root schema for a DCM YAML Data Contract file.

    This is the single source of truth for a PostgreSQL schema's structure.
    """

    version: str = Field(..., description="Contract format version.")
    schema_name: str = Field(
        ...,
        alias="schema",
        description="PostgreSQL schema name (e.g., agent_metadata).",
    )
    description: Optional[str] = Field(None, description="Schema purpose and scope.")
    tables: list[TableSpec] = Field(
        ..., min_length=1, description="Table definitions."
    )

    model_config = {"populate_by_name": True}

    @field_validator("schema_name")
    @classmethod
    def validate_schema_name(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError(
                f"Schema name '{v}' is not a valid SQL identifier."
            )
        return v
