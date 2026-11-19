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
> The entire `impl_docs/` package (`agent.yaml`, `stages/`, `contracts/`, `governance.yaml`, `config.yaml`, `tests.yaml`) is committed directly into the generated agent's Git repository.
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
│   └── stage_mask_pii.yaml
├── contracts/                    # One file per data contract
│   ├── customer_raw.yaml
│   └── customer_validated.yaml
├── governance.yaml               # All governance rules and enforcement points
├── config.yaml                   # All externalized configuration
└── tests.yaml                    # Test cases derived from contracts and governance
```

### Why Multiple Files Instead of One

| Concern | Why Separate Files |
|---|---|
| **Cognitive load** | A reviewer can open `stages/stage_validate.yaml` and understand the validation stage without scrolling through 500 lines of other content |
| **Change isolation** | When a contract changes, only its file and referencing stages change. The governance file may not need updating. |
| **Builder efficiency** | The Builder can generate code file-by-file, processing one stage at a time, keeping its context window focused |
| **Cross-document referencing** | Stages reference contracts by name. Governance rules reference stages by ID. The graph of references is explicit and auditable. |
| **Version control** | Git diffs show exactly which document changed and why. A contract change is clearly visible. |

### Document Inventory

| Document | Count | Responsibility |
|---|---|---|
| `agent.yaml` | Exactly 1 | Top-level agent identity, runtime, architecture pattern, shared component declarations |
| `stages/stage_<id>.yaml` | 1 per stage | Full specification of one pipeline stage — inputs, outputs, tools, error handling, governance hooks |
| `contracts/contract_<name>.yaml` | 1 per data contract | Field-level schema, quality rules per field, SLA definitions |
| `governance.yaml` | Exactly 1 | All governance rules, PII handling, audit requirements, human gate specs, compliance mappings |
| `config.yaml` | Exactly 1 | All externalized configuration, environment overrides, secret references |
| `tests.yaml` | Exactly 1 | Test cases auto-derivable from contracts and governance, plus custom test cases |

> [!TIP]
> **Blank Starter Templates Available**  
> Clean, unpopulated starter templates for all six documents are located in [`templates/`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates):
> - [`templates/agent.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/agent.yaml)
> - [`templates/stage.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/stage.yaml)
> - [`templates/contract.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/contract.yaml)
> - [`templates/governance.yaml`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/implementation-docs/templates/governance.yaml)
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
                                               # batch_processor, streaming_processor, multi_source_aggregator,
                                               # governance_heavy_pipeline
  runtime: "langgraph"                         # The runtime the Builder uses to select its generation strategy.
                                               # Valid runtimes: langgraph, python_pipeline, streaming
  autonomy_level: "semi_autonomous"            # How much the agent can do without human intervention.
                                               # Valid levels: fully_autonomous, semi_autonomous, human_in_the_loop

# --- Stage Ordering -------------------------------------------
# Declares the pipeline topology. The Builder uses this to wire
# stages together in the correct sequence. Each stage_id must
# have a corresponding file in stages/stage_<id>.yaml.
pipeline:
  stages:
    - stage_id: ingest                         # References stages/stage_ingest.yaml
      next: validate                           # Linear flow: ingest → validate
    - stage_id: validate
      next: mask_pii
      on_error: quarantine_handler             # Conditional routing on validation failure
    - stage_id: mask_pii
      next: enrich
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
    - component_id: "pii_masker_v2"            # References component_registry in Tier 2 metadata
      version: "2.1.0"
      used_by: [mask_pii]
      usage_type: "import"                     # import (use as-is), extend (subclass), wrap (adapter)
      description: "Shared PII masking component — email, phone, SSN strategies"
    - component_id: "audit_logger_v1"
      version: "1.0.0"
      used_by: [validate, mask_pii, publish]
      usage_type: "import"
      description: "Shared audit logging component — writes to governance_audit table"

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

Each stage document fully specifies one pipeline stage. It declares the stage's purpose, its inputs and outputs (as contract references), custom transformation hooks, the tools it uses, how it handles errors, and what governance rules apply to it.

#### Stage Specification Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `stage.id` | string | Yes | Unique stage identifier. Must match `pipeline.stages[].stage_id` in `agent.yaml`. |
| `stage.name` | string | Yes | Human-readable stage name for display and logging. |
| `stage.type` | enum | Yes | Stage category informing code template selection: `ingestion`, `validation`, `transformation`, `enrichment`, `publication`, `governance`, `aggregation`, `notification`. |
| `stage.description` | string | Yes | Plain English explanation of stage responsibility and business intent. |
| `stage.autonomy` | enum | Yes | Autonomy level: `autonomous`, `supervised`, `human_required`. |
| `stage.transform_handler` | string | Optional | Hook path (`"handlers/<mod>.py::<func>"`) dispatching execution to an isolated Python handler function synthesized by the **LLM Leaf Coder**. Used for bespoke imperative logic. |
| `input.contract` | string | Required* | Reference to input contract in `contracts/`. (*Required for non-ingestion stages). |
| `input.source` | object | Optional | Source connection details (for ingestion stages). |
| `output.contract` | string | Required* | Reference to output contract in `contracts/`. (*Required for non-publication stages). |
| `output.destination` | object | Optional | Destination connection details (for publication stages). |
| `processing.logic_description` | string | Conditional | Algorithmic, step-by-step description of transformations and rules. **Required** when `transform_handler` is declared; projected directly into the Leaf Coder's prompt. |
| `processing.tools` | list | Optional | Tools and libraries invoked within this stage. |
| `processing.shared_components` | list | Optional | Reusable shared components referenced by the stage. |
| `error_strategy` | object | Yes | Handlers for failure scenarios (`action`: retry, quarantine, skip, fail, warn, alert). |
| `governance_hooks` | list | Optional | References to governance rules in `governance.yaml` with explicit `enforcement_point`. |
| `performance` | object | Optional | Operational SLA expectations (`expected_record_count`, `expected_duration_seconds`, `timeout_seconds`). |

```yaml
# ============================================================
# stages/stage_ingest.yaml — Ingestion Stage Specification
# ============================================================
# Fully specifies the ingestion stage of the Customer Data
# Pipeline. The Builder reads this to generate the stage's
# code, error handling, and governance hooks.
# ============================================================

stage:
  id: ingest                                    # Unique stage identifier. Must match pipeline.stages[].stage_id in agent.yaml.
  name: "Ingest Customer CSV"                   # Human-readable stage name.
  type: ingestion                               # Stage type category. Informs Builder's code template selection.
                                                # Valid types: ingestion, validation, transformation, enrichment,
                                                # publication, governance, aggregation, notification
  description: >
    Pulls daily CSV files from the S3 raw data bucket.
    Handles schema evolution by detecting new columns and logging
    them as warnings rather than failing. Supports both single-file
    and multi-file ingestion (daily partitioned directories).
    Empty file drops are treated as no-ops with an info log, not errors.

  # --- Autonomy -----------------------------------------------
  autonomy: autonomous                          # This stage runs without human intervention.
                                                # Valid levels: autonomous, supervised, human_required

  # --- Input Specification ------------------------------------
  input:
    source:
      type: "s3"                                # Source system type. Builder generates the appropriate reader.
                                                # Valid types: s3, gcs, azure_blob, database, api, kafka, local_file
      uri: "${config.sources.customer_csv.uri}" # Externalized — references config.yaml. NEVER hardcoded.
      format: "csv"                             # File format. Valid: csv, parquet, json, jsonl, avro, orc
      options:                                  # Format-specific read options
        delimiter: ","
        header: true
        encoding: "utf-8"
        quote_char: '"'
        null_values: ["", "NULL", "N/A"]
    contract: customer_raw                      # References contracts/customer_raw.yaml. The input data must
                                                # conform to this contract AFTER ingestion. Validation happens
                                                # in the next stage — this contract defines the expected schema.

  # --- Output Specification -----------------------------------
  output:
    destination:
      type: "database"                          # Destination system type.
      uri: "${config.destinations.staging.uri}"  # Externalized connection string
      table: "staging.customer_raw"             # Target table name
      write_mode: "append"                      # Valid: append, overwrite, upsert, merge
    contract: customer_raw                      # The output conforms to the same contract as input for this stage.

  # --- Tools --------------------------------------------------
  tools:
    - name: "boto3"
      purpose: "Read CSV files from S3 bucket"
      configuration:                            # Tool-specific config — all values externalized
        region: "${config.aws.region}"
        assume_role: "${config.aws.s3_read_role_arn}"

  # --- Error Handling -----------------------------------------
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

  # --- Governance Hooks ---------------------------------------
  # References governance rules defined in governance.yaml.
  # The Builder generates enforcement code at the specified points.
  governance_hooks:
    - rule_id: audit_data_access                # Must exist in governance.yaml
      enforcement_point: "after_read"           # When in the stage's execution this rule fires
      description: "Log source access to audit trail after reading from S3"
    - rule_id: track_record_counts
      enforcement_point: "after_write"
      description: "Record input/output row counts for reconciliation"

  # --- Performance Expectations --------------------------------
  performance:
    expected_record_count:
      min: 0                                    # Empty drops are valid
      max: 500000                               # Alert if exceeds this — possible duplicate drop
      alert_on_exceed: true
    expected_duration_seconds: 300               # 5-minute SLA for ingestion
    timeout_seconds: 600                         # Hard timeout — kill after 10 minutes
```

**A second stage example — validation with richer logic:**

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
    contract: customer_raw                      # The contract defines the quality rules to check

  output:
    destination:
      type: "database"
      uri: "${config.destinations.staging.uri}"
      table: "staging.customer_validated"
      write_mode: overwrite                     # Each run replaces validated records
    contract: customer_validated                # Output conforms to a stricter contract

  tools:
    - name: "great_expectations"
      purpose: "Execute data quality validation suite"
      configuration:
        data_context_root: "${config.tools.great_expectations.context_root}"
        checkpoint_name: "customer_raw_quality"

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

  governance_hooks:
    - rule_id: audit_quarantined_records
      enforcement_point: "after_quarantine"
      description: "Log all quarantined records to governance_audit with reason codes"
    - rule_id: quality_gate
      enforcement_point: "after_validation"
      description: "Check quality score against threshold — fail stage if below minimum"

  performance:
    expected_record_count:
      min: 0
      max: 500000
    expected_duration_seconds: 120
    timeout_seconds: 300

  # --- Quality Gate (stage-level) -----------------------------
  # A quality gate that determines whether the stage passes.
  # This is different from per-record validation — this is a
  # stage-level decision based on aggregate quality metrics.
  quality_gate:
    metric: "quality_score"                     # Computed as: valid_records / total_records
    threshold: 0.95                             # 95% of records must pass validation
    on_failure:
      action: fail                              # Fail the stage if quality is below threshold
      alert: true
      alert_channel: "${config.alerts.channel}"
```

**A third stage example — PII masking with shared component:**

```yaml
# ============================================================
# stages/stage_mask_pii.yaml — PII Masking Stage Specification
# ============================================================

stage:
  id: mask_pii
  name: "Mask PII Fields"
  type: transformation
  description: >
    Applies PII masking to sensitive fields before data
    moves to the analytics warehouse. Uses the shared
    pii_masker_v2 component. Masking strategies are field-specific:
    email gets domain-preserving hashing, phone gets partial redaction,
    SSN gets full replacement. Original values are never persisted
    past this stage.

  autonomy: autonomous

  input:
    source:
      type: "database"
      uri: "${config.destinations.staging.uri}"
      table: "staging.customer_validated"
    contract: customer_validated

  output:
    destination:
      type: "database"
      uri: "${config.destinations.staging.uri}"
      table: "staging.customer_masked"
      write_mode: overwrite
    contract: customer_masked

  tools:
    - name: "shared:pii_masker_v2"              # Prefixed with "shared:" to indicate a shared component
      purpose: "Apply field-level PII masking strategies"
      configuration:
        masking_key: "${secrets.pii_masking_key}"    # Secret reference — never a literal value
        strategies:                                   # Maps fields to masking functions in the shared component
          email: "domain_preserving_hash"
          phone: "partial_redact_last_4"
          ssn: "full_replace_token"

  error_strategy:
    on_masking_failure:
      action: fail                              # PII masking failures are NEVER quarantined — they block the pipeline.
      details: >                                # Unmasked PII must never reach the warehouse.
        PII masking failed. Pipeline halted to prevent unmasked PII
        from reaching downstream destinations. Manual intervention required.

  governance_hooks:
    - rule_id: pii_masking_verification
      enforcement_point: "after_masking"
      description: "Verify all PII fields are masked — scan output for unmasked patterns"
    - rule_id: audit_pii_masking
      enforcement_point: "after_write"
      description: "Log masking operation details to governance_audit"

  performance:
    expected_record_count:
      min: 0
      max: 500000
    expected_duration_seconds: 60
    timeout_seconds: 180
```

**A fourth stage example — bespoke transformation with Leaf Coder hook (`transform_handler` and `processing.logic_description`):**

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
    Enriches validated customer records with loyalty tiers and calculate_date.
    Uses bespoke scoring logic based on credit scores and lifetime spend.

  autonomy: autonomous

  input:
    contract: customer_validated

  output:
    contract: customer_enriched

  processing:
    logic_description: >
      Calculate loyalty tier based on credit_score and historical purchases:
      - Tier Platinum: credit_score >= 750 AND lifetime_spend > 10000
      - Tier Gold: credit_score >= 700 OR lifetime_spend > 5000
      - Tier Silver: All other active customers
      Add calculate_date timestamp as current UTC time.

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

  governance_hooks:
    - rule_id: audit_enrichment_tiers
      enforcement_point: "after_write"
      description: "Log enriched tier distribution metrics to governance_audit"

  performance:
    expected_record_count:
      min: 0
      max: 500000
    expected_duration_seconds: 60
    timeout_seconds: 180
```

---

### 4.3 Contract Documents — `contracts/contract_<name>.yaml`

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
    and PII masking. Fields match the source system's export format.
  direction: internal                           # Valid: input (external source), output (external sink), internal (between stages)
  owner: "data-engineering-team"

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
    validation rules. Contains the same fields as customer_raw
    plus validation metadata. PII fields are still present
    (masking happens in the next stage).
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

### 4.4 Governance Document — `governance.yaml`

The governance document centralizes all governance rules, PII declarations, audit requirements, human gate specifications, and compliance mappings. Every governance hook referenced in stage documents must be defined here.

```yaml
# ============================================================
# governance.yaml — Governance Rules and Enforcement
# ============================================================
# All governance rules for the Customer Data Pipeline.
# Each rule declares WHAT must happen and WHERE (which stages).
# The Builder generates enforcement code at each enforcement point.
# ============================================================

governance:
  # --- PII Management -----------------------------------------
  pii:
    description: >
      PII handling policy for the Customer Data Pipeline.
      All PII fields must be masked before data leaves the staging
      layer. Original PII values are never persisted in the
      analytics warehouse or any downstream system.
    fields:                                     # Master list of all PII fields across all contracts
      - field_name: email
        pii_category: email_address
        contracts: [customer_raw, customer_validated]     # Which contracts contain this PII field
        masking_strategy: domain_preserving_hash          # How this field is masked
        masking_stage: mask_pii                           # Which stage performs the masking
        description: "Email addresses are hashed preserving domain for analytics grouping"
      - field_name: phone
        pii_category: phone_number
        contracts: [customer_raw, customer_validated]
        masking_strategy: partial_redact_last_4
        masking_stage: mask_pii
        description: "Phone numbers have last 4 digits preserved, rest redacted"
    verification:
      enabled: true
      method: "pattern_scan"                    # After masking, scan output for unmasked PII patterns
      patterns:                                 # Regex patterns that should NOT appear in post-masking output
        - name: "email_pattern"
          regex: "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"
        - name: "phone_pattern"
          regex: "\\+?[1-9]\\d{7,14}"
      on_detection:
        action: fail                            # Pipeline MUST fail if unmasked PII detected post-masking
        alert: true
        alert_channel: "${config.alerts.pii_channel}"

  # --- Audit Trail Requirements --------------------------------
  audit:
    description: >
      All significant operations are logged to the governance_audit
      table in the metadata framework (Tier 2). Audit entries are
      immutable — append-only, never updated or deleted.
    rules:
      - rule_id: audit_data_access
        description: "Log every source data access (reads from external systems)"
        event_type: "DATA_ACCESS"
        trigger: "after_read"
        logged_fields:
          - source_uri
          - record_count
          - timestamp
          - stage_id
          - run_id
        stages: [ingest]                        # Which stages this rule applies to

      - rule_id: audit_quarantined_records
        description: "Log every quarantined record with reason codes"
        event_type: "DATA_QUARANTINE"
        trigger: "after_quarantine"
        logged_fields:
          - record_id
          - quarantine_reason
          - failed_rules
          - timestamp
          - stage_id
          - run_id
        stages: [validate]

      - rule_id: audit_pii_masking
        description: "Log PII masking operations — which fields, which strategy, record count"
        event_type: "PII_MASKING"
        trigger: "after_masking"
        logged_fields:
          - masked_fields
          - masking_strategies
          - record_count
          - timestamp
          - stage_id
          - run_id
        stages: [mask_pii]

      - rule_id: track_record_counts
        description: "Log input/output record counts at every stage for reconciliation"
        event_type: "RECORD_COUNT"
        trigger: "after_write"
        logged_fields:
          - records_in
          - records_out
          - records_quarantined
          - timestamp
          - stage_id
          - run_id
        stages: [ingest, validate, mask_pii, enrich, publish]

  # --- Human Gate Specifications --------------------------------
  human_gates:
    description: >
      Human gates defined for this agent. These are points where
      the pipeline pauses and waits for human approval before
      proceeding. Different from the meta-agent's own human gates —
      these are gates IN the generated agent.
    gates: []                                   # This agent has no runtime human gates (fully automated pipeline).
                                                # A governance-heavy agent would declare gates here:
                                                # - gate_id: approve_pii_masking_config
                                                #   description: "Require approval before applying new masking strategies"
                                                #   stage: mask_pii
                                                #   trigger: "before_execution"
                                                #   approver_role: "data-governance-team"
                                                #   timeout_hours: 24
                                                #   on_timeout: fail

  # --- Quality Gates -------------------------------------------
  quality_gates:
    - gate_id: quality_gate
      description: "Validates that the quality score meets the minimum threshold"
      stage: validate
      metric: quality_score
      threshold: 0.95
      on_failure:
        action: fail
        alert: true
        alert_channel: "${config.alerts.channel}"

  # --- Compliance Mappings -------------------------------------
  # Maps regulatory or organizational compliance requirements
  # to their enforcement points in the pipeline.
  compliance:
    - requirement: "GDPR Article 5(1)(f) — Integrity and Confidentiality"
      description: "Personal data must be processed with appropriate security, including protection against unauthorized processing."
      enforced_by:
        - rule_id: pii_masking_verification
          stage: mask_pii
        - rule_id: audit_pii_masking
          stage: mask_pii
    - requirement: "Internal Policy DP-007 — Data Quality Monitoring"
      description: "All data pipelines must log quality metrics and maintain a minimum 95% quality score."
      enforced_by:
        - rule_id: quality_gate
          stage: validate
        - rule_id: track_record_counts
          stage: validate
```

---

### 4.5 Configuration Document — `config.yaml`

The configuration document declares every externalized value the agent needs. Nothing is hardcoded in stage specifications or governance rules — all values reference entries in this file via `${config.*}` syntax. Environment-specific overrides allow the same impl doc set to deploy across dev, staging, and production.

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
      pii_channel: "${secrets.slack_pii_alert_webhook}"

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
    - name: slack_pii_alert_webhook
      description: "Slack webhook URL for PII-specific alerts"
      required: true
    - name: pii_masking_key
      description: "Encryption key used for PII hashing operations"
      required: true
```

---

### 4.6 Test Document — `tests.yaml`

The test document declares test cases that the Builder generates into executable test suites. Tests are derived from contracts (schema validation, quality rules) and governance rules (enforcement verification). The Documentarian may also add custom test cases for edge cases not captured by contracts.

```yaml
# ============================================================
# tests.yaml — Test Specifications
# ============================================================
# Test cases the Builder generates into executable test suites.
# Organized by source: contract-derived, governance-derived,
# and custom (manually specified by the Documentarian).
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
          auto_generate: true                   # Builder generates from contract field definitions

        - test_id: "customer_raw_customer_id_format"
          type: field_quality
          description: "Verify customer_id matches CUS-XXXXXXXX format"
          field: customer_id
          rule: regex
          auto_generate: true                   # Builder generates from field's quality_rules

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

  # --- Governance-Derived Tests --------------------------------
  # Tests that verify governance enforcement is working correctly.
  governance_tests:
    - test_id: "pii_masking_email"
      type: governance_enforcement
      description: "Verify email field is masked in output of mask_pii stage"
      governance_rule: pii_masking_verification
      stage: mask_pii
      assertion: "output must not contain any value matching email regex pattern"

    - test_id: "pii_masking_phone"
      type: governance_enforcement
      description: "Verify phone field is masked in output of mask_pii stage"
      governance_rule: pii_masking_verification
      stage: mask_pii
      assertion: "output must not contain any value matching phone regex pattern"

    - test_id: "audit_trail_data_access"
      type: governance_enforcement
      description: "Verify audit entry is created when ingest stage reads from S3"
      governance_rule: audit_data_access
      stage: ingest
      assertion: "governance_audit table must contain a DATA_ACCESS entry for this run_id"

    - test_id: "quarantine_audit_logging"
      type: governance_enforcement
      description: "Verify quarantined records are logged to governance_audit"
      governance_rule: audit_quarantined_records
      stage: validate
      assertion: "governance_audit table must contain DATA_QUARANTINE entries matching quarantined record count"

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
| `agent.yaml` → Component | Tier 2 `component_registry` | `dependencies.shared_components[].component_id` | `component_id: pii_masker_v2` |
| Stage → Contract | `contracts/contract_<name>.yaml` | `input.contract`, `output.contract` | `contract: customer_raw` → file `contracts/customer_raw.yaml` |
| Stage → Governance Rule | `governance.yaml` | `governance_hooks[].rule_id` | `rule_id: audit_data_access` → rule in `governance.yaml` |
| Stage → Tool | `agent.yaml` `dependencies.tools` | `tools[].name` | `name: great_expectations` |
| Stage → Config | `config.yaml` | `${config.<path>}` syntax | `${config.sources.customer_csv.uri}` |
| Stage → Secret | `config.yaml` `secrets` | `${secrets.<name>}` syntax | `${secrets.pii_masking_key}` |
| Contract → Contract | `contracts/contract_<name>.yaml` | `extends` | `extends: customer_raw` |
| Governance → Stage | `stages/stage_<id>.yaml` | `audit.rules[].stages[]` | `stages: [validate]` |
| Governance → Contract | `contracts/contract_<name>.yaml` | `pii.fields[].contracts[]` | `contracts: [customer_raw, customer_validated]` |
| Tests → Contract | `contracts/contract_<name>.yaml` | `contract_tests[].contract` | `contract: customer_raw` |
| Tests → Stage | `stages/stage_<id>.yaml` | `governance_tests[].stage`, `custom_tests[].stage` | `stage: mask_pii` |
| Tests → Governance Rule | `governance.yaml` | `governance_tests[].governance_rule` | `governance_rule: pii_masking_verification` |

### Consistency Rules

The Documentarian **must** maintain these invariants. The Builder **assumes** they hold.

1. **Every `stage_id` in `agent.yaml`'s pipeline must have a corresponding stage file.** If `agent.yaml` lists `stage_id: validate`, then `stages/stage_validate.yaml` must exist.

2. **Every contract referenced by a stage must have a corresponding contract file.** If a stage declares `input.contract: customer_raw`, then `contracts/customer_raw.yaml` must exist.

3. **Every `governance_hooks[].rule_id` in a stage must be defined in `governance.yaml`.** No orphan references.

4. **Every `${config.*}` reference in any document must resolve to a path in `config.yaml`.** The Builder does not invent configuration values.

5. **Every `${secrets.*}` reference must appear in `config.yaml`'s `secrets` list.** This documents the required secrets for operators.

6. **Contract `extends` references must point to an existing contract.** Circular extends chains are forbidden.

7. **Every PII field declared in `governance.yaml` must exist in the contracts it references.** If governance declares `email` as PII in `customer_raw`, then `customer_raw.yaml` must have a field named `email` with `pii: true`.

8. **Every stage referenced in `governance.yaml`'s rules must exist in the pipeline.** If a rule applies to `stages: [validate]`, then `validate` must be a valid stage ID.

9. **Pipeline `next` references must point to valid stage IDs or `null`.** No broken links in the execution chain.

10. **The `on_error` route in `agent.yaml` must reference a valid stage ID or error handler.** Error routing paths must be resolvable.

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

## 7. Governance Specification Detail

### 7.1 PII Field Declarations and Masking Strategies

Every PII field must be declared at two levels:

1. **In the contract** — the field itself is marked `pii: true` with a `pii_category`
2. **In governance.yaml** — the `pii` section declares the masking strategy and the stage where masking occurs

This dual declaration is intentional. The contract says "this field IS PII." The governance doc says "here is HOW we handle it." The Builder cross-references both to generate masking code and verification code.

Supported masking strategies:

| Strategy | Description | When to Use |
|---|---|---|
| `domain_preserving_hash` | SHA-256 hash of value, preserving domain (e.g., email domain preserved) | When analytics needs grouping by domain but not individual identity |
| `partial_redact_last_4` | Keeps last 4 characters, replaces rest with `*` | Phone numbers, SSNs where partial value has analytics utility |
| `full_replace_token` | Replaces entire value with a deterministic token | SSNs, IDs where no partial value should be visible |
| `full_redact` | Replaces entire value with `[REDACTED]` | When no value should be visible at all |
| `date_generalize` | Rounds dates to month or year | Birth dates where approximate age is sufficient |
| `k_anonymize` | Ensures at least k identical records per quasi-identifier combination | Research datasets with privacy requirements |

### 7.2 Audit Trail Requirements

Every audit rule in `governance.yaml` specifies:

- **What event is logged** (`event_type`) — a category string like `DATA_ACCESS`, `PII_MASKING`, `DATA_QUARANTINE`
- **When it fires** (`trigger`) — `after_read`, `after_write`, `after_quarantine`, `after_masking`, `before_execution`, `after_execution`
- **What data is captured** (`logged_fields`) — the specific fields recorded in the `governance_audit` table's `details` JSONB column
- **Which stages** (`stages[]`) — where this rule is enforced

The Builder generates code at each enforcement point that writes to the `ddlc_shared_metadata.governance_audit` table via the metadata framework's callback hooks.

### 7.3 Human Gate Specifications

Human gates in the generated agent (not the meta-agent's own gates) are declared in `governance.yaml`'s `human_gates` section. Each gate specifies:

| Property | Type | Description |
|---|---|---|
| `gate_id` | string | Unique identifier for this gate |
| `description` | string | What the approver is reviewing |
| `stage` | string | Which stage this gate precedes or follows |
| `trigger` | enum | `before_execution` or `after_execution` |
| `approver_role` | string | Who is authorized to approve |
| `timeout_hours` | integer | How long to wait before timeout action |
| `on_timeout` | enum | `fail`, `auto_approve`, `escalate` |
| `presentation` | object | What information is shown to the approver (fields, summaries, samples) |

### 7.4 Compliance Rule Mappings

The `compliance` section in `governance.yaml` creates traceability from regulatory requirements to enforcement code:

```
Compliance Requirement → enforced_by → Governance Rule → enforcement_point → Stage → Generated Code
```

This chain is auditable. An auditor can ask "How does this pipeline comply with GDPR Article 5(1)(f)?" and trace the answer:

1. `compliance[0].requirement` = "GDPR Article 5(1)(f)"
2. `compliance[0].enforced_by[0]` = rule `pii_masking_verification` in stage `mask_pii`
3. Stage `mask_pii` has governance hook `pii_masking_verification` at `after_masking`
4. The Builder generated verification code that scans for unmasked PII patterns
5. The audit trail has entries proving the verification ran on every execution

---

## 8. How the Builder Reads Impl Docs

The Builder specialist receives the approved implementation documents as its **entire projected context**. It does not see the original spec, clarification Q&A, research findings, or architecture proposals. The impl docs are the Builder's world.

### 8.1 Builder Processing Order

1. **Read `agent.yaml`** — determine the runtime and generation strategy
2. **Read `config.yaml`** — understand the configuration surface (for generating config readers)
3. **Read all contracts** — generate data models (Pydantic, DDL) and validation code
4. **Read `governance.yaml`** — understand enforcement requirements across all stages
5. **Read each stage in pipeline order** — generate stage code, wiring, error handling
6. **Read `tests.yaml`** — generate test suite
7. **Generate project scaffolding** — directory structure, entry point, requirements

### 8.2 Generation Strategy Mapping

The Builder selects a generation strategy based on `architecture.runtime` and maps impl doc concepts to runtime-specific constructs:

| Impl Doc Concept | LangGraph Strategy | Python Pipeline Strategy | Streaming Strategy |
|---|---|---|---|
| **Stage** | A node function registered in the state graph | A function called in sequential `main()` | A message handler class |
| **Pipeline wiring** (`next`, `on_error`) | Graph edges (fixed and conditional) | Sequential function calls with try/except | Kafka topic routing and error topics |
| **Contract** | Claim Check pointer (`ContractPointer` in `AgentState`) with Pydantic model validation at stage boundary | Pydantic model or dataclass for function I/O | Message schema (Avro/Pydantic) |
| **Error handling** (quarantine) | State update with error flag, conditional routing | Function return with error dataframe, logging | Dead-letter topic publishing |
| **Governance hook** | State machine gate node or decorator on node function | Decorator or explicit call in function body | Middleware interceptor on handler |
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

For governance-derived tests, the Builder generates:

1. **Enforcement verification tests** — verify that governance hooks fire and log correctly
2. **PII verification tests** — verify that masked output contains no unmasked PII patterns
3. **Audit trail tests** — verify that audit entries are created at enforcement points

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
> - [Section 4.2](#42-stage-documents--stagesstage_idyaml) for `stages/stage_ingest.yaml`, `stages/stage_validate.yaml`, `stages/stage_mask_pii.yaml`, `stages/stage_enrich.yaml`
> - [Section 4.3](#43-contract-documents--contractscontract_nameyaml) for `contracts/customer_raw.yaml`, `contracts/customer_validated.yaml`
> - [Section 4.4](#44-governance-document--governanceyaml) for `governance.yaml`
> - [Section 4.5](#45-configuration-document--configyaml) for `config.yaml`

### Complete File List

```
customer_data_pipeline/
├── agent.yaml                          # Agent manifest (Section 4.1)
├── stages/
│   ├── stage_ingest.yaml               # Ingestion stage (Section 4.2 — first example)
│   ├── stage_validate.yaml             # Validation stage (Section 4.2 — second example)
│   ├── stage_mask_pii.yaml             # PII masking stage (Section 4.2 — third example)
│   ├── stage_enrich.yaml               # Enrichment stage with Leaf Coder hook (Section 4.2 — fourth example)
│   └── stage_publish.yaml              # Publication stage (not shown — follows same pattern)
├── contracts/
│   ├── customer_raw.yaml               # Raw customer contract (Section 4.3 — first example)
│   ├── customer_validated.yaml         # Validated customer contract (Section 4.3 — second example)
│   └── customer_masked.yaml            # Masked customer contract (not shown — follows same pattern)
├── governance.yaml                     # Governance rules (Section 4.4)
├── config.yaml                         # Configuration (Section 4.5)
└── tests.yaml                          # Test specifications (Section 4.6)
```

### Cross-Reference Integrity Map

```
agent.yaml
  ├── pipeline.stages[0].stage_id: ingest ──────────── stages/stage_ingest.yaml ✓
  ├── pipeline.stages[1].stage_id: validate ────────── stages/stage_validate.yaml ✓
  ├── pipeline.stages[2].stage_id: mask_pii ────────── stages/stage_mask_pii.yaml ✓
  ├── pipeline.stages[3].stage_id: enrich ──────────── stages/stage_enrich.yaml ✓
  ├── pipeline.stages[4].stage_id: publish ─────────── stages/stage_publish.yaml ✓
  ├── dependencies.tools[0]: great_expectations ────── used_by: [validate] ✓
  ├── dependencies.tools[1]: boto3 ─────────────────── used_by: [ingest] ✓
  ├── dependencies.tools[2]: snowflake-connector ───── used_by: [publish] ✓
  ├── dependencies.shared_components[0]: pii_masker_v2  used_by: [mask_pii] ✓
  └── dependencies.shared_components[1]: audit_logger_v1 used_by: [validate, mask_pii, publish] ✓

stages/stage_ingest.yaml
  ├── input.contract: customer_raw ─────────────────── contracts/customer_raw.yaml ✓
  ├── output.contract: customer_raw ────────────────── contracts/customer_raw.yaml ✓
  ├── governance_hooks[0].rule_id: audit_data_access ── governance.yaml → audit.rules[0] ✓
  ├── governance_hooks[1].rule_id: track_record_counts  governance.yaml → audit.rules[3] ✓
  └── ${config.sources.customer_csv.uri} ───────────── config.yaml → defaults.sources.customer_csv.uri ✓

stages/stage_validate.yaml
  ├── input.contract: customer_raw ─────────────────── contracts/customer_raw.yaml ✓
  ├── output.contract: customer_validated ──────────── contracts/customer_validated.yaml ✓
  ├── governance_hooks[0].rule_id: audit_quarantined_records ── governance.yaml → audit.rules[1] ✓
  └── governance_hooks[1].rule_id: quality_gate ────── governance.yaml → quality_gates[0] ✓

stages/stage_mask_pii.yaml
  ├── input.contract: customer_validated ───────────── contracts/customer_validated.yaml ✓
  ├── output.contract: customer_masked ─────────────── contracts/customer_masked.yaml ✓
  ├── tools[0]: shared:pii_masker_v2 ──────────────── agent.yaml → shared_components[0] ✓
  ├── governance_hooks[0].rule_id: pii_masking_verification ── governance.yaml → pii.verification ✓
  └── ${secrets.pii_masking_key} ──────────────────── config.yaml → secrets[8] ✓

governance.yaml
  ├── pii.fields[0].contracts: [customer_raw, customer_validated] ── ✓ both exist
  ├── pii.fields[0].masking_stage: mask_pii ──────── stages/stage_mask_pii.yaml ✓
  ├── audit.rules[0].stages: [ingest] ────────────── stages/stage_ingest.yaml ✓
  ├── audit.rules[1].stages: [validate] ──────────── stages/stage_validate.yaml ✓
  ├── audit.rules[2].stages: [mask_pii] ──────────── stages/stage_mask_pii.yaml ✓
  └── compliance[0].enforced_by → pii_masking_verification → mask_pii ── ✓ chain complete
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
| Every `input.contract` and `output.contract` must have a corresponding `contracts/contract_<name>.yaml` | File existence per contract |
| Every `governance_hooks[].rule_id` must exist in `governance.yaml` | Governance rule lookup |
| Every `${config.*}` must resolve in `config.yaml` | Config path resolution |
| Every `${secrets.*}` must appear in `config.yaml`'s `secrets` list | Secret registration |
| Contract `extends` must reference an existing contract | Inheritance resolution |
| Pipeline `next` must reference a valid `stage_id` or be `null` | Stage reference check |
| PII fields in `governance.yaml` must exist in referenced contracts with `pii: true` | PII declaration consistency |
| Every stage referenced in governance rules must exist in the pipeline | Stage existence in governance context |
| `stage.transform_handler` string must conform to `<filepath>::<function_name>` pattern | Hook format validation |
| `stage.transform_handler` target module must match callable signature against stage contracts | Hook interface integrity |

### 10.3 Completeness

| Rule | Check |
|---|---|
| Every governance rule must have at least one enforcement point | No unenforced rules |
| Every PII field must have a masking strategy and masking stage | No unmasked PII |
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
| Every governance hook has an explicit enforcement point — not "somewhere in the stage" | Enforcement point explicitness |
| Every shared component reference has a `usage_type` — not "figure it out" | Component usage explicitness |
| Bespoke transformation stages must explicitly specify either a `transform_handler` or a shared component tool | Transformation logic explicitness |
| Every configuration reference resolves to a value or a secret — no dangling references | Configuration completeness |

---

## 11. Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Document format | Pure YAML with rich `description` fields | Same dual-audience pattern as OpenAPI, dbt, Kubernetes manifests. Machine-parseable structure + human-readable descriptions. No mixed formats. |
| 2 | Multi-file vs single file | Multiple files per agent | Cognitive load management, change isolation, Builder context efficiency, clean git diffs. Each file has one responsibility. |
| 3 | Runtime-agnostic stage specs | Stages specify WHAT, not HOW | The same impl doc works for LangGraph, Python pipeline, and streaming runtimes. Adding a new runtime means adding a Builder strategy, not changing the spec format. |
| 4 | Contract inheritance (`extends`) | Supported via named reference | Validated contracts add fields to raw contracts. Avoids field duplication across related contracts while maintaining explicit schema. |
| 5 | Configuration externalization | All values via `${config.*}` and `${secrets.*}` syntax | Nothing hardcoded. Same impl doc set deploys across dev/staging/production via environment overrides. Operators know exactly what secrets to provision. |
| 6 | PII dual declaration | Marked in contracts (`pii: true`) AND governance (`pii.fields[]`) | Contract says what IS PII. Governance says how to HANDLE PII. Different concerns, both needed for complete generation. |
| 7 | Governance as centralized doc | Single `governance.yaml` per agent | Governance rules need cross-cutting visibility — rules span stages, compliance mappings span rules. A single file makes this visible and auditable. |
| 8 | Test derivation | Tests declared in `tests.yaml`, many auto-generated from contracts | Builder generates schema tests, quality rule tests, and governance enforcement tests mechanically. Custom tests cover edge cases contracts can't express. |
| 9 | Quality rules at field AND aggregate level | Both supported | Field rules catch individual bad records. Aggregate rules catch batch-level anomalies (too few records, too many nulls). Both are needed for complete data quality. |
| 10 | Cross-document references by name | Named string references, not file paths | `contract: customer_raw` is cleaner than `contract: ./contracts/customer_raw.yaml`. The mapping from name to file is a convention the Builder knows. |
| 11 | `${config.*}` syntax for externalization | Template variable syntax parsed at build/deploy time | Familiar pattern from Docker Compose, Helm, Spring Boot. Builder generates code that reads config; it doesn't inline values. |
| 12 | Error strategy per-scenario | Multiple error strategies per stage, keyed by scenario | Different error types need different handling. Source unavailable needs retry. Parse errors need quarantine. Tool errors need fail. One-size-fits-all error handling is insufficient. |
| 13 | Masking strategies as named references | Strategy names map to shared component functions | The Builder doesn't need to understand masking algorithms — it generates code that calls `pii_masker.apply("domain_preserving_hash", value)`. The shared component owns the implementation. |
| 14 | Performance expectations in stage specs | `expected_duration`, `timeout`, `expected_record_count` | Builder generates monitoring, alerting, and timeout code from these. Without them, the Builder would have to guess or skip observability. |
| 15 | Single source of truth post-build | `impl_docs/` committed to agent Git repo; `spec.yaml` retired | Eliminates dual-spec drift. `spec.yaml` is strictly Day-0 intake. Day-2 enhancements operate directly on `impl_docs/*.yaml` via Git PR or Chainlit, avoiding competing sources of truth. |
| 16 | Leaf Coder hook fields (`transform_handler`, `processing.logic_description`) | Explicit YAML stage hooks for bespoke imperative logic | Preserves mathematical determinism in orchestration skeleton compilation while allowing isolated LLM synthesis for custom transformation handlers. Handlers remain version-controlled and editable without touching graph wiring. |

---

## 12. What This Document Does NOT Cover

This document defines the **schema and structure** of implementation documents. It does **not** cover:

- **How the Documentarian generates impl docs** — The prompts, reasoning, and iteration logic the Documentarian uses to translate architecture into YAML. That is a specialist implementation detail.
- **How the Builder generates code from impl docs** — The code templates, generation strategies, and file emission logic. That is defined in `builder.md`.
- **The spec template** — What fields the user fills in to bootstrap a new DDLC agent. That is defined in [`input-spec-template/spec_template.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/input-spec-template/spec_template.md).
- **The architecture proposal format** — What the Architect produces and the Documentarian reads. That is an architecture detail.
- **Governance enforcement implementation** — How governance hooks become actual code at runtime. That is defined in `governance.md`.
- **DCM Schema Engine internals** — How YAML contracts compile to DDL. That is defined in [`dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/dcm-agent/dcm_agent.md).
- **Metadata framework table schemas** — The DDL for `governance_audit`, `stage_execution`, etc. That is defined in [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md).
- **Prompt engineering** — The actual LLM prompts used by the Documentarian or Builder specialists.
- **Versioning and migration of impl docs** — How impl docs evolve across agent versions. Deferred to a future operational design document.
- **Multi-agent impl doc coordination** — How impl docs for agents that depend on each other are kept consistent. Deferred to the change management design.
