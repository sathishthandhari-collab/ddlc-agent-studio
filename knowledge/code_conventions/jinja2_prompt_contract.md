# Jinja2 Universal Prompt Contract & Security Standards

Standards governing editable prompt templates (`src/prompts/*.jinja2`), static AST verification, XML RAG injection, and safe artifact staging.

---

## 1. The Universal Prompt Context Contract (`PromptContext`)

Per **Decision 28**, the deterministic Skeleton (`src/graph.py`) invokes every `llm_reasoning` stage prompt with an identical **4-key context dictionary**, preventing `UndefinedError` crashes when templates are edited or regenerated:

| Key | Type | Source & Purpose |
|---|---|---|
| `input` | Pydantic `BaseModel` / `dict` | Validated stage input contract (e.g., `SourceSchemaProfile`, `TransformationPlan`). Supports attribute access (`input.tables`). |
| `diagnostics` | `List[str]` | Compiler, `dbt compile`, or SQLGlot AST failure messages from prior reflection loops. Empty on initial pass. |
| `feedback` | `List[str]` | Human reviewer comments captured at review gates (`Reviewer feedback: ...` stripped cleanly). |
| `config` | `Dict[str, Any]` | Non-secret stage and platform settings compiled from `config.yaml` (e.g., `target_dialect`, `project_name`, `sandbox_dir`). |

### Template Naming & `StrictUndefined` Setup

- **Filename Convention**: `src/prompts/stage_<id>.jinja2`, referenced in stage YAML as `processing.llm_config.prompt_template_ref: "prompts/stage_<id>.jinja2"`.
- **Fail-Fast Jinja2 Environment**:
  ```python
  from jinja2 import Environment, FileSystemLoader, StrictUndefined

  jinja_env = Environment(
      loader=FileSystemLoader("src/prompts"),
      autoescape=False,
      undefined=StrictUndefined,
  )
  ```
  `StrictUndefined` guarantees any typo or unprovided top-level variable fails loudly instead of silently rendering blank prompt text.

---

## 2. Canonical Stage Prompt Examples

### Planning Stage (`src/prompts/stage_plan.jinja2`)
```jinja2
You are an expert analytics engineer. Design a clean, modular dbt transformation DAG
for project "{{ config.project_name }}".
Target SQL dialect: {{ config.target_dialect }}.

### Discovered Source Catalog Tables
{% for table in input.tables %}
- Table: {{ table.table_name }} (Schema: {{ table.schema_name }})
  Columns:
  {% for col in table.columns %}
  * {{ col.name }} ({{ col.data_type }}{% if col.primary_key %}, PRIMARY KEY{% endif %})
  {% endfor %}
{% endfor %}

{% if feedback %}
### Human Reviewer Feedback to Incorporate:
{% for comment in feedback %}
- {{ comment }}
{% endfor %}
{% endif %}
```

### Generation Stage (`src/prompts/stage_generate.jinja2`)
Always guard optional reflection and review sections with `{% if diagnostics %}` and `{% if feedback %}`:
```jinja2
Synthesize dbt SQL models and schema.yml tests for project "{{ input.project_name }}".
Target Dialect: {{ config.target_dialect }}.

{% if diagnostics %}
### Previous Compilation / Syntax Errors to Correct (Reflection Feedback):
{% for err in diagnostics %}
- {{ err }}
{% endfor %}
{% endif %}

{% if feedback %}
### Reviewer Guidance to Incorporate:
{% for comment in feedback %}
- {{ comment }}
{% endfor %}
{% endif %}
```

---

## 3. Phase 7 Static AST Verification (`find_undeclared_variables`)

Before an agent passes Phase 7 Validation, the Validator statically parses every template in `src/prompts/*.jinja2` using `jinja2.meta.find_undeclared_variables()`:

```python
import jinja2
import jinja2.meta
from typing import List, Set

ALLOWED_PROMPT_CONTEXT_VARS: Set[str] = {"input", "diagnostics", "feedback", "config"}

def validate_prompt_template_ast(template_path: str, template_source: str) -> List[str]:
    """Validate Jinja2 template AST against the Universal Prompt Contract."""
    env = jinja2.Environment()
    try:
        parsed_ast = env.parse(template_source)
    except jinja2.TemplateSyntaxError as e:
        return [f"Syntax error in '{template_path}' at line {e.lineno}: {e.message}"]

    undeclared_vars = jinja2.meta.find_undeclared_variables(parsed_ast)
    illegal_vars = undeclared_vars - ALLOWED_PROMPT_CONTEXT_VARS
    if illegal_vars:
        return [
            f"Prompt template '{template_path}' references undeclared variables: "
            f"{sorted(illegal_vars)}. Allowed: {sorted(ALLOWED_PROMPT_CONTEXT_VARS)}."
        ]
    return []
```

Any reference outside `{input, diagnostics, feedback, config}` (e.g. `{{ source_profile }}`) is rejected deterministically before deployment or LLM execution.

---

## 4. XML Escaping & Path Traversal Prevention

- **RAG XML Context Escaping (`RAGContextSlice.to_xml()`)**: Retrieved chunks often contain `<`, `>`, `&`, quotes, SQL operators (`<=>`), or Jinja2 tags (`{% ... %}`). Always quote attributes with `xml.sax.saxutils.quoteattr()` and escape body content with `xml.sax.saxutils.escape()`, rendering `<rag_context ... count="0"/>` when empty.
- **Path Traversal Guard for Generated Files (Decision 31)**: When staging LLM-emitted `GeneratedFileSet` entries to disk inside `DBT_SANDBOX_DIR`, strictly enforce canonical path containment:
  ```python
  base_bundle_dir = bundle_path.resolve()
  target_path = (base_bundle_dir / file_entry.path).resolve()
  if not target_path.is_relative_to(base_bundle_dir):
      raise ValueError(f"Path traversal detected in LLM output: {file_entry.path}")
  ```
