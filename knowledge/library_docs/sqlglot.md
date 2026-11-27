# SQLGlot — AST Validation & Dialect Transpilation Reference

Technical guide for `sqlglot>=25.0.0` used by the DCM Schema Engine (`src/dcm/compiler.py`) and generated DDLC agents (`src/tools/sqlglot_verifier.py`).

---

## 1. Core AST Parsing & Strict Error Raising

Always pass the explicit target `dialect` and `error_level=sqlglot.ErrorLevel.RAISE` when validating generated SQL or DDL so syntax errors fail deterministically rather than degrading silently into fallback `exp.Command` nodes:

```python
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from typing import Tuple

def validate_sql_strict(sql: str, dialect: str = "snowflake") -> Tuple[bool, str]:
    try:
        nodes = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except ParseError as pe:
        return False, str(pe)

    non_empty = [n for n in nodes if n is not None]
    if not non_empty:
        return False, "Empty SQL statement"
    if any(isinstance(n, exp.Command) for n in non_empty):
        return False, "Unparsed SQL Command fallback detected"
    return True, ""
```

---

## 2. Validating DDL Statements & Data Types (`src/dcm/compiler.py`)

The DCM Schema Engine uses SQLGlot AST inspection to validate compiled PostgreSQL DDL before Alembic runs:

- **Data Type Validation (`exp.DataType.build`)**:
  ```python
  from sqlglot import exp

  # Validates PostgreSQL types including pgvector's vector(1536)
  dtype_ast = exp.DataType.build("TIMESTAMPTZ", dialect="postgres")
  vec_ast = exp.DataType.build("vector(1536)", dialect="postgres")
  ```
- **DDL Node Verification**:
  - Verify `isinstance(ast, (exp.Create, exp.Alter, exp.Drop))` and reject `exp.Block` wrappers when expecting single DDL statements.
  - For `exp.Create` with `kind="TABLE"`, assert `ast.this.expressions` is non-empty.
  - For `exp.Alter`, verify `actions` is non-empty and contains no `exp.Command` unparsed actions.

---

## 3. Preprocessing Uncompiled dbt Jinja SQL

Uncompiled dbt model templates contain Jinja expressions (`{{ ref('stg_orders') }}`, `{{ source('raw', 'customers') }}`, `{{ config(materialized='table') }}`) that trigger false-positive `ParseError`s if passed directly to `sqlglot.parse()`.

Two valid verification modes exist:
1. **Post-Compile Verification (`node_dry_run_stage`)**: Run `dbt compile` first and validate files under `target/compiled/` where Jinja is already expanded.
2. **Pre-Compile AST Verification (`validate_sql_ast` in `builder.md` §5.4)**: Strip `config(...)` blocks and substitute `ref(...)` / `source(...)` with dummy identifiers before parsing:

```python
import re
import sqlglot
from sqlglot.errors import ParseError
from typing import Tuple

def validate_sql_ast(sql_content: str, dialect: str = "snowflake") -> Tuple[bool, str]:
    sanitized_sql = re.sub(
        r"\{\{\s*(?:ref|source)\s*\([^}]+\)\s*\}\}", "__dbt_table__", sql_content
    )
    sanitized_sql = re.sub(r"\{\{\s*config\s*\([^}]+\)\s*\}\}", "", sanitized_sql)
    try:
        sqlglot.parse(sanitized_sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        return True, ""
    except ParseError as pe:
        return False, str(pe)
```

*(Note: Models containing `{% for %}` or complex Jinja control flow must be validated post-`dbt compile` via `target/compiled/`.)*

---

## 4. Warehouse Dialect Caveats (`snowflake`, `postgres`, `bigquery`, `databricks`)

| Dialect | Key Syntax & Type Caveats in SQLGlot |
|---|---|
| **`snowflake`** | Supports `QUALIFY ROW_NUMBER() OVER (...) = 1`, semi-structured `VARIANT`, `TIMESTAMP_NTZ`, and `::` cast shorthand. Unquoted identifiers fold to uppercase. |
| **`postgres`** | Use `TIMESTAMPTZ`, `JSONB`, `INET`, `vector(1536)`. Does **not** support `QUALIFY` natively (requires subquery filtering); FKs require explicit `PRIMARY KEY` or `UNIQUE` constraints (not standalone unique indexes). |
| **`bigquery`** | Uses backtick quoting `` `project.dataset.table` ``, `SAFE_CAST`, `STRUCT` / `ARRAY`, and `DATETIME` vs `TIMESTAMP`. |
| **`databricks`** | Uses Spark SQL / Delta syntax, backtick quoting, `TRY_CAST`, `STRING` instead of `VARCHAR(n)`, and `USING DELTA`. |
