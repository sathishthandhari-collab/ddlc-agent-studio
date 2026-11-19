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

This abstraction exists because the meta-agent builds agents with completely different architectures from the same process. One agent might be a full LangGraph state machine. Another might be a simple Python batch script. Another might be a streaming Kafka processor. The implementation documents are identical in format regardless of runtime — the Builder's generation strategy is what changes.

### The Same Impl Doc, Three Different Outputs

Consider this stage specification from an impl doc:

```yaml
id: validate_quality
type: validation
description: >
  Validates incoming customer records against quality rules.
  Records that fail validation are quarantined with reason codes.
  A summary of validation results is logged to the audit trail.
autonomy: autonomous
input_contract: customer_raw
output_contract: customer_validated
error_strategy:
  action: quarantine
  quarantine_destination: "staging.customer_quarantine"
  log_summary: true
  proceed_with_valid: true
tools:
  - great_expectations
governance:
  - rule: audit_quarantined_records
    action: log_to_audit_trail
```

The Builder reads this spec and generates different code depending on the runtime:

**LangGraph Runtime** — becomes a node in a state machine using Claim Check pointers:
```python
# Generated: stages/validate_quality.py
from typing import Dict, Any
from langgraph.graph import StateGraph
from great_expectations import DataContext

def validate_quality(state: AgentState) -> Dict[str, Any]:
    """Validates customer records against quality rules using Claim Check pointer."""
    in_ptr = state["contracts"]["customer_raw"]
    context = DataContext()
    results = context.run_checkpoint(
        checkpoint_name="customer_raw_quality",
        batch_request={"uri": in_ptr["uri"]}
    )
    valid_uri, quarantined_uri, valid_count, quarantined_count, sha256 = partition_and_store(results, state["run_id"])
    log_quarantine_audit(quarantined_uri, quarantined_count, state["run_id"])
    return {
        "contracts": {
            **state["contracts"],
            "customer_validated": {
                "contract_id": "customer_validated",
                "uri": valid_uri,
                "record_count": valid_count,
                "payload_sha256": sha256,
            }
        },
        "quarantine_count": quarantined_count,
        "quality_score": valid_count / (valid_count + quarantined_count) if (valid_count + quarantined_count) > 0 else 0.0,
    }
```

**Python Pipeline Runtime** — becomes a function in a sequential script:
```python
# Generated: pipeline.py
def validate_quality(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Validates customer records against quality rules."""
    context = DataContext()
    results = context.run_checkpoint(
        checkpoint_name="customer_raw_quality",
        batch_request=BatchRequest(dataframe=df)
    )
    valid_df, quarantine_df = partition_results(results)
    quarantine_df.to_sql("customer_quarantine", config.staging_engine)
    log_quarantine_audit(quarantine_df, config.audit_engine)
    return valid_df
```

**Streaming Runtime** — becomes a handler in a Kafka consumer:
```python
# Generated: handlers/validate_quality.py
class ValidateQualityHandler(MessageHandler):
    """Validates customer records against quality rules."""
    def handle(self, message: CustomerRawMessage) -> CustomerValidatedMessage:
        context = DataContext()
        result = context.run_validation(
            expectation_suite="customer_raw_quality",
            batch=message.to_batch()
        )
        if not result.success:
            self.quarantine(message, result.failures)
            self.audit_log(message, result.failures)
            return None  # Drop from stream
        return CustomerValidatedMessage.from_raw(message)
```

The impl doc didn't change. The Builder's generation strategy changed. This is why the Documentarian never needs to know the runtime — it writes the same specification regardless.

---

## 3. Document Structure

An implementation document set for a DDLC agent consists of multiple YAML files organized in a standard directory structure. Each file has a single responsibility.

```
<agent_name>/
├── agent.yaml                    # Agent manifest — identity, runtime, architecture
├── stages/                       # One file per pipeline stage
│   ├── stage_ingest.yaml
│   ├── stage_validate.yaml
│   └── stage_enrich.yaml
├── contracts/                    # One file per data contract
│   ├── customer_raw.yaml
│   └── customer_validated.yaml
├── config.yaml                   # All externalized configuration
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
> In the schemas below, field annotations explain the schema rules (types, requirements, allowed values). To make cross-document references and dependencies tangible and realistic, the example YAML blocks are populated with values from our canonical reference agent: `"customer_data_pipeline"` (comparable to the Swagger Petstore in OpenAPI).  
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
  name: "customer_data_pipeline"               # Unique agent identifier. Used in file paths, metadata registry, and cross-agent references.
  display_name: "Customer Data Pipeline"       # Human-readable name for UI and documentation.
  version: "1.0.0"                             # Semantic version. Incremented on every impl doc revision.
  description: >                               # Human-readable purpose statement. NOT read by the Builder for code generation
    Ingests raw customer CSVs from S3, validates data quality,               # — exists purely for human understanding.
    masks PII fields (email, phone), enriches with derived fields
    (customer_tier, signup_cohort), and publishes validated records
    to the analytics Snowflake warehouse. Runs daily on a schedule.
  owner: "data-engineering-team"               # Team or individual responsible for this agent.
  tags:                                        # Searchable tags for metadata catalog discovery.
    - "customer"
    - "pii"
    - "batch"
    - "daily"

# --- Architecture Selection -----------------------------------
architecture:
  pattern: "stateful_pipeline_with_gates"      # Pattern from the catalog. See architecture.md Section 7.1.
                                               # Valid patterns: simple_pipeline, stateful_pipeline_with_gates,
                                               # batch_processor, streaming_processor, multi_source_aggregator
  runtime: "langgraph"                         # The runtime the Builder uses to select its generation strategy.
                                               # Valid runtimes: langgraph, python_pipeline, streaming
  autonomy_level: "semi_autonomous"            # How much the agent can do without human intervention.
                                               # Valid levels: autonomous, semi_autonomous, human_required

# --- Stage Ordering -------------------------------------------
# Declares the pipeline topology. The Builder uses this to wire
# stages together in the correct sequence. Each stage_id must
# have a corresponding file in stages/stage_<id>.yaml.
pipeline:
  stages:
    - stage_id: ingest                         # References stages/stage_ingest.yaml
      next: validate                           # Linear flow: ingest → validate
    - stage_id: validate
      next: enrich
      on_error: quarantine_handler             # Conditional routing on validation failure
    - stage_id: enrich
      next: publish
    - stage_id: publish
      next: null                               # Terminal stage — no successor

# --- Dependencies & Shared Components -------------------------
# External tools, libraries, and shared components this agent
# requires. The Builder uses this to generate import statements,
# requirements.txt entries, and shared component references.
dependencies:
  tools:                                       # External libraries/tools required by stages
    - name: "great_expectations"
      version: ">=0.18.0"
      used_by: [validate]                      # Which stages use this tool
      description: "Data quality validation framework"
    - name: "boto3"
      version: ">=1.34.0"
      used_by: [ingest]
      description: "AWS S3 client for reading source files"
    - name: "snowflake-connector-python"
      version: ">=3.6.0"
      used_by: [publish]
      description: "Snowflake warehouse connector for publishing"
  shared_components:                           # Components from the shared component catalog
    - component_id: "s3_reader_v1"             # References component_registry in Tier 2 metadata
      version: "1.3.0"
      used_by: [ingest]
      usage_type: "import"                     # import (use as-is), extend (subclass), wrap (adapter)
      description: "Shared S3 ingestion component — multi-file and partition detection"
    - component_id: "audit_logger_v1"
      version: "1.0.0"
      used_by: [validate, enrich, publish]
      usage_type: "import"
      description: "Shared audit logging component — writes to metadata framework tables"

# --- Metadata Integration -------------------------------------
# Declares which metadata framework tiers and tables this agent
# provisions and writes to during execution.
metadata:
  tier_3_schema: "ddlc_customer_pipeline_metadata"   # Tier 3 schema name (auto-provisioned by DCM engine)
  tier_3_tables:                                      # Agent-specific tables needed
    - "source_profiling_results"
    - "reconciliation_log"
  writes_to:                                          # Tier 1 and 2 tables this agent writes to via callbacks
    tier_1: [agent_run_header, execution_log, artifact_registry, audit_log]
    tier_2: [stage_execution, data_quality_summary, governance_audit]

# --- Schedule -------------------------------------------------
schedule:
  type: "cron"                                 # cron, event_driven, manual, on_demand
  expression: "0 2 * * *"                      # Daily at 2 AM UTC
  timezone: "UTC"
  description: "Runs daily at 2 AM UTC to process previous day's customer CSV drops"
```

**How the Builder reads `agent.yaml`:**

| Field | Builder Action |
|---|---|
| `architecture.runtime` | Selects the generation strategy (LangGraph, Python pipeline, streaming) |
| `architecture.pattern` | Informs project structure and boilerplate |
| `pipeline.stages` | Generates the stage wiring — graph edges (LangGraph), function call sequence (pipeline), or handler registration (streaming) |
| `pipeline.stages[].on_error` | Generates error routing branches |
| `dependencies.tools` | Generates `requirements.txt` / `pyproject.toml` entries |
| `dependencies.shared_components` | Generates import statements or adapter code |
| `metadata.tier_3_schema` | Invokes DCM Schema Engine to compile and apply Tier 3 migrations |

---

### 4.2 Stage Documents — `stages/stage_<id>.yaml`

Each stage document fully specifies one pipeline stage. It declares the stage's purpose, its inputs and outputs (as contract references), custom transformation hooks, tools, error handling strategies, and performance SLAs.

#### Canonical Pydantic Schema (`StageDocument`)

To guarantee mathematical determinism between the Documentarian, Parser, and Compiler, stage specifications adhere to the following top-level Pydantic schema:

```python
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class StageHeader(BaseModel):
    id: str = Field(..., description="Unique stage identifier matching pipeline.stages[].stage_id")
    name: str = Field(..., description="Human-readable display name for logging and telemetry")
    type: Literal["ingestion", "validation", "transformation", "enrichment", "publication", "aggregation", "notification", "custom"]
    description: str = Field(..., description="Plain English description of stage responsibility")
    autonomy: Literal["autonomous", "semi_autonomous", "human_required"] = "autonomous"
    transform_handler: Optional[str] = Field(None, description="Hook for Leaf Coder custom imperative logic: handlers/<mod>.py::<func>")

class StageInput(BaseModel):
    contract: Optional[str] = Field(None, description="Input contract reference in contracts/<name>.yaml")
    source: Optional[Dict[str, Any]] = Field(None, description="Source connection details (ingestion stages)")

class StageOutput(BaseModel):
    contract: Optional[str] = Field(None, description="Output contract reference in contracts/<name>.yaml")
    destination: Optional[Dict[str, Any]] = Field(None, description="Destination connection details (publication stages)")

class ToolConfig(BaseModel):
    name: str
    purpose: str
    configuration: Optional[Dict[str, Any]] = Field(default_factory=dict)

class SharedComponentUsage(BaseModel):
    name: str
    usage_type: Literal["import", "extend", "wrap"] = "import"
    configuration: Optional[Dict[str, Any]] = Field(default_factory=dict)

class StageProcessing(BaseModel):
    execution_mode: Literal["batch", "chunked", "row"] = Field(
        default="batch", 
        description="Execution paradigm: 'batch' (vectorized Polars/Pandas DataFrame), 'chunked' (bounded streaming generator), 'row' (single-record Pydantic)"
    )
    dataframe_engine: Optional[Literal["polars", "pandas"]] = Field(
        default="polars", 
        description="Vectorized DataFrame engine when execution_mode is 'batch'"
    )
    chunk_size: Optional[int] = Field(
        default=None, 
        description="Maximum record batch size in memory when execution_mode is 'chunked' (e.g. 10000)"
    )
    logic_description: Optional[str] = Field(None, description="Algorithmic description projected directly into Leaf Coder prompt")
    tools: List[ToolConfig] = Field(default_factory=list)
    shared_components: List[SharedComponentUsage] = Field(default_factory=list)

class ErrorScenario(BaseModel):
    action: Literal["retry", "quarantine", "skip", "fail", "warn", "alert"]
    max_retries: Optional[int] = 3
    retry_delay_seconds: Optional[int] = 60
    escalation: Optional[str] = "fail"
    quarantine_destination: Optional[str] = None
    quarantine_table: Optional[str] = None
    include_reason_codes: Optional[bool] = False
    log_summary: Optional[bool] = True
    proceed_with_valid: Optional[bool] = False
    log_level: Optional[str] = "INFO"
    details: Optional[str] = None

class StagePerformance(BaseModel):
    expected_record_count: Optional[Dict[str, Any]] = None
    expected_duration_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None

class QualityGate(BaseModel):
    metric: str
    threshold: float
    on_failure: Dict[str, Any]

class StageDocument(BaseModel):
    """Canonical model for stages/stage_*.yaml validated before compilation."""
    stage: StageHeader
    input: StageInput
    output: StageOutput
    processing: Optional[StageProcessing] = Field(default_factory=StageProcessing)
    error_strategy: Dict[str, ErrorScenario]
    performance: Optional[StagePerformance] = None
    quality_gate: Optional[QualityGate] = None
```

#### Stage Specification Fields Reference

| Field | Type | Required | Description |
|---|---|---|---|
| `stage.id` | string | Yes | Unique stage identifier. Must match `pipeline.stages[].stage_id` in `agent.yaml`. |
| `stage.name` | string | Yes | Human-readable stage name for display and logging. |
| `stage.type` | enum | Yes | Stage category informing code template selection: `ingestion`, `validation`, `transformation`, `enrichment`, `publication`, `aggregation`, `notification`, `custom`. |
| `stage.description` | string | Yes | Plain English explanation of stage responsibility and business intent. |
| `stage.autonomy` | enum | Yes | Autonomy level: `autonomous`, `semi_autonomous`, `human_required`. |
| `stage.transform_handler` | string | Optional | Hook path (`"handlers/<mod>.py::<func>"`) dispatching execution to an isolated Python handler function synthesized by the **LLM Leaf Coder**. Used for bespoke imperative logic. |
| `input.contract` | string | Required* | Reference to input contract in `contracts/`. (*Required for non-ingestion stages). |
| `input.source` | object | Optional | Source connection details (for ingestion stages). |
| `output.contract` | string | Required* | Reference to output contract in `contracts/`. (*Required for non-publication stages). |
| `output.destination` | object | Optional | Destination connection details (for publication stages). |
| `processing.execution_mode` | enum | Yes | Execution paradigm: `batch` (vectorized Polars/Pandas DataFrame), `chunked` (bounded memory generator batches), `row` (single Pydantic record; default for streaming). |
| `processing.dataframe_engine` | enum | Optional | Vectorized DataFrame engine when `execution_mode: batch`. Default: `polars` (Apache Arrow native); fallback: `pandas`. |
| `processing.chunk_size` | integer | Optional | Maximum memory batch size when `execution_mode: chunked` (e.g., `10000`). Binds peak RAM to $O(\text{chunk\_size})$. |
| `processing.logic_description` | string | Conditional | Algorithmic, step-by-step description of transformations and rules. **Required** when `transform_handler` is declared; projected directly into the Leaf Coder's prompt. |
| `processing.tools` | list | Optional | Tools and libraries invoked within this stage. |
| `processing.shared_components` | list | Optional | Reusable shared components referenced by the stage. |
| `error_strategy` | object | Yes | Handlers for failure scenarios (`action`: retry, quarantine, skip, fail, warn, alert). |
| `performance` | object | Optional | Operational SLA expectations (`expected_record_count`, `expected_duration_seconds`, `timeout_seconds`). |
| `quality_gate` | object | Optional | Stage-level gate based on aggregate quality metrics (`metric`, `threshold`, `on_failure`). |

```yaml
# ============================================================
# stages/stage_ingest.yaml — Ingestion Stage Specification
# ============================================================
# Fully specifies the ingestion stage of the Customer Data
# Pipeline. The Builder reads this to generate the stage's
# code, telemetry, and error handling.
# Top-level sections: stage, input, output, processing, error_strategy, performance.
# ============================================================

stage:
  id: ingest                                    # Unique stage identifier. Must match pipeline.stages[].stage_id in agent.yaml.
  name: "Ingest Customer CSV"                   # Human-readable stage name.
  type: ingestion                               # Stage type category. Informs Builder's code template selection.
                                                # Valid types: ingestion, validation, transformation, enrichment,
                                                # publication, aggregation, notification, custom
  description: >
    Pulls daily CSV files from the S3 raw data bucket.
    Handles schema evolution by detecting new columns and logging
    them as warnings rather than failing. Supports both single-file
    and multi-file ingestion (daily partitioned directories).
    Empty file drops are treated as no-ops with an info log, not errors.
  autonomy: autonomous                          # autonomous, semi_autonomous, human_required

input:
  source:
    type: "s3"                                # Source system type. Builder generates the appropriate reader.
    uri: "${config.sources.customer_csv.uri}" # Externalized — references config.yaml. NEVER hardcoded.
    format: "csv"                             # File format: csv, parquet, json, jsonl, avro
    options:
      delimiter: ","
      header: true
      encoding: "utf-8"
      quote_char: '"'
      null_values: ["", "NULL", "N/A"]
  contract: customer_raw                      # References contracts/customer_raw.yaml.

output:
  destination:
    type: "database"                          # Destination system type.
    uri: "${config.destinations.staging.uri}" # Externalized connection string
    table: "staging.customer_raw"             # Target table name
    write_mode: "append"                      # Valid: append, overwrite, upsert, merge
  contract: customer_raw                      # Output conforms to customer_raw contract.

processing:
  tools:
    - name: "boto3"
      purpose: "Read CSV files from S3 bucket"
      configuration:
        region: "${config.aws.region}"
        assume_role: "${config.aws.s3_read_role_arn}"

error_strategy:
  on_source_unavailable:
    action: retry                             # Valid actions: retry, fail, skip, quarantine, alert
    max_retries: 3
    retry_delay_seconds: 60
    escalation: fail                          # What happens after retries exhausted
  on_schema_evolution:
    action: warn                              # Log new columns, don't fail
    log_level: "WARNING"
    details: "New columns detected in source file — logged for review"
  on_empty_source:
    action: skip                              # No data is not an error
    log_level: "INFO"
    details: "Empty file drop detected — no records to process"
  on_parse_error:
    action: quarantine                        # Unparseable rows go to quarantine
    quarantine_destination: "${config.destinations.quarantine.uri}"
    quarantine_table: "staging.customer_parse_errors"
    log_summary: true

performance:
  expected_record_count:
    min: 0                                    # Empty drops are valid
    max: 500000                               # Alert if exceeds this — possible duplicate drop
    alert_on_exceed: true
  expected_duration_seconds: 300               # 5-minute SLA for ingestion
  timeout_seconds: 600                         # Hard timeout — kill after 10 minutes
```

**A second stage example — validation with quality gate and shared audit component:**

```yaml
# ============================================================
# stages/stage_validate.yaml — Validation Stage Specification
# ============================================================

stage:
  id: validate
  name: "Validate Customer Data Quality"
  type: validation
  description: >
    Validates ingested customer records against data quality rules
    defined in the customer_raw contract. Records that fail any rule
    are quarantined with reason codes. A quality summary is logged
    to the data_quality_summary metadata table. The stage proceeds
    with valid records only — it never blocks the pipeline on
    individual record failures.
  autonomy: autonomous

input:
  source:
    type: "database"
    uri: "${config.destinations.staging.uri}"
    table: "staging.customer_raw"
  contract: customer_raw                      # Contract defining the quality rules to check

output:
  destination:
    type: "database"
    uri: "${config.destinations.staging.uri}"
    table: "staging.customer_validated"
    write_mode: overwrite                     # Each run replaces validated records
  contract: customer_validated                # Output conforms to stricter validated contract

processing:
  tools:
    - name: "great_expectations"
      purpose: "Execute data quality validation suite"
      configuration:
        data_context_root: "${config.tools.great_expectations.context_root}"
        checkpoint_name: "customer_raw_quality"
  shared_components:
    - name: "audit_logger_v1"
      usage_type: "import"

error_strategy:
  on_validation_failure:
    action: quarantine
    quarantine_destination: "${config.destinations.quarantine.uri}"
    quarantine_table: "staging.customer_quarantine"
    include_reason_codes: true                # Each quarantined row gets failure details
    log_summary: true
    proceed_with_valid: true                  # Pipeline continues with clean data
  on_tool_error:
    action: fail
    details: "Great Expectations runtime failure — cannot validate data"

performance:
  expected_record_count:
    min: 0
    max: 500000
  expected_duration_seconds: 120
  timeout_seconds: 300

# Stage-level quality gate evaluated against batch metrics
quality_gate:
  metric: "quality_score"                     # Computed as: valid_records / total_records
  threshold: 0.95                             # 95% of records must pass validation
  on_failure:
    action: fail                              # Fail the stage if quality is below threshold
    alert: true
    alert_channel: "${config.alerts.channel}"
```

**A third stage example — bespoke transformation with Leaf Coder hook (`transform_handler` and `processing.logic_description`):**

```yaml
# ============================================================
# stages/stage_enrich.yaml — Custom Enrichment Stage Specification
# ============================================================
# Specifies custom imperative transformation logic. Because this stage
# declares a transform_handler and processing.logic_description, the
# Builder's Deterministic Scaffold Compiler emits the graph wiring and
# invokes the LLM Leaf Coder to synthesize the editable handler in
# src/handlers/enrich_customer.py and unit tests in tests/unit/.
# ============================================================

stage:
  id: enrich
  name: "Enrich Customer Tier"
  type: transformation
  transform_handler: "handlers/enrich_customer.py::compute_tiers"
  description: >
    Enriches validated customer records with loyalty tiers and calculation_timestamp.
    Uses bespoke scoring logic based on credit scores and lifetime spend.
  autonomy: autonomous

input:
  contract: customer_validated

output:
  contract: customer_enriched

processing:
  execution_mode: batch
  dataframe_engine: polars
  logic_description: >
    Calculate loyalty tier based on credit_score and historical purchases:
    - Tier Platinum: credit_score >= 750 AND lifetime_spend > 10000
    - Tier Gold: credit_score >= 700 OR lifetime_spend > 5000
    - Tier Silver: All other active customers
    Add calculation_timestamp as current UTC time.

error_strategy:
  on_calculation_error:
    action: quarantine
    quarantine_destination: "${config.destinations.quarantine.uri}"
    quarantine_table: "staging.customer_enrichment_errors"
    log_summary: true
    proceed_with_valid: true
  on_missing_fields:
    action: fail
    details: "Required credit_score or lifetime_spend missing from validated contract"

performance:
  expected_record_count:
    min: 0
    max: 500000
  expected_duration_seconds: 60
  timeout_seconds: 180
```

---

### 4.3 Contract Documents — `contracts/<name>.yaml`

Contract documents define the exact schema of data flowing between stages. They are the **most critical** documents in the set — every field, every type, every quality rule, every SLA is specified here. The Builder generates Pydantic models, schema validators, and test suites from contracts. The DCM Schema Engine compiles contracts into database DDL.

```yaml
# ============================================================
# contracts/customer_raw.yaml — Customer Raw Data Contract
# ============================================================
# Defines the schema and quality expectations for raw customer
# data as it enters the pipeline from S3. This contract is
# referenced by the ingest and validate stages.
# ============================================================

contract:
  name: customer_raw                            # Unique contract name. Referenced by stages via input_contract/output_contract.
  version: "1.0.0"                              # Contract version. Changes here trigger downstream impact analysis.
  description: >
    Schema definition for raw customer records as ingested from
    daily S3 CSV drops. Represents the data BEFORE validation
    and warehouse publication. Fields match the source system's export format.
  direction: internal                           # Valid: input (external source), output (external sink), internal (between stages)
  owner: "data-engineering-team"
  classification: "confidential"                # Platform data classification: public, internal, confidential, restricted
  tags: ["crm", "customer_profile"]

  # --- Field Definitions --------------------------------------
  # Every field the contract covers. No field may exist in the data
  # that is not declared here (strict schema enforcement).
  fields:
    - name: customer_id
      type: string                              # Logical type. Builder maps to language-specific types.
                                                # Valid types: string, integer, float, decimal, boolean,
                                                # date, datetime, timestamp, uuid, json, array, map
      nullable: false                           # Whether null/missing values are permitted.
      description: "Unique customer identifier from the source CRM system. Format: CUS-XXXXXXXX."
      primary_key: true                         # Indicates this field is the primary identifier for dedup and lookup.
      quality_rules:
        - rule: regex
          pattern: "^CUS-[A-Z0-9]{8}$"
          severity: error                       # error = quarantine the record. warning = log and continue.
          message: "customer_id must match format CUS-XXXXXXXX"
        - rule: uniqueness
          scope: batch                          # unique within this batch (not globally)
          severity: error
          message: "Duplicate customer_id detected within batch"

    - name: first_name
      type: string
      nullable: false
      description: "Customer's first name as recorded in CRM."
      max_length: 100
      quality_rules:
        - rule: not_empty
          severity: error
          message: "first_name must not be an empty string"

    - name: last_name
      type: string
      nullable: false
      description: "Customer's last name as recorded in CRM."
      max_length: 100
      quality_rules:
        - rule: not_empty
          severity: error
          message: "last_name must not be an empty string"

    - name: email
      type: string
      nullable: false
      description: "Customer's primary email address. PII field — will be masked in downstream stages."
      pii: true                                 # Marks this field as containing personally identifiable information.
      pii_category: "email_address"             # PII classification. Governance rules use this for masking strategy selection.
      max_length: 320
      quality_rules:
        - rule: regex
          pattern: "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$"
          severity: error
          message: "email must be a valid email address format"

    - name: phone
      type: string
      nullable: true                            # Phone is optional in the source system
      description: "Customer's primary phone number. PII field — will be masked downstream."
      pii: true
      pii_category: "phone_number"
      max_length: 20
      quality_rules:
        - rule: regex
          pattern: "^\\+?[1-9]\\d{1,14}$"
          severity: warning                     # Invalid phone format is a warning, not an error
          message: "phone should match E.164 format"

    - name: lifetime_spend
      type: float
      nullable: false
      default: 0.0
      description: "Customer total historical spend in USD. Used for customer loyalty tiering."
      quality_rules:
        - rule: range
          min: 0.0
          severity: error
          message: "lifetime_spend must be non-negative"

    - name: signup_date
      type: date
      nullable: false
      description: "Date the customer signed up. Used for cohort analysis."
      quality_rules:
        - rule: range
          min: "2020-01-01"                     # Business rule: no records before system launch
          max: "${today}"                       # Dynamic — evaluated at runtime
          severity: error
          message: "signup_date must be between 2020-01-01 and today"

    - name: account_status
      type: string
      nullable: false
      description: "Current account status in the CRM system."
      quality_rules:
        - rule: allowed_values
          values: ["active", "inactive", "suspended", "closed"]
          severity: error
          message: "account_status must be one of: active, inactive, suspended, closed"

    - name: annual_revenue
      type: decimal
      nullable: true
      description: "Customer's reported annual revenue in USD. Used for tiering."
      precision: 15
      scale: 2
      quality_rules:
        - rule: range
          min: 0
          max: 999999999999.99
          severity: error
          message: "annual_revenue must be non-negative and within reasonable bounds"

    - name: created_at
      type: timestamp
      nullable: false
      description: "Timestamp when this record was created in the source system. Used for freshness checks."
      quality_rules:
        - rule: freshness
          max_age_hours: 48                     # Records older than 48 hours trigger a warning
          severity: warning
          message: "Record is older than 48 hours — may indicate a stale source"

  # --- Aggregate Quality Rules --------------------------------
  # Rules that apply to the entire dataset, not individual records.
  aggregate_rules:
    - rule: completeness
      description: "At least 99% of records must have non-null email"
      field: email
      threshold: 0.99
      severity: error

    - rule: volume
      description: "Daily batch should contain between 100 and 500,000 records"
      min_records: 100
      max_records: 500000
      severity: warning

  # --- SLA Definitions ----------------------------------------
  # Service-level agreements for this contract's data.
  sla:
    freshness:
      max_age_hours: 24                         # Data must be no older than 24 hours at point of consumption
      description: "Consumer systems expect data refreshed within 24 hours"
    availability:
      uptime_percent: 99.5                      # The pipeline producing this data must be available 99.5% of the time
      description: "Daily pipeline availability target"
    latency:
      max_processing_seconds: 600               # End-to-end processing time from ingestion to publication
      description: "Total pipeline latency SLA from S3 read to warehouse write"

  # --- Schema Evolution Rules ---------------------------------
  schema_evolution:
    allow_new_fields: true                      # New columns in source are logged as warnings, not errors
    allow_field_removal: false                  # Removed columns are errors — must be explicitly handled
    allow_type_changes: false                   # Type changes are errors — require contract version bump
```

**A second contract — the validated output with additional derived fields:**

```yaml
# ============================================================
# contracts/customer_validated.yaml — Validated Customer Contract
# ============================================================

contract:
  name: customer_validated
  version: "1.0.0"
  description: >
    Schema for customer records that have passed all quality
    validation rules. Contains the same fields as customer_raw (including
    lifetime_spend) plus validation metadata. PII fields are retained and
    classified for downstream warehouse masking policies.
  direction: internal
  owner: "data-engineering-team"

  # Inherits all fields from customer_raw and adds validation metadata
  extends: customer_raw                         # Contract inheritance — all fields from customer_raw are included
  
  additional_fields:
    - name: validation_timestamp
      type: timestamp
      nullable: false
      description: "When this record was validated."
      default: "${now}"

    - name: validation_status
      type: string
      nullable: false
      description: "Outcome of validation for this record."
      quality_rules:
        - rule: allowed_values
          values: ["passed"]                    # Only passed records exist in this contract
          severity: error

    - name: quality_score
      type: float
      nullable: false
      description: "Aggregate quality score for this record (0.0–1.0). Based on number of rules passed."
      quality_rules:
        - rule: range
          min: 0.0
          max: 1.0
          severity: error

  # Override aggregate rules for the validated output
  aggregate_rules:
    - rule: completeness
      description: "All records in validated output must have non-null email (100%)"
      field: email
      threshold: 1.0
      severity: error

  sla:
    freshness:
      max_age_hours: 24
    availability:
      uptime_percent: 99.5
    latency:
      max_processing_seconds: 600
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
  # --- Environment Defaults ------------------------------------
  # Base configuration values. Overridden per environment below.
  defaults:
    sources:
      customer_csv:
        uri: "s3://data-lake-prod/raw/customers/"
        file_pattern: "customers_*.csv"

    destinations:
      staging:
        uri: "postgresql://${secrets.staging_db_user}:${secrets.staging_db_password}@${secrets.staging_db_host}:5432/staging"
      quarantine:
        uri: "postgresql://${secrets.staging_db_user}:${secrets.staging_db_password}@${secrets.staging_db_host}:5432/staging"
      warehouse:
        uri: "snowflake://${secrets.snowflake_account}/${secrets.snowflake_database}"
        schema: "analytics"
        warehouse: "ETL_WH"

    aws:
      region: "us-east-1"
      s3_read_role_arn: "${secrets.s3_read_role_arn}"

    tools:
      great_expectations:
        context_root: "./great_expectations"

    alerts:
      channel: "${secrets.slack_webhook_url}"

    logging:
      level: "INFO"
      format: "json"

    performance:
      global_timeout_seconds: 3600              # 1-hour hard timeout for entire pipeline

  # --- Environment Overrides ----------------------------------
  # Override specific values per deployment environment.
  # Only changed values need to be specified — defaults apply for the rest.
  environments:
    development:
      sources:
        customer_csv:
          uri: "s3://data-lake-dev/raw/customers/"
      destinations:
        staging:
          uri: "postgresql://localhost:5432/staging_dev"
        quarantine:
          uri: "postgresql://localhost:5432/staging_dev"
      logging:
        level: "DEBUG"
      performance:
        global_timeout_seconds: 600

    staging:
      sources:
        customer_csv:
          uri: "s3://data-lake-staging/raw/customers/"
      destinations:
        staging:
          uri: "postgresql://${secrets.staging_db_user}:${secrets.staging_db_password}@staging-db.internal:5432/staging"

    production:
      # Production uses all defaults — no overrides needed

  # --- Secret References --------------------------------------
  # Documents which secrets the agent needs, for operators
  # provisioning the secrets manager. NEVER contains actual values.
  secrets:
    - name: staging_db_user
      description: "Postgres staging database username"
      required: true
    - name: staging_db_password
      description: "Postgres staging database password"
      required: true
    - name: staging_db_host
      description: "Postgres staging database hostname"
      required: true
    - name: snowflake_account
      description: "Snowflake account identifier"
      required: true
    - name: snowflake_database
      description: "Snowflake target database name"
      required: true
    - name: s3_read_role_arn
      description: "AWS IAM role ARN for S3 read access"
      required: true
    - name: slack_webhook_url
      description: "Slack webhook URL for general alerts"
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
  # --- Contract-Derived Tests ---------------------------------
  # Auto-generated from contract field definitions and quality rules.
  # The Builder reads the contracts and generates test cases for
  # every field and every quality rule.
  contract_tests:
    - contract: customer_raw
      description: "Tests derived from the customer_raw contract"
      cases:
        - test_id: "customer_raw_schema_valid"
          type: schema_validation
          description: "Verify all required fields are present with correct types"
          auto_generate: true

        - test_id: "customer_raw_customer_id_format"
          type: field_quality
          description: "Verify customer_id matches CUS-XXXXXXXX format"
          field: customer_id
          rule: regex
          auto_generate: true

        - test_id: "customer_raw_email_format"
          type: field_quality
          description: "Verify email matches valid email format"
          field: email
          rule: regex
          auto_generate: true

        - test_id: "customer_raw_completeness"
          type: aggregate_quality
          description: "Verify at least 99% of records have non-null email"
          auto_generate: true

    - contract: customer_validated
      description: "Tests derived from the customer_validated contract"
      cases:
        - test_id: "customer_validated_inherits_raw"
          type: schema_validation
          description: "Verify validated contract includes all fields from customer_raw plus additions"
          auto_generate: true

        - test_id: "customer_validated_score_range"
          type: field_quality
          description: "Verify quality_score is between 0.0 and 1.0"
          field: quality_score
          rule: range
          auto_generate: true

  # --- Custom Tests -------------------------------------------
  # Edge cases and integration scenarios specified by the Documentarian.
  custom_tests:
    - test_id: "empty_file_handling"
      type: integration
      description: "Verify pipeline handles empty CSV files gracefully (no-op, not error)"
      stage: ingest
      setup: "Provide empty CSV file in source location"
      expected_behavior: "Stage completes with 0 records processed, INFO log, no errors"

    - test_id: "schema_evolution_new_column"
      type: integration
      description: "Verify pipeline handles new columns in source CSV without failing"
      stage: ingest
      setup: "Provide CSV with an extra column not in the contract"
      expected_behavior: "Stage completes with WARNING log about new column, all existing columns processed normally"

    - test_id: "quality_gate_threshold"
      type: integration
      description: "Verify pipeline fails when quality score drops below 95%"
      stage: validate
      setup: "Provide batch where 10% of records have invalid customer_id format"
      expected_behavior: "Quality gate fails, pipeline halts, alert sent"

    - test_id: "full_pipeline_happy_path"
      type: end_to_end
      description: "Verify complete pipeline from S3 ingestion to Snowflake publication"
      setup: "Provide valid sample CSV in source location"
      expected_behavior: "All stages complete, records appear in warehouse, audit trail complete, no quarantine"
```

---

## 5. Cross-Document References

Implementation documents reference each other through a system of named identifiers. These references create an explicit dependency graph that the Documentarian must keep consistent and the Builder must resolve.

### Reference Types

| From | To | Reference Field | Example |
|---|---|---|---|
| `agent.yaml` → Stage | `stages/stage_<id>.yaml` | `pipeline.stages[].stage_id` | `stage_id: validate` → file `stages/stage_validate.yaml` |
| `agent.yaml` → Component | Tier 2 `component_registry` | `dependencies.shared_components[].component_id` | `component_id: audit_logger_v1` |
| Stage → Contract | `contracts/<name>.yaml` | `input.contract`, `output.contract` | `contract: customer_raw` → file `contracts/customer_raw.yaml` |
| Stage → Tool | `agent.yaml` `dependencies.tools` | `tools[].name` | `name: great_expectations` |
| Stage → Config | `config.yaml` | `${config.<path>}` syntax | `${config.sources.customer_csv.uri}` |
| Stage → Secret | `config.yaml` `secrets` | `${secrets.<name>}` syntax | `${secrets.staging_db_password}` |
| Contract → Contract | `contracts/<name>.yaml` | `extends` | `extends: customer_raw` |
| Tests → Contract | `contracts/<name>.yaml` | `contract_tests[].contract` | `contract: customer_raw` |
| Tests → Stage | `stages/stage_<id>.yaml` | `custom_tests[].stage` | `stage: validate` |

### Consistency Rules

The Documentarian **must** maintain these invariants. The Builder **assumes** they hold.

1. **Every `stage_id` in `agent.yaml`'s pipeline must have a corresponding stage file.** If `agent.yaml` lists `stage_id: validate`, then `stages/stage_validate.yaml` must exist.

2. **Every contract referenced by a stage must have a corresponding contract file.** If a stage declares `input.contract: customer_raw`, then `contracts/customer_raw.yaml` must exist.

3. **Every `${config.*}` reference in any document must resolve to a path in `config.yaml`.** The Builder does not invent configuration values.

4. **Every `${secrets.*}` reference must appear in `config.yaml`'s `secrets` list.** This documents the required secrets for operators.

5. **Contract `extends` references must point to an existing contract.** Circular extends chains are forbidden.

6. **Pipeline `next` references must point to valid stage IDs or `null`.** No broken links in the execution chain.

7. **The `on_error` route in `agent.yaml` must reference a valid stage ID or error handler.** Error routing paths must be resolvable.

---

## 6. Contract Specification Detail

Data contracts are the core abstraction of the implementation document system. They serve as the single source of truth for what data looks like at every point in the pipeline.

### 6.1 Field-Level Specification

Every field in a contract requires these properties:

| Property | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Field identifier. Must be a valid identifier in all target systems (no spaces, special characters). |
| `type` | enum | Yes | Logical data type. The Builder maps these to runtime-specific types (Python, SQL, Pydantic). |
| `nullable` | boolean | Yes | Whether null/missing values are permitted. Drives NOT NULL constraints in DDL and Optional[] in Pydantic. |
| `description` | string | Yes | Human-readable explanation of the field's meaning, source, and usage. |
| `primary_key` | boolean | No | Whether this field is the primary identifier. Default: false. |
| `pii` | boolean | No | Whether this field contains personally identifiable information. Default: false. |
| `pii_category` | string | If `pii: true` | Classification of PII type. Used by governance rules to select masking strategy. |
| `max_length` | integer | No | Maximum string length. Generates VARCHAR(n) in DDL, max_length validator in Pydantic. |
| `precision` | integer | No | Decimal precision (total digits). |
| `scale` | integer | No | Decimal scale (digits after decimal point). |
| `default` | any | No | Default value. Use `${now}` for current timestamp, `${today}` for current date. |
| `quality_rules` | list | No | Per-field quality validation rules. See Section 6.2. |

### Logical Type Mapping

The Builder maps logical types to target-specific types:

| Logical Type | Python / Pydantic | PostgreSQL DDL | Snowflake DDL |
|---|---|---|---|
| `string` | `str` | `VARCHAR(n)` or `TEXT` | `VARCHAR(n)` or `STRING` |
| `integer` | `int` | `INTEGER` or `BIGINT` | `INTEGER` or `BIGINT` |
| `float` | `float` | `DOUBLE PRECISION` | `FLOAT` |
| `decimal` | `Decimal` | `NUMERIC(p,s)` | `NUMBER(p,s)` |
| `boolean` | `bool` | `BOOLEAN` | `BOOLEAN` |
| `date` | `datetime.date` | `DATE` | `DATE` |
| `datetime` | `datetime.datetime` | `TIMESTAMP` | `TIMESTAMP_NTZ` |
| `timestamp` | `datetime.datetime` | `TIMESTAMPTZ` | `TIMESTAMP_TZ` |
| `uuid` | `uuid.UUID` | `UUID` | `VARCHAR(36)` |
| `json` | `dict` | `JSONB` | `VARIANT` |
| `array` | `list` | `JSONB` (array) | `ARRAY` |
| `map` | `dict` | `JSONB` (object) | `OBJECT` |

### 6.2 Quality Rules

Quality rules are specified per-field and define the validation logic the Builder generates.

| Rule Type | Parameters | What the Builder Generates |
|---|---|---|
| `regex` | `pattern`, `severity`, `message` | Regex match validation. Error = quarantine. Warning = log. |
| `not_empty` | `severity`, `message` | Check that string value is not empty (after trimming). |
| `uniqueness` | `scope` (batch/global), `severity`, `message` | Dedup check. Batch = within current batch. Global = across all historical data. |
| `allowed_values` | `values[]`, `severity`, `message` | Enum validation — value must be one of the listed options. |
| `range` | `min`, `max`, `severity`, `message` | Numeric or date range check. Supports `${today}` for dynamic bounds. |
| `freshness` | `max_age_hours`, `severity`, `message` | Timestamp freshness check — record age must be within threshold. |
| `referential_integrity` | `reference_contract`, `reference_field`, `severity`, `message` | FK check — value must exist in the referenced contract's field. |
| `custom` | `expression`, `severity`, `message` | Custom validation expression (Python-evaluable). Use sparingly. |

### 6.3 Aggregate Quality Rules

Aggregate rules apply to the entire dataset:

| Rule Type | Parameters | Description |
|---|---|---|
| `completeness` | `field`, `threshold`, `severity` | Percentage of non-null values for a field must exceed threshold |
| `volume` | `min_records`, `max_records`, `severity` | Total record count must fall within expected range |
| `distribution` | `field`, `expected_distribution`, `tolerance`, `severity` | Value distribution should roughly match expectations (future) |

### 6.4 SLA Definitions

SLAs declare operational expectations. The Builder generates monitoring and alerting code from these.

| SLA Type | Parameters | What the Builder Generates |
|---|---|---|
| `freshness` | `max_age_hours` | Data age check at pipeline completion. Alert if stale. |
| `availability` | `uptime_percent` | Monitoring target for the pipeline schedule. Dashboards. |
| `latency` | `max_processing_seconds` | Pipeline duration monitoring. Alert if exceeds. |

### 6.5 How the DCM Schema Engine Compiles Contracts

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

Operational data quality is enforced deterministically at the pipeline boundaries:
- **Field & Aggregate Assertions**: Declared in `contracts/*.yaml` and compiled into Great Expectations checkpoints.
- **Stage Error Routing**: Declared in `stages/*.yaml` via `error_strategy: quarantine | fail | retry`. If a batch fails quality checks, records are quarantined to a staging destination and summary metrics are recorded.
- **Human Approval Gates**: Declared in `agent.yaml` or `stages/*.yaml` (`autonomy: human_required`). Implements LangGraph `interrupt()` nodes with Claim Check pointers (`PAT-01`), requiring explicit human sign-off before irreversible actions (e.g., publishing to golden analytics tables).

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

| Impl Doc Concept | LangGraph Strategy | Python Pipeline Strategy | Streaming Strategy |
|---|---|---|---|
| **Stage** | A node function registered in the state graph | A function called in sequential `main()` | A message handler class |
| **Pipeline wiring** (`next`, `on_error`) | Graph edges (fixed and conditional) | Sequential function calls with try/except | Kafka topic routing and error topics |
| **Contract** | Claim Check pointer (`ContractPointer` in `AgentState`) with Pydantic model validation at stage boundary | Pydantic model or dataclass for function I/O | Message schema (Avro/Pydantic) |
| **Error handling** (quarantine) | State update with error flag, conditional routing | Function return with error dataframe, logging | Dead-letter topic publishing |
| **Quality & State Machine Gates** | State machine gate node or Claim Check checkpoint | Validation function check with quarantine return | Middleware interceptor or DLQ router |
| **Human gate** | LangGraph interrupt node with Chainlit UI | Input prompt / API call with blocking wait | N/A (streaming pipelines rarely have human gates) |
| **Configuration** | Environment-resolved config dataclass | Config file reader (YAML/env) | Environment variables + config map |
| **Quality gate** | Conditional edge based on quality metric in state | If-check after validation function | Metric check with circuit breaker |
| **Test** | pytest functions testing node state transitions | pytest functions testing function I/O | pytest functions testing handler behavior |

### 8.3 Shared Component Resolution

When a stage references a shared component (prefixed with `shared:` in the tools section), the Builder:

1. Looks up the component in `agent.yaml`'s `dependencies.shared_components`
2. Finds the component's `usage_type` — `import`, `extend`, or `wrap`
3. If `import`: generates an import statement for the shared component from the shared folder
4. If `extend`: generates a subclass that extends the component with agent-specific behavior
5. If `wrap`: generates an adapter class that wraps the component's interface

The Builder does **not** copy shared component code into the agent's directory. It always generates references to the shared folder.

### 8.4 Test Suite Generation from Contracts

For every contract, the Builder generates:

1. **Schema tests** — verify all required fields exist with correct types
2. **Per-field quality tests** — one test per `quality_rules` entry per field
3. **Aggregate quality tests** — one test per `aggregate_rules` entry
4. **Synthetic data generators** — test fixtures that produce data matching the contract's schema

For pipeline integration and quality gate tests, the Builder generates:

1. **Gate enforcement tests** — verify that quality gates halt execution or quarantine invalid records when thresholds are breached
2. **Metadata telemetry tests** — verify that lifecycle callbacks write execution and quality records to metadata framework Tier 1 and Tier 2 tables
3. **SLA verification tests** — verify that latency and freshness assertions trigger alerts when operational bounds are exceeded

### 8.5 Bespoke Logic Generation via Leaf Coder

When processing stages in pipeline order:
1. **Hook Detection**: The Builder inspects each stage for `stage.transform_handler: "<path>::<func>"` (e.g., `"handlers/enrich_customer.py::compute_tiers"`).
2. **Deterministic Skeleton Generation**: The Deterministic Scaffold Compiler generates the StateGraph node and wiring, importing the handler dynamically (`from src.handlers.enrich_customer import compute_tiers`).
3. **State Projection**: The Builder extracts a minimal, laser-focused state projection containing ONLY:
   - Input contract Pydantic schema (`input.contract`)
   - Output contract Pydantic schema (`output.contract`)
   - Stage transformation requirements (`processing.logic_description`)
   - Declared tools/libraries (`processing.tools`)
4. **Leaf Coder Synthesis**: The **LLM Leaf Coder** receives this narrow context and synthesizes the isolated, version-controlled handler function (`src/handlers/enrich_customer.py`) and corresponding unit tests (`tests/unit/test_enrich_customer.py`).
5. **Decoupled Evolution**: Orchestration skeleton and contracts remain strictly immutable (`# DO NOT EDIT`), while custom imperative logic in `src/handlers/` is fully maintainable and editable by human engineers.

---

## 9. A Complete Example

The following is a complete implementation document set for the **Customer Data Pipeline** agent. All files are shown in full. This is the exact set of documents the Documentarian would produce and the Builder would consume.

> [!NOTE]
> The complete schemas for all documents in this example have already been provided inline in Section 4 above. This section confirms the full file list and summarizes the inter-document references for the Customer Data Pipeline, rather than duplicating the YAML. Refer to:
> - [Section 4.1](#41-agent-manifest--agentyaml) for `agent.yaml`
> - [Section 4.2](#42-stage-documents--stagesstage_idyaml) for `stages/stage_ingest.yaml`, `stages/stage_validate.yaml`, `stages/stage_enrich.yaml`
> - [Section 4.3](#43-contract-documents--contractsnameyaml) for `contracts/customer_raw.yaml`, `contracts/customer_validated.yaml`
> - [Section 4.4](#44-configuration-document--configyaml) for `config.yaml`
> - [Section 4.5](#45-test-document--testsyaml) for `tests.yaml`

### Complete File List

```
customer_data_pipeline/
├── agent.yaml                          # Agent manifest (Section 4.1)
├── stages/
│   ├── stage_ingest.yaml               # Ingestion stage (Section 4.2 — first example)
│   ├── stage_validate.yaml             # Validation stage (Section 4.2 — second example)
│   ├── stage_enrich.yaml               # Enrichment stage with Leaf Coder hook (Section 4.2 — third example)
│   └── stage_publish.yaml              # Publication stage (not shown — follows same pattern)
├── contracts/
│   ├── customer_raw.yaml               # Raw customer contract (Section 4.3 — first example)
│   ├── customer_validated.yaml         # Validated customer contract (Section 4.3 — second example)
│   └── customer_enriched.yaml          # Enriched customer contract (not shown — follows same pattern)
├── config.yaml                         # Configuration (Section 4.4)
└── tests.yaml                          # Test specifications (Section 4.5)
```

### Cross-Reference Integrity Map

```
agent.yaml
  ├── pipeline.stages[0].stage_id: ingest ──────────── stages/stage_ingest.yaml ✓
  ├── pipeline.stages[1].stage_id: validate ────────── stages/stage_validate.yaml ✓
  ├── pipeline.stages[2].stage_id: enrich ──────────── stages/stage_enrich.yaml ✓
  ├── pipeline.stages[3].stage_id: publish ─────────── stages/stage_publish.yaml ✓
  ├── dependencies.tools[0]: great_expectations ────── used_by: [validate] ✓
  ├── dependencies.tools[1]: boto3 ─────────────────── used_by: [ingest] ✓
  ├── dependencies.tools[2]: snowflake-connector ───── used_by: [publish] ✓
  └── dependencies.shared_components[0]: audit_logger_v1 used_by: [validate, enrich, publish] ✓

stages/stage_ingest.yaml
  ├── input.contract: customer_raw ─────────────────── contracts/customer_raw.yaml ✓
  ├── output.contract: customer_raw ────────────────── contracts/customer_raw.yaml ✓
  └── ${config.sources.customer_csv.uri} ───────────── config.yaml → defaults.sources.customer_csv.uri ✓

stages/stage_validate.yaml
  ├── input.contract: customer_raw ─────────────────── contracts/customer_raw.yaml ✓
  ├── output.contract: customer_validated ──────────── contracts/customer_validated.yaml ✓
  └── ${config.destinations.quarantine.uri} ────────── config.yaml → defaults.destinations.quarantine.uri ✓

stages/stage_enrich.yaml
  ├── input.contract: customer_validated ───────────── contracts/customer_validated.yaml ✓
  ├── output.contract: customer_enriched ──────────── contracts/customer_enriched.yaml ✓
  └── stage.transform_handler: handlers/enrich_customer.py::compute_tiers ✓
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
| Every `${config.*}` must resolve in `config.yaml` | Config path resolution |
| Every `${secrets.*}` must appear in `config.yaml`'s `secrets` list | Secret registration |
| Contract `extends` must reference an existing contract | Inheritance resolution |
| Pipeline `next` must reference a valid `stage_id` or be `null` | Stage reference check |
| `stage.transform_handler` string must conform to `<filepath>::<function_name>` pattern | Hook format validation |
| `stage.transform_handler` target module must match callable signature against stage contracts | Hook interface integrity |

### 10.3 Completeness

| Rule | Check |
|---|---|
| Every contract field must have a `description` | Human readability |
| Every stage must have at least one error strategy | No unhandled errors |
| If `transform_handler` is declared, `processing.logic_description` must be present and non-empty | Leaf Coder prompt completeness |
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
| Bespoke transformation stages must explicitly specify either a `transform_handler` or a shared component tool | Transformation logic explicitness |
| Every configuration reference resolves to a value or a secret — no dangling references | Configuration completeness |

---

## 11. Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Document format | Pure YAML with rich `description` fields | Same dual-audience pattern as OpenAPI, dbt, Kubernetes manifests. Machine-parseable structure + human-readable descriptions. No mixed formats. |
| 2 | Multi-file vs single file | Multiple files per agent (5-document suite) | Cognitive load management, change isolation, Builder context efficiency, clean git diffs. Each file has one responsibility. |
| 3 | Runtime-agnostic stage specs | Stages specify WHAT, not HOW | The same impl doc works for LangGraph, Python pipeline, and streaming runtimes. Adding a new runtime means adding a Builder strategy, not changing the spec format. |
| 4 | Contract inheritance (`extends`) | Supported via named reference | Validated contracts add fields to raw contracts. Avoids field duplication across related contracts while maintaining explicit schema. |
| 5 | Configuration externalization | All values via `${config.*}` and `${secrets.*}` syntax | Nothing hardcoded. Same impl doc set deploys across dev/staging/production via environment overrides. Operators know exactly what secrets to provision. |
| 6 | Data Sensitivity & Tagging on Contracts | Classification and column tags declared in `contracts/*.yaml` | Contracts define data classification (`confidential`, `restricted`) and semantic tags (`pii`, `phi`). Read by DCM to emit native database column tags for platform dynamic masking. |
| 7 | Streamlined 5-Document Suite | Retire standalone `governance.yaml` | Governance is an emergent property of contracts, quality gates, and metadata framework callbacks. Eliminates artificial separation and unifies rules with contracts and stages. |
| 8 | Test derivation | Tests declared in `tests.yaml`, auto-generated from contracts | Builder generates schema tests, quality rule tests, and pipeline integration tests mechanically. Custom tests cover edge cases contracts can't express. |
| 9 | Quality rules at field AND aggregate level | Both supported | Field rules catch individual bad records. Aggregate rules catch batch-level anomalies (too few records, too many nulls). Both are needed for complete data quality. |
| 10 | Cross-document references by name | Named string references, not file paths | `contract: customer_raw` is cleaner than `contract: ./contracts/customer_raw.yaml`. The mapping from name to file is a convention the Builder knows. |
| 11 | `${config.*}` syntax for externalization | Template variable syntax parsed at build/deploy time | Familiar pattern from Docker Compose, Helm, Spring Boot. Builder generates code that reads config; it doesn't inline values. |
| 12 | Error strategy per-scenario | Multiple error strategies per stage, keyed by scenario | Different error types need different handling. Source unavailable needs retry. Parse errors need quarantine. Tool errors need fail. One-size-fits-all error handling is insufficient. |
| 13 | Platform-Managed Data Protection | Enterprise policy consumer model (Collibra, Snowflake DDM) | Individual pipeline agents consume central governance rather than compiling bespoke regex masking scripts in Python. Tokenization happens at the ingestion edge or dynamic warehouse masking. |
| 14 | Performance expectations in stage specs | `expected_duration`, `timeout`, `expected_record_count` | Builder generates monitoring, alerting, and timeout code from these. Without them, the Builder would have to guess or skip observability. |
| 15 | Single source of truth post-build | `impl_docs/` committed to agent Git repo; `spec.yaml` retired | Eliminates dual-spec drift. `spec.yaml` is strictly Day-0 intake. Day-2 enhancements operate directly on `impl_docs/*.yaml` via Git PR or Chainlit, avoiding competing sources of truth. |
| 16 | Leaf Coder hook fields (`transform_handler`, `processing.logic_description`) | Explicit YAML stage hooks for bespoke imperative logic | Preserves mathematical determinism in orchestration skeleton compilation while allowing isolated LLM synthesis for custom transformation handlers. Handlers remain version-controlled and editable without touching graph wiring. |
| 17 | Multi-Paradigm Execution Modes (`batch`, `chunked`, `row`) | Declared in `stage.yaml` (`processing.execution_mode`, `dataframe_engine`, `chunk_size`) | Prevents OOM and 500k-row latency bottlenecks. Vectorized DataFrame engines (Polars/Pandas) process bulk records with sub-second latency; chunked generators bind memory to $O(\text{chunk\_size})$ for row-level external calls; single-record mode serves streaming events. |

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
