# DDLC Meta-Agent — Implementation Document Schema

## The Authoritative Schema for YAML Implementation Documents

> **Document Status**: Draft v0.1  
> **Last Updated**: 2026-11-16  
> **Audience**: Documentarian Specialist (producer), Builder Specialist (consumer), Architecture & Development Team  
> **Prerequisites**:  
> - [`architecture.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/architecture.md) — Sections 4.4, 4.5, 5, 7.2, 7.3  
> - [`approach.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/approach.md) — Section 8  
> - [`dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/dcm-agent/dcm_agent.md) — YAML contract compilation  
> - [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md) — Three-tier metadata

---

## 1. Document Purpose

Implementation documents are the **single source of truth** for every DDLC agent the meta-agent builds. They are the bridge between design and construction — the approved architecture translated into a structured, unambiguous specification that the Builder specialist compiles into a working agent.

### Who Produces Them

The **Documentarian** specialist produces implementation documents during Phase 5 (Documentation) of the meta-agent workflow. The Documentarian's projected context is the approved architecture proposal from the Architect specialist — nothing else. It translates that architecture into YAML documents detailed enough that the Builder can generate code without making design decisions.

### Who Consumes Them

The **Builder** specialist consumes implementation documents during Phase 6 (Construction). The impl docs are the Builder's **entire world** — it does not see the original spec, the Q&A history, the research findings, or the architecture proposal. If something is not in the impl docs, the Builder does not know about it.

The **Validator** specialist also consumes them during Phase 7 (Validation), using them as ground truth to verify the generated code.

### Lifecycle Role: Single Source of Truth Post-Build (Preventing Dual-Spec Drift)

> [!IMPORTANT]
> **Retiring `spec.yaml` Post-Build**  
> While the human-authored `spec.yaml` serves as the initial Day-0 bootstrapping intake document, **it is permanently retired once Gate 3 (Impl Doc Review) is approved and the agent is delivered**.
> 
> The entire `impl_docs/` package (`agent.yaml`, `stages/`, `contracts/`, `config.yaml`, `tests.yaml`) is committed directly into the generated agent's Git repository.
> 
> For all **Day-2 maintenance, bug fixes, and feature enhancements**, `impl_docs/` in Git is the **sole source of truth**. Developers submit changes by:
> - Directly editing `impl_docs/*.yaml` and opening a Git Pull Request (GitOps workflow), OR
> - Asking the Meta-Agent in Chainlit to update `impl_docs/*.yaml`.
> 
> Developers are **never** asked to maintain `spec.yaml` and `impl_docs/` in parallel.

### The Dual-Audience Principle

Every element in an implementation document serves two audiences simultaneously:

| Audience | What They Read | What Makes It Work |
|---|---|---|
| **Humans** (operators, reviewers, auditors) | The `description` fields — plain English explaining intent, context, edge cases, and rationale | A data engineer can read an impl doc and understand what the agent does without seeing any code |
| **The Builder** (machine consumer) | The typed structural fields — names, types, contracts, tools, rules, configuration keys | The Builder reads these fields directly. Given the same impl doc, the Builder always produces the same output. |

This is the same pattern used by OpenAPI specifications, dbt `schema.yml`, Kubernetes manifests, and Terraform configurations. The structure is machine-readable. The descriptions are human-readable. No mixed formats, no parsing ambiguity.

---

## 2. The Abstraction Principle

### Why Implementation Documents Are Runtime-Agnostic

Implementation documents specify **WHAT** each stage does, not **HOW** it does it in code. The Builder decides the HOW based on the runtime the Architect selected.

This abstraction exists because the meta-agent builds AI agents with different architectures and runtimes from the same specification process. One DDLC agent might be a full LangGraph state machine with ReAct tool-calling loops and human approval gates. Another might be a lightweight event-driven or CLI agent powered by Python + LiteLLM. The implementation documents are identical in format regardless of runtime — the Builder's generation strategy is what changes.

### The Same Impl Doc, Two Different Outputs

Consider this stage specification from an impl doc:

```yaml
id: discover_sources
name: "Discover Source Schemas"
type: discovery
description: >
  Introspects source relational database catalogs to extract table schemas,
  column definitions, data types, and primary key constraints.
autonomy: autonomous
input:
  source:
    type: "database"
    uri: "${config.sources.postgres_oltp.uri}"
output:
  contract: source_schema_profile
processing:
  processing_type: deterministic_tool
  tools:
    - name: inspect_information_schema
      purpose: "Extract information_schema metadata via database inspector"
      access: read_only
error_strategy:
  on_connection_error:
    action: retry
    max_retries: 3
    retry_delay_seconds: 30
    escalation: fail_fast
```

The Builder reads this spec and generates different code depending on the runtime:

**LangGraph Runtime** — becomes a node in a state machine storing the contract in `AgentState`:
```python
# Generated: src/stages/discover_sources.py
from typing import Dict, Any
from src.models.contracts_pydantic import SourceSchemaProfile
from src.tools.db_inspector import inspect_information_schema
from src.core.config import settings

def discover_sources(state: AgentState) -> Dict[str, Any]:
    """Introspects source database schema and updates graph state with profile contract."""
    profile_data = inspect_information_schema(
        connection_uri=settings.sources.postgres_oltp.uri,
        target_schema="public"
    )
    # Deterministic contract validation via Pydantic
    profile = SourceSchemaProfile.model_validate(profile_data)
    return {
        "contracts": {
            **state["contracts"],
            "source_schema_profile": profile.model_dump()
        },
        "discovery_status": "completed"
    }
```

**Python + LiteLLM Runtime** — becomes a function in a linear script:
```python
# Generated: src/pipeline.py
from src.models.contracts_pydantic import SourceSchemaProfile
from src.tools.db_inspector import inspect_information_schema

def run_discover_stage(context: PipelineContext) -> SourceSchemaProfile:
    """Executes source schema introspection in linear pipeline runtime."""
    profile_data = inspect_information_schema(
        connection_uri=context.config.source_postgres_uri,
        target_schema="public"
    )
    profile = SourceSchemaProfile.model_validate(profile_data)
    context.contracts["source_schema_profile"] = profile
    return profile
```

The impl doc didn't change. The Builder's generation strategy changed. This is why the Documentarian never needs to know the runtime — it writes the same specification regardless.

---

## 3. Document Structure

An implementation document set for a DDLC agent consists of multiple YAML files organized in a standard directory structure. Each file has a single responsibility.

```
dbt_pipeline_builder_agent/
├── agent.yaml                    # Agent manifest — identity, runtime, architecture
├── stages/                       # One file per agent stage
│   ├── stage_discover.yaml
│   ├── stage_plan.yaml
│   ├── stage_generate.yaml
│   ├── stage_dry_run.yaml
│   ├── stage_review.yaml
│   └── stage_deliver.yaml
├── contracts/                    # One file per agent state/artifact contract
│   ├── source_database_reference.yaml  # Initial database reference input parameters
│   ├── source_schema_profile.yaml      # Discovered database catalog schema
│   ├── transformation_plan.yaml        # Architecture DAG plan
│   ├── dbt_artifact_bundle.yaml        # Synthesized dbt models & YAML Claim Check pointer
│   ├── compilation_report.yaml         # Sandbox test & compiler diagnostics
│   ├── reviewed_artifact_bundle.yaml   # Human-approved artifact pointer
│   └── delivery_manifest.yaml          # Git branch, commit, and Pull Request manifest
├── config.yaml                   # All externalized configuration & tool settings
└── tests.yaml                    # Test cases derived from contracts & quality rules
```

### Why Multiple Files Instead of One

| Concern | Why Separate Files |
|---|---|
| **Cognitive load** | A reviewer can open `stages/stage_validate.yaml` and understand the validation stage without scrolling through 500 lines of other content |
| **Change isolation** | When a contract changes, only its file and referencing stages change. Other documents do not need updating. |
| **Builder efficiency** | The Builder can generate code file-by-file, processing one stage at a time, keeping its context window focused |
| **Cross-document referencing** | Stages reference contracts by name. The graph of references is explicit and auditable. |
| **Version control** | Git diffs show exactly which document changed and why. A contract change is clearly visible. |

### Document Inventory

| Document | Count | Responsibility |
|---|---|---|
| `agent.yaml` | Exactly 1 | Top-level agent identity, runtime, architecture pattern, pipeline topology, and shared component declarations |
| `stages/stage_<id>.yaml` | 1 per stage | Full specification of one pipeline stage — inputs, outputs, tools, error handling, quality gates, and leaf handlers |
| `contracts/<name>.yaml` | 1 per data contract | Field-level schema, quality rules per field, SLA definitions, data classification tags |
| `config.yaml` | Exactly 1 | All externalized configuration, environment overrides, secret references |
| `tests.yaml` | Exactly 1 | Test cases auto-derivable from contracts and quality rules, plus custom integration test cases |

> [!TIP]
> **Blank Starter Templates Available**  
> Clean, unpopulated starter templates for all five documents are located in [`templates/`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates):
> - [`templates/agent.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/agent.yaml)
> - [`templates/stage.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/stage.yaml)
> - [`templates/contract.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/contract.yaml)
> - [`templates/config.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/config.yaml)
> - [`templates/tests.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/tests.yaml)

---

## 4. Complete YAML Schemas

> [!NOTE]
> **Schema Rules vs. Reference Walkthrough Values**  
> In the schemas below, field annotations explain the schema rules (types, requirements, allowed values). To make cross-document references and dependencies tangible and realistic, the example YAML blocks are populated with values from our canonical reference agent: `"dbt_pipeline_builder_agent"` (comparable to the Swagger Petstore in OpenAPI).  
> The comprehensive field tables define the generic schema rules for *any* DDLC agent, and the complete multi-file suite for this reference agent is assembled in [Section 9 (A Complete Example)](#9-a-complete-example).

### 4.1 Agent Manifest — `agent.yaml`

The agent manifest is the entry point for the entire impl doc set. It declares who the agent is, what architecture it uses, what runtime it targets, and what external dependencies it requires.

```yaml
# ============================================================
# agent.yaml — Agent Manifest
# ============================================================
# The top-level identity and architecture declaration for this
# DDLC agent. The Builder reads this file first to determine
# the generation strategy and project structure.
# ============================================================

# --- Agent Identity -------------------------------------------
agent:
  name: "dbt_pipeline_builder_agent"           # Unique agent identifier. Used in file paths, metadata registry, and cross-agent references.
  display_name: "dbt Pipeline Builder Agent"   # Human-readable name for UI and documentation.
  version: "1.0.0"                             # Semantic version. Incremented on every impl doc revision.
  description: >                               # Human-readable purpose statement. NOT read by the Builder for code generation
    Introspects source database catalogs, plans transformation              # — exists purely for human understanding.
    models, generates dbt SQL models and schema YAMLs, dry-runs
    compilation in a sandbox, routes artifacts through human review,
    and opens a Git Pull Request with Tier 2 lineage registration.
  owner: "analytics-engineering-team"          # Team or individual responsible for this agent.
  tags:                                        # Searchable tags for metadata catalog discovery.
    - "dbt"
    - "code_generation"
    - "schema_discovery"
    - "gitops"

# --- Architecture Selection -----------------------------------
architecture:
  pattern: "stateful_pipeline_with_gates"      # Pattern from catalog: stateful_pipeline_with_gates, react_tool_agent, multi_step_planner_agent, lightweight_event_cli_agent, multi_source_lineage_inspector
  runtime: "langgraph"                         # Generation strategy: langgraph, python_litellm
  autonomy_level: "semi_autonomous"            # Autonomy level: autonomous, semi_autonomous, human_required

# --- Stage Ordering -------------------------------------------
# Declares the pipeline topology. The Builder uses this to wire
# stages together in the correct sequence. Each stage_id must
# have a corresponding file in stages/stage_<id>.yaml.
pipeline:
  stages:
    - stage_id: discover                       # References stages/stage_discover.yaml
      next: plan                               # Linear flow: discover → plan
    - stage_id: plan
      next: generate
    - stage_id: generate
      next: dry_run
      on_error: plan                           # Self-correction loop: retry planning on compilation failure
    - stage_id: dry_run
      next: review                             # Advances to human gate upon successful dry-run
      on_error: generate                       # Loop back to code generation on dry-run failure
    - stage_id: review
      transitions:                             # Multi-way human review gate routing
        on_approved: deliver
        on_revise: generate                    # Returns to code generation with reviewer feedback
        on_abort: null                         # Terminates workflow (END)
    - stage_id: deliver
      next: null                               # Terminal stage — opens PR & registers in catalog

# --- Dependencies & Shared Components -------------------------
# External tools, libraries, and shared components this agent
# requires. The Builder uses this to generate import statements,
# requirements.txt entries, and shared component references.
dependencies:
  tools:                                       # External libraries/tools required by stages
    - name: "litellm"
      version: ">=1.35.0"
      used_by: [plan, generate]                # Which stages use this tool
      description: "Unified LLM interface for model calls"
    - name: "instructor"
      version: ">=1.0.0"
      used_by: [plan, generate]
      description: "Structured output validation using Pydantic contracts"
    - name: "dbt-core"
      version: ">=1.7.0"
      used_by: [dry_run]
      description: "dbt engine for compiling and testing generated SQL models"
    - name: "sqlglot"
      version: ">=23.0.0"
      used_by: [generate, dry_run]
      description: "SQL parser and dialect transpiler for syntax verification"
  shared_components:                           # Components from the shared component catalog
    - component_id: "schema_inspector_v1"      # References component_registry in Tier 2 metadata
      version: "1.1.0"
      used_by: [discover]
      usage_type: "import"                     # import (use as-is), extend (subclass), wrap (adapter)
      description: "Shared relational database schema discovery tool"
    - component_id: "git_deliverer_v1"
      version: "1.0.0"
      used_by: [deliver]
      usage_type: "import"
      description: "Shared Git branch creator and PR publisher"

# --- Metadata Integration -------------------------------------
# Declares which metadata framework tiers and tables this agent
# provisions and writes to during execution.
metadata:
  tier_3_schema: "ddlc_dbt_builder_metadata"   # Tier 3 schema name (auto-provisioned by DCM engine)
  tier_3_tables:                               # Agent-specific tables needed
    - "generated_model_manifest"
    - "compilation_diagnostics"
  writes_to:                                   # Tier 1 and 2 tables this agent writes to via callbacks
    tier_1: [agent_run_header, execution_log, artifact_registry, decision_log]
    tier_2: [agent_registry, component_registry, contract_lineage]

# --- Schedule -------------------------------------------------
schedule:
  type: "on_demand"                            # on_demand, event_driven, cron
  description: "Triggered on-demand via Meta-Agent CLI or GitHub Webhook when source schema changes"
```

**How the Builder reads `agent.yaml`:**

| Field | Builder Action |
|---|---|
| `architecture.runtime` | Selects the generation strategy (`langgraph` or `python_litellm`) |
| `architecture.pattern` | Informs project structure, state representation, and tool loops |
| `pipeline.stages` | Generates stage wiring — graph edges & `interrupt()` gates (LangGraph), or sequential step dispatch (Python+LiteLLM) |
| `pipeline.stages[].on_error` | Generates self-correction reflection edges and error routing branches |
| `pipeline.stages[].transitions` | Generates multi-way conditional routing edges (e.g., human review gate approval, revision loops, or termination) |
| `dependencies.tools` | Generates `requirements.txt` / `pyproject.toml` entries |
| `dependencies.shared_components` | Generates import statements or adapter code for shared tools |
| `metadata.tier_3_schema` | Invokes DCM Schema Engine to compile and apply Tier 3 operational migrations |

---

### 4.2 Stage Documents — `stages/stage_<id>.yaml`

Each stage document fully specifies one agent stage. It declares the stage's purpose, its inputs and outputs (as typed contract references), LLM reasoning parameters, agentic tool bindings, safety valves, and error handling strategies.

#### Canonical Pydantic Schema (`StageDocument`)

To guarantee mathematical determinism between the Documentarian, Parser, and Compiler, stage specifications adhere to the following top-level Pydantic schema:

```python
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class StageHeader(BaseModel):
    id: str = Field(..., description="Unique stage identifier matching pipeline.stages[].stage_id")
    name: str = Field(..., description="Human-readable display name for logging and telemetry")
    type: Literal["discovery", "reasoning", "code_generation", "sandbox_testing", "human_review", "delivery", "custom"]
    description: str = Field(..., description="Plain English description of stage responsibility")
    autonomy: Literal["autonomous", "semi_autonomous", "human_required"] = "autonomous"

class StageInput(BaseModel):
    contract: Optional[str] = Field(None, description="Input contract reference in contracts/<name>.yaml")
    source: Optional[Dict[str, Any]] = Field(None, description="Source connection details (e.g. database, catalog)")

class StageOutput(BaseModel):
    contract: Optional[str] = Field(None, description="Output contract reference in contracts/<name>.yaml")
    destination: Optional[Dict[str, Any]] = Field(None, description="Destination details (e.g. git repo, catalog)")

class ToolConfig(BaseModel):
    name: str = Field(..., description="Tool function identifier matching src/tools/ or dependencies")
    type: Literal["builtin", "custom_code", "shared_component"] = Field(
        default="builtin",
        description="Tool implementation category: 'builtin' (standard framework SDK/library), 'custom_code' (synthesized by Leaf Coder), 'shared_component' (Tier 2 catalog component)"
    )
    handler: Optional[str] = Field(None, description="Hook for Leaf Coder custom Python tool: tools/<mod>.py::<func>")
    signature: Optional[str] = Field(None, description="Explicit typed callable signature for Leaf Coder synthesis (e.g. '(sql: str, dialect: str) -> Tuple[bool, str]')")
    purpose: str = Field(..., description="Plain English description of tool role for LLM function calling")
    access: Literal["read_only", "side_effecting"] = "read_only"
    requires_approval: bool = Field(default=False, description="Whether invocation requires human approval gate")
    configuration: Optional[Dict[str, Any]] = Field(default_factory=dict)

class LLMConfig(BaseModel):
    model_tier: Literal["speed", "reasoning", "heavy"] = "reasoning"
    prompt_template_ref: Optional[str] = Field(
        None,
        description="Path to Jinja2 prompt template: prompts/<name>.jinja2. Enforces Universal Prompt Contract: {input, diagnostics, feedback, config}"
    )
    system_prompt: Optional[str] = Field(None, description="Direct system prompt if not using external Jinja2 file")
    output_schema: Optional[str] = Field(
        None,
        description="Target contract name in contracts/<name>.yaml or an allowed built-in compiler wrapper model (e.g. GeneratedFileSet for claim_check artifact staging) enforced via Instructor"
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096)
    cost_cap_usd: Optional[float] = Field(default=None, description="Maximum single-stage LLM spend ceiling")

class SafetyValves(BaseModel):
    max_tool_iterations: Optional[int] = Field(default=5, description="Maximum tool-calling loop iterations before escalation")
    max_reflection_retries: Optional[int] = Field(default=3, description="Maximum self-correction retries on compilation/lint failure")
    timeout_seconds: Optional[int] = Field(default=300, description="Stage timeout ceiling")

class StageProcessing(BaseModel):
    processing_type: Literal["llm_reasoning", "deterministic_tool", "human_gate"] = Field(
        default="deterministic_tool",
        description="Processing paradigm: 'llm_reasoning' (LLM inference with structured output), 'deterministic_tool' (pure Python/tool execution), 'human_gate' (interrupt for human review)"
    )
    llm_config: Optional[LLMConfig] = Field(None, description="LLM reasoning configuration when processing_type is 'llm_reasoning'")
    tools: List[ToolConfig] = Field(default_factory=list, description="Agentic tools bound to this stage")
    safety_valves: Optional[SafetyValves] = Field(default_factory=SafetyValves)
    logic_description: Optional[str] = Field(None, description="Bespoke logic description projected directly into Leaf Coder prompt")

class ErrorScenario(BaseModel):
    action: Literal["retry", "fail_fast", "skip", "escalate", "reflect"]
    max_retries: Optional[int] = 3
    retry_delay_seconds: Optional[int] = 30
    escalation: Optional[Literal["fail_fast", "escalate", "skip"]] = "fail_fast"
    log_level: Optional[str] = "INFO"
    details: Optional[str] = None

class StagePerformance(BaseModel):
    expected_duration_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None

class StageDocument(BaseModel):
    """Canonical model for stages/stage_*.yaml validated before compilation."""
    stage: StageHeader
    input: StageInput
    output: StageOutput
    processing: Optional[StageProcessing] = Field(default_factory=StageProcessing)
    error_strategy: Dict[str, ErrorScenario]
    performance: Optional[StagePerformance] = None
```

#### Stage Specification Fields Reference

| Field | Type | Required | Description |
|---|---|---|---|
| `stage.id` | string | Yes | Unique stage identifier. Must match `pipeline.stages[].stage_id` in `agent.yaml`. |
| `stage.name` | string | Yes | Human-readable stage name for display and logging. |
| `stage.type` | enum | Yes | Stage category informing code template selection: `discovery`, `reasoning`, `code_generation`, `sandbox_testing`, `human_review`, `delivery`, `custom`. |
| `stage.description` | string | Yes | Plain English explanation of stage responsibility and business intent. |
| `stage.autonomy` | enum | Yes | Autonomy level: `autonomous`, `semi_autonomous`, `human_required`. |
| `input.contract` | string | Required* | Reference to input contract in `contracts/`. (*Required for non-initial stages). |
| `input.source` | object | Optional | Source connection details (e.g., database, catalog, API). |
| `output.contract` | string | Required* | Reference to output contract in `contracts/`. (*Required for non-terminal stages). |
| `output.destination` | object | Optional | Destination connection details (e.g., Git repository, catalog). |
| `processing.processing_type` | enum | Yes | Processing mechanism: `llm_reasoning` (LLM inference with structured output), `deterministic_tool` (pure Python/tool execution), `human_gate` (interrupt for human review). |
| `processing.llm_config` | object | Conditional | LLM configuration (`model_tier`, `prompt_template_ref`, `output_schema`, `temperature`, `max_tokens`). Required when `processing_type: llm_reasoning`. Prompt templates strictly adhere to the Universal Prompt Contract (`input`, `diagnostics`, `feedback`, `config`). |
| `processing.tools` | list | Optional | Agentic or verification tools bound to this stage. |
| `processing.tools[].name` | string | Yes | Unique tool function identifier. |
| `processing.tools[].type` | enum | Yes | `builtin` (SDK/standard library), `custom_code` (synthesized by Leaf Coder), `shared_component` (Tier 2 catalog component). |
| `processing.tools[].handler` | string | Conditional | Hook path (`"tools/<mod>.py::<func>"`) dispatching execution to an isolated Python tool synthesized by the **LLM Leaf Coder** when `type: custom_code`. |
| `processing.tools[].signature` | string | Conditional | Explicit typed callable signature (e.g. `(sql_content: str, dialect: str) -> Tuple[bool, str]`) used for Leaf Coder code synthesis and test generation. |
| `processing.tools[].access` | enum | Yes | `read_only` or `side_effecting`. |
| `processing.tools[].requires_approval` | boolean | No | Whether invoking tool triggers human approval gate (default: false). |
| `processing.safety_valves` | object | Optional | Safety limits: `max_tool_iterations`, `max_reflection_retries`, `timeout_seconds`. |
| `processing.logic_description` | string | Conditional | Algorithmic, step-by-step description of transformations and rules. Projected directly into the Leaf Coder's prompt when synthesizing bespoke tools or prompts. |
| `error_strategy` | object | Yes | Handlers for failure scenarios (`action`: retry, fail, skip, escalate, reflect). |
| `performance` | object | Optional | Operational SLA expectations (`expected_duration_seconds`, `timeout_seconds`). |

```yaml
# ============================================================
# stages/stage_discover.yaml — Schema Discovery Stage
# ============================================================
# Fully specifies the discovery stage of the dbt Pipeline
# Builder Agent. The Builder reads this to generate the stage's
# tool binding, contract validation, and execution wrapper.
# ============================================================

stage:
  id: discover
  name: "Discover Source Schemas"
  type: discovery
  description: >
    Connects to the source PostgreSQL OLTP database, inspects the
    specified schema, and produces a structured profile containing
    table definitions, column data types, nullability, and primary keys.
  autonomy: autonomous

input:
  source:
    type: "database"
    uri: "${config.sources.postgres_oltp.uri}"
    schema: "public"

output:
  contract: source_schema_profile               # References contracts/source_schema_profile.yaml

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

error_strategy:
  on_connection_error:
    action: retry
    max_retries: 3
    retry_delay_seconds: 30
    escalation: fail
  on_schema_not_found:
    action: fail
    details: "Target schema does not exist in source database"

performance:
  expected_duration_seconds: 15
  timeout_seconds: 60
```

**A second stage example — LLM reasoning stage planning transformations:**

```yaml
# ============================================================
# stages/stage_plan.yaml — Transformation Planning Stage
# ============================================================

stage:
  id: plan
  name: "Plan dbt Transformation Architecture"
  type: reasoning
  description: >
    Analyzes the discovered source schema profile and corporate dbt
    modeling guidelines to design a staging and marts transformation
    layer. Produces a formal transformation plan declaring model names,
    materializations, dependencies, and business logic intent.
  autonomy: autonomous

input:
  contract: source_schema_profile

output:
  contract: transformation_plan                 # References contracts/transformation_plan.yaml

processing:
  processing_type: llm_reasoning
  llm_config:
    model_tier: "reasoning"                     # High-reasoning model tier (e.g. Claude 3.5 Sonnet / Gemini Pro)
    prompt_template_ref: "prompts/stage_plan.jinja2"
    output_schema: "transformation_plan"        # Instructor validates LLM output against this Pydantic contract
    temperature: 0.1
    max_tokens: 4096
    cost_cap_usd: 0.25
  tools:
    - name: "query_tier2_catalog"
      purpose: "Check existing corporate dbt models and naming conventions in Tier 2 metadata"
      access: read_only
  safety_valves:
    max_tool_iterations: 3
    timeout_seconds: 120

error_strategy:
  on_validation_failure:
    action: retry
    max_retries: 2
    details: "Instructor schema validation failed — retry with error feedback in prompt"
  on_cost_cap_exceeded:
    action: escalate
    details: "LLM token spend exceeded stage budget"

performance:
  expected_duration_seconds: 30
  timeout_seconds: 120
```

**A third stage example — code generation with Leaf Coder verifier hook:**

```yaml
# ============================================================
# stages/stage_generate.yaml — dbt Code Generation Stage
# ============================================================

stage:
  id: generate
  name: "Generate dbt Models and Schema YAML"
  type: code_generation
  description: >
    Synthesizes production-ready dbt SQL models, schema.yml files with
    generic tests (unique, not_null), and sources.yml definitions.
    Emits a complete dbt artifact bundle verified against SQLGlot AST parser.
  autonomy: autonomous

input:
  contract: transformation_plan

output:
  contract: dbt_artifact_bundle                 # References contracts/dbt_artifact_bundle.yaml

processing:
  processing_type: llm_reasoning
  llm_config:
    model_tier: "reasoning"
    prompt_template_ref: "prompts/stage_generate.jinja2"
    output_schema: "GeneratedFileSet"           # Instructor emits structured file models; node stages to disk & emits dbt_artifact_bundle
    temperature: 0.0
    max_tokens: 8192
    cost_cap_usd: 0.50
  tools: []                                     # Code generation emits structured files directly; verification runs in stage_dry_run
  safety_valves:
    max_reflection_retries: 3                   # Self-correction loop: reflection on SQL syntax or sandbox compilation error
    timeout_seconds: 180
  logic_description: >
    Generate modular dbt SQL files for each model in transformation_plan:
    - Staging models: select from source, rename to snake_case, cast types explicitly
    - Mart models: join staging models, compute derived metrics, apply business rules
    - schema.yml: attach unique and not_null tests to primary keys; description for each column
    - sources.yml: declare freshness SLA and database/schema identifiers

error_strategy:
  on_generation_failure:
    action: fail_fast
    details: "Failed to generate valid dbt artifact bundle"

performance:
  expected_duration_seconds: 45
  timeout_seconds: 180
```

---

### 4.3 Contract Documents — `contracts/<name>.yaml`

Contract documents define the exact schema of data, agent state, and engineering artifacts flowing between stages. In Layer 2 DDLC AI agents, contracts define structured intermediate representations (such as schema profiles, transformation plans, generated dbt bundles, and compilation reports), compiling deterministically into Pydantic v2 / Instructor data models.

```yaml
# ============================================================
# contracts/source_schema_profile.yaml — Discovered Source Schema
# ============================================================
# Defines the structured profile of the source database catalog
# extracted by the discover stage and consumed by the plan stage.
# ============================================================

contract:
  name: source_schema_profile
  version: "1.0.0"
  description: >
    Catalog metadata and column-level schemas discovered from the
    source relational database. Consumed by the planning stage.
  direction: internal
  owner: "analytics-engineering-team"
  classification: "internal"
  tags: ["metadata", "discovery", "catalog"]

  fields:
    - name: database_name
      type: string
      nullable: false
      description: "Source database identifier"
      quality_rules:
        - rule: not_empty
          severity: error
          message: "database_name must not be empty"

    - name: schema_name
      type: string
      nullable: false
      description: "Schema namespace introspected (e.g., public, core)"

    - name: tables
      type: array
      nullable: false
      description: "List of discovered table profiles with column schemas, primary keys, and row counts"
      items:
        type: object
        model_name: TableProfile
        fields:
          - name: table_name
            type: string
            nullable: false
            description: "Table name identifier"
          - name: schema_name
            type: string
            nullable: false
            description: "Schema namespace"
          - name: columns
            type: array
            nullable: false
            description: "List of columns discovered in table"
            items:
              type: object
              model_name: ColumnProfile
              fields:
                - name: name
                  type: string
                  nullable: false
                  description: "Column identifier"
                - name: data_type
                  type: string
                  nullable: false
                  description: "SQL data type"
                - name: primary_key
                  type: boolean
                  nullable: false
                  default: false
                  description: "Whether column is part of primary key"

    - name: discovery_timestamp
      type: timestamp
      nullable: false
      default: "${now}"
      description: "Timestamp when schema discovery was executed"

  sla:
    latency:
      max_processing_seconds: 60
      description: "Schema introspection SLA"
```

**A second contract — the transformation plan emitted by LLM reasoning:**

```yaml
# ============================================================
# contracts/transformation_plan.yaml — dbt Architecture Plan
# ============================================================
# Defines the transformation architecture designed by the LLM
# planner. Emitted by the plan stage and consumed by generate.
# ============================================================

contract:
  name: transformation_plan
  version: "1.0.0"
  description: >
    Structured dbt architecture plan specifying staging and marts models,
    dependencies, materializations, column transformations, and tests.
  direction: internal
  owner: "analytics-engineering-team"
  classification: "internal"
  tags: ["dbt", "architecture", "plan"]

  fields:
    - name: project_name
      type: string
      nullable: false
      description: "Target dbt project identifier"

    - name: target_dialect
      type: string
      nullable: false
      description: "Target SQL dialect (e.g., snowflake, postgres, bigquery)"
      quality_rules:
        - rule: allowed_values
          values: ["snowflake", "postgres", "bigquery", "databricks"]
          severity: error

    - name: staging_models
      type: array
      nullable: false
      description: "List of staging models mapping source tables with 1:1 clean views"
      items:
        type: object
        model_name: "StagingModelSpec"
        description: "Specification for a dbt staging model"
        fields:
          - name: model_name
            type: string
            nullable: false
            description: "Staging model identifier (e.g., stg_customers)"
          - name: source_table
            type: string
            nullable: false
            description: "Source table identifier"
          - name: columns
            type: array
            nullable: false
            items:
              type: string
            description: "Projected column list"

    - name: marts_models
      type: array
      nullable: false
      description: "List of dimensional/fact mart models joining staging sources"
      items:
        type: object
        model_name: "MartModelSpec"
        description: "Specification for a dbt dimensional or fact mart model"
        fields:
          - name: model_name
            type: string
            nullable: false
            description: "Mart model identifier (e.g., fct_orders)"
          - name: materialization
            type: string
            nullable: false
            default: "table"
            description: "dbt materialization strategy"
            quality_rules:
              - rule: allowed_values
                values: ["table", "view", "incremental"]
                severity: error
          - name: description
            type: string
            nullable: false
            description: "Business documentation for the mart model"
          - name: staging_dependencies
            type: array
            nullable: false
            items:
              type: string
            description: "Upstream staging models referenced"

    - name: total_models_planned
      type: integer
      nullable: false
      description: "Count of models planned for synthesis"
      quality_rules:
        - rule: range
          min: 1
          max: 100
          severity: error

  sla:
    latency:
      max_processing_seconds: 120
      description: "Planning reasoning SLA"
```

**A third contract — synthesized dbt artifact bundle (Claim Check pattern):**

```yaml
# ============================================================
# contracts/dbt_artifact_bundle.yaml — Synthesized Artifacts
# ============================================================
# Carries Claim Check pointers to the generated code files
# (SQL models, schema.yml, sources.yml) stored in the workspace.
# ============================================================

contract:
  name: dbt_artifact_bundle
  version: "1.0.0"
  description: >
    Complete set of synthesized dbt artifacts including SQL model files,
    schema documentation, test assertions, and source configs.
  direction: internal
  storage_mode: claim_check                     # inline | claim_check (externalize large multi-file bundles)
  owner: "analytics-engineering-team"
  classification: "internal"
  tags: ["dbt", "code_artifacts"]

  fields:
    - name: bundle_id
      type: string
      nullable: false
      description: "Unique UUID identifying this artifact release bundle"

    - name: storage_uri
      type: string
      nullable: false
      description: "Filesystem path or cloud storage URI where generated dbt files are staged"

    - name: file_manifest
      type: array
      nullable: false
      items:
        type: string
      description: "List of relative file paths in the bundle (e.g., models/staging/stg_customers.sql)"

    - name: artifact_sha256
      type: string
      nullable: false
      description: "Cryptographic SHA-256 hash of the artifact bundle for tamper-proof verification"
      quality_rules:
        - rule: regex
          pattern: "^[a-f0-9]{64}$"
          severity: error

    - name: generated_at
      type: timestamp
      nullable: false
      default: "${now}"
      description: "Generation timestamp"
```

---

### 4.4 Configuration Document — `config.yaml`

The configuration document declares every externalized value the agent needs. Nothing is hardcoded in stage specifications or contracts — all values reference entries in this file via `${config.*}` syntax. Environment-specific overrides allow the same impl doc set to deploy across dev, staging, and production.

```yaml
# ============================================================
# config.yaml — Externalized Configuration
# ============================================================
# Every configurable value the agent uses at runtime.
# Stage specs reference these via ${config.<path>} syntax.
# Secret values reference ${secrets.<name>} and are resolved
# from a secrets manager at runtime — never stored here.
# ============================================================

config:
  defaults:
    sources:
      postgres_oltp:
        uri: "postgresql://${secrets.source_db_user}:${secrets.source_db_password}@${secrets.source_db_host}:5432/app_production"
        schema: "public"

    destinations:
      git:
        repository_url: "https://github.com/org/analytics-dbt-core.git"
        target_branch: "feature/automated-source-onboarding"
        base_branch: "main"

    dbt:
      sandbox_project_dir: "/tmp/dbt_sandbox"
      target_profile: "dev"

    llm:
      default_provider: "anthropic"
      model_tiers:
        speed: "claude-3-haiku-20240307"
        reasoning: "claude-3-5-sonnet-20241022"
        heavy: "claude-3-opus-20240229"

    alerts:
      channel: "${secrets.slack_webhook_url}"

    logging:
      level: "INFO"
      format: "json"

    performance:
      global_timeout_seconds: 900              # 15-minute global timeout for complete build agent run

  environments:
    development:
      sources:
        postgres_oltp:
          uri: "postgresql://localhost:5432/app_dev"
      dbt:
        sandbox_project_dir: "./sandbox/dbt_dev"
      logging:
        level: "DEBUG"
      performance:
        global_timeout_seconds: 300

    staging:
      sources:
        postgres_oltp:
          uri: "postgresql://${secrets.source_db_user}:${secrets.source_db_password}@staging-db.internal:5432/app_staging"

    production:
      # Uses defaults

  secrets:
    - name: source_db_user
      description: "Postgres OLTP database read-only username"
      required: true
    - name: source_db_password
      description: "Postgres OLTP database password"
      required: true
    - name: source_db_host
      description: "Postgres OLTP database hostname"
      required: true
    - name: github_token
      description: "GitHub personal access token for opening PRs and pushing branches"
      required: true
    - name: anthropic_api_key
      description: "API key for LiteLLM Anthropic provider"
      required: true
    - name: slack_webhook_url
      description: "Slack webhook URL for notifications and human gate alerts"
      required: false
```

---

### 4.5 Test Document — `tests.yaml`

The test document declares test cases that the Builder generates into executable test suites. Tests are derived from contracts (schema validation, quality rules) and stage integration scenarios.

```yaml
# ============================================================
# tests.yaml — Test Specifications
# ============================================================
# Test cases the Builder generates into executable test suites.
# Organized by source: contract-derived and custom integration tests.
# ============================================================

tests:
  contract_tests:
    - contract: source_schema_profile
      description: "Tests derived from source_schema_profile contract"
      cases:
        - test_id: "profile_schema_valid"
          type: schema_validation
          description: "Verify all required catalog fields are present"
          auto_generate: true

    - contract: transformation_plan
      description: "Tests derived from transformation_plan contract"
      cases:
        - test_id: "plan_has_valid_dialect"
          type: field_quality
          description: "Verify dialect is one of supported platforms"
          field: target_dialect
          rule: allowed_values
          auto_generate: true

        - test_id: "plan_model_count_bounds"
          type: field_quality
          description: "Verify planned model count is within bounds"
          field: total_models_planned
          rule: range
          auto_generate: true

  custom_tests:
    - test_id: "dbt_compile_sandbox_dry_run"
      type: integration
      description: "Verify generated dbt artifact bundle compiles with zero syntax errors via dbt compile"
      stage: dry_run
      setup: "Mount generated dbt_artifact_bundle into sandbox directory with mocked profiles.yml"
      expected_behavior: "dbt compile returns exit code 0 and emits manifest.json"

    - test_id: "sqlglot_ast_lint_pass"
      type: integration
      description: "Verify all generated SQL model files pass SQLGlot dialect validation"
      stage: generate
      setup: "Feed generated SQL strings to SQLGlot parser"
      expected_behavior: "Parser emits valid AST without ParseError"

    - test_id: "human_review_gate_interruption"
      type: end_to_end
      description: "Verify state machine halts at review stage before executing deliver"
      stage: review
      setup: "Trigger agent run with valid source profile"
      expected_behavior: "State machine enters LangGraph interrupt() state and awaits human approval"
```

---

## 5. Cross-Document References

Implementation documents reference each other through a system of named identifiers. These references create an explicit dependency graph that the Documentarian must keep consistent and the Builder must resolve.

### Reference Types

| From | To | Reference Field | Example |
|---|---|---|---|
| `agent.yaml` → Stage | `stages/stage_<id>.yaml` | `pipeline.stages[].stage_id` | `stage_id: plan` → file `stages/stage_plan.yaml` |
| `agent.yaml` → Component | Tier 2 `component_registry` | `dependencies.shared_components[].component_id` | `component_id: schema_inspector_v1` |
| Stage → Contract | `contracts/<name>.yaml` | `input.contract`, `output.contract` | `contract: source_schema_profile` → file `contracts/source_schema_profile.yaml` |
| Stage → Tool | `agent.yaml` `dependencies.tools` | `tools[].name` | `name: litellm` |
| Stage → Config | `config.yaml` | `${config.<path>}` syntax | `${config.sources.postgres_oltp.uri}` |
| Stage → Secret | `config.yaml` `secrets` | `${secrets.<name>}` syntax | `${secrets.source_db_password}` |
| Tests → Contract | `contracts/<name>.yaml` | `contract_tests[].contract` | `contract: transformation_plan` |
| Tests → Stage | `stages/stage_<id>.yaml` | `custom_tests[].stage` | `stage: dry_run` |

### Consistency Rules

The Documentarian **must** maintain these invariants. The Builder **assumes** they hold.

1. **Every `stage_id` in `agent.yaml`'s pipeline must have a corresponding stage file.** If `agent.yaml` lists `stage_id: plan`, then `stages/stage_plan.yaml` must exist.

2. **Every contract referenced by a stage must have a corresponding contract file.** If a stage declares `input.contract: source_schema_profile`, then `contracts/source_schema_profile.yaml` must exist.

3. **Every `${config.*}` reference in any document must resolve to a path in `config.yaml`.** The Builder does not invent configuration values.

4. **Every `${secrets.*}` reference must appear in `config.yaml`'s `secrets` list.** This documents the required secrets for operators.

5. **Pipeline `next` references must point to valid stage IDs or `null`.** No broken links in the execution chain.

6. **The `on_error` route in `agent.yaml` or stage error strategies must reference a valid stage ID or self-correction reflection step.** Error routing paths must be resolvable.

---

## 6. Contract Specification Detail

Data contracts are the core abstraction of the implementation document system. They serve as the single source of truth for agent state schemas and generated engineering artifacts flowing between stages.

### 6.1 Contract Header & Storage Modes

Contracts support two execution storage modes:
- `storage_mode: inline` (default): Payloads and data structures flow directly within memory and are serialized into the LangGraph `AgentState`.
- `storage_mode: claim_check`: Large code repositories, generated files, or multi-megabyte artifact archives are stored externally (on local filesystem or object storage). The contract defines Claim Check pointer attributes (`bundle_id`, `storage_uri`, `file_manifest`, `artifact_sha256`), ensuring PostgreSQL checkpointer snapshots remain sub-millisecond and tamper-evident.

### 6.2 Field-Level Specification & Recursive Nesting

Every field in an agent contract requires these properties:

| Property | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Field identifier. Must be a valid identifier (no spaces, special characters). |
| `type` | enum | Yes | Logical data type: `string`, `integer`, `float`, `boolean`, `timestamp`, `array`, `object`. Compiles directly to Pydantic v2 / Instructor types. |
| `nullable` | boolean | Yes | Whether null/missing values are permitted. Drives `Optional[...]` in Pydantic models. |
| `description` | string | Yes | Human-readable explanation of the field's meaning and purpose in agent reasoning. |
| `items` | object | Conditional | **Required when `type: array`**. For scalar arrays, specify scalar type (e.g. `items: { type: string }`). For nested object arrays, specify `type: object`, `model_name` (the nested Pydantic class to compile), and recursive `fields[]`. |
| `fields` | list | Conditional | **Required when `type: object`**. List of child field specifications compiling into a nested Pydantic model (`model_name`). |
| `default` | any | No | Default value. Use `${now}` for current timestamp, `${today}` for current date. |
| `quality_rules` | list | No | Per-field validation rules enforced by Pydantic validators and Instructor retries. |

### Logical Type Mapping to Python Pydantic

The Builder maps contract logical types to typed Python constructs:

| Logical Type | Python / Pydantic v2 Type | Usage in Agent Reasoning |
|---|---|---|
| `string` | `str` | Text attributes, model names, SQL strings, JSON strings |
| `integer` | `int` | Counts, retry counters, latency measurements, row estimates |
| `float` | `float` | Quality scores, cost tracking, probabilities |
| `boolean` | `bool` | Status flags, approval indicators |
| `timestamp` | `datetime.datetime` | Generation timestamps, run benchmarks |
| `array` (scalar) | `List[T]` | Collections of strings, integers, or paths |
| `array` (nested) | `List[NestedModel]` | Collections of compiled child models (`items.model_name`) |
| `object` | Nested `BaseModel` | Structured child specifications (`model_name`, `fields[]`) |

### 6.3 Quality Rules

Quality rules are specified per-field and define the validation logic the Builder generates.

| Rule Type | Parameters | What the Builder Generates |
|---|---|---|
| `regex` | `pattern`, `severity`, `message` | Regex match validation. Error = fail. Warning = log. |
| `not_empty` | `severity`, `message` | Check that string value is not empty (after trimming). |
| `uniqueness` | `scope` (batch/global), `severity`, `message` | Dedup check. Batch = within current batch. Global = across all historical data. |
| `allowed_values` | `values[]`, `severity`, `message` | Enum validation — value must be one of the listed options. |
| `range` | `min`, `max`, `severity`, `message` | Numeric or date range check. Supports `${today}` for dynamic bounds. |
| `freshness` | `max_age_hours`, `severity`, `message` | Timestamp freshness check — record age must be within threshold. |
| `referential_integrity` | `reference_contract`, `reference_field`, `severity`, `message` | FK check — value must exist in the referenced contract's field. |
| `custom` | `expression`, `severity`, `message` | Custom validation expression (Python-evaluable). Use sparingly. |

### 6.4 Aggregate Quality Rules

Aggregate rules apply to complete contract payloads or artifact collections:

| Rule Type | Parameters | Description |
|---|---|---|
| `completeness` | `field`, `threshold`, `severity` | Percentage of non-null values for a field must exceed threshold |
| `cardinality` | `min_items`, `max_items`, `severity` | Total count of models, tables, or files must fall within expected range |
| `distribution` | `field`, `expected_distribution`, `tolerance`, `severity` | Value distribution should roughly match expectations (future) |

### 6.5 SLA Definitions

SLAs declare operational expectations. The Builder generates monitoring and alerting code from these.

| SLA Type | Parameters | What the Builder Generates |
|---|---|---|
| `freshness` | `max_age_hours` | Data age check at pipeline completion. Alert if stale. |
| `availability` | `uptime_percent` | Monitoring target for the pipeline schedule. Dashboards. |
| `latency` | `max_processing_seconds` | Pipeline duration monitoring. Alert if exceeds. |

### 6.6 How the DCM Schema Engine Compiles Contracts

When a contract is used as a database destination (i.e., a stage writes to a database table), the Builder invokes the DCM Schema Engine to compile the contract into PostgreSQL DDL. The flow:

1. **Builder reads** the contract's field definitions
2. **Builder generates** a Tier 3 YAML data contract in DCM format (mapping logical types to PostgreSQL types)
3. **DCM Schema Engine validates** the contract (Pydantic validation of YAML structure)
4. **SQLGlot compiles** the contract into a DDL AST (CREATE TABLE, indexes, constraints)
5. **Alembic diffs** the compiled schema against the live database
6. **Alembic applies** the migration (with human gates for destructive changes)

The impl doc contract and the DCM YAML contract are deliberately similar in structure. The Builder's job is to translate from the impl doc's logical types to the DCM's PostgreSQL-specific types — a mechanical mapping, not a design decision.

---

## 7. Enterprise Governance & Policy Alignment

In modern enterprise data platforms, individual pipeline agents do not invent ad-hoc Python masking routines or regex heuristic scanners. Instead, DDLC agents are **policy consumers** of central enterprise governance (Collibra, Immuta, Snowflake DDM, Databricks Unity Catalog).

Governance is enforced through three clean, native platform mechanisms:

### 7.1 Contract Classification & Column Tagging

Data sensitivity is declared directly on the data contracts (`contracts/*.yaml`):
- **Contract Level**: `classification: public | internal | confidential | restricted`
- **Field Level**: `pii: true`, `pii_category: email_address | phone_number | ssn`, `tags: ["gdpr", "financial"]`

When the DCM Schema Engine compiles YAML contracts into physical database DDL, it manages PostgreSQL metadata framework schemas and staging tables. For analytical warehouse destinations (Snowflake, Databricks), the Builder's warehouse connector strategy translates these contract attributes into native database column tags (e.g. Snowflake `WITH TAG (governance.pii = 'email')` or Databricks Unity Catalog tags) during DDL compilation and deployment. This allows central enterprise governance policies to govern data access dynamically without brittle, ad-hoc Python masking inside pipeline code.

### 7.2 Quality Thresholds & Stage Gates

Operational data quality and engineering integrity are enforced deterministically at the pipeline boundaries:
- **Engineering Contract & Model Assertions**: Agent payload contracts are validated deterministically via Instructor / Pydantic models at stage boundaries. For generated Layer 3 data models, assertions are compiled into native dbt generic tests (`unique`, `not_null`, `relationships`, `accepted_values`) verified during the sandbox dry run.
- **Stage Error Routing**: Declared in `stages/*.yaml` via `error_strategy: retry | reflect | fail_fast | escalate`. If a tool execution, AST check, or sandbox compilation fails, diagnostic traces trigger reflection loops or escalate to human review.
- **Human Approval Gates**: Declared in `agent.yaml` or `stages/*.yaml` (`autonomy: human_required`). Implements LangGraph `interrupt()` nodes with Claim Check pointers (`PAT-01`), requiring explicit human sign-off before side-effecting actions (e.g., delivering Git Pull Requests or applying production migrations).

### 7.3 Automated Auditability via Metadata Callbacks

Every pipeline execution, stage transition, data quality summary, and human gate decision is captured automatically and immutably by the Metadata Framework (`01_metadata_framework.md`) via non-invasive LangGraph lifecycle callbacks. Application logic remains 100% clean of manual audit logging calls.

---

## 8. How the Builder Reads Impl Docs

The Builder specialist receives the approved implementation documents as its **entire projected context**. It does not see the original spec, clarification Q&A, research findings, or architecture proposals. The impl docs are the Builder's world.

### 8.1 Builder Processing Order

1. **Read `agent.yaml`** — determine the runtime and generation strategy
2. **Read `config.yaml`** — understand the configuration surface (for generating config readers)
3. **Read all contracts** — generate data models (Pydantic, DDL) and validation code
4. **Read each stage in pipeline order** — generate stage code, wiring, error handling
5. **Read `tests.yaml`** — generate test suite
6. **Generate project scaffolding** — directory structure, entry point, requirements

### 8.2 Generation Strategy Mapping

The Builder selects a generation strategy based on `architecture.runtime` and maps impl doc concepts to runtime-specific constructs:

| Impl Doc Concept | LangGraph Strategy (`langgraph`) | Python + LiteLLM Strategy (`python_litellm`) |
|---|---|---|
| **Stage** | A node function registered in the `StateGraph` | A function called in sequential pipeline execution |
| **Pipeline wiring** (`next`, `on_error`) | Graph edges, conditional routing, and self-correction reflection loops | Sequential function calls with error handling & retry |
| **Contract** | Typed Instructor Pydantic model validating node I/O and structured LLM reasoning | Pydantic model for function parameter and return validation |
| **Tool Invocation** | Agentic `@tool` functions bound to nodes or tool-calling loops (`ToolNode`) | Direct Python function or SDK execution |
| **Human Gate** | LangGraph `interrupt()` node with state freeze and Chainlit UI review form | Blocking CLI prompt, webhook trigger, or approval callback |
| **Configuration** | Pydantic `BaseSettings` reading environment variables with secrets resolution | Pydantic `BaseSettings` instance |
| **Test** | pytest suite testing StateGraph node transitions, mocks, and reflection loops | pytest suite testing function I/O and mocked tool responses |

### 8.3 Shared Component Resolution

When a stage declares a tool with `type: shared_component` in `processing.tools[]` (e.g. `name: schema_inspector_v1`), the Builder:

1. Looks up the component in `agent.yaml`'s `dependencies.shared_components` matching `component_id == tool.name`
2. Resolves its `usage_type` declared in `agent.yaml` — `import`, `extend`, or `wrap` (`usage_type` is configured exclusively in `agent.yaml` to prevent conflicting stage-level overrides)
3. If `import`: generates an import statement for the shared component from the shared folder
4. If `extend`: generates a subclass that extends the component with agent-specific behavior
5. If `wrap`: generates an adapter class that wraps the component's interface

The Builder does **not** copy shared component code into the agent's directory. It always generates references to the shared folder.

### 8.4 Test Suite Generation from Contracts

For every contract, the Builder generates:

1. **Schema tests** — verify all required fields exist with correct types in Pydantic models
2. **Field validation tests** — one test per `quality_rules` entry per field
3. **Synthetic artifact generators** — test fixtures that produce mock artifacts matching the contract's schema

For agent integration and gate tests, the Builder generates:

1. **Gate enforcement tests** — verify that the state machine halts execution at human review gates before side-effecting actions
2. **Metadata telemetry tests** — verify that lifecycle callbacks record token usage, execution spans, and decision records to Tier 1 tables
3. **Reflection loop tests** — verify that lint or compilation failures trigger self-correction reflection loops up to safety valve ceilings

### 8.5 Bespoke Logic Generation via Leaf Coder

When processing stages in pipeline order:
1. **Hook Detection**: The Builder inspects each stage's `processing.tools[]` for entries declaring `type: custom_code` and `handler: "<path>::<func>"` (e.g., `"tools/sqlglot_verifier.py::validate_sql_ast"`).
2. **Deterministic Skeleton Generation**: The Deterministic Scaffold Compiler generates the StateGraph DAG, LiteLLM client config, Instructor models, and imports the tool handler hook.
3. **State Projection**: The Builder extracts a minimal, laser-focused state projection containing ONLY:
   - Tool callable signature (`signature`) and hook path (`handler`)
   - Input contract Pydantic schema (`input.contract`)
   - Output contract Pydantic schema (`output.contract`)
   - Stage logic description (`processing.logic_description`)
   - Declared tools/libraries (`processing.tools`)
4. **Leaf Coder Synthesis**: The **LLM Leaf Coder** receives this narrow context and synthesizes the isolated, version-controlled tool or verifier (`src/tools/sqlglot_verifier.py`) and corresponding unit tests (`tests/unit/test_sqlglot_verifier.py`).
5. **Decoupled Evolution**: Orchestration skeleton and contracts remain strictly immutable (`# DO NOT EDIT`), while custom imperative logic in `src/tools/` and `src/prompts/` is fully maintainable and editable by human engineers.

---

## 9. A Complete Example

The following is a complete implementation document set for the **dbt Pipeline Builder Agent**. All files are shown in full. This is the exact set of documents the Documentarian would produce and the Builder would consume.

> [!NOTE]
> The complete schemas for all documents in this example have already been provided inline in Section 4 above. This section confirms the full file list and summarizes the inter-document references for the dbt Pipeline Builder Agent, rather than duplicating the YAML. Refer to:
> - [Section 4.1](#41-agent-manifest--agentyaml) for `agent.yaml`
> - [Section 4.2](#42-stage-documents--stagesstage_idyaml) for `stages/stage_discover.yaml`, `stages/stage_plan.yaml`, `stages/stage_generate.yaml`
> - [Section 4.3](#43-contract-documents--contractsnameyaml) for `contracts/source_schema_profile.yaml`, `contracts/transformation_plan.yaml`, `contracts/dbt_artifact_bundle.yaml`
> - [Section 4.4](#44-configuration-document--configyaml) for `config.yaml`
> - [Section 4.5](#45-test-document--testsyaml) for `tests.yaml`

### Complete File List

```
dbt_pipeline_builder_agent/
├── agent.yaml                          # Agent manifest (Section 4.1)
├── stages/
│   ├── stage_discover.yaml             # Discovery stage (Section 4.2 — first example)
│   ├── stage_plan.yaml                 # Transformation planning stage (Section 4.2 — second example)
│   ├── stage_generate.yaml             # Code generation stage with verifier hook (Section 4.2 — third example)
│   ├── stage_dry_run.yaml              # Sandbox compilation & test execution stage
│   ├── stage_review.yaml               # Human review gate stage (interrupt)
│   └── stage_deliver.yaml              # Git PR delivery & Tier 2 catalog registration
├── contracts/
│   ├── source_database_reference.yaml  # Initial catalog inspection parameters
│   ├── source_schema_profile.yaml      # Discovered source catalog schema (Section 4.3 — first example)
│   ├── transformation_plan.yaml        # Transformation architecture plan (Section 4.3 — second example)
│   ├── dbt_artifact_bundle.yaml        # Synthesized dbt bundle (Section 4.3 — third example)
│   ├── compilation_report.yaml         # Sandbox compilation diagnostics
│   ├── reviewed_artifact_bundle.yaml   # Human-signed artifact bundle
│   └── delivery_manifest.yaml          # Git delivery and PR publication manifest
├── config.yaml                         # Configuration (Section 4.4)
└── tests.yaml                          # Test specifications (Section 4.5)
```

### Cross-Reference Integrity Map

```
agent.yaml
  ├── pipeline.stages[0].stage_id: discover ────────── stages/stage_discover.yaml ✓
  ├── pipeline.stages[1].stage_id: plan ────────────── stages/stage_plan.yaml ✓
  ├── pipeline.stages[2].stage_id: generate ────────── stages/stage_generate.yaml ✓
  ├── pipeline.stages[3].stage_id: dry_run ─────────── stages/stage_dry_run.yaml ✓
  ├── pipeline.stages[4].stage_id: review ──────────── stages/stage_review.yaml ✓
  ├── pipeline.stages[5].stage_id: deliver ─────────── stages/stage_deliver.yaml ✓
  ├── dependencies.tools[0]: litellm ───────────────── used_by: [plan, generate] ✓
  ├── dependencies.tools[1]: instructor ────────────── used_by: [plan, generate] ✓
  ├── dependencies.tools[2]: dbt-core ──────────────── used_by: [dry_run] ✓
  ├── dependencies.tools[3]: sqlglot ───────────────── used_by: [generate, dry_run] ✓
  ├── dependencies.shared_components[0]: schema_inspector_v1 used_by: [discover] ✓
  └── dependencies.shared_components[1]: git_deliverer_v1    used_by: [deliver] ✓

stages/stage_discover.yaml
  ├── input.source.uri: ${config.sources.postgres_oltp.uri} ── config.yaml ✓
  └── output.contract: source_schema_profile ───────── contracts/source_schema_profile.yaml ✓

stages/stage_plan.yaml
  ├── input.contract: source_schema_profile ────────── contracts/source_schema_profile.yaml ✓
  └── output.contract: transformation_plan ─────────── contracts/transformation_plan.yaml ✓

stages/stage_generate.yaml
  ├── input.contract: transformation_plan ──────────── contracts/transformation_plan.yaml ✓
  ├── output.contract: dbt_artifact_bundle ─────────── contracts/dbt_artifact_bundle.yaml ✓
  └── processing.tools[0].handler: tools/sqlglot_verifier.py::validate_sql_ast ✓

stages/stage_dry_run.yaml
  ├── input.contract: dbt_artifact_bundle ─────────── contracts/dbt_artifact_bundle.yaml ✓
  └── output.contract: compilation_report ─────────── contracts/compilation_report.yaml ✓

stages/stage_review.yaml
  ├── input.contract: compilation_report ──────────── contracts/compilation_report.yaml ✓
  └── output.contract: reviewed_artifact_bundle ───── contracts/reviewed_artifact_bundle.yaml ✓

stages/stage_deliver.yaml
  ├── input.contract: reviewed_artifact_bundle ────── contracts/reviewed_artifact_bundle.yaml ✓
  └── output.contract: delivery_manifest ──────────── contracts/delivery_manifest.yaml ✓
```

---

## 10. Validation Rules

An implementation document set must satisfy these rules before the Builder processes it. The Documentarian is responsible for producing valid docs. The meta-agent's validation layer checks these rules before passing docs to the Builder.

### 10.1 Schema Validation

| Rule | Check |
|---|---|
| `agent.yaml` must exist | File existence |
| Every file must parse as valid YAML | Syntax validation |
| Every file must conform to its schema (this document) | Pydantic model validation |
| Required fields must be present | Schema completeness |
| Enum fields must have valid values | Allowed value validation |
| Version strings must follow semver | Format validation |

### 10.2 Cross-Reference Integrity

| Rule | Check |
|---|---|
| Every `pipeline.stages[].stage_id` must have a corresponding `stages/stage_<id>.yaml` | File existence per stage |
| Every `input.contract` and `output.contract` must have a corresponding `contracts/<name>.yaml` | File existence per contract |
| Every `processing.llm_config.output_schema` must match a contract in `contracts/<name>.yaml` OR an allowed built-in compiler wrapper model (`GeneratedFileSet`) | LLM output schema cross-reference resolution |
| Every `${config.*}` must resolve in `config.yaml` | Config path resolution |
| Every `${secrets.*}` must appear in `config.yaml`'s `secrets` list | Secret registration |
| Pipeline `next` must reference a valid `stage_id` or be `null` | Stage reference check |
| `processing.tools[].handler` string must conform to `<filepath>::<function_name>` pattern | Hook format validation |
| `processing.tools[].handler` target module must match callable signature against declared `signature` | Hook interface integrity |

### 10.3 Completeness

| Rule | Check |
|---|---|
| Every contract field must have a `description` | Human readability |
| Every stage must have at least one error strategy | No unhandled errors |
| If `processing.tools[].handler` is declared, `processing.logic_description` must be present and non-empty | Leaf Coder prompt completeness |
| Every `${config.*}` reference must resolve in all declared environments | Config completeness per environment |
| Every required secret must be declared | Secret documentation completeness |

### 10.4 No Ambiguity

The Builder must not need to make design decisions. These rules ensure the impl docs are sufficiently detailed:

| Rule | Check |
|---|---|
| Every field has an explicit type — no inferred types | Type explicitness |
| Every field has an explicit nullable declaration — no defaults | Nullability explicitness |
| Every error strategy has an explicit action — no fallback to "whatever seems right" | Error handling explicitness |
| Every shared component reference has a `usage_type` — not "figure it out" | Component usage explicitness |
| Reasoning stages must explicitly specify `llm_config` with model tier and output schema | Reasoning parameters explicitness |
| Every configuration reference resolves to a value or a secret — no dangling references | Configuration completeness |

---

## 11. Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Document format | Pure YAML with rich `description` fields | Same dual-audience pattern as OpenAPI, dbt, Kubernetes manifests. Machine-parseable structure + human-readable descriptions. No mixed formats. |
| 2 | Multi-file vs single file | Multiple files per agent (5-document suite) | Cognitive load management, change isolation, Builder context efficiency, clean git diffs. Each file has one responsibility. |
| 3 | Runtime-agnostic stage specs | Stages specify WHAT, not HOW | The same impl doc works for LangGraph (stateful tool-calling agent with gates) and Python+LiteLLM (lightweight CLI/event agent). Adding a new runtime means adding a Builder strategy, not changing the spec format. |
| 4 | Discrete Typed Contracts over Inheritance | Independent typed contracts per stage boundary | Layer 2 AI agents pass distinct artifacts (profiles, plans, bundles, reports) rather than mutating a single row schema down an inheritance pipeline. Avoids tight coupling between disparate stage interfaces. |
| 5 | Configuration externalization | All values via `${config.*}` and `${secrets.*}` syntax | Nothing hardcoded. Same impl doc set deploys across dev/staging/production via environment overrides. Operators know exactly what secrets to provision. |
| 6 | Data Sensitivity & Tagging on Contracts | Classification and semantic tags declared in `contracts/*.yaml` | Contracts define data classification (`internal`, `confidential`) and semantic tags (`metadata`, `dbt`). Injected into generated models and lineage catalog. |
| 7 | Streamlined 5-Document Suite | Retire standalone `governance.yaml` | Governance is an emergent property of contracts, quality gates, and metadata framework callbacks. Eliminates artificial separation and unifies rules with contracts and stages. |
| 8 | Test derivation | Tests declared in `tests.yaml`, auto-generated from contracts | Builder generates schema tests, field quality tests, and integration tests mechanically. Custom tests cover edge cases contracts can't express. |
| 9 | Quality rules at field AND artifact level | Both supported | Field rules validate specific outputs (e.g. valid SQL dialect). Artifact rules validate complete deliverables (e.g. non-empty SQL files). |
| 10 | Cross-document references by name | Named string references, not file paths | `contract: source_schema_profile` is cleaner than `contract: ./contracts/source_schema_profile.yaml`. The mapping from name to file is a convention the Builder knows. |
| 11 | `${config.*}` syntax for externalization | Template variable syntax parsed at build/deploy time | Familiar pattern from Docker Compose, Helm, Spring Boot. Builder generates code that reads config; it doesn't inline values. |
| 12 | Error strategy per-scenario | Multiple error strategies per stage, keyed by scenario | Different error types need different handling. Connection failures need retry. Lint/syntax errors need LLM reflection. Fatal issues need escalation. |
| 13 | Platform-Managed Data Protection | Enterprise policy consumer model (Collibra, Snowflake DDM) | Individual pipeline agents consume central governance rather than compiling bespoke regex masking scripts in Python. |
| 14 | Performance expectations in stage specs | `expected_duration`, `timeout` | Builder generates monitoring, alerting, and timeout code from these. Without them, the Builder would have to guess or skip observability. |
| 15 | Single source of truth post-build | `impl_docs/` committed to agent Git repo; `spec.yaml` retired | Eliminates dual-spec drift. `spec.yaml` is strictly Day-0 intake. Day-2 enhancements operate directly on `impl_docs/*.yaml` via Git PR or Chainlit, avoiding competing sources of truth. |
| 16 | Leaf Coder hook fields (`processing.tools[].handler`, `processing.logic_description`) | Explicit YAML tool hooks for bespoke imperative logic | Preserves mathematical determinism in orchestration skeleton compilation while allowing isolated LLM synthesis for custom tools, prompt templates, and verifiers. |
| 17 | Agentic Processing Paradigms | Declared in `stage.yaml` (`processing.processing_type: llm_reasoning | deterministic_tool | human_gate`) | Replaces Layer 3 bulk DataFrame execution modes (`batch`, `chunked`, `row`). LLM reasoning stages declare model tiers, Instructor output schemas, and prompt template refs; deterministic tool stages bind `@tool` definitions; human gates configure review pauses. |
| 18 | Layer 2 DDLC AI Agent Modeling | Canonical reference agent: `dbt_pipeline_builder_agent` | Resolves the Layer collapse problem by cleanly separating Layer 2 (AI agents that build/operate data pipelines) from Layer 3 (the data pipelines themselves). Contracts define typed agent state and engineering artifacts (Pydantic/Instructor models), tools are agentic functions (`inspect_schema`, `run_dbt_compile`, `create_pr`), and telemetry tracks LLM token usage, spans, and human decisions natively. |

---

## 12. What This Document Does NOT Cover

This document defines the **schema and structure** of implementation documents. It does **not** cover:

- **How the Documentarian generates impl docs** — The prompts, reasoning, and iteration logic the Documentarian uses to translate architecture into YAML. That is a specialist implementation detail.
- **How the Builder generates code from impl docs** — The code templates, generation strategies, and file emission logic. That is defined in `builder.md`.
- **The spec template** — What fields the user fills in to bootstrap a new DDLC agent. That is defined in [`input-spec-template/spec_template.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/input-spec-template/spec_template.md).
- **The architecture proposal format** — What the Architect produces and the Documentarian reads. That is an architecture detail.
- **Enterprise Governance Integration** — How enterprise platforms (Collibra, Immuta, Snowflake DDM) enforce dynamic masking and access policies via DCM-compiled contract tags.
- **DCM Schema Engine internals** — How YAML contracts compile to DDL. That is defined in [`dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/dcm-agent/dcm_agent.md).
- **Metadata framework table schemas** — The DDL for `stage_execution`, `data_quality_summary`, etc. That is defined in [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md).
- **Prompt engineering** — The actual LLM prompts used by the Documentarian or Builder specialists.
- **Versioning and migration of impl docs** — How impl docs evolve across agent versions. Deferred to a future operational design document.
- **Multi-agent impl doc coordination** — How impl docs for agents that depend on each other are kept consistent. Deferred to the change management design.
