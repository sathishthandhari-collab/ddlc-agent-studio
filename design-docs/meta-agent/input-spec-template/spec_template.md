# DDLC Meta-Agent — Specification Template

## The YAML Spec That Starts Every Build

> **Document Status**: Draft v0.1  
> **Last Updated**: 2026-11-16  
> **Audience**: Data Engineers (who fill it in), Meta-Agent Specialists (who parse and validate it)  
> **Prerequisite**: [architecture.md](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/architecture.md) — understand the system that consumes this template

---

## 1. Document Purpose

This document defines the **YAML specification template** — the starting input to the entire meta-agent pipeline. A data engineer fills in this template to describe the DDLC agent they want built. The meta-agent's Spec Analyst specialist then parses, validates, and enriches it.

### Who Uses This Template

| Role | How They Use It |
|---|---|
| **Data Engineer** | Fills in the template to describe the agent they want. Writes `description` fields in plain English. Selects from predefined enums where applicable. |
| **Spec Analyst** | Parses the YAML against a Pydantic schema. Extracts structured information. Identifies gaps — missing fields, ambiguities, contradictions, implicit assumptions. Generates clarifying questions. |
| **Researcher** | Reads the parsed spec's tool references, source/destination systems, and constraints to evaluate feasibility. |
| **Architect** | Reads the parsed spec's stages, autonomy levels, governance rules, and complexity indicators to select an architecture pattern. |
| **Documentarian** | References the spec's data contracts, governance rules, and stage definitions when producing implementation documents. |

### How It Flows Through the System

The spec enters the pipeline at the broadest point of the funnel (Section 5.1 of architecture.md). It is the rawest, most verbose input. Every subsequent phase narrows it:

```
Raw YAML Spec (this template, filled in)
    → Spec Analyst parses and validates
    → Clarification resolves gaps
    → Researcher evaluates feasibility
    → Architect selects pattern and designs
    → Documentarian produces impl docs
    → Builder generates code
```

The spec itself is never passed directly to the Builder. By the time construction begins, every decision in the spec has been refined into implementation documents. The spec's job is to capture **intent** — what the agent should do, not how it should be built.

### Lifecycle Role: Day-0 Bootstrapping Intake Form (Retired Post-Build)

> [!IMPORTANT]
> **Single Source of Truth Post-Build (Preventing Dual-Spec Drift)**  
> This specification template is strictly a **Day-0 bootstrapping intake document**. It exists to take a raw agent concept from human intent through clarification, feasibility research, and architectural design.
>
> Once Gate 3 (Implementation Document Review) is approved and the agent is delivered:
> 1. The multi-file **`impl_docs/` package** (`agent.yaml`, `stages/`, `contracts/`, `governance.yaml`, `config.yaml`, `tests.yaml`) is committed directly into the generated agent's Git repository as the **permanent, authoritative source of truth**.
> 2. **`spec.yaml` is retired** and archived in the metadata framework (Tier 1 artifact registry) as an immutable intake audit record.
> 3. **Day-2 Maintenance & Enhancements**: Developers make modifications directly to `impl_docs/*.yaml` (via Git Pull Requests or natural language prompts in Chainlit). Developers are **never** asked to maintain `spec.yaml` and `impl_docs/` in parallel.

---

## 2. Template Overview

The spec is organized into eight top-level sections, each targeting a different concern:

| Section | Purpose | Primary Consumer |
|---|---|---|
| **`agent`** | Identity — what this agent is, who owns it, what domain it serves | Spec Analyst, Metadata Framework |
| **`architecture`** | Preferences — autonomy level, complexity hints, runtime preferences | Architect |
| **`stages`** | Processing steps — ordered list of what the agent does, step by step | Spec Analyst, Architect, Documentarian |
| **`contracts`** | Data shapes — input/output schemas with field-level detail and quality rules | Spec Analyst, Documentarian, Validator |
| **`governance`** | Compliance — PII handling, audit requirements, approval gates, data retention | Architect, Documentarian, Validator |
| **`infrastructure`** | Systems — source/destination connections, scheduling, resource constraints | Researcher, Architect |
| **`cross_agent`** | Context — upstream/downstream dependencies, shared components to reuse | Architect, Metadata Framework |
| **`constraints`** | Guardrails — libraries to use/avoid, performance requirements, SLAs | Researcher, Architect, Builder |

```
spec.yaml
├── agent:              # WHO — identity, ownership, domain
├── architecture:       # HOW COMPLEX — autonomy, complexity, runtime hints
├── stages:             # WHAT — ordered processing steps
│   ├── stage[0]:       #   each stage has id, type, description, tools, contracts
│   ├── stage[1]:       #   ...
│   └── stage[N]:       #   ...
├── contracts:          # WHAT SHAPE — input/output data schemas
│   ├── contract[0]:    #   each contract has fields, types, quality rules
│   └── contract[N]:    #   ...
├── governance:         # WHAT RULES — PII, audit, approvals, retention
├── infrastructure:     # WHERE — sources, destinations, scheduling
├── cross_agent:        # WHO ELSE — dependencies, shared components
└── constraints:        # WHAT LIMITS — libraries, performance, SLAs
```

---

## 3. The Complete YAML Template

This is the full template. Every field includes an inline comment explaining its purpose, whether it is required or optional, and its valid values.

```yaml
# ============================================================================
# DDLC Agent Specification Template
# ============================================================================
# Fill in this template to describe the DDLC agent you want the meta-agent
# to build. Fields marked [REQUIRED] must be provided. Fields marked
# [OPTIONAL] can be omitted — the Spec Analyst will use defaults or ask
# clarifying questions.
#
# Description fields (using YAML > block scalars) should be written in
# plain English. Be specific about intent, edge cases, and business context.
# The more detail you provide, the fewer clarifying questions you will receive.
# ============================================================================


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 1: Agent Identity
# ──────────────────────────────────────────────────────────────────────────────
agent:
  name: ""                          # [REQUIRED] Snake_case identifier. Must be unique across the agent portfolio.
                                    # Example: "customer_data_pipeline"

  version: ""                       # [REQUIRED] Semantic version. Use "1.0.0" for new agents.
                                    # Example: "1.0.0"

  description: >                    # [REQUIRED] Plain English description of what this agent does,
                                    # why it exists, and what business problem it solves.
                                    # Be specific — this is the Spec Analyst's primary context.
    Describe the agent's purpose, the business problem it solves,
    and the high-level data flow in 2-5 sentences.

  owner: ""                         # [REQUIRED] Team or individual responsible for this agent.
                                    # Example: "data-engineering-team"

  domain: ""                        # [REQUIRED] Business domain this agent serves.
                                    # Valid values: customer, finance, product, marketing, operations, 
                                    # supply_chain, hr, compliance, analytics, infrastructure, other
                                    # Example: "customer"

  tags: []                          # [OPTIONAL] Freeform tags for discoverability.
                                    # Example: ["etl", "daily", "pii", "warehouse"]


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2: Architecture Preferences
# ──────────────────────────────────────────────────────────────────────────────
architecture:
  autonomy_level: ""                # [REQUIRED] How much human oversight does this agent need?
                                    # Valid values:
                                    #   "autonomous"       — runs end-to-end without human intervention
                                    #   "semi-autonomous"  — runs with human approval at key gates
                                    #   "supervised"       — requires human approval at every stage
                                    # Example: "semi-autonomous"

  complexity: ""                    # [OPTIONAL] Hint to the Architect about expected complexity.
                                    # Valid values:
                                    #   "simple"   — linear pipeline, few stages, no branching
                                    #   "moderate" — some branching, error recovery, multiple sources
                                    #   "complex"  — governance gates, conditional routing, multi-source
                                    #                aggregation, streaming, or real-time requirements
                                    # If omitted, the Spec Analyst infers from stages and governance.

  runtime_preference: ""            # [OPTIONAL] Preferred runtime for the generated agent.
                                    # Valid values:
                                    #   "langgraph"  — stateful agent with LangGraph state machine
                                    #   "python"     — simple Python script pipeline
                                    #   "streaming"  — event-driven streaming processor
                                    #   "no_preference" — let the Architect decide based on requirements
                                    # If omitted, defaults to "no_preference".
                                    # The Architect may override this if requirements demand it.

  description: >                    # [OPTIONAL] Additional architectural context or constraints
                                    # the Architect should be aware of.
    Any architectural preferences, patterns to follow or avoid,
    or technical context that influences the design.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 3: Processing Stages
# ──────────────────────────────────────────────────────────────────────────────
# Ordered list of processing stages. The agent executes these in sequence
# (unless the Architect determines parallelism is appropriate).
# Each stage must declare its type, what it does, and its input/output contracts.
# ──────────────────────────────────────────────────────────────────────────────
stages:
  - id: ""                          # [REQUIRED] Unique snake_case identifier for this stage.
                                    # Example: "ingest_raw_data"

    type: ""                        # [REQUIRED] Category of work this stage performs.
                                    # Valid values:
                                    #   "ingestion"       — pulls data from external source
                                    #   "validation"      — checks data quality and applies rules
                                    #   "transformation"  — reshapes, enriches, or derives data
                                    #   "governance"      — applies PII masking, access control, audit
                                    #   "aggregation"     — combines data from multiple sources
                                    #   "publishing"      — writes data to destination system
                                    #   "notification"    — sends alerts or status updates
                                    #   "monitoring"      — observes and reports on data/system state
                                    #   "custom"          — anything not covered above (describe in detail)

    description: >                  # [REQUIRED] What this stage does, in plain English.
                                    # Include: purpose, edge cases, expected behavior on failure.
      Describe what this stage does, why it exists,
      and any important edge cases or business rules.

    autonomy: ""                    # [REQUIRED] Stage-level autonomy.
                                    # Valid values: "autonomous", "human_approval", "automated_check"
                                    #   "autonomous"      — runs without pause
                                    #   "human_approval"  — pauses for human review before proceeding
                                    #   "automated_check" — runs an automated quality gate, no human needed

    input_contract: ""              # [REQUIRED] Name of the input data contract (defined in the
                                    # contracts section below). Must match a contract id exactly.
                                    # Example: "customer_raw"

    output_contract: ""             # [REQUIRED] Name of the output data contract.
                                    # Example: "customer_validated"

    tools: []                       # [OPTIONAL] Specific tools or libraries this stage should use.
                                    # The Researcher validates availability and compatibility.
                                    # Example: ["great_expectations", "pandas"]

    error_strategy: ""              # [OPTIONAL] How to handle errors in this stage.
                                    # Valid values:
                                    #   "fail_fast"       — stop the pipeline on first error
                                    #   "quarantine"      — isolate bad records, continue with good ones
                                    #   "retry"           — retry the operation (with backoff)
                                    #   "skip"            — log the error and skip to next stage
                                    #   "escalate"        — pause and alert a human
                                    # If omitted, defaults to "fail_fast".

    retry_config:                   # [OPTIONAL] Only relevant if error_strategy is "retry".
      max_retries: 3                # Maximum number of retry attempts.
      backoff_seconds: 60           # Seconds to wait between retries.

    description_details: >          # [OPTIONAL] Additional implementation-level details.
                                    # Use this for specific business rules, algorithms, or
                                    # transformations that need explicit documentation.
      Any additional detail: specific transformation logic,
      business rule formulas, lookup table references, etc.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 4: Data Contracts
# ──────────────────────────────────────────────────────────────────────────────
# Data contracts define the shape and quality expectations for data flowing
# between stages. Every stage references contracts by id. Contracts ensure
# the Documentarian and Validator can verify end-to-end data integrity.
# ──────────────────────────────────────────────────────────────────────────────
contracts:
  - id: ""                          # [REQUIRED] Unique snake_case identifier. Referenced by stages.
                                    # Example: "customer_raw"

    description: >                  # [REQUIRED] What this data represents and where it comes from.
      Describe the data this contract represents,
      its business meaning, and its source.

    direction: ""                   # [REQUIRED] How this contract is used in the pipeline.
                                    # Valid values: "input", "output", "internal"
                                    #   "input"    — data entering the agent from an external source
                                    #   "output"   — data leaving the agent to a destination
                                    #   "internal" — data passed between stages within the agent

    fields:                         # [REQUIRED] List of fields in this contract.
      - name: ""                    # [REQUIRED] Field name. Example: "customer_id"
        type: ""                    # [REQUIRED] Data type.
                                    # Valid values: "string", "integer", "float", "boolean",
                                    #   "date", "datetime", "timestamp", "json", "array", "binary"
        nullable: false             # [OPTIONAL] Whether this field can be null. Default: false.
        description: >              # [REQUIRED] What this field represents.
          Describe the business meaning of this field.

        quality_rules: []           # [OPTIONAL] Quality rules applied to this field.
                                    # Each rule is a string describing the check.
                                    # Examples:
                                    #   - "must be unique across the dataset"
                                    #   - "must match regex ^[A-Z]{2}\\d{6}$"
                                    #   - "must be a valid email address"
                                    #   - "must be within the last 24 hours"
                                    #   - "must reference a valid entry in the products table"
                                    #   - "must be positive"

    sla:                            # [OPTIONAL] Service-level expectations for this contract.
      max_records: null             # Maximum expected record count per batch. Null = no limit.
      min_records: null             # Minimum expected record count per batch. Null = no minimum.
      freshness_hours: null         # Data must not be older than this many hours. Null = no requirement.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 5: Governance
# ──────────────────────────────────────────────────────────────────────────────
governance:
  pii_fields: []                    # [REQUIRED if agent handles personal data, otherwise OPTIONAL]
                                    # List of field names (from any contract) that contain PII.
                                    # The Documentarian maps each to a masking/handling strategy.
                                    # Example: ["email", "phone_number", "social_security_number"]

  pii_strategy: ""                  # [REQUIRED if pii_fields is non-empty]
                                    # How PII fields should be handled.
                                    # Valid values:
                                    #   "mask"       — replace with masked values (e.g., "j***@e***.com")
                                    #   "hash"       — one-way hash (irreversible)
                                    #   "encrypt"    — reversible encryption (requires key management)
                                    #   "redact"     — remove entirely
                                    #   "tokenize"   — replace with surrogate tokens
                                    #   "mixed"      — different strategies per field (detail in description)

  audit_requirements:               # [OPTIONAL] What audit trail the agent must maintain.
    enabled: false                  # Whether audit logging is required. Default: false.
    log_level: ""                   # Valid values: "minimal", "standard", "comprehensive"
                                    #   "minimal"        — log stage start/end and errors only
                                    #   "standard"       — log stage transitions, record counts, quality scores
                                    #   "comprehensive"  — log everything including record-level lineage
    retention_days: null            # How many days to retain audit records. Null = permanent.

  approval_gates: []                # [OPTIONAL] Stages that require human approval before proceeding.
                                    # List of stage ids from the stages section.
                                    # Example: ["publish_to_warehouse"]

  compliance_rules: []              # [OPTIONAL] Regulatory or policy rules the agent must follow.
                                    # Each rule is a descriptive string.
                                    # Examples:
                                    #   - "GDPR: data subject deletion requests must be honored within 72 hours"
                                    #   - "SOX: all financial data transformations must be auditable"
                                    #   - "Internal: no raw PII in the analytics warehouse"

  data_classification: ""           # [OPTIONAL] Classification level of the data this agent handles.
                                    # Valid values: "public", "internal", "confidential", "restricted"
                                    # Influences access control and encryption decisions.

  description: >                    # [OPTIONAL] Additional governance context.
    Any governance requirements not captured above:
    specific regulations, organizational policies,
    data handling agreements, or compliance constraints.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 6: Infrastructure
# ──────────────────────────────────────────────────────────────────────────────
infrastructure:
  sources:                          # [REQUIRED] Where the agent reads data from.
    - name: ""                      # [REQUIRED] Descriptive name. Example: "raw_customer_s3_bucket"
      system: ""                    # [REQUIRED] System type.
                                    # Valid values: "s3", "gcs", "azure_blob", "sftp", "http_api",
                                    #   "postgres", "mysql", "snowflake", "bigquery", "redshift",
                                    #   "kafka", "rabbitmq", "kinesis", "local_filesystem", "other"
      connection: ""                # [REQUIRED] Connection identifier or URI pattern.
                                    # Do NOT put credentials here — use a config reference.
                                    # Example: "s3://data-lake/raw/customers/"
      format: ""                    # [OPTIONAL] Data format at this source.
                                    # Valid values: "csv", "json", "jsonl", "parquet", "avro",
                                    #   "orc", "xml", "database_table", "api_response", "other"
      description: >                # [OPTIONAL] Additional context about this source.
        Describe the source system, data refresh cadence,
        access patterns, and any quirks.

  destinations:                     # [REQUIRED] Where the agent writes data to.
    - name: ""                      # [REQUIRED] Descriptive name. Example: "analytics_warehouse"
      system: ""                    # [REQUIRED] System type. Same valid values as sources.
      connection: ""                # [REQUIRED] Connection identifier or URI pattern.
      format: ""                    # [OPTIONAL] Data format at this destination.
      description: >                # [OPTIONAL] Additional context about this destination.
        Describe the destination system, expected consumers,
        and any write constraints.

  scheduling:                       # [OPTIONAL] When the agent should run.
    type: ""                        # Valid values:
                                    #   "cron"       — cron-scheduled recurring execution
                                    #   "event"      — triggered by external event (message, file arrival)
                                    #   "manual"     — triggered manually by a human
                                    #   "continuous" — always running (streaming)
    schedule: ""                    # Cron expression if type is "cron". Example: "0 6 * * *" (daily 6AM)
    timezone: ""                    # Timezone for the cron schedule. Example: "UTC"
    description: >                  # Additional scheduling context.
      Describe the scheduling requirements: why this cadence,
      dependencies on upstream systems, SLA windows.

  resource_constraints:             # [OPTIONAL] Resource limits the agent should respect.
    max_memory_gb: null             # Maximum memory allocation. Null = no constraint.
    max_cpu_cores: null             # Maximum CPU cores. Null = no constraint.
    max_execution_minutes: null     # Maximum runtime before timeout. Null = no constraint.
    description: >                  # Additional resource context.
      Describe resource constraints, shared infrastructure
      considerations, or cost sensitivity.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 7: Cross-Agent Context
# ──────────────────────────────────────────────────────────────────────────────
cross_agent:
  upstream_dependencies: []         # [OPTIONAL] Agents whose output this agent consumes.
                                    # Each entry references an existing agent by name.
                                    # Example:
                                    #   - agent: "raw_data_collector"
                                    #     contract: "raw_events_output"
                                    #     description: "Consumes raw event data published daily"

  downstream_consumers: []          # [OPTIONAL] Agents that consume this agent's output.
                                    # Example:
                                    #   - agent: "customer_analytics_dashboard"
                                    #     contract: "customer_enriched"
                                    #     description: "Dashboard reads enriched customer data"

  shared_components: []             # [OPTIONAL] Existing shared components to reuse.
                                    # The Architect verifies these exist in the component registry.
                                    # Example:
                                    #   - name: "pii_masker"
                                    #     version: "2.1.0"
                                    #     usage: "Use for email and phone masking in the governance stage"
                                    #   - name: "s3_reader"
                                    #     version: "1.3.0"
                                    #     usage: "Use for ingesting CSV files from S3"

  description: >                    # [OPTIONAL] Additional cross-agent context.
    Describe the agent's role in the broader data ecosystem.
    Which teams depend on its outputs? What breaks if it fails?


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 8: Constraints and Preferences
# ──────────────────────────────────────────────────────────────────────────────
constraints:
  required_libraries: []            # [OPTIONAL] Libraries that MUST be used.
                                    # Example: ["great_expectations", "pandas", "boto3"]

  avoided_libraries: []             # [OPTIONAL] Libraries that MUST NOT be used.
                                    # Example: ["pyspark"]  # (too heavy for this use case)

  performance:                      # [OPTIONAL] Performance requirements.
    max_latency_seconds: null       # Maximum acceptable end-to-end latency per run. Null = no SLA.
    min_throughput_records: null     # Minimum records processed per second. Null = no requirement.
    max_batch_size: null            # Maximum records per batch. Null = no limit.

  sla:                              # [OPTIONAL] Service-level agreements.
    availability_percent: null      # Target uptime (e.g., 99.9). Null = no SLA.
    max_downtime_minutes: null      # Maximum acceptable downtime per incident. Null = no SLA.
    recovery_time_minutes: null     # Target recovery time after failure. Null = no SLA.

  description: >                    # [OPTIONAL] Any constraints not captured above.
    Describe additional constraints: team preferences,
    infrastructure limitations, budget considerations,
    or organizational policies that affect implementation.
```

---

## 4. A Complete Filled Example

The following is a realistic, fully filled-in spec for a "Customer Data Pipeline" agent — the same agent referenced in the approach document's YAML example (Section 8).

```yaml
# ============================================================================
# Customer Data Pipeline — Agent Specification
# ============================================================================
# This is a completed spec for a daily batch pipeline that ingests customer
# CSV data from S3, validates quality, masks PII, enriches with derived
# fields, and publishes to the Snowflake analytics warehouse.
# ============================================================================

agent:
  name: "customer_data_pipeline"
  version: "1.0.0"
  description: >
    Ingests raw customer CSVs from S3, validates data quality,
    masks PII fields (email, phone), enriches records with derived
    fields (customer tenure, lifecycle stage), and publishes clean
    data to the Snowflake analytics warehouse. Runs daily at 6AM UTC.
    Feeds the customer analytics dashboard and the marketing
    segmentation engine.
  owner: "data-engineering-team"
  domain: "customer"
  tags: ["etl", "daily", "pii", "warehouse", "batch"]

architecture:
  autonomy_level: "semi-autonomous"
  complexity: "moderate"
  runtime_preference: "no_preference"
  description: >
    The pipeline has four stages with a governance step for PII masking.
    The publish stage requires human approval in production but can run
    autonomously in staging. Error handling should quarantine bad records
    rather than failing the entire batch — we prefer partial loads over
    no loads.

stages:
  - id: "ingest_from_s3"
    type: "ingestion"
    description: >
      Pulls daily CSV drops from the S3 raw data bucket. Files arrive
      between 4-5AM UTC. The stage should handle schema evolution by
      logging new or missing columns as warnings rather than failing.
      Multiple files per day are possible — process all files matching
      the date partition pattern.
    autonomy: "autonomous"
    input_contract: "customer_raw_files"
    output_contract: "customer_raw"
    tools: ["boto3", "pandas"]
    error_strategy: "retry"
    retry_config:
      max_retries: 3
      backoff_seconds: 120
    description_details: >
      S3 path pattern: s3://data-lake/raw/customers/dt={YYYY-MM-DD}/
      File naming: customers_{timestamp}.csv
      Expected encoding: UTF-8
      Expected delimiter: comma
      Header row: always present

  - id: "validate_quality"
    type: "validation"
    description: >
      Validates all ingested records against quality rules defined in
      the customer_raw contract. Records that fail validation are
      quarantined to a separate table for manual review. The stage
      produces a quality summary with pass/fail counts per rule.
    autonomy: "automated_check"
    input_contract: "customer_raw"
    output_contract: "customer_validated"
    tools: ["great_expectations"]
    error_strategy: "quarantine"
    description_details: >
      Quality gate: proceed only if >= 95% of records pass all rules.
      Below 95%: quarantine failures, send alert, proceed with valid records.
      Below 80%: halt the pipeline and escalate to the data engineering team.

  - id: "mask_pii"
    type: "governance"
    description: >
      Masks PII fields (email, phone_number) before data enters the
      analytics warehouse. Email is masked to preserve domain
      (j***@example.com). Phone is masked to show only last 4 digits
      (***-***-1234). Original values are never written to the warehouse.
    autonomy: "autonomous"
    input_contract: "customer_validated"
    output_contract: "customer_masked"
    tools: []
    error_strategy: "fail_fast"
    description_details: >
      Uses the shared pii_masker component (v2.1.0) from the component
      registry. If the component is unavailable, fail — do not proceed
      with unmasked PII.

  - id: "enrich_derived_fields"
    type: "transformation"
    description: >
      Enriches customer records with derived fields: customer_tenure
      (days since signup), lifecycle_stage (new/active/dormant/churned
      based on last_activity_date), and customer_value_tier (based on
      total_purchases). These derived fields power the marketing
      segmentation engine.
    autonomy: "autonomous"
    input_contract: "customer_masked"
    output_contract: "customer_enriched"
    tools: ["pandas"]
    error_strategy: "quarantine"
    description_details: >
      Lifecycle stage logic:
        - "new": signed up within last 30 days
        - "active": last activity within 90 days
        - "dormant": last activity 91-365 days ago
        - "churned": no activity for > 365 days
      Customer value tier logic:
        - "platinum": total_purchases >= 10000
        - "gold": total_purchases >= 5000
        - "silver": total_purchases >= 1000
        - "bronze": total_purchases < 1000

  - id: "publish_to_warehouse"
    type: "publishing"
    description: >
      Writes enriched customer data to the Snowflake analytics warehouse.
      Uses a merge/upsert strategy keyed on customer_id — updates existing
      records and inserts new ones. Maintains a history table with SCD Type 2
      tracking for customer_value_tier and lifecycle_stage changes.
    autonomy: "human_approval"
    input_contract: "customer_enriched"
    output_contract: "customer_warehouse"
    tools: ["snowflake-connector-python"]
    error_strategy: "fail_fast"
    description_details: >
      Target table: analytics.customers.customer_profile
      History table: analytics.customers.customer_profile_history
      Merge key: customer_id
      SCD Type 2 tracked columns: customer_value_tier, lifecycle_stage
      Batch size: 10000 records per INSERT/MERGE batch

contracts:
  - id: "customer_raw_files"
    description: >
      Raw CSV files as they arrive in S3. This contract describes the
      expected file-level metadata, not the record schema (which is
      defined in customer_raw).
    direction: "input"
    fields:
      - name: "file_path"
        type: "string"
        nullable: false
        description: >
          Full S3 URI of the source CSV file.
        quality_rules:
          - "must match pattern s3://data-lake/raw/customers/dt=\\d{4}-\\d{2}-\\d{2}/"
      - name: "file_size_bytes"
        type: "integer"
        nullable: false
        description: >
          Size of the file in bytes. Used to detect empty or truncated uploads.
        quality_rules:
          - "must be greater than 0"
      - name: "upload_timestamp"
        type: "timestamp"
        nullable: false
        description: >
          S3 object last-modified timestamp. Used for freshness checks.
        quality_rules:
          - "must be within the last 36 hours"

  - id: "customer_raw"
    description: >
      Individual customer records parsed from the raw CSV files. This is the
      record-level schema before any validation or transformation.
    direction: "internal"
    fields:
      - name: "customer_id"
        type: "string"
        nullable: false
        description: >
          Unique customer identifier assigned by the source CRM system.
          Format: two uppercase letters followed by six digits (e.g., CU000123).
        quality_rules:
          - "must be unique across the dataset"
          - "must match regex ^[A-Z]{2}\\d{6}$"
      - name: "first_name"
        type: "string"
        nullable: false
        description: >
          Customer's first name as recorded in the CRM.
        quality_rules:
          - "must not be empty string"
      - name: "last_name"
        type: "string"
        nullable: false
        description: >
          Customer's last name as recorded in the CRM.
        quality_rules:
          - "must not be empty string"
      - name: "email"
        type: "string"
        nullable: false
        description: >
          Customer's primary email address. PII — will be masked before warehouse.
        quality_rules:
          - "must be a valid email address"
      - name: "phone_number"
        type: "string"
        nullable: true
        description: >
          Customer's phone number. PII — will be masked before warehouse.
          Not all customers provide a phone number.
        quality_rules: []
      - name: "signup_date"
        type: "date"
        nullable: false
        description: >
          Date the customer first signed up. Used to calculate tenure.
        quality_rules:
          - "must not be in the future"
          - "must be after 2015-01-01"
      - name: "last_activity_date"
        type: "date"
        nullable: true
        description: >
          Date of the customer's most recent interaction. Null means
          no recorded activity since CRM migration.
        quality_rules:
          - "if not null, must not be in the future"
      - name: "total_purchases"
        type: "float"
        nullable: false
        description: >
          Lifetime total purchase amount in USD.
        quality_rules:
          - "must be >= 0"
      - name: "created_at"
        type: "timestamp"
        nullable: false
        description: >
          Timestamp when this record was created/exported by the CRM.
        quality_rules:
          - "must be within the last 48 hours"
    sla:
      max_records: 500000
      min_records: 1000
      freshness_hours: 36

  - id: "customer_validated"
    description: >
      Customer records that passed all quality validation rules.
      Same schema as customer_raw — this contract exists to mark the
      boundary between raw and validated data.
    direction: "internal"
    fields:
      - name: "customer_id"
        type: "string"
        nullable: false
        description: >
          Validated customer identifier.
        quality_rules:
          - "must be unique across the dataset"
      - name: "first_name"
        type: "string"
        nullable: false
        description: >
          Validated first name.
        quality_rules: []
      - name: "last_name"
        type: "string"
        nullable: false
        description: >
          Validated last name.
        quality_rules: []
      - name: "email"
        type: "string"
        nullable: false
        description: >
          Validated email address. Still contains raw PII at this stage.
        quality_rules: []
      - name: "phone_number"
        type: "string"
        nullable: true
        description: >
          Validated phone number. Still contains raw PII at this stage.
        quality_rules: []
      - name: "signup_date"
        type: "date"
        nullable: false
        description: >
          Validated signup date.
        quality_rules: []
      - name: "last_activity_date"
        type: "date"
        nullable: true
        description: >
          Validated last activity date.
        quality_rules: []
      - name: "total_purchases"
        type: "float"
        nullable: false
        description: >
          Validated purchase total.
        quality_rules: []
      - name: "created_at"
        type: "timestamp"
        nullable: false
        description: >
          Record creation timestamp.
        quality_rules: []

  - id: "customer_masked"
    description: >
      Customer records after PII masking. Email and phone_number fields
      have been replaced with masked versions. All other fields are
      unchanged from customer_validated.
    direction: "internal"
    fields:
      - name: "customer_id"
        type: "string"
        nullable: false
        description: >
          Customer identifier (not PII, unmasked).
        quality_rules: []
      - name: "first_name"
        type: "string"
        nullable: false
        description: >
          Customer first name (not classified as PII for this pipeline).
        quality_rules: []
      - name: "last_name"
        type: "string"
        nullable: false
        description: >
          Customer last name (not classified as PII for this pipeline).
        quality_rules: []
      - name: "email_masked"
        type: "string"
        nullable: false
        description: >
          Masked email. Format: first char + *** + @ + domain.
          Example: j***@example.com
        quality_rules:
          - "must contain exactly one @ character"
          - "must contain at least one * character"
      - name: "phone_masked"
        type: "string"
        nullable: true
        description: >
          Masked phone. Format: ***-***-{last 4 digits}.
          Example: ***-***-1234. Null if original was null.
        quality_rules: []
      - name: "signup_date"
        type: "date"
        nullable: false
        description: >
          Signup date (unchanged).
        quality_rules: []
      - name: "last_activity_date"
        type: "date"
        nullable: true
        description: >
          Last activity date (unchanged).
        quality_rules: []
      - name: "total_purchases"
        type: "float"
        nullable: false
        description: >
          Lifetime purchase total (unchanged).
        quality_rules: []
      - name: "created_at"
        type: "timestamp"
        nullable: false
        description: >
          Record creation timestamp (unchanged).
        quality_rules: []

  - id: "customer_enriched"
    description: >
      Customer records after enrichment with derived fields. This is the
      shape that enters the analytics warehouse.
    direction: "internal"
    fields:
      - name: "customer_id"
        type: "string"
        nullable: false
        description: >
          Customer identifier.
        quality_rules:
          - "must be unique across the dataset"
      - name: "first_name"
        type: "string"
        nullable: false
        description: >
          Customer first name.
        quality_rules: []
      - name: "last_name"
        type: "string"
        nullable: false
        description: >
          Customer last name.
        quality_rules: []
      - name: "email_masked"
        type: "string"
        nullable: false
        description: >
          Masked email address.
        quality_rules: []
      - name: "phone_masked"
        type: "string"
        nullable: true
        description: >
          Masked phone number.
        quality_rules: []
      - name: "signup_date"
        type: "date"
        nullable: false
        description: >
          Original signup date.
        quality_rules: []
      - name: "last_activity_date"
        type: "date"
        nullable: true
        description: >
          Most recent activity date.
        quality_rules: []
      - name: "total_purchases"
        type: "float"
        nullable: false
        description: >
          Lifetime purchase total in USD.
        quality_rules: []
      - name: "customer_tenure_days"
        type: "integer"
        nullable: false
        description: >
          Derived: number of days since signup_date.
          Calculated as (current_date - signup_date).days.
        quality_rules:
          - "must be >= 0"
      - name: "lifecycle_stage"
        type: "string"
        nullable: false
        description: >
          Derived: customer lifecycle classification based on
          last_activity_date. Values: new, active, dormant, churned.
        quality_rules:
          - "must be one of: new, active, dormant, churned"
      - name: "customer_value_tier"
        type: "string"
        nullable: false
        description: >
          Derived: value tier based on total_purchases.
          Values: platinum, gold, silver, bronze.
        quality_rules:
          - "must be one of: platinum, gold, silver, bronze"
      - name: "created_at"
        type: "timestamp"
        nullable: false
        description: >
          Record creation timestamp from source.
        quality_rules: []
      - name: "enriched_at"
        type: "timestamp"
        nullable: false
        description: >
          Timestamp when enrichment was applied.
        quality_rules:
          - "must not be in the future"

  - id: "customer_warehouse"
    description: >
      Final output contract for the Snowflake analytics warehouse.
      Identical to customer_enriched — this contract exists to mark
      the data as published and to serve as the contract that
      downstream consumers reference.
    direction: "output"
    fields:
      - name: "customer_id"
        type: "string"
        nullable: false
        description: >
          Primary key in the warehouse table.
        quality_rules:
          - "must be unique across the dataset"
      - name: "first_name"
        type: "string"
        nullable: false
        description: >
          Customer first name.
        quality_rules: []
      - name: "last_name"
        type: "string"
        nullable: false
        description: >
          Customer last name.
        quality_rules: []
      - name: "email_masked"
        type: "string"
        nullable: false
        description: >
          Masked email address. No raw PII in the warehouse.
        quality_rules: []
      - name: "phone_masked"
        type: "string"
        nullable: true
        description: >
          Masked phone number. No raw PII in the warehouse.
        quality_rules: []
      - name: "signup_date"
        type: "date"
        nullable: false
        description: >
          Customer signup date.
        quality_rules: []
      - name: "last_activity_date"
        type: "date"
        nullable: true
        description: >
          Most recent activity date.
        quality_rules: []
      - name: "total_purchases"
        type: "float"
        nullable: false
        description: >
          Lifetime purchase total.
        quality_rules: []
      - name: "customer_tenure_days"
        type: "integer"
        nullable: false
        description: >
          Days since signup.
        quality_rules: []
      - name: "lifecycle_stage"
        type: "string"
        nullable: false
        description: >
          Lifecycle classification.
        quality_rules: []
      - name: "customer_value_tier"
        type: "string"
        nullable: false
        description: >
          Value tier classification.
        quality_rules: []
      - name: "created_at"
        type: "timestamp"
        nullable: false
        description: >
          Record creation timestamp from source.
        quality_rules: []
      - name: "enriched_at"
        type: "timestamp"
        nullable: false
        description: >
          Timestamp when enrichment was applied.
        quality_rules: []
      - name: "published_at"
        type: "timestamp"
        nullable: false
        description: >
          Timestamp when this record was written to the warehouse.
        quality_rules: []
    sla:
      max_records: 500000
      min_records: 1000
      freshness_hours: 24

governance:
  pii_fields: ["email", "phone_number"]
  pii_strategy: "mask"
  audit_requirements:
    enabled: true
    log_level: "standard"
    retention_days: 365
  approval_gates: ["publish_to_warehouse"]
  compliance_rules:
    - "Internal policy: no raw PII in the analytics warehouse"
    - "Data retention: raw source files in S3 retained for 90 days"
    - "GDPR: support data subject deletion requests via customer_id lookup"
  data_classification: "confidential"
  description: >
    This pipeline handles customer PII. All PII must be masked before
    reaching the warehouse. The governance stage uses the shared
    pii_masker component. Audit logs must capture which records were
    masked and which were quarantined. The publish stage requires
    human approval in production.

infrastructure:
  sources:
    - name: "raw_customer_s3_bucket"
      system: "s3"
      connection: "s3://data-lake/raw/customers/"
      format: "csv"
      description: >
        Daily CSV exports from the CRM system. Files arrive between
        4-5AM UTC. Typically 50-200K records per file. One to three
        files per day. UTF-8 encoded, comma-delimited, with headers.
  destinations:
    - name: "snowflake_analytics"
      system: "snowflake"
      connection: "snowflake://analytics_warehouse/customers"
      format: "database_table"
      description: >
        Snowflake analytics warehouse. Target schema: analytics.customers.
        The data engineering team has write access. The analytics and
        marketing teams have read access.
  scheduling:
    type: "cron"
    schedule: "0 6 * * *"
    timezone: "UTC"
    description: >
      Runs daily at 6AM UTC, after the CRM export window (4-5AM).
      Must complete before 8AM UTC when the analytics dashboard
      refreshes. Weekend runs are expected but lower volume.
  resource_constraints:
    max_memory_gb: 8
    max_cpu_cores: 4
    max_execution_minutes: 60
    description: >
      Runs on the shared data engineering Kubernetes cluster.
      Memory and CPU are bounded to avoid starving other pipelines.
      If processing exceeds 60 minutes, something is wrong — alert.

cross_agent:
  upstream_dependencies: []
  downstream_consumers:
    - agent: "customer_analytics_dashboard"
      contract: "customer_warehouse"
      description: >
        The analytics dashboard reads enriched customer data from
        Snowflake every hour. Schema changes must be coordinated.
    - agent: "marketing_segmentation_engine"
      contract: "customer_warehouse"
      description: >
        The marketing engine reads lifecycle_stage and customer_value_tier
        for audience segmentation. Breaking changes to these fields
        will break segmentation rules.
  shared_components:
    - name: "pii_masker"
      version: "2.1.0"
      usage: >
        Use for masking email and phone_number fields in the
        mask_pii stage. Supports domain-preserving email masking
        and last-N-digits phone masking.
    - name: "s3_reader"
      version: "1.3.0"
      usage: >
        Use for reading CSV files from S3 in the ingest_from_s3 stage.
        Handles multi-file ingestion and schema evolution detection.
  description: >
    This pipeline is the primary source of customer data for the analytics
    warehouse. Two downstream agents depend on its output. Any schema
    changes to customer_warehouse must be coordinated with both teams.

constraints:
  required_libraries: ["boto3", "pandas", "great_expectations", "snowflake-connector-python"]
  avoided_libraries: ["pyspark"]
  performance:
    max_latency_seconds: 3600
    min_throughput_records: 1000
    max_batch_size: 50000
  sla:
    availability_percent: 99.5
    max_downtime_minutes: 120
    recovery_time_minutes: 30
  description: >
    PySpark is avoided because the data volumes (< 500K records) do not
    justify the overhead of a Spark cluster. Pandas is sufficient and
    simpler to maintain. The pipeline must complete within 1 hour to
    meet the 8AM dashboard refresh SLA.
```

---

## 5. Field Reference Table

Comprehensive listing of every field path, its type, required/optional status, valid values, and which specialist primarily consumes it.

| Field Path | Type | Required | Valid Values | Primary Consumer |
|---|---|---|---|---|
| `agent.name` | string | Yes | Snake_case, unique | Spec Analyst, Metadata |
| `agent.version` | string | Yes | Semver (e.g., "1.0.0") | Spec Analyst, Metadata |
| `agent.description` | string | Yes | Free text | Spec Analyst |
| `agent.owner` | string | Yes | Free text | Metadata |
| `agent.domain` | string | Yes | Enum: customer, finance, product, marketing, operations, supply_chain, hr, compliance, analytics, infrastructure, other | Spec Analyst, Metadata |
| `agent.tags` | list[string] | No | Free text | Metadata |
| `architecture.autonomy_level` | string | Yes | Enum: autonomous, semi-autonomous, supervised | Architect |
| `architecture.complexity` | string | No | Enum: simple, moderate, complex | Architect |
| `architecture.runtime_preference` | string | No | Enum: langgraph, python, streaming, no_preference | Architect |
| `architecture.description` | string | No | Free text | Architect |
| `stages[].id` | string | Yes | Snake_case, unique within stages | All specialists |
| `stages[].type` | string | Yes | Enum: ingestion, validation, transformation, governance, aggregation, publishing, notification, monitoring, custom | Architect, Documentarian |
| `stages[].description` | string | Yes | Free text | Spec Analyst, Documentarian |
| `stages[].autonomy` | string | Yes | Enum: autonomous, human_approval, automated_check | Architect |
| `stages[].input_contract` | string | Yes | Must reference a contract id | Spec Analyst (chain validation) |
| `stages[].output_contract` | string | Yes | Must reference a contract id | Spec Analyst (chain validation) |
| `stages[].tools` | list[string] | No | Free text (Researcher validates) | Researcher |
| `stages[].error_strategy` | string | No | Enum: fail_fast, quarantine, retry, skip, escalate | Documentarian |
| `stages[].retry_config.max_retries` | integer | No | Positive integer | Documentarian |
| `stages[].retry_config.backoff_seconds` | integer | No | Positive integer | Documentarian |
| `stages[].description_details` | string | No | Free text | Documentarian |
| `contracts[].id` | string | Yes | Snake_case, unique | All specialists |
| `contracts[].description` | string | Yes | Free text | Spec Analyst |
| `contracts[].direction` | string | Yes | Enum: input, output, internal | Spec Analyst, Documentarian |
| `contracts[].fields[].name` | string | Yes | Snake_case | Documentarian, Validator |
| `contracts[].fields[].type` | string | Yes | Enum: string, integer, float, boolean, date, datetime, timestamp, json, array, binary | Documentarian, Builder |
| `contracts[].fields[].nullable` | boolean | No | true/false (default: false) | Documentarian, Builder |
| `contracts[].fields[].description` | string | Yes | Free text | Documentarian |
| `contracts[].fields[].quality_rules` | list[string] | No | Free text rules | Documentarian, Validator |
| `contracts[].sla.max_records` | integer | No | Positive integer or null | Documentarian |
| `contracts[].sla.min_records` | integer | No | Positive integer or null | Documentarian |
| `contracts[].sla.freshness_hours` | integer | No | Positive integer or null | Documentarian |
| `governance.pii_fields` | list[string] | Conditional | Field names from contracts | Documentarian, Validator |
| `governance.pii_strategy` | string | Conditional | Enum: mask, hash, encrypt, redact, tokenize, mixed | Documentarian |
| `governance.audit_requirements.enabled` | boolean | No | true/false (default: false) | Documentarian |
| `governance.audit_requirements.log_level` | string | No | Enum: minimal, standard, comprehensive | Documentarian |
| `governance.audit_requirements.retention_days` | integer | No | Positive integer or null | Documentarian |
| `governance.approval_gates` | list[string] | No | Stage ids from stages section | Architect |
| `governance.compliance_rules` | list[string] | No | Free text | Documentarian, Validator |
| `governance.data_classification` | string | No | Enum: public, internal, confidential, restricted | Architect, Documentarian |
| `governance.description` | string | No | Free text | Spec Analyst |
| `infrastructure.sources[].name` | string | Yes | Free text | Researcher |
| `infrastructure.sources[].system` | string | Yes | Enum: s3, gcs, azure_blob, sftp, http_api, postgres, mysql, snowflake, bigquery, redshift, kafka, rabbitmq, kinesis, local_filesystem, other | Researcher |
| `infrastructure.sources[].connection` | string | Yes | URI pattern (no credentials) | Documentarian, Builder |
| `infrastructure.sources[].format` | string | No | Enum: csv, json, jsonl, parquet, avro, orc, xml, database_table, api_response, other | Researcher, Documentarian |
| `infrastructure.sources[].description` | string | No | Free text | Researcher |
| `infrastructure.destinations[].name` | string | Yes | Free text | Researcher |
| `infrastructure.destinations[].system` | string | Yes | Same enum as sources | Researcher |
| `infrastructure.destinations[].connection` | string | Yes | URI pattern (no credentials) | Documentarian, Builder |
| `infrastructure.destinations[].format` | string | No | Same enum as source format | Researcher, Documentarian |
| `infrastructure.destinations[].description` | string | No | Free text | Researcher |
| `infrastructure.scheduling.type` | string | No | Enum: cron, event, manual, continuous | Architect |
| `infrastructure.scheduling.schedule` | string | No | Cron expression | Documentarian |
| `infrastructure.scheduling.timezone` | string | No | Timezone string (e.g., "UTC") | Documentarian |
| `infrastructure.scheduling.description` | string | No | Free text | Spec Analyst |
| `infrastructure.resource_constraints.max_memory_gb` | float | No | Positive number or null | Architect |
| `infrastructure.resource_constraints.max_cpu_cores` | integer | No | Positive integer or null | Architect |
| `infrastructure.resource_constraints.max_execution_minutes` | integer | No | Positive integer or null | Architect |
| `infrastructure.resource_constraints.description` | string | No | Free text | Architect |
| `cross_agent.upstream_dependencies[]` | object | No | agent (string), contract (string), description (string) | Architect, Metadata |
| `cross_agent.downstream_consumers[]` | object | No | agent (string), contract (string), description (string) | Architect, Metadata |
| `cross_agent.shared_components[]` | object | No | name (string), version (string), usage (string) | Architect |
| `cross_agent.description` | string | No | Free text | Architect |
| `constraints.required_libraries` | list[string] | No | Library names | Researcher, Builder |
| `constraints.avoided_libraries` | list[string] | No | Library names | Researcher, Builder |
| `constraints.performance.max_latency_seconds` | integer | No | Positive integer or null | Architect |
| `constraints.performance.min_throughput_records` | integer | No | Positive integer or null | Architect |
| `constraints.performance.max_batch_size` | integer | No | Positive integer or null | Documentarian |
| `constraints.sla.availability_percent` | float | No | 0-100 or null | Architect |
| `constraints.sla.max_downtime_minutes` | integer | No | Positive integer or null | Architect |
| `constraints.sla.recovery_time_minutes` | integer | No | Positive integer or null | Architect |
| `constraints.description` | string | No | Free text | Architect |

---

## 6. Validation Rules

The Spec Analyst validates the spec in two passes: **schema validation** (structural correctness) and **semantic validation** (logical consistency). Failures are categorized into three outcomes.

### 6.1 Schema Validation

Schema validation is Pydantic-based. It checks structure, types, required fields, and enum values. These checks are deterministic — they pass or fail without LLM involvement.

| Rule | What It Checks | On Failure |
|---|---|---|
| Required fields present | `agent.name`, `agent.version`, `agent.description`, `agent.owner`, `agent.domain`, `architecture.autonomy_level`, at least one stage, at least one contract | **Reject** — cannot proceed without these |
| Field types correct | Strings are strings, lists are lists, integers are integers | **Reject** |
| Enum values valid | `autonomy_level` ∈ {autonomous, semi-autonomous, supervised}, etc. | **Reject** |
| Stage ids unique | No two stages share the same `id` | **Reject** |
| Contract ids unique | No two contracts share the same `id` | **Reject** |
| Version is semver | `agent.version` matches `\d+\.\d+\.\d+` | **Reject** |
| Agent name is snake_case | `agent.name` matches `^[a-z][a-z0-9_]*$` | **Reject** |
| Stage ids are snake_case | Each `stages[].id` matches `^[a-z][a-z0-9_]*$` | **Reject** |
| At least one stage defined | `stages` list is non-empty | **Reject** |
| At least one contract defined | `contracts` list is non-empty | **Reject** |
| Each stage has input and output contracts | `input_contract` and `output_contract` are non-empty strings | **Reject** |
| Each contract has at least one field | `fields` list is non-empty | **Reject** |
| PII strategy required when PII fields listed | If `governance.pii_fields` is non-empty, `governance.pii_strategy` must be set | **Reject** |
| Retry config valid | If `error_strategy` is "retry", `retry_config` must be present with positive values | **Reject** |

### 6.2 Semantic Validation

Semantic validation uses LLM reasoning to check logical consistency. These checks may produce clarifying questions rather than outright rejections.

| Rule | What It Checks | On Failure |
|---|---|---|
| Contract reference integrity | Every `input_contract` and `output_contract` in stages references a contract defined in the `contracts` section | **Reject** — broken references make the spec unparseable |
| Contract chain continuity | Stage N's `output_contract` should align with Stage N+1's `input_contract` (where sequential flow is implied) | **Clarify** — ask if the gap is intentional |
| Field propagation consistency | Fields present in an early contract but absent in a later one — was the omission intentional? | **Clarify** — ask if the field was deliberately dropped |
| PII field existence | Every field listed in `governance.pii_fields` must exist in at least one contract | **Reject** — references a non-existent field |
| Approval gate validity | Every stage listed in `governance.approval_gates` must match a stage id | **Reject** — references a non-existent stage |
| Tool availability | Tools listed in stages should be recognizable libraries/tools | **Clarify** — ask for clarification on unrecognized tools |
| Source/destination consistency | Stages of type "ingestion" should have a corresponding source in infrastructure; stages of type "publishing" should have a corresponding destination | **Clarify** — ask if the infrastructure is missing or intentionally omitted |
| Cross-agent reference validation | Agents listed in `upstream_dependencies` and `downstream_consumers` are checked against the metadata framework's agent registry | **Clarify** — ask if the referenced agent exists or is planned |
| Shared component existence | Components listed in `cross_agent.shared_components` are checked against the component registry | **Clarify** — ask if the component should be created as new |
| Governance completeness | If `data_classification` is "confidential" or "restricted", are PII rules defined? Is audit logging enabled? | **Clarify** — surface potential governance gaps |
| Scheduling vs. type consistency | If scheduling type is "continuous", at least one stage should be type "monitoring" or ingestion from a streaming source (kafka, kinesis, rabbitmq) | **Clarify** — ask if the scheduling is correct |
| Description sufficiency | Are description fields detailed enough for downstream specialists? | **Clarify** — ask for more detail on vague descriptions |

### 6.3 Outcome Categories

| Outcome | When | What Happens |
|---|---|---|
| **Reject** | Required field missing, type error, enum violation, broken reference | Spec is returned to the user with specific error messages. Must be fixed before re-submission. |
| **Clarify** | Logical inconsistency, potential gap, ambiguity, implicit assumption | Spec Analyst generates clarifying questions. User answers. Answers are incorporated and the spec is re-validated. Loop up to max clarification rounds. |
| **Accept** | All schema checks pass, all semantic checks pass or are resolved through clarification | Parsed spec is written to the blackboard. Process advances to the Research phase. |

---

## 7. Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Template format | YAML with inline comments and `>` block descriptions | Follows the approach.md pattern: machine-readable structure + human-readable descriptions. Same pattern as OpenAPI, dbt schema.yml, and Kubernetes manifests. |
| 2 | Eight top-level sections | agent, architecture, stages, contracts, governance, infrastructure, cross_agent, constraints | Each section maps to a distinct concern and a primary consumer specialist. No section tries to do two things. |
| 3 | Contracts as a separate section (not embedded in stages) | Standalone contract definitions referenced by stage id | Contracts may be shared across multiple stages. Embedding them in stages would force duplication. Separate contracts also enable cross-stage chain validation and cross-agent lineage tracking. |
| 4 | Enum fields over free text | Enums for type, autonomy, strategy, system, format, classification | Reduces LLM interpretation burden. The Spec Analyst validates against a known set rather than reasoning about arbitrary strings. Free text is reserved for description fields where intent matters. |
| 5 | Stage ordering by list position | YAML list implies execution order | Explicit ordering avoids ambiguity. The Architect may reorder or parallelize, but the spec expresses the user's intended sequence. |
| 6 | Quality rules as free text strings | list[string] with natural language rules | Quality rules are too varied to enumerate as an enum. The Documentarian translates these into concrete Great Expectations rules or custom validators. LLM interpretation is appropriate here. |
| 7 | No credentials in the spec | Connection fields hold URI patterns, not credentials | Credentials belong in config files and secrets managers, never in specification documents. The approach.md rule: no hardcoding. |
| 8 | Cross-agent references are names, not ids | Reference agents and components by human-readable name | The Spec Analyst and Architect resolve names to registry ids via the metadata framework. Users should not need to know internal ids. |
| 9 | Governance as a dedicated section | Not distributed across stages | Governance rules often span multiple stages (PII masking in one stage, audit logging across all stages, approval gates at specific stages). Centralizing governance in one section ensures nothing is missed. The Documentarian distributes these to specific enforcement points in the impl docs. |
| 10 | Infrastructure separated from stages | Sources and destinations are top-level, not stage-local | Multiple stages may read from the same source or write to the same destination. Top-level infrastructure avoids duplication and gives the Researcher a single place to evaluate all external system dependencies. |
| 11 | Description fields on nearly everything | description, description_details, and per-section descriptions | The spec must serve both the Pydantic parser (structured fields) and the LLM specialists (intent and context). Description fields bridge the gap — they carry business context that no enum can capture. |
| 12 | Conditional required fields | PII strategy required only when PII fields listed | Avoids forcing users to fill in governance fields when the agent handles no PII. The Pydantic validator enforces conditional requirements. |
| 13 | Retire spec.yaml post-build | Day-0 bootstrapping intake form only; retired post-build | Eliminates dual-spec drift. Once an agent is delivered, `impl_docs/*.yaml` in Git is the single source of truth. Day-2 enhancements operate directly on `impl_docs/`, never on `spec.yaml`. |

---

## 8. What This Template Does NOT Cover

This template captures **user intent** — what the agent should do. It deliberately does not cover:

- **Implementation details** — how stages are coded, what functions are called, what the file structure looks like → that is the impl doc's job (produced by the Documentarian)
- **Architecture design** — which pattern is used, what runtime is selected, how stages are wired → that is the Architect's job
- **Builder instructions** — code generation strategies, test frameworks, project layout → that is the Builder's job
- **Operational configuration** — model tiers, safety valve settings, prompt templates for the meta-agent itself → that is `config/settings.yaml`
- **Database DDL** — the Tier 3 metadata schema for this agent → that is generated by the Documentarian and compiled by the DCM Schema Engine
- **Deployment configuration** — Docker, Kubernetes, CI/CD → post-MVP
- **Day-2 change requests & maintenance** — modifications to an existing agent operate directly on `impl_docs/*.yaml` in the agent's Git repository, not by re-submitting or editing `spec.yaml`
- **Prompt engineering** — how each specialist's LLM prompts are structured → implementation detail

The spec is the **broadest input** to the funnel. Everything else is derived from it through the meta-agent's pipeline.

---

## 9. Next Steps

1. **Pydantic schema implementation** — Translate this template into a Pydantic v2 model used by the Spec Analyst for validation (`src/specialists/spec_analyst/models.py`)
2. **Spec Analyst prompt design** — Prompts that use the parsed Pydantic model to identify gaps and generate clarifying questions
3. **Impl doc schema** — How the Documentarian translates a validated spec into implementation documents → `impl_doc_schema.md`
4. **Example spec library** — Additional filled-in specs for different agent types (streaming, simple pipeline, multi-source aggregator) to test the Spec Analyst and Architect
