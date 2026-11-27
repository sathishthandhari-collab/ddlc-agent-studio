# Instructor — Structured Pydantic Output Reference

Technical guide for `instructor>=1.7.0` with `litellm` and Pydantic v2 (`pydantic>=2.9.0`) in DDLC Agent Studio.

---

## 1. Client Initialization & Contract Enforcement

Every `llm_reasoning` stage compiles its `contracts/<name>.yaml` specification into a Pydantic v2 `BaseModel` (`src/models/contracts_pydantic.py`) and enforces structured extraction via `instructor.from_litellm`:

```python
import instructor
import litellm
from src.models.contracts_pydantic import TransformationPlan

client = instructor.from_litellm(litellm.completion)

plan: TransformationPlan = client.chat.completions.create(
    model=settings.llm.model_tiers.reasoning,
    response_model=TransformationPlan,
    messages=[
        {"role": "system", "content": "Design a clean dbt project structure."},
        {"role": "user", "content": prompt_content},
    ],
    temperature=0.1,
    max_retries=2,
)
```

- When Pydantic validation fails (`ValidationError`), Instructor automatically formats the exact field validation error back into a follow-up prompt up to `max_retries` times before raising.
- Never parse free-form LLM JSON strings with `json.loads()` when an output contract exists — always supply `response_model`.

---

## 2. Pydantic v2 Quality Rules & Validators

Deterministic contract compilation (`builder.md` §3.1, `impl_doc_schema.md` §6.3) maps YAML `quality_rules` onto Pydantic v2 constraints and `@field_validator` hooks:

```python
from typing import List, Literal
from pydantic import BaseModel, Field, field_validator

class StagingModelSpec(BaseModel):
    model_name: str = Field(..., description="Staging model name, e.g. stg_customers")
    source_table: str = Field(..., description="Source table identifier")
    columns: List[str] = Field(default_factory=list)

    @field_validator("model_name")
    @classmethod
    def must_have_stg_prefix(cls, v: str) -> str:
        if not v.startswith("stg_"):
            raise ValueError("Staging model_name must start with 'stg_'")
        return v

class TransformationPlan(BaseModel):
    project_name: str = Field(..., min_length=1)
    target_dialect: Literal["snowflake", "postgres", "bigquery", "databricks"]
    staging_models: List[StagingModelSpec] = Field(default_factory=list)
    total_models_planned: int = Field(..., ge=1, le=100)
```

### Mapping YAML Rules to Pydantic v2
- `allowed_values` -> `Literal[...]`
- `range` (`min`/`max`) -> `Field(..., ge=min, le=max)`
- `not_empty` -> `Field(..., min_length=1)` plus `.strip()` validator
- `regex` -> `Field(..., pattern=r"...")`

---

## 3. `GeneratedFileSet` vs. Claim Check Staging (Decision 27)

An LLM **cannot** write files directly to disk or calculate true cryptographic SHA-256 hashes. Therefore, code-generation stages (`stage_generate.yaml`) must **not** set `output_schema: dbt_artifact_bundle` directly on the Instructor call.

Instead:
1. `stage_generate.yaml` sets `llm_config.output_schema: "GeneratedFileSet"` (an allowed compiler wrapper model):
   ```python
   class GeneratedFile(BaseModel):
       path: str = Field(..., description="Relative file path, e.g. models/staging/stg_customers.sql")
       content: str = Field(..., description="Complete file source code or YAML configuration")

   class GeneratedFileSet(BaseModel):
       files: List[GeneratedFile] = Field(..., description="Set of generated dbt models and YAML files")
   ```
2. The deterministic Skeleton (`node_generate_stage`) validates each `file_entry.path` with `target_path.is_relative_to(base_bundle_dir)`, writes bytes to `DBT_SANDBOX_DIR / bundle_id`, computes the true SHA-256 digest, and emits `DbtArtifactBundle` (`bundle_id`, `storage_uri`, `file_manifest`, `artifact_sha256`, `generated_at`) into `AgentState`.

---

## 4. Schema Design Caveats for Reliable Extraction

- **Explicit Nested Models**: Use explicit named `BaseModel` classes (`TableProfile`, `ColumnProfile`, `StagingModelSpec`, `MartModelSpec`) rather than untyped `Dict[str, Any]` so tool-calling JSON schemas remain unambiguous across Anthropic, OpenAI, and Gemini.
- **Descriptive `Field(description=...)`**: Instructor transmits Pydantic field descriptions directly into the provider's JSON schema; concise descriptions substantially improve first-pass extraction accuracy.
- **Avoid Deeply Recursive or Union-Heavy Models**: Keep nesting depth shallow and prefer discriminated literals over arbitrary `Union` types.
