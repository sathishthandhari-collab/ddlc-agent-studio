# Component Guide: `schema_inspector_v1`

Implementation guide for the Tier 2 shared discovery component (`schema_inspector_v1`), used to introspect relational database catalogs into typed `SourceSchemaProfile` contracts.

---

## 1. Component Identity & YAML Declaration

| Attribute | Value |
|---|---|
| **Component ID** | `schema_inspector_v1` (`version: "1.1.0"`) |
| **Component Type** | `connector` (Tier 2 `ddlc_shared_metadata.component_registry`) |
| **Tool Access Level** | `read_only` (`requires_approval: false`) |
| **Callable Entrypoint** | `src/tools/db_inspector.py::inspect_information_schema` |
| **Output Contract** | `source_schema_profile` (`SourceSchemaProfile` Pydantic v2 model) |

### Declaration in `impl_docs/`
In `agent.yaml`:
```yaml
dependencies:
  shared_components:
    - component_id: "schema_inspector_v1"
      version: "1.1.0"
      used_by: [discover]
      usage_type: "import"
      description: "Shared relational database schema discovery tool"
```
In `stages/stage_discover.yaml`:
```yaml
processing:
  processing_type: deterministic_tool
  tools:
    - name: "schema_inspector_v1"
      type: shared_component
      purpose: "Introspect database catalog tables via shared Tier 2 component"
      access: read_only
      configuration:
        include_views: false
        sample_rows_for_stats: 100
```

---

## 2. Python Interface & Output Contract

### Callable Signature
```python
from typing import Any, Dict

def inspect_information_schema(
    connection_uri: str,
    schema: str = "public",
    *,
    include_views: bool = False,
    sample_rows_for_stats: int = 100,
    connect_timeout: int = 15,
) -> Dict[str, Any]:
    """Introspect relational information_schema metadata and return a SourceSchemaProfile dict."""
```

### Stage Node Invocation (`node_discover_stage`)
```python
def node_discover_stage(state: AgentState) -> Dict[str, Any]:
    settings = get_settings()
    db_ref = SourceDatabaseReference.model_validate(
        state["contracts"]["source_database_reference"]
    )
    schema_to_inspect = (
        db_ref.schema_names[0]
        if db_ref.schema_names
        else settings.sources.postgres_oltp.schema_name
    )
    raw_profile = inspect_information_schema(
        connection_uri=settings.sources.postgres_oltp.uri,
        schema=schema_to_inspect,
    )
    profile = SourceSchemaProfile.model_validate(raw_profile)
    return {
        "contracts": {**state["contracts"], "source_schema_profile": profile.model_dump()},
        "reflection_count": 0,
    }
```

### Nested Pydantic Output Models (`contracts_pydantic.py`)
`SourceSchemaProfile` compiles with deterministic nested models (`Decision 31`) so `stage_plan.jinja2` accesses typed fields safely:
- `ColumnProfile`: `name: str`, `data_type: str`, `primary_key: bool = False`
- `TableProfile`: `table_name: str`, `schema_name: str`, `columns: List[ColumnProfile]`
- `SourceSchemaProfile`: `database_name: str`, `tables: List[TableProfile]`, `foreign_keys: List[Dict[str, Any]]`, `profiled_at: datetime` (`datetime.now(timezone.utc)`)

---

## 3. Read-Only Query Semantics & Security

1. **Catalog-Only Parameterized SQL**: Query strictly read-only metadata views (`information_schema.tables`, `information_schema.columns`, `information_schema.table_constraints`, `information_schema.key_column_usage`, `information_schema.referential_constraints`) using `psycopg` parameterized `%(schema)s` bindings — never string-interpolate schema names into SQL.
2. **Table Type Filtering**: Filter `table_type = 'BASE TABLE'` by default; include `'VIEW'` only when `include_views=True`.
3. **Zero Write Authority**: Connect using a read-only database replica/role (`settings.sources.postgres_oltp.uri`); never execute `INSERT`, `UPDATE`, `DELETE`, or DDL.
4. **No Credential Leaks**: `connection_uri` is resolved dynamically from `AgentSettings` (`${config.sources.postgres_oltp.uri}` with `${secrets.source_db_*}`). Never log or persist `connection_uri` inside `SourceSchemaProfile` or exceptions.

---

## 4. Error Handling & Zero-Infra Testing

- **Connection Errors (`on_connection_error`)**: Wrap connection acquisition with `connect_timeout` and `statement_timeout`; stage `error_strategy` retries transient connection failures (`max_retries: 3`, `retry_delay_seconds: 30`) and escalates/fails fast on exhaustion.
- **Missing or Empty Schema (`on_schema_not_found`)**: Raise an explicit `ValueError` when the target schema does not exist or yields zero tables (preventing `PAT-01` `FM-07` vacuous success downstream).
- **Zero-Infra Unit Testing**: Stub `inspect_information_schema` or mock the `psycopg` cursor returning sample `information_schema` rows so unit tests verify `SourceSchemaProfile.model_validate()`, primary key detection, and empty-schema failure paths offline in milliseconds.
