# dbt-core — Sandbox Compilation & Artifact Reference

Technical guide for `dbt-core>=1.7.0` execution, sandbox isolation, compiled artifact inspection, and modeling standards in DDLC agents.

---

## 1. Sandbox Isolation (`DBT_SANDBOX_DIR`)

Generated DDLC agents (`dbt_pipeline_builder_agent`) must never compile or test synthesized SQL against production warehouses or live production credentials:

- **Ephemeral Bundle Directories**: Stage every generated `GeneratedFileSet` into an isolated subdirectory under `DBT_SANDBOX_DIR` (`settings.dbt.sandbox_project_dir`), e.g. `<sandbox_dir>/<bundle_id>/`, enforcing `target_path.resolve().is_relative_to(base_bundle_dir)`.
- **Isolated `profiles.yml`**: Point `--profiles-dir` at a dedicated sandbox/dev profile (e.g., local DuckDB or a non-privileged dev schema) matching `settings.dbt.target_profile`.

---

## 2. Deterministic `dbt compile` Subprocess Execution

Run `dbt compile` deterministically inside `compile_dbt_project(project_dir, target_profile)` (`src/tools/dbt_runner.py` invoked by `node_dry_run_stage`):

```python
import subprocess
from pathlib import Path
from typing import Tuple

def compile_dbt_project(
    project_dir: str,
    target_profile: str = "dev",
    timeout_seconds: int = 120,
) -> Tuple[bool, str]:
    """Execute dbt compile in an isolated sandbox directory and capture diagnostics."""
    resolved_dir = Path(project_dir).resolve()
    cmd = [
        "dbt",
        "compile",
        "--project-dir",
        str(resolved_dir),
        "--profiles-dir",
        str(resolved_dir),
        "--target",
        target_profile,
        "--no-write-json",  # omit if manifest.json inspection is required
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    diagnostic = (proc.stderr or proc.stdout).strip()
    return False, diagnostic
```

> [!NOTE]
> Omit `--no-write-json` whenever downstream verification inspects `target/manifest.json` and `target/compiled/` (default in `node_dry_run_stage`). Always bound `subprocess.run` with `timeout` matching stage `safety_valves.timeout_seconds`.

---

## 3. Inspecting `target/manifest.json` & `target/compiled/`

Instead of scraping ANSI-formatted CLI stdout logs, inspect deterministic artifacts emitted by `dbt compile`:

- **`target/manifest.json`**: Authoritative JSON graph containing resolved `nodes` (`model.<proj>.<name>`, `test.<proj>.<name>`), `sources`, `depends_on`, `compiled_path`, and column metadata. Use it to verify DAG completeness and test attachment without live database execution.
- **`target/compiled/`**: Contains fully Jinja-rendered SQL (`{{ ref() }}`, `{{ source() }}`, macros already expanded). In `node_dry_run_stage`, scan `Path(bundle.storage_uri) / "target" / "compiled"` recursively (`rglob("*.sql")`) and pass each compiled SQL file to SQLGlot (`validate_sql_ast(sql_content, dialect=plan.target_dialect)`).

---

## 4. Layer 3 dbt Modeling & Generic Test Standards

Synthesized dbt projects must adhere to canonical analytics engineering conventions (`spec_template.md`, `impl_doc_schema.md`):

1. **Layered Model Prefixes & Materializations**:
   - `models/staging/stg_<source>_<entity>.sql`: 1:1 clean views over `{{ source('<schema>', '<table>') }}`, renaming to `snake_case` and casting types explicitly.
   - `models/intermediate/int_<domain>_<verb>.sql`: ephemeral or table joins.
   - `models/marts/fct_<entity>.sql` & `dim_<entity>.sql`: `table` or `incremental` materializations referencing only `{{ ref('stg_...') }}` / `{{ ref('int_...') }}` (never raw `source()` directly in marts).
2. **Mandatory Generic Tests (`models/schema.yml`)**:
   - Every staging and mart model must attach `unique` and `not_null` tests on primary keys.
   - Foreign keys must declare `relationships` tests (`to: ref('...')`, `field: ...`); categorical columns declare `accepted_values`.
3. **`models/sources.yml`**: Declare `database`, `schema`, `tables`, and `freshness` SLA blocks when specified by contracts.
