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
> 1. The multi-file **`impl_docs/` package** (`agent.yaml`, `stages/`, `contracts/`, `config.yaml`, `tests.yaml`) is committed directly into the generated agent's Git repository as the **permanent, authoritative source of truth**.
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
                                    # Example: "dbt_pipeline_builder_agent"

  version: ""                       # [REQUIRED] Semantic version. Use "1.0.0" for new agents.
                                    # Example: "1.0.0"

  description: >                    # [REQUIRED] Plain English description of what this agent does,
                                    # why it exists, and what business problem it solves.
                                    # Be specific — this is the Spec Analyst's primary context.
    Describe the agent's purpose, the business problem it solves,
    and the high-level data flow in 2-5 sentences.

  owner: ""                         # [REQUIRED] Team or individual responsible for this agent.
                                    # Example: "analytics-engineering-team"

  domain: ""                        # [REQUIRED] Business domain this agent serves.
                                    # Valid values: customer, finance, product, marketing, operations, 
                                    # supply_chain, hr, compliance, analytics, infrastructure, other
                                    # Example: "analytics"

  tags: []                          # [OPTIONAL] Freeform tags for discoverability.
                                    # Example: ["dbt", "code_generation", "gitops"]


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2: Architecture Preferences
# ──────────────────────────────────────────────────────────────────────────────
architecture:
  autonomy_level: ""                # [REQUIRED] How much human oversight does this agent need?
                                    # Valid values:
                                    #   "autonomous"       — runs end-to-end without human intervention
                                    #   "semi_autonomous"  — runs with human approval at key gates (PAT-01)
                                    #   "human_required"   — requires human approval before execution
                                    # Example: "semi_autonomous"

  complexity: ""                    # [OPTIONAL] Hint to the Architect about expected complexity.
                                    # Valid values:
                                    #   "simple"   — linear pipeline, few stages, no branching
                                    #   "moderate" — some branching, error recovery, multiple sources
                                    #   "complex"  — conditional routing, multi-source aggregation,
                                    #                streaming, or real-time requirements
                                    # If omitted, the Spec Analyst infers from stages and contracts.

  runtime_preference: ""            # [OPTIONAL] Preferred runtime for the generated agent.
                                    # Valid values:
                                    #   "langgraph"        — stateful agent with tool-calling loops and gates (PAT-01)
                                    #   "python_litellm"   — lightweight linear or CLI agent powered by LiteLLM
                                    #   "no_preference"    — let the Architect decide based on requirements
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
                                    # Example: "discover_sources"

    type: ""                        # [REQUIRED] Category of work this stage performs.
                                    # Valid values:
                                    #   "discovery"        — inspects database catalogs, schemas, or APIs
                                    #   "reasoning"        — LLM planning, decision making, or architectural synthesis
                                    #   "code_generation"  — LLM synthesizes SQL, Python, or configuration files
                                    #   "sandbox_testing"  — executes dry-runs, lints, or sandbox compilations
                                    #   "human_review"     — halts for human approval before side-effecting actions
                                    #   "delivery"         — pushes branches, opens PRs, or registers catalog entries
                                    #   "custom"           — bespoke processing (describe in detail)

    description: >                  # [REQUIRED] What this stage does, in plain English.
                                    # Include: purpose, edge cases, expected behavior on failure.
      Describe what this stage does, why it exists,
      and any important edge cases or business rules.

    autonomy: ""                    # [REQUIRED] Stage-level autonomy.
                                    # Valid values: "autonomous", "semi_autonomous", "human_required"
                                    #   "autonomous"      — runs without pause
                                    #   "semi_autonomous" — pauses for human approval if validation fails
                                    #   "human_required"  — pauses unconditionally for human approval at a gate

    input_contract: ""              # [REQUIRED for non-initial stages] Name of input contract.
                                    # Example: "source_schema_profile"

    output_contract: ""             # [REQUIRED for non-terminal stages] Name of output contract.
                                    # Example: "transformation_plan"

    tools: []                       # [OPTIONAL] Specific tools or libraries this stage should use.
                                    # The Researcher validates availability and compatibility.
                                    # Example: ["sqlglot", "litellm", "dbt-core"]

    error_strategy: ""              # [OPTIONAL] How to handle errors in this stage.
                                    # Valid values:
                                    #   "fail_fast"       — stop the pipeline on first error
                                    #   "retry"           — retry the operation (with backoff)
                                    #   "reflect"         — loop back to LLM with error diagnostics
                                    #   "skip"            — log the error and skip to next stage
                                    #   "escalate"        — pause and alert a human
                                    # If omitted, defaults to "fail_fast".

    retry_config:                   # [OPTIONAL] Only relevant if error_strategy is "retry" or "reflect".
      max_retries: 3                # Maximum number of retry attempts.
      backoff_seconds: 30           # Seconds to wait between retries.

    description_details: >          # [OPTIONAL] Additional implementation-level details.
                                    # Use this for specific prompt instructions, algorithms,
                                    # or verifier rules that need explicit documentation.
      Any additional detail: specific transformation logic,
      business rule formulas, prompt guidance, verifier rules, etc.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 4: Data Contracts
# ──────────────────────────────────────────────────────────────────────────────
# Data contracts define the shape and quality expectations for data and artifacts
# flowing between stages. Every stage references contracts by id.
# ──────────────────────────────────────────────────────────────────────────────
contracts:
  - id: ""                          # [REQUIRED] Unique snake_case identifier. Referenced by stages.
                                    # Example: "source_schema_profile"

    description: >                  # [REQUIRED] What this data represents and where it comes from.
      Describe the data this contract represents,
      its business meaning, and its source.

    direction: ""                   # [REQUIRED] How this contract is used in the pipeline.
                                    # Valid values: "input", "output", "internal"
                                    #   "input"    — data entering the agent from an external source
                                    #   "output"   — data leaving the agent to a destination
                                    #   "internal" — data passed between stages within the agent

    storage_mode: ""                # [OPTIONAL] How contract payloads are transferred.
                                    # Valid values: "inline", "claim_check" (Default: "inline")
                                    #   "inline"      — passed directly in node state / memory
                                    #   "claim_check" — written to disk/storage; state passes pointer metadata

    fields:                         # [REQUIRED] List of fields in this contract.
      - name: ""                    # [REQUIRED] Field name. Example: "customer_id"
        type: ""                    # [REQUIRED] Data type.
                                    # Valid values: "string", "integer", "float", "boolean",
                                    #   "date", "datetime", "timestamp", "json", "array", "object", "binary"
        nullable: false             # [OPTIONAL] Whether this field can be null. Default: false.
        description: >              # [REQUIRED] What this field represents.
          Describe the business meaning of this field.

        items:                      # [OPTIONAL] For "array" or "object" types: item schema or nested model.
          type: ""                  # Element type (e.g., "string", "object")
          model_name: ""            # [OPTIONAL for object] Deterministic Pydantic sub-model name (e.g. "StagingModelSpec")
          fields: []                # [OPTIONAL for object] Nested field definitions following this same structure

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
      max_items: null               # Maximum expected items or records. Null = no limit.
      min_items: null               # Minimum expected items or records. Null = no minimum.
      freshness_hours: null         # Data must not be older than this many hours. Null = no requirement.


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 5: Governance & Classification
# ──────────────────────────────────────────────────────────────────────────────
# Note on PII & Data Protection: In enterprise environments, analytical warehouses
# (Snowflake, Databricks) enforce dynamic masking policies at query time via column tags,
# or data is tokenized at the ingestion gateway before landing. Pipeline-level masking/
# tokenization (pii_strategy) is only applicable for edge egress to untrusted external
# endpoints (e.g. third-party APIs); for internal analytical destinations, declare data
# sensitivity tags on contracts instead.
governance:
  pii_fields: []                    # [REQUIRED if agent handles personal data, otherwise OPTIONAL]
                                    # List of field names (from any contract) that contain PII.
                                    # Example: ["email", "phone_number", "social_security_number"]

  pii_strategy: ""                  # [REQUIRED only if pii_fields is non-empty AND egressing to untrusted target]
                                    # Valid values: "mask", "hash", "encrypt", "redact", "tokenize", "none_warehouse_managed"

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
                                    # Valid values: "git_repo", "data_catalog", "dbt_project", "relational_db",
                                    #   "postgres", "mysql", "snowflake", "bigquery", "redshift",
                                    #   "s3", "gcs", "azure_blob", "sftp", "http_api", "local_filesystem", "other"
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
                                    #   "on_demand"    — triggered manually or via API invocation
                                    #   "cron"         — cron-scheduled recurring execution
                                    #   "event_driven" — triggered by external event (webhook, Git push, PR)
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
                                    #   - agent: "dbt_ci_test_runner"
                                    #     contract: "delivery_manifest"
                                    #     description: "CI pipeline that triggers integration tests on opened Pull Requests"

  shared_components: []             # [OPTIONAL] Existing shared components to reuse.
                                    # The Architect verifies these exist in the component registry.
                                    # Example:
                                    #   - name: "metadata_telemetry_callback"
                                    #     version: "1.0.0"
                                    #     usage: "Tier 1 agent run logging and prompt token telemetry"
                                    #   - name: "git_deliverer_v1"
                                    #     version: "2.1.0"
                                    #     usage: "Shared GitHub/GitLab PR automation wrapper"

  description: >                    # [OPTIONAL] Additional cross-agent context.
    Describe the agent's role in the broader data ecosystem.
    Which teams depend on its outputs? What breaks if it fails?


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 8: Constraints and Preferences
# ──────────────────────────────────────────────────────────────────────────────
constraints:
  required_libraries: []            # [OPTIONAL] Libraries that MUST be used.
                                    # Example: ["litellm", "instructor", "sqlglot", "dbt-core"]

  avoided_libraries: []             # [OPTIONAL] Libraries that MUST NOT be used.
                                    # Example: ["pyspark", "langchain_community"]

  performance:                      # [OPTIONAL] Performance requirements.
    max_latency_seconds: null       # Maximum acceptable end-to-end latency per run. Null = no SLA.
    max_cost_per_run_usd: null      # Maximum acceptable LLM token cost per run in USD. Null = no limit.

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

The following is a realistic, fully filled-in spec for a "dbt Pipeline Builder Agent" — the canonical Layer 2 DDLC AI Agent referenced across the architecture and implementation documentation.

```yaml
# ============================================================================
# dbt Pipeline Builder Agent — Agent Specification
# ============================================================================
# This is a completed spec for an AI agent that inspects source relational
# database catalogs, plans transformation models, synthesizes dbt SQL and
# schema YAML files, dry-runs them in an isolated DuckDB/Snowflake sandbox,
# halts for human approval, and opens a Git Pull Request with the pipeline.
# ============================================================================

agent:
  name: "dbt_pipeline_builder_agent"
  version: "1.0.0"
  description: >
    Inspects source database schemas, reasons over transformation requirements,
    generates modular dbt SQL models with YAML documentation and schema tests,
    dry-runs and validates compilation in an isolated development sandbox,
    presents generated artifacts for human review, and delivers the approved
    pipeline directly as a Git Pull Request. Runs on demand or via CI/CD events.
  owner: "analytics-engineering-team"
  domain: "analytics"
  tags: ["dbt", "code_generation", "analytics_engineering", "gitops", "llm"]

architecture:
  autonomy_level: "semi_autonomous"
  complexity: "complex"
  runtime_preference: "langgraph"
  description: >
    Stateful LangGraph agent structured with explicit reflection loops and a human
    review gate. Stage 1 introspects source catalog tables; Stage 2 reasons over
    business requirements to build an AST model plan; Stage 3 synthesizes dbt SQL
    and YAML; Stage 4 executes sandbox compilation and linting, looping back to Stage 3
    on syntax or model errors (max 3 retries); Stage 5 halts unconditionally for human
    approval; Stage 6 commits code to a Git branch and opens a Pull Request.

stages:
  - id: "discover"
    type: "discovery"
    description: >
      Connects to source database information schemas and data catalogs to discover
      tables, column names, data types, primary keys, and foreign key relationships.
      Produces a structured source schema profile for downstream reasoning.
    autonomy: "autonomous"
    input_contract: "source_database_reference"
    output_contract: "source_schema_profile"
    tools: ["inspect_information_schema", "query_catalog_lineage"]
    error_strategy: "retry"
    retry_config:
      max_retries: 3
      backoff_seconds: 30
    description_details: >
      Target databases: PostgreSQL OLTP schemas (public, sales, inventory).
      Extract column comments, nullability constraints, and relational cardinalities.
      Handle connection timeouts gracefully with exponential backoff.

  - id: "plan"
    type: "reasoning"
    description: >
      Analyzes the source schema profile alongside business transformation goals.
      Formulates a multi-layer dbt modeling DAG (staging, intermediate, marts)
      specifying model names, materialization strategies, and join dependencies.
    autonomy: "autonomous"
    input_contract: "source_schema_profile"
    output_contract: "transformation_plan"
    tools: ["query_tier2_contracts", "sqlglot_analyze_lineage"]
    error_strategy: "reflect"
    retry_config:
      max_retries: 2
      backoff_seconds: 10
    description_details: >
      Model tiers must strictly adhere to the analytics engineering style guide:
      stg_ (views), int_ (ephemeral or tables), fct_ / dim_ (tables or incremental).
      Identifies primary key uniqueness tests and relationship tests for each model.

  - id: "generate"
    type: "code_generation"
    description: >
      Synthesizes dbt Jinja-SQL model files and companion YAML schema files
      (containing column descriptions, sources, and dbt generic tests)
      based on the approved transformation plan.
    autonomy: "autonomous"
    input_contract: "transformation_plan"
    output_contract: "dbt_artifact_bundle"
    tools: ["render_jinja_template", "sqlglot_format_sql"]
    error_strategy: "reflect"
    retry_config:
      max_retries: 3
      backoff_seconds: 5
    description_details: >
      Generates models/staging, models/marts, and models/schema.yml.
      Enforces SQLGlot dialect safety (e.g. Snowflake or DuckDB).
      Bundles all generated files into a Claim Check bundle URI.

  - id: "dry_run"
    type: "sandbox_testing"
    description: >
      Executes dbt compile and dbt test against an isolated sandbox environment
      (or DuckDB runner) to verify SQL syntax, macro resolution, and DAG validity.
      On syntax or compilation error, feeds diagnostic tracebacks back to Stage 3.
    autonomy: "autonomous"
    input_contract: "dbt_artifact_bundle"
    output_contract: "compilation_report"
    tools: ["run_dbt_compile", "run_sqlglot_validate"]
    error_strategy: "reflect"
    retry_config:
      max_retries: 3
      backoff_seconds: 15
    description_details: >
      Executes in an ephemeral sandbox. Captures compiler stdout, stderr, and SQLGlot
      AST parse diagnostics. If dbt compile exits non-zero, reflects back to
      generate with the exact failing model path and compiler error.

  - id: "review"
    type: "human_review"
    description: >
      Pauses execution at an explicit human-in-the-loop gate. Presents the generated
      dbt models, YAML documentation, and sandbox compilation report to an analytics
      engineer for visual inspection and sign-off.
    autonomy: "human_required"
    input_contract: "compilation_report"
    output_contract: "reviewed_artifact_bundle"
    tools: ["render_diff_summary"]
    error_strategy: "escalate"
    description_details: >
      Invokes LangGraph interrupt() to serialize state to the PostgreSQL checkpoint table.
      Surfaces git diffs and lineage diagrams in Chainlit or Web UI. Resumes on human approval.

  - id: "deliver"
    type: "delivery"
    description: >
      Creates a new Git branch, commits all validated dbt models and schema YAML files,
      pushes the branch to the remote repository, and opens a Pull Request with a
      comprehensive markdown summary of models, tests, and lineage.
    autonomy: "autonomous"
    input_contract: "reviewed_artifact_bundle"
    output_contract: "delivery_manifest"
    tools: ["create_git_branch", "commit_files", "open_pull_request"]
    error_strategy: "fail_fast"
    retry_config:
      max_retries: 2
      backoff_seconds: 20
    description_details: >
      Branch naming pattern: feat/dbt-models-{run_id}.
      PR body includes transformation summary, source references, and test checklist.

contracts:
  - id: "source_database_reference"
    description: >
      Initial input parameters specifying target source databases, schemas,
      and connection references for catalog introspection.
    direction: "input"
    fields:
      - name: "database_name"
        type: "string"
        description: "Name of the source database catalog (e.g., 'production_oltp')"
        quality_rules: ["must not be empty"]
      - name: "schema_names"
        type: "array"
        description: "List of database schema names to introspect"
        quality_rules: ["must contain at least one schema"]
      - name: "connection_ref"
        type: "string"
        description: "Configuration reference key for database credentials in secrets store"
        quality_rules: ["must start with secret:// or config://"]

  - id: "source_schema_profile"
    description: >
      Introspected catalog metadata describing tables, columns, data types,
      keys, and constraints discovered across source schemas.
    direction: "internal"
    fields:
      - name: "database_name"
        type: "string"
        description: "Source database catalog name"
        quality_rules: ["must not be empty"]
      - name: "tables"
        type: "array"
        description: "List of discovered tables with column definitions and primary keys"
        items:
          type: "object"
          model_name: "TableProfile"
          fields:
            - name: "table_name"
              type: "string"
              description: "Source table identifier"
            - name: "schema_name"
              type: "string"
              description: "Schema namespace"
            - name: "columns"
              type: "array"
              description: "List of column definitions"
              items:
                type: "object"
                model_name: "ColumnProfile"
                fields:
                  - name: "name"
                    type: "string"
                    description: "Column name"
                  - name: "data_type"
                    type: "string"
                    description: "SQL data type"
                  - name: "primary_key"
                    type: "boolean"
                    description: "Whether column is part of primary key"
        quality_rules: ["must contain at least one table definition"]
      - name: "foreign_keys"
        type: "array"
        description: "Inferred and declared relational foreign keys across tables"
      - name: "profiled_at"
        type: "timestamp"
        description: "UTC timestamp when the database inspection completed"

  - id: "transformation_plan"
    description: >
      Structured architectural plan generated by the LLM reasoning stage outlining
      the DAG of dbt models to build, join strategies, and test definitions.
    direction: "internal"
    fields:
      - name: "project_name"
        type: "string"
        description: "Target dbt project identifier"
        quality_rules: ["must not be empty"]
      - name: "target_dialect"
        type: "string"
        description: "SQL dialect for the models (e.g. snowflake, postgres, duckdb)"
        quality_rules: ["must be a supported SQLGlot dialect"]
      - name: "staging_models"
        type: "array"
        description: "List of staging models mapping source tables with 1:1 clean views"
        items:
          type: "object"
          model_name: "StagingModelSpec"
          fields:
            - name: "model_name"
              type: "string"
              description: "Staging model identifier (e.g., stg_customers)"
            - name: "source_table"
              type: "string"
              description: "Source table identifier"
            - name: "columns"
              type: "array"
              items:
                type: "string"
              description: "Projected column list"
      - name: "marts_models"
        type: "array"
        description: "List of dimensional/fact mart models joining staging sources"
        items:
          type: "object"
          model_name: "MartModelSpec"
          fields:
            - name: "model_name"
              type: "string"
              description: "Mart model identifier (e.g., fct_orders)"
            - name: "materialization"
              type: "string"
              default: "table"
              description: "dbt materialization strategy (table, view, incremental)"
            - name: "description"
              type: "string"
              description: "Business documentation for the mart model"
            - name: "staging_dependencies"
              type: "array"
              items:
                type: "string"
              description: "Upstream staging models referenced"
      - name: "total_models_planned"
        type: "integer"
        description: "Count of models planned for synthesis"
        quality_rules: ["must be between 1 and 100"]

  - id: "dbt_artifact_bundle"
    description: >
      Claim Check reference pointing to the bundle of synthesized dbt SQL models,
      macros, and schema.yml specification files written to staging disk.
    direction: "internal"
    storage_mode: "claim_check"
    fields:
      - name: "bundle_id"
        type: "string"
        description: "Unique UUID for the artifact bundle"
        quality_rules: ["must be a valid UUIDv4"]
      - name: "storage_uri"
        type: "string"
        description: "File or object storage URI pointing to the artifact archive"
        quality_rules: ["must be a valid file:// or s3:// URI"]
      - name: "file_manifest"
        type: "array"
        description: "List of relative file paths contained in the generated bundle"
        quality_rules: ["must contain at least one .sql model and one .yml schema"]
      - name: "artifact_sha256"
        type: "string"
        description: "Cryptographic hash of the bundle archive for tamper-proof verification"
        quality_rules: ["must match regex ^[a-f0-9]{64}$"]

  - id: "compilation_report"
    description: >
      Execution diagnostics from the sandbox compilation and validation run,
      including dbt compile stdout, linting status, and error traces.
    direction: "internal"
    fields:
      - name: "bundle_id"
        type: "string"
        description: "Reference to the dbt_artifact_bundle evaluated"
      - name: "success"
        type: "boolean"
        description: "True if all models compiled cleanly with zero syntax or test errors"
      - name: "compilation_errors"
        type: "array"
        description: "List of compiler or parser errors encountered, if any"
      - name: "sandbox_runtime_seconds"
        type: "float"
        description: "Duration of sandbox execution in seconds"

  - id: "reviewed_artifact_bundle"
    description: >
      Signed-off artifact bundle post-human review gate, containing human approval
      metadata and any reviewer feedback.
    direction: "internal"
    fields:
      - name: "bundle_id"
        type: "string"
        description: "Reference to the approved dbt_artifact_bundle"
      - name: "approved_by"
        type: "string"
        description: "User ID or email of the approving engineer"
        quality_rules: ["must not be empty"]
      - name: "approval_timestamp"
        type: "timestamp"
        description: "UTC timestamp when human approval was granted"

  - id: "delivery_manifest"
    description: >
      Output manifest documenting the delivered Git repository branch, commit SHA,
      and opened Pull Request URL.
    direction: "output"
    fields:
      - name: "manifest_id"
        type: "string"
        description: "Unique UUID identifying this delivery manifest"
      - name: "run_id"
        type: "string"
        description: "Run ID that generated this delivery"
      - name: "repository"
        type: "string"
        description: "Git repository slug (e.g. 'org/analytics-dbt-repo')"
        quality_rules: ["must match regex ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"]
      - name: "branch_name"
        type: "string"
        description: "Git branch where artifacts were committed"
        quality_rules: ["must start with feat/"]
      - name: "commit_sha"
        type: "string"
        description: "Git commit hash of the committed files"
        quality_rules: ["must match regex ^[a-f0-9]{40}$"]
      - name: "pull_request_url"
        type: "string"
        description: "URL of the created Pull Request"
        quality_rules: ["must be a valid URL"]
      - name: "artifact_sha256"
        type: "string"
        description: "Cryptographic SHA-256 hash of the delivered bundle"
        quality_rules: ["must match regex ^[a-f0-9]{64}$"]
      - name: "delivered_at"
        type: "timestamp"
        description: "UTC timestamp when the delivery was finalized"

governance:
  approval_gates: ["review"]
  audit_requirements:
    enabled: true
    log_level: "comprehensive"
    retention_days: 365
  compliance_rules:
    - "All generated SQL must conform to company analytics engineering naming conventions"
    - "No hardcoded credentials or plaintext secrets in dbt profiles or connection strings"
    - "Every staging and marts model must include at least one unique test and one not_null test"
    - "Side-effecting Git delivery operations require unconditional human-in-the-loop review"
  data_classification: "internal"
  description: >
    Agent-generated code and Pull Requests are subject to GitOps peer review.
    All LLM prompt/completion spans, tool invocations, and human approval timestamps
    are recorded in the Tier 1 metadata operational telemetry database.

infrastructure:
  sources:
    - name: "production_oltp_postgres"
      system: "postgres"
      connection: "config://databases.postgres_oltp"
      format: "database_table"
      description: >
        Read-only replica of the transactional OLTP database for catalog
        introspection and information_schema queries.
  destinations:
    - name: "analytics_git_repo"
      system: "git_repo"
      connection: "config://git.target_repo_uri"
      format: "git_repo"
      description: >
        Enterprise GitHub / GitLab repository housing the dbt analytics engineering project.
  scheduling:
    type: "on_demand"
    description: >
      Invoked on demand by analytics engineers via CLI, Slack, or webhook trigger
      when onboarding new source tables or refactoring data models.
  resource_constraints:
    max_memory_gb: 4.0
    max_cpu_cores: 2
    max_execution_minutes: 15
    description: >
      Lightweight compute requirements. Code generation and LLM reasoning run via
      LiteLLM API calls. Sandbox compilation runs in isolated local DuckDB memory.

cross_agent:
  upstream_dependencies: []
  downstream_consumers:
    - agent: "dbt_ci_test_runner"
      contract: "delivery_manifest"
      description: "CI pipeline that triggers integration tests on opened Pull Requests"
  shared_components:
    - name: "metadata_telemetry_callback"
      version: "1.0.0"
      usage: "Tier 1 agent run logging and prompt token telemetry"
    - name: "git_deliverer_v1"
      version: "2.1.0"
      usage: "Shared GitHub/GitLab PR automation wrapper"
  description: >
    This agent serves as the front-door builder for the analytics data platform.
    It synthesizes verified dbt code that downstream CI and transformation runners execute.

constraints:
  required_libraries:
    - "litellm"
    - "instructor"
    - "sqlglot"
    - "dbt-core"
  avoided_libraries:
    - "pyspark"
    - "langchain_community"
  performance:
    max_latency_seconds: 600
  sla:
    availability_percent: 99.0
    max_downtime_minutes: 60
    recovery_time_minutes: 15
  description: >
    The agent must run idempotently. Repeated runs on the same source schema must produce
    deterministic model naming and identical AST structures for stable git diffs.
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
| `architecture.autonomy_level` | string | Yes | Enum: autonomous, semi_autonomous, human_required | Architect |
| `architecture.complexity` | string | No | Enum: simple, moderate, complex | Architect |
| `architecture.runtime_preference` | string | No | Enum: langgraph, python_litellm, no_preference | Architect |
| `architecture.description` | string | No | Free text | Architect |
| `stages[].id` | string | Yes | Snake_case, unique within stages | All specialists |
| `stages[].type` | string | Yes | Enum: discovery, reasoning, code_generation, sandbox_testing, human_review, delivery, custom | Architect, Documentarian |
| `stages[].description` | string | Yes | Free text | Spec Analyst, Documentarian |
| `stages[].autonomy` | string | Yes | Enum: autonomous, semi_autonomous, human_required | Architect |
| `stages[].input_contract` | string | Yes | Must reference a contract id | Spec Analyst (chain validation) |
| `stages[].output_contract` | string | Yes | Must reference a contract id | Spec Analyst (chain validation) |
| `stages[].tools` | list[string] | No | Free text (Researcher validates) | Researcher |
| `stages[].error_strategy` | string | No | Enum: fail_fast, retry, reflect, skip, escalate | Documentarian |
| `stages[].retry_config.max_retries` | integer | No | Positive integer | Documentarian |
| `stages[].retry_config.backoff_seconds` | integer | No | Positive integer | Documentarian |
| `stages[].description_details` | string | No | Free text | Documentarian |
| `contracts[].id` | string | Yes | Snake_case, unique | All specialists |
| `contracts[].description` | string | Yes | Free text | Spec Analyst |
| `contracts[].direction` | string | Yes | Enum: input, output, internal | Spec Analyst, Documentarian |
| `contracts[].storage_mode` | string | No | Enum: inline, claim_check (default: inline) | Documentarian, Builder |
| `contracts[].fields[].name` | string | Yes | Snake_case | Documentarian, Validator |
| `contracts[].fields[].type` | string | Yes | Enum: string, integer, float, boolean, date, datetime, timestamp, json, array, object, binary | Documentarian, Builder |
| `contracts[].fields[].nullable` | boolean | No | true/false (default: false) | Documentarian, Builder |
| `contracts[].fields[].description` | string | Yes | Free text | Documentarian |
| `contracts[].fields[].items` | object | No | Element schema or nested model for array/object types | Documentarian, Builder |
| `contracts[].fields[].items.model_name` | string | No | PascalCase identifier for deterministic Pydantic sub-model compilation | Documentarian, Builder |
| `contracts[].fields[].items.fields` | list[object] | No | Nested field list following same field structure | Documentarian, Builder |
| `contracts[].fields[].quality_rules` | list[string] | No | Free text rules | Documentarian, Validator |
| `contracts[].sla.max_items` | integer | No | Positive integer or null | Documentarian |
| `contracts[].sla.min_items` | integer | No | Positive integer or null | Documentarian |
| `contracts[].sla.freshness_hours` | integer | No | Positive integer or null | Documentarian |
| `governance.pii_fields` | list[string] | Conditional | Field names from contracts | Documentarian, Validator |
| `governance.pii_strategy` | string | Conditional | Enum: mask, hash, encrypt, redact, tokenize, none_warehouse_managed | Documentarian |
| `governance.audit_requirements.enabled` | boolean | No | true/false (default: false) | Documentarian |
| `governance.audit_requirements.log_level` | string | No | Enum: minimal, standard, comprehensive | Documentarian |
| `governance.audit_requirements.retention_days` | integer | No | Positive integer or null | Documentarian |
| `governance.approval_gates` | list[string] | No | Stage ids from stages section | Architect |
| `governance.compliance_rules` | list[string] | No | Free text | Documentarian, Validator |
| `governance.data_classification` | string | No | Enum: public, internal, confidential, restricted | Architect, Documentarian |
| `governance.description` | string | No | Free text | Spec Analyst |
| `infrastructure.sources[].name` | string | Yes | Free text | Researcher |
| `infrastructure.sources[].system` | string | Yes | Enum: git_repo, data_catalog, dbt_project, relational_db, postgres, mysql, snowflake, bigquery, redshift, s3, gcs, azure_blob, sftp, http_api, local_filesystem, other | Researcher |
| `infrastructure.sources[].connection` | string | Yes | URI pattern (no credentials) | Documentarian, Builder |
| `infrastructure.sources[].format` | string | No | Enum: csv, json, jsonl, parquet, avro, orc, xml, database_table, api_response, git_repo, other | Researcher, Documentarian |
| `infrastructure.sources[].description` | string | No | Free text | Researcher |
| `infrastructure.destinations[].name` | string | Yes | Free text | Researcher |
| `infrastructure.destinations[].system` | string | Yes | Same enum as sources | Researcher |
| `infrastructure.destinations[].connection` | string | Yes | URI pattern (no credentials) | Documentarian, Builder |
| `infrastructure.destinations[].format` | string | No | Same enum as source format | Researcher, Documentarian |
| `infrastructure.destinations[].description` | string | No | Free text | Researcher |
| `infrastructure.scheduling.type` | string | No | Enum: on_demand, cron, event_driven | Architect |
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
| `constraints.performance.max_cost_per_run_usd` | float | No | Positive number or null | Architect |
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
| Enum values valid | `autonomy_level` ∈ {autonomous, semi_autonomous, human_required}, etc. | **Reject** |
| Stage ids unique | No two stages share the same `id` | **Reject** |
| Contract ids unique | No two contracts share the same `id` | **Reject** |
| Version is semver | `agent.version` matches `\d+\.\d+\.\d+` | **Reject** |
| Agent name is snake_case | `agent.name` matches `^[a-z][a-z0-9_]*$` | **Reject** |
| Stage ids are snake_case | Each `stages[].id` matches `^[a-z][a-z0-9_]*$` | **Reject** |
| At least one stage defined | `stages` list is non-empty | **Reject** |
| At least one contract defined | `contracts` list is non-empty | **Reject** |
| Contract references on stages | Non-initial stages must declare `input_contract`; non-terminal stages must declare `output_contract` | **Reject** |
| Each contract has at least one field | `fields` list is non-empty | **Reject** |
| PII strategy required when PII fields listed | If `governance.pii_fields` is non-empty, `governance.pii_strategy` must be set | **Reject** |
| Retry config valid | If `error_strategy` is "retry" or "reflect", `retry_config` must be present with positive values | **Reject** |

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
| Source/destination consistency | Stages of type "discovery" or reading from external systems should have a corresponding source in infrastructure; stages of type "delivery" should have a corresponding destination in infrastructure | **Clarify** — ask if the infrastructure is missing or intentionally omitted |
| Cross-agent reference validation | Agents listed in `upstream_dependencies` and `downstream_consumers` are checked against the metadata framework's agent registry | **Clarify** — ask if the referenced agent exists or is planned |
| Shared component existence | Components listed in `cross_agent.shared_components` are checked against the component registry | **Clarify** — ask if the component should be created as new |
| Governance completeness | If `data_classification` is "confidential" or "restricted", are PII rules defined? Is audit logging enabled? | **Clarify** — surface potential governance gaps |
| Scheduling vs. trigger consistency | If scheduling type is "event_driven", agent must declare an event trigger, webhook, or listener in infrastructure/sources | **Clarify** — ask if the scheduling trigger is fully specified |
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
| 6 | Quality rules as free text strings | list[string] with natural language rules | Quality rules are too varied to enumerate as an enum. The Documentarian translates these into concrete Instructor/Pydantic validators, native dbt generic tests, or AST verifiers. LLM interpretation is appropriate here. |
| 7 | No credentials in the spec | Connection fields hold URI patterns, not credentials | Credentials belong in config files and secrets managers, never in specification documents. The approach.md rule: no hardcoding. |
| 8 | Cross-agent references are names, not ids | Reference agents and components by human-readable name | The Spec Analyst and Architect resolve names to registry ids via the metadata framework. Users should not need to know internal ids. |
| 9 | Governance as a dedicated section | Not distributed across stages | Governance rules often span multiple stages (PII masking in one stage, audit logging across all stages, approval gates at specific stages). Centralizing governance in one section ensures nothing is missed. The Documentarian distributes these to specific enforcement points in the impl docs. |
| 10 | Infrastructure separated from stages | Sources and destinations are top-level, not stage-local | Multiple stages may read from the same source or write to the same destination. Top-level infrastructure avoids duplication and gives the Researcher a single place to evaluate all external system dependencies. |
| 11 | Description fields on nearly everything | description, description_details, and per-section descriptions | The spec must serve both the Pydantic parser (structured fields) and the LLM specialists (intent and context). Description fields bridge the gap — they carry business context that no enum can capture. |
| 12 | Conditional required fields | PII strategy required only when PII fields listed | Avoids forcing users to fill in governance fields when the agent handles no PII. The Pydantic validator enforces conditional requirements. |
| 13 | Retire spec.yaml post-build | Day-0 bootstrapping intake form only; retired post-build | Eliminates dual-spec drift. Once an agent is delivered, `impl_docs/*.yaml` in Git is the single source of truth. Day-2 enhancements operate directly on `impl_docs/`, never on `spec.yaml`. |
| 14 | Layer 2 DDLC AI Agent Modeling | Intake spec models AI agents (e.g. dbt Pipeline Builder Agent) that build and operate data pipelines | Distinguishes Layer 2 (LLM-driven data engineering agents) from Layer 3 (raw batch/streaming data pipelines). Eliminates batch record loop assumptions from agent intake. |

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
4. **Example spec library** — Additional filled-in specs for different DDLC AI agent types (e.g., dbt pipeline builder, schema evolution agent, data quality monitor) to test the Spec Analyst and Architect
