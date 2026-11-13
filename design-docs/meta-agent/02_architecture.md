# DDLC Meta-Agent — Architecture

## Coordinator-Specialist with Hierarchical State Machine and Blackboard State

> **Document Status**: Draft v0.1  
> **Last Updated**: 2026-11-09  
> **Audience**: Architecture & Development Team  
> **Prerequisite**: [01_approach.md](file:///S:/DATA/agents/ddlc/meta-agent/01_approach.md) — read that first

---

## System Overview

```
                                       ┌──────────────────────────────────────────────────────────────────────────────────────┐
                                       │                                                                                      │
                                       │                                   DDLC META-AGENT                                    │
                                       │  ┌────────────────────────────────────────────────────────────────────────────────┐  │
                                       │  │                                  COORDINATOR                                   │  │
                                       │  │                    (LangGraph Deterministic State Machine)                     │  │
                                       │  │                                                                                │  │
                                       │  │    Deterministic workflow control -- phase sequencing, conditional routing,    │  │
                                       │  │     human gate enforcement, safety valves, mode switching (build / change)     │  │
                                       │  │                                                                                │  │
                                       │  │  ┌───────────┐  ┌───────────┐   ┌───────────┐  ┌─────────────┐  ┌───────────┐  │  │
                                       │  │  │   PHASE   │  │   PHASE   │   │   PHASE   │  │    PHASE    │  │   PHASE   │  │  │
                                       │  │  │UNDERSTAND │→ │ RESEARCH  │ → │  DESIGN   │→ │  DOCUMENT   │→ │ CONSTRUCT │  │  │
                                       │  │  │ Subgraph  │  │ Subgraph  │   │ Subgraph  │  │  Subgraph   │  │ Subgraph  │  │  │
                                       │  │  └─────┬─────┘  └─────┬─────┘   └─────┬─────┘  └──────┬──────┘  └─────┬─────┘  │  │
                                       │  └───────┴──────────────┴───────────────┴───────────────┴───────────────┴─────────┘  │
                                       │          │              │               │               │               │            │
                                       │          ▼              ▼               ▼               ▼               ▼            │
                                       │  ┌───────┬──────────────┬───────────────┬───────────────┬───────────────┬─────────┐  │
                                       │  │                SPECIALISTS (Domain Intelligence -- LLM Powered)                │  │
                                       │  │                                                                                │  │
                                       │  │  ┌───────────┐  ┌───────────┐   ┌───────────┐  ┌─────────────┐  ┌───────────┐  │  │
                                       │  │  │   Spec    │  │Researcher │   │ Architect │  │Documentarian│  │  Builder  │  │  │
                                       │  │  │  Analyst  │  │           │   │           │  │             │  │           │  │  │
                                       │  │  │  parses   │  │  queries  │   │  queries  │  │ translates  │  │   reads   │  │  │
                                       │  │  │  spec,    │  │  RAG for  │   │  RAG for  │  │ architecture│  │ impl docs │  │  │
                                       │  │  │   finds   │  │ library & │   │ patterns  │  │  into YAML  │  │  & emits  │  │  │
                                       │  │  │   gaps    │  │ tool docs │   │& decisions│  │  impl docs  │  │ codebase  │  │  │
                                       │  │  └───────────┘  └─────┬─────┘   └─────┬─────┘  └─────────────┘  └─────▲─────┘  │  │
                                       │  │  ┌───────────┐        │               │                              │         │  │
                                       │  │  │ Validator │        │               │                              │         │  │
                                       │  │  │ verifies  │────────┼───────────────┼── fix loop (max N) ──────────┘         │  │
                                       │  │  │ build vs  │        │               │                                        │  │
                                       │  │  │ impl docs │        │               │                                        │  │
                                       │  │  └───────────┘        │               │                                        │  │
                                       │  └──────────────────────┴───────────────┴─────────────────────────────────────────┘  │
                                       │                         │               │                                            │
                                       │                     ┌───┴───────────────┴──────────────────────┐                     │
                                       │                     ▼                                          ▼                     │
                                       │  ┌─────────────────────────────────────┐    ┌─────────────────────────────────────┐  │
                                       │  │          BLACKBOARD STATE           │    │           KNOWLEDGE LAYER           │  │
                                       │  │          (LangGraph State)          │    │                                     │  │
                                       │  │                                     │    │  ┌───────────────────────────────┐  │  │
                                       │  │  • Shared accumulating state        │    │  │      RAG Knowledge Base       │  │  │
                                       │  │  • Append-only decision & spec log  │    │  │                               │  │  │
                                       │  │  • State projection slice per agent │    │  │  • Library & Tool Docs        │  │  │
                                       │  │  • Checkpointed after every node    │    │  │  • Organizational Standards   │  │  │
                                       │  │                                     │    │  │  • Architecture Catalog       │  │  │
                                       │  └──────────────────┬──────────────────┘    │  │  • Past Build Decisions       │  │  │
                                       │                     │                       │  │  • Component Interface Docs   │  │  │
                                       │  ┌──────────────────┴──────────────────┐    │  │  • Code & Test Conventions    │  │  │
                                       │  │             HUMAN GATES             │    │  └───────────────────────────────┘  │  │
                                       │  │                                     │    │                                     │  │
                                       │  │  [1] Clarification Approval         │    │  ┌───────────────────────────────┐  │  │
                                       │  │  [2] Architecture Proposal Review   │    │  │      Metadata Framework       │  │  │
                                       │  │  [3] Implementation Document Review │    │  │     (Postgres Relational)     │  │  │
                                       │  │  [4] Build Fix Escalation Valve     │    │  │                               │  │  │
                                       │  │                                     │    │  │  • Catalog (Agents/Components)│  │  │
                                       │  └──────────────────┬──────────────────┘    │  │  • Lineage (Dependencies)     │  │  │
                                       │                     │                       │  │  • Operational (Runs/Logs)    │  │  │
                                       │                     │                       │  │  • Audit & Governance Logs    │  │  │
                                       │                     │                       │  └───────────────────────────────┘  │  │
                                       │                     │                       └──────────────────┬──────────────────┘  │
                                       │                     │                                          │                     │
                                       │                     ▼                                          ▼                     │
                                       │                     ┌──────────────────────────────────────────┐                     │
                                       │                     │          POSTGRESQL + PGVECTOR           │                     │
                                       │                     │         (Single Storage Engine)          │                     │
                                       │                     │                                          │                     │
                                       │                     │  • State Checkpoints (Resumability)      │                     │
                                       │                     │  • Metadata Framework Tables (SQL)       │                     │
                                       │                     │  • RAG Embeddings & Vectors (pgvector)   │                     │
                                       │                     └────────────────────┬─────────────────────┘                     │
                                       └──────────────────────────────────────────┴───────────────────────────────────────────┘
                                                                                  │
                                                                                  ▼
                                                             ┌──────────────────────────────────────────┐
                                                             │               CHAINLIT UI                │
                                                             │     (User Interface & Gate Prompts)      │
                                                             │                                          │
                                                             │  • Conversational Chat Interface         │
                                                             │  • Human Gate Review Forms & Prompts     │
                                                             │  • Streaming Specialist Execution Logs   │
                                                             │  • Session Persistence & Resumption      │
                                                             └──────────────────────────────────────────┘
```                                   
                                   
---                                   
                                   
##1. Document Purpose

The approach document established *what* the meta-agent does (builds DDLC agents from specifications), *why* it uses a state machine (enforced workflow with loops, branches, and human gates), and *which technologies* it uses (LangGraph, LiteLLM, Instructor, Postgres).

This document defines *how* the meta-agent is architecturally organized:

- What pattern governs its internal structure
- How responsibilities are divided between components
- How data flows and accumulates without overwhelming any single component
- How it builds agents with completely different architectures from the same process
- How it acquires and uses knowledge
- How quality is evaluated

This document is deliberately free of code. It describes the system at the level of components, responsibilities, data flows, and decisions. Detailed schemas, node specifications, database DDL, and configuration formats are the subject of subsequent documents.

---

## 2. The Architecture Pattern

The meta-agent uses three patterns, each governing a different concern:

| Concern | Pattern | What It Governs |
|---|---|---|
| **Organization** | Coordinator-Specialist | How responsibilities are divided — the coordinator owns workflow, specialists own domains |
| **Control Flow** | Hierarchical State Machine | How phases sequence, how nodes within phases connect, where loops and gates live |
| **Data Model** | Blackboard | How state works — a shared, accumulating knowledge structure that every specialist reads from and writes to |

These are not three separate systems. They are three lenses on the same system. The coordinator-specialist pattern describes the component boundaries. The hierarchical state machine describes how those components are invoked. The blackboard describes how they share information.

### 2.1 Coordinator-Specialist

A coordinator manages the workflow and routes work to domain specialists. The coordinator holds the workflow logic — the sequencing, the branching conditions, the gate enforcement. The specialists hold the domain intelligence — how to parse a spec, how to design an architecture, how to generate code.

The critical property: **the coordinator is not an LLM**. It is deterministic code. It reads flags from the state ("is the architecture approved?") and returns the name of the next specialist to invoke. It never reasons about content. It never interprets natural language. It never needs a context window.

The specialists use LLMs. They receive focused context, do intelligent work, and write results back to the shared state. Each specialist is scoped to one domain and sees only the state relevant to its job.

This separation is the approach document's principle made structural: "the LLM reasons, the graph orchestrates."

### 2.2 Hierarchical State Machine

The workflow is not a flat list of steps. It is a hierarchy:

**Top level** — the phase sequence:

> Understand → Research -> Design → Document → Construct (for new builds)  
> Analyze → Update → Review → Rebuild (for changes)

**Second level** — within each phase, an internal workflow with its own loops, branches, and gates:

> Understanding phase: parse spec → identify gaps → present questions → process answers → (loop back to identify gaps, or advance)  
> Design phase: draft architecture → present for review → (approved → advance, or revise → loop back to present)

Each phase is a self-contained state machine. It has its own entry point, its own exit condition, and its own internal routing logic. The top-level machine doesn't know or care about the internal structure of each phase — it just knows when a phase is done and what phase comes next.

This maps directly to LangGraph's subgraph model. Each phase is a subgraph. The top-level graph connects them. Each subgraph can be compiled, tested, and debugged in isolation.

### 2.3 Blackboard

The state is a shared knowledge structure — the blackboard — that starts nearly empty and progressively fills as specialists contribute.

At the start of a run, the blackboard contains just the user's specification. By the time the process completes, it contains the parsed spec, every clarification question and answer, all research findings, the approved architecture (and its revision history), the implementation documents, and the validation results.

No specialist passes data directly to another specialist. Every specialist reads from the blackboard and writes back to it. This means:

- **Any specialist can read anything a prior specialist wrote** — the Architect can see what the Researcher found
- **The full history is preserved** — rejected architecture revisions remain in the blackboard for traceability
- **The process can resume from any point** — the blackboard is checkpointed after every specialist invocation

---

## 3. The Coordinator

The coordinator is the LangGraph graph structure — nodes, edges, and routing functions. It is the simplest component in the system and the most important.

### What the Coordinator Does

1. **Enforces phase ordering** — Understanding must complete before Design. Design must complete before Documentation. The coordinator ensures no phase is skipped.
2. **Evaluates transition conditions** — After each specialist runs, the coordinator checks the state to decide what happens next. "Are there clarification questions?" → route to the human gate. "Is the architecture approved?" → advance to documentation.
3. **Enforces human gates** — At defined points, the coordinator pauses execution and waits for human input. The graph structurally cannot advance until the human responds.
4. **Enforces safety valves** — Loops have maximum iteration counts. If clarification has gone through five rounds without resolution, the coordinator escalates rather than looping forever.
5. **Routes between modes** — A new build enters the Understanding phase. A change request enters the Impact Analysis phase. Same graph, different entry point.

### What the Coordinator Does NOT Do

1. **It does not use an LLM** — every routing decision is a deterministic field lookup on the state
2. **It does not interpret content** — it reads flags like "approved" or "revisions_requested," never the content of the architecture itself
3. **It does not hold a context window** — it has no prompt, no token budget, no risk of context overflow
4. **It does not know domain logic** — it doesn't know what a data contract is or how to evaluate architecture quality. That is the specialists' job.

### Why This Matters for Context Management

The meta-agent's total workflow involves an enormous amount of information — specs, Q&A rounds, research findings, architecture proposals, implementation documents, generated code. If any single component had to hold all of this in its context, it would fail.

The coordinator solves this by being stateless with respect to content. It reads three-word flags, not three-thousand-token documents. The context pressure is entirely on the specialists, and each specialist manages its own context through state projection (see Section 5).

---

## 4. The Specialists

Six domain specialists, each responsible for one concern. Each specialist is a group of related nodes within the state machine — not a single monolithic function, but a coherent subsystem with a focused job.

### 4.1 Spec Analyst

**Domain**: Understanding the user's specification.

**Responsibilities**:
- Parse and validate the YAML spec against the template schema
- Extract structured information from the spec (stages, contracts, governance rules, tools, constraints)
- Identify gaps — ambiguities, missing information, contradictions, implicit assumptions
- Generate clarifying questions, categorized by type
- Incorporate user answers and assess their impact on the design

**Knowledge sources**: The spec template schema (to validate against), previous Q&A rounds (to avoid re-asking).

**Exits when**: All gaps are resolved, or the maximum clarification rounds are reached.

### 4.2 Researcher

**Domain**: Feasibility analysis and external knowledge.

**Responsibilities**:
- Evaluate whether the spec's requirements are achievable with available tools and patterns
- Research specific libraries and tools (Snowflake connector capabilities, PySpark limitations, Kafka client options)
- Assess tool compatibility (can these tools work together in the same pipeline?)
- Identify risks and constraints that the spec doesn't mention
- Produce a feasibility verdict with caveats

**Knowledge sources**: RAG — library and tool documentation, organizational standards, API references (see Section 6 for RAG details).

**Exits when**: A feasibility verdict is produced (feasible, feasible with caveats, or not feasible).

### 4.3 Architect

**Domain**: Agent architecture design.

**Responsibilities**:
- Select an architecture pattern for the DDLC agent being built (from the pattern catalog — see Section 7)
- Determine the runtime (LangGraph vs. Python + LiteLLM vs. other, based on agent complexity)
- Design each stage's high-level implementation approach
- Identify which shared components from the metadata framework (Tier 2 catalog) can be reused
- Identify what new shared components should be created
- Map tools and libraries to stages
- Document risks, trade-offs, and alternatives considered
- Incorporate revision feedback and re-present

**Knowledge sources**: RAG — architecture pattern catalog, past architecture decisions. Metadata framework (Tier 2 catalog: existing agents, shared components; see Section 6).

**Exits when**: The architecture is approved by the human.

### 4.4 Documentarian

**Domain**: Implementation document creation.

**Responsibilities**:
- Translate the approved architecture into YAML implementation documents
- Produce documents detailed enough that the Builder can generate code without ambiguity
- Ensure all data contracts are fully specified (every field, every quality rule, every SLA)
- Ensure all governance rules are mapped to specific enforcement points
- Ensure all configuration values are externalized (nothing hardcoded)
- Incorporate revision feedback and re-present

**Knowledge sources**: The approved architecture (from the Architect), shared component interface specs (from the metadata framework Tier 2 catalog).

**Exits when**: The implementation documents are approved by the human.

### 4.5 Builder

**Domain**: Code generation.

**Responsibilities**:
- Read the approved implementation documents and generate the agent's codebase
- Generate stage code, configuration files, governance enforcement hooks, and test suites
- For shared components: generate new ones into the shared folder, or generate imports for existing ones
- Select the code generation strategy based on the runtime (LangGraph agent, Python script, etc.)
- Respect project conventions (file structure, naming, code style)

**Knowledge sources**: The approved implementation documents (from the Documentarian), shared component source code (referenced in the metadata framework), code conventions (from RAG).

**Exits when**: All files are generated.

### 4.6 Validator

**Domain**: Build verification.

**Responsibilities**:
- Verify the generated code matches the implementation documents
- Check contract compliance — every field in every contract has a corresponding implementation
- Check governance compliance — every governance rule has an enforcement hook
- Check for hardcoded values — all configuration must be externalized
- Check shared component usage — reusable components are imported, not duplicated
- Run generated tests where possible (without live infrastructure)
- If issues are found, produce fix instructions for the Builder

**Knowledge sources**: The implementation documents (as ground truth), the generated code (as the thing being verified).

**Exits when**: All checks pass, or maximum fix attempts are exhausted (escalate to human).

---

## 5. The Blackboard and State Projection

### 5.1 The Funnel Principle

Each phase of the meta-agent acts as a **funnel**. It takes broad, messy input and produces narrow, precise output. The next phase reads the output, not the input.

```
Phase                   Input (broad)                    Output (narrow)
─────                   ─────────────                    ───────────────
Understanding           Raw YAML spec                    Parsed spec + resolved Q&A
Research                Parsed spec + Q&A                Feasibility verdict + tool evaluations
Design                  All of above                     Approved architecture proposal
Documentation           Approved architecture            Approved implementation documents
Construction            Approved impl docs               Generated agent codebase
Validation              Impl docs + generated code       Pass/fail verdict
```

Notice how each phase's output is smaller and more precise than its input. The raw spec might be hundreds of lines. The parsed spec is a structured object. The architecture proposal distills everything into a design. The impl docs translate the design into buildable specifications. By the time the Builder runs, it doesn't need to know what clarification questions were asked in round 2 — all of those decisions have been baked into the impl docs.

### 5.2 State Projection

The blackboard holds everything — the full history of every phase. But each specialist sees only a **projection** of the blackboard: the subset of fields relevant to its job.

The projection is not a filter on the blackboard. It is a deliberate design decision for each specialist: what does this specialist need to see, and what would be noise?

**Spec Analyst** sees: the raw spec, the template schema, and previous Q&A rounds (to avoid re-asking). It does NOT see: research findings, architecture proposals, impl docs, generated code.

**Researcher** sees: the parsed spec and the clarified requirements (answers that changed the design). It does NOT see: the raw Q&A transcripts, architecture proposals, impl docs.

**Architect** sees: a summary of the parsed spec, the enriched requirements from clarification, the research findings, and the available shared components from the metadata framework (Tier 2 catalog). It does NOT see: the raw spec YAML, individual Q&A exchanges, build artifacts.

**Documentarian** sees: the approved architecture proposal and shared component interface specs. It does NOT see: the raw spec, Q&A history, research details, rejected architecture revisions.

**Builder** sees: the approved implementation documents and shared component source code. It does NOT see: anything from Part A. The impl docs ARE the Builder's entire world.

**Validator** sees: the implementation documents (as ground truth) and the generated code (as the thing being checked). It does NOT see: anything from Part A or the build process.

This is how context stays manageable. The Builder's prompt contains the impl docs and nothing else. If the impl docs are 3,000 tokens and the code generation prompt is 500 tokens, the Builder operates in a 3,500-token context — well within any model's capability. It doesn't matter that the blackboard holds 50,000 tokens of accumulated history. The Builder never sees it.

### 5.3 Summarization at Phase Boundaries

Some phase transitions explicitly summarize to compress further. When the Architect finishes and the architecture is approved:

- The full architecture proposal, all revision history, and all feedback are stored in the blackboard (for audit and traceability)
- But only the **approved version** is projected forward to the Documentarian
- The three rejected revisions and their feedback are not projected — they served their purpose and are no longer relevant

Similarly, when clarification resolves:

- All Q&A rounds are stored in the blackboard
- But only the **design-impacting answers** are projected forward to the Researcher and Architect — "the user confirmed they want daily batch, not streaming" matters, "the user said the field name is `customer_id` not `cust_id`" was already folded into the parsed spec

### 5.4 State Accumulation — Append-Only

The blackboard is append-only where possible. Lists accumulate; nothing is silently overwritten.

- Clarification rounds are appended. Round 3 doesn't replace round 2.
- Architecture revisions are appended. The current proposal is always the latest, but the history is preserved.
- Build logs are appended. Every file generated, every validation check run, every fix attempted.

This serves two purposes:

1. **Resumability** — if the process crashes and restarts, it picks up where it left off. Nothing was lost because nothing was overwritten.
2. **Auditability** — after the agent is built, you can trace every decision. Why did the architecture change? Because the user requested revisions in round 2, specifically asking for X. The full history is there.

### 5.5 What the Blackboard Does NOT Hold

The blackboard holds **workflow-driving state** — the data the coordinator and specialists need to route, project, and execute. It does **not** hold persistent operational metadata such as:

- **Audit trails** — who approved what, when, with what rationale
- **Artifact registries** — catalog of every artifact produced across all runs
- **Execution logs** — detailed per-node timing, token counts, model usage
- **Event streams** — fine-grained observability data for dashboards and alerts
- **Governance evaluations** — records of every compliance rule check

These concerns are served by the **Metadata Framework** — a separate set of PostgreSQL tables organized into three tiers (generic agent metadata, DDLC shared metadata, and agent-specific metadata). The metadata framework is defined in [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md).

Nodes write to the metadata framework as a **side effect** of execution — either via direct calls or (preferably) via LangGraph callback hooks. The blackboard remains focused on what specialists need to do their job, not on what operators need to see after the fact.

---

## 6. Knowledge Architecture — RAG and the Metadata Framework

The meta-agent needs external knowledge and persistent operational data at several points. This comes from two sources: the **RAG knowledge base** (unstructured documents and patterns) and the **Metadata Framework** (structured relational data organized by concern: catalog, lineage, operational, and audit; see [`01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md)).

### 6.1 RAG — Shared Infrastructure, Domain-Tagged Knowledge

There is one RAG engine — one embedding pipeline, one vector store (pgvector), one retrieval mechanism. But the documents stored in it are organized into knowledge domains, and each specialist queries only its relevant domain.

**Knowledge domains:**

| Domain | Contents | Queried By |
|---|---|---|
| **Library & Tool Documentation** | API references, usage guides, and limitation docs for tools like Snowflake, PySpark, Kafka, Great Expectations, dbt, etc. | Researcher |
| **Organizational Standards** | Naming conventions, data governance policies, project structure rules, approved tool lists, security requirements | Researcher, Architect |
| **Architecture Pattern Catalog** | Documented patterns for DDLC agents (see Section 7). Each pattern describes when to use it, what it looks like, what runtime it implies, and what trade-offs it carries. | Architect |
| **Past Architecture Decisions** | Architecture proposals from previous builds, including what was chosen, what was rejected, and why. Grows with every build. | Architect |
| **Shared Component Documentation** | Interface specifications, usage examples, and behavioral descriptions for shared components in the catalog | Architect, Builder |
| **Code Conventions** | Project code style, test patterns, configuration structure, file naming rules | Builder |

When the Researcher needs to know whether the Snowflake Python connector supports schema evolution, it queries the "Library & Tool Documentation" domain. When the Architect needs to know what pattern was used the last time a streaming agent was built, it queries the "Past Architecture Decisions" domain. Same engine, different document sets.

### 6.2 Metadata Framework — Structured Relational Data

The metadata framework is **not RAG**. It is structured relational data in Postgres organized across three tiers (generic agent metadata, DDLC shared metadata, and agent-specific metadata) and four concerns (catalog, lineage, operational, audit). It replaces the previous standalone "portfolio memory" concept by unifying all persistent metadata into a single framework. It answers questions like:

- What agents exist? What version are they? What status are they in? (*Catalog: `agent_registry`*)
- What shared components are available? Which agents use them? (*Catalog & Lineage: `component_registry`, `component_usage`*)
- What are the cross-agent dependencies? If I change Agent A's output contract, which agents break? (*Lineage: `agent_dependency`, `contract_lineage`*)
- How did previous build runs perform? What were the revision counts and gate decisions? (*Operational & Audit: `build_summary`, `agent_run_header`, `decision_log`*)

These are SQL queries, not semantic searches. The catalog is a relational registry, not a document collection.

**The one overlap**: shared component discovery. When the Architect wants to find "a component that does something like PII masking," it runs a semantic search over component embeddings stored in pgvector (`component_registry.embedding` in Tier 2). This is semantically a vector similarity search, but it lives directly inside the metadata framework catalog table rather than the RAG document store.

### 6.3 Knowledge Flow Through the Process

```
                    ┌─────────────────────────────┐
                    │     RAG Knowledge Base       │
                    │                              │
                    │  Library Docs                │
                    │  Org Standards               │
                    │  Pattern Catalog ────────────────────────────┐
                    │  Past Decisions              │              │
                    │  Component Docs              │              │
                    │  Code Conventions            │              │
                    └──────┬──────────────┬────────┘              │
                           │              │                       │
                     queries by      queries by              grows with
                     Researcher      Architect               every build
                           │              │                       │
                           ▼              ▼                       │
                    ┌─────────────────────────────┐              │
                    │        Blackboard           │              │
                    │     (LangGraph State)       │──────────────┘
                    └──────┬──────────────────────┘
                           │
                     read/write by
                     all specialists
                           │
                    ┌──────┴──────────────────────┐
                    │     Metadata Framework      │
                    │   (3-Tier Postgres Tables)  │
                    │                             │
                    │  Catalog (Agents & Comps) ─── semantic search (pgvector)
                    │  Lineage (Dependencies)    │
                    │  Operational (Runs & Logs)  │
                    │  Audit (Gates & Governance) │
                    └─────────────────────────────┘
```

---

## 7. Building Agents with Different Architectures

Every DDLC agent the meta-agent builds can have a completely different architecture. One might be a simple Python batch script. Another might be a full LangGraph state machine with governance gates. Another might be a streaming Kafka processor.

The meta-agent handles this through three mechanisms: the **pattern catalog**, **runtime-agnostic implementation documents**, and the **Builder's generation strategies**.

### 7.1 The Pattern Catalog

The Architect specialist does not invent architectures from scratch. It selects and adapts from a catalog of known architecture patterns stored in the RAG knowledge base.

The initial catalog includes patterns like:
| Pattern | Characteristics | Typical Runtime |
|---|---|---|
| **Simple Pipeline** | Linear stages, no human gates, no conditional routing, batch processing | Python + LiteLLM |
| **Stateful Pipeline with Gates** | Has governance gates, human checkpoints, error recovery branches, conditional routing | LangGraph |
| **Batch Processor** | Periodic runs, bulk data, schedule-driven, no real-time component | Python + scheduler |
| **Streaming Processor** | Real-time data, event-driven, message queues (Kafka), continuous processing | Python + Kafka client |
| **Multi-Source Aggregator** | Pulls from multiple sources, joins/reconciles, publishes unified output | Depends on complexity |
| **Governance-Heavy Pipeline** | PII handling, audit trails, approval gates at multiple stages, lineage tracking | LangGraph (for the gate enforcement) |

The Architect's process:

1. Read the spec requirements — stages, autonomy levels, governance rules, tool requirements
2. Query the pattern catalog: "Which patterns fit an agent with these characteristics?"
3. Query the metadata catalog: "Have I built something similar before? What pattern did I use? How did it work out?"
4. Select a base pattern
5. Adapt it — add stages, adjust routing, plug in specific tools
6. Present the adapted architecture to the human for approval

The catalog is not static. Every time the meta-agent builds an agent, the architecture decision and its outcome are stored as a "past architecture decision" in the RAG knowledge base. The catalog grows with experience. The tenth agent benefits from the patterns learned building the first nine.

### 7.2 Runtime-Agnostic Implementation Documents

The implementation documents are the **abstraction layer** between the Architect and the Builder.

The impl docs do NOT say "create a LangGraph node called `validate_data`." They say:

> Stage 3: Validate data quality.  
> Input contract: `customer_staging` (fields: id, name, email, created_at).  
> Quality rules: email must match regex, id must be unique, created_at must be within 24 hours.  
> Tool: Great Expectations.  
> Error strategy: quarantine failing rows, log summary, proceed with valid rows.  
> Governance: log all quarantined rows to audit trail.

This description is runtime-agnostic. The Builder can implement it as:

- A LangGraph node (if the agent uses LangGraph)
- A Python function in a pipeline script (if the agent is a simple pipeline)
- A processing handler in a Kafka consumer (if the agent is a streaming processor)

The impl docs specify **what** each stage does. The Builder decides **how** to express it in code for the chosen runtime.

### 7.3 Builder Generation Strategies

The Builder is not a single monolithic code generator. It has **generation strategies** — one per supported runtime. The Architect's runtime selection determines which strategy the Builder uses.

Each strategy knows:

- How to structure a project for that runtime (file layout, entry point, configuration shape)
- How to express a stage in that runtime (LangGraph node vs. Python function vs. Kafka handler)
- How to express data contracts in that runtime (Pydantic models, schema validators, etc.)
- How to express governance hooks in that runtime (state machine gates vs. decorator checks vs. middleware)
- How to wire stages together (graph edges vs. function calls vs. message routing)

The Builder reads the impl docs (which are the same regardless of runtime) and applies the selected strategy to produce the agent's codebase.

This means adding support for a new runtime is adding a new generation strategy, not changing the meta-agent's workflow or the impl doc format.

---

## 8. Process and Interaction Flow

This section illustrates how components collaborate across the agent development lifecycle, followed by detailed phase breakdowns for both new builds and change requests.

### 8.1 Interaction Flow Diagram

The following sequence diagram details the runtime interactions between the User, Chainlit UI, Coordinator, Specialists, Blackboard State, and Knowledge & Metadata Layer (RAG & Postgres).

```
User / UI               Coordinator             Specialists               Blackboard               Knowledge Layer
   │                         │                       │                         │                   (RAG & Postgres)
   │── Submit Spec ─────────>│                       │                         │                           │
   │                         │── Initialize State ────────────────────────────>│                           │
   │                         │── Query Metadata Catalog (Tier 2) ─────────────────────────────────────────>│
   │                         │<── Active Agents, Shared Components ────────────────────────────────────────│
   │                         │── Store Initial Context ───────────────────────>│                           │
   │                         │                       │                         │                           │
   │   [PHASE: UNDERSTANDING]│                       │                         │                           │
   │                         │── Invoke (Spec Slice) │                         │                           │
   │                         │──────────────────────>│ [Spec Analyst]          │                           │
   │                         │                       │── Read Projected Spec ─>│                           │
   │                         │                       │── Write Gaps & Qs ─────>│                           │
   │                         │<── Gaps Found ────────│                         │                           │
   │<── Gate 1: Clarify Qs ──│                       │                         │                           │
   │── Provide Answers ─────>│                       │                         │                           │
   │                         │── Record Answers ──────────────────────────────>│                           │
   │                         │── Re-invoke (Answers) │                         │                           │
   │                         │──────────────────────>│ [Spec Analyst]          │                           │
   │                         │                       │── Finalize Spec ───────>│                           │
   │                         │<── Spec Complete ─────│                         │                           │
   │                         │                       │                         │                           │
   │   [PHASE: RESEARCH]     │                       │                         │                           │
   │                         │── Invoke (Clean Spec) │                         │                           │
   │                         │──────────────────────>│ [Researcher]            │                           │
   │                         │                       │── Query Tool Docs ─────────────────────────────────>│
   │                         │                       │<── Capabilities, Limits ────────────────────────────│
   │                         │                       │── Search Components ───────────────────────────────>│
   │                         │                       │<── Matching Candidates ─────────────────────────────│
   │                         │                       │── Write Verdict & Findings ────────────────────────>│
   │                         │<── Research Complete ─│                         │                           │
   │                         │                       │                         │                           │
   │   [PHASE: DESIGN]       │                       │                         │                           │
   │                         │── Invoke (Findings)   │                         │                           │
   │                         │──────────────────────>│ [Architect]             │                           │
   │                         │                       │── Query Pattern Catalog ───────────────────────────>│
   │                         │                       │<── Patterns & Past Decisions ───────────────────────│
   │                         │                       │── Write Arch Proposal ─>│                           │
   │                         │<── Proposal Ready ────│                         │                           │
   │<── Gate 2: Review Arch ─│                       │                         │                           │
   │── Approve Architecture >│                       │                         │                           │
   │                         │── Set Approved Flag ───────────────────────────>│                           │
   │                         │                       │                         │                           │
   │   [PHASE: DOCUMENT]     │                       │                         │                           │
   │                         │── Invoke (Arch Slice) │                         │                           │
   │                         │──────────────────────>│ [Documentarian]         │                           │
   │                         │                       │── Write YAML Impl Docs >│                           │
   │                         │<── Impl Docs Ready ───│                         │                           │
   │<── Gate 3: Review Docs ─│                       │                         │                           │
   │── Approve Impl Docs ───>│                       │                         │                           │
   │                         │── Set Approved Flag ───────────────────────────>│                           │
   │                         │                       │                         │                           │
   │   [PHASE: CONSTRUCT]    │                       │                         │                           │
   │                         │── Invoke (Impl Docs)  │                         │                           │
   │                         │──────────────────────>│ [Builder]               │                           │
   │                         │                       │── Generate Code/Tests ─>│                           │
   │                         │<── Build Complete ────│                         │                           │
   │                         │                       │                         │                           │
   │   [PHASE: VALIDATE]     │                       │                         │                           │
   │                         │── Invoke (Code + Docs)│                         │                           │
   │                         │──────────────────────>│ [Validator]             │                           │
   │                         │                       │── Verify Contracts/Gov ─│                           │
   │                         │   ┌── (On Failure: Fix Loop up to N attempts) ──│                           │
   │                         │   │                   │                         │                           │
   │                         │   └─── Pass ─────────>│                         │                           │
   │                         │<── Validation Passed ─│                         │                           │
   │                         │                       │                         │                           │
   │   [PHASE: DELIVERY]     │                       │                         │                           │
   │                         │── Write Code to Workspace Disk                  │                           │
   │                         │── Register Agent & Components in Metadata ─────────────────────────────────>│
   │                         │── Store Arch Decision in RAG ──────────────────────────────────────────────>│
   │<── Delivery Summary ────│                       │                         │                           │
   ▼                         ▼                       ▼                         ▼                           ▼
```

### 8.2 New Build Flow

**Phase 1 — Setup**

The coordinator loads catalog context from the metadata framework (Tier 2). What agents already exist? What shared components are available? What cross-agent dependencies exist? This context is written to the blackboard so that downstream specialists have awareness of existing assets.

**Phase 2 — Understanding**

The Spec Analyst specialist takes over. It receives the raw YAML spec from the blackboard, validates it against the template schema, and extracts structured information — stages, contracts, governance rules, tools, constraints.

It then examines the structured spec for gaps. Are there ambiguities? Missing information? Contradictions? If so, it generates clarifying questions and the coordinator pauses execution at a human gate. The user answers. The Spec Analyst processes the answers, determines their impact on the design, and decides whether to loop back for another round of questions or advance.

The coordinator enforces a maximum number of clarification rounds. If the limit is reached without resolution, it escalates to the human rather than looping forever.

**Phase 3 — Research**

The Researcher specialist takes over. Its projected context is the parsed spec and the design-impacting answers from clarification — not the raw Q&A transcripts.

The Researcher queries the RAG knowledge base for library and tool documentation. Can the required tools do what the spec asks? Are there compatibility issues? Are there better alternatives? It also queries organizational standards to check for policy constraints.

It produces a feasibility verdict: feasible, feasible with caveats, or not feasible. If not feasible, the coordinator routes to a human gate to discuss alternatives.

Separately (but in the same phase), a semantic search runs against the shared component registry in the metadata framework (Tier 2). Are there existing components that can be reused for this agent? Results are written to the blackboard for the Architect.

**Phase 4 — Design**

The Architect specialist takes over. Its projected context includes a summary of the parsed spec, the enriched requirements from clarification, the research findings, and the available shared components.

The Architect queries the pattern catalog and past architecture decisions in the RAG knowledge base, plus existing components in the metadata framework. It selects a base pattern, adapts it, selects tools, identifies reusable components, documents risks and trade-offs, and produces an architecture proposal.

The coordinator pauses at a human gate. The user reviews the architecture. If approved, the process advances. If revisions are requested, the Architect incorporates feedback and re-presents. The coordinator tracks revision count and enforces a maximum.

**Phase 5 — Documentation**

The Documentarian specialist takes over. Its projected context is the approved architecture proposal (not the revision history, not the research details, not the Q&A transcripts).

The Documentarian produces YAML implementation documents — one per stage, one per contract, one for governance, one for configuration. Each document is detailed enough that the Builder can generate code without ambiguity.

The coordinator pauses at a human gate. The user reviews the impl docs. If revisions are requested, the Documentarian updates the docs, ensuring cross-document consistency (if a contract changes, all references to it update). Loop until approved or maximum revisions reached.

**Phase 6 — Construction**

The Builder specialist takes over. Its projected context is the approved implementation documents and the shared component source code. Nothing from Part A — the impl docs are the Builder's entire world.

The Builder selects the generation strategy based on the runtime specified in the architecture (which is recorded in the impl docs). It generates stage code, configuration files, governance hooks, and test suites. New shared components go to the shared folder. Existing shared components are imported, not duplicated.

**Phase 7 — Validation**

The Validator specialist takes over. Its projected context is the implementation documents (ground truth) and the generated code (the thing being checked).

The Validator runs a series of checks: contract compliance, governance compliance, no hardcoded values, shared component usage, configuration completeness, and generated tests. If all checks pass, the coordinator routes to delivery. If checks fail, the coordinator routes back to the Builder with fix instructions. The loop runs up to a maximum number of fix attempts, after which the coordinator escalates to the human.

**Phase 8 — Delivery**

The coordinator writes all generated files to the agent's project directory, registers the agent, new shared components, and cross-agent dependencies in the metadata framework (Tier 2 catalog and lineage), and presents a summary to the user.

The architecture decision and its outcome are stored as a "past architecture decision" in the RAG knowledge base, enriching the pattern catalog for future builds.

### 8.3 Change Flow

The change flow reuses the same specialists and infrastructure, but enters at a different point.

**Phase 1 — Setup**: Same as new build. Load catalog and lineage context from the metadata framework.

**Phase 2 — Change Analysis**: The Spec Analyst receives the change request (natural language or edited spec). It identifies the target agent, loads its current implementation documents from the metadata framework (Tier 1 artifact registry), and structures the change.

**Phase 3 — Impact Analysis**: The Architect evaluates the impact. Which stages are affected? Which contracts change? Which governance rules need updating? Are there downstream agents that consume the affected contracts? The metadata framework's dependency and contract lineage tracking (Tier 2 `agent_dependency` and `contract_lineage`, relational SQL) answers the cross-agent questions.

**Phase 4 — Documentation Update**: The Documentarian updates the affected implementation documents, keeping all contracts and cross-references in sync.

**Phases 5–8**: The flow rejoins the new build flow at the human review of impl docs. From there, it follows the same path: review → approve → build → validate → deliver.

### 8.4 Human Gates — Summary

| Gate | What the Human Reviews | What Happens on Rejection |
|---|---|---|
| **Clarification** | Questions the meta-agent wants to ask | User answers; loop back to gap identification if answers have design impact |
| **Architecture Review** | The proposed architecture for the DDLC agent | Architect incorporates feedback; re-presents |
| **Impl Doc Review** | The YAML implementation documents | Documentarian revises; re-presents |
| **Build Escalation** | Validation failures that the Builder could not fix after max attempts | Human intervenes manually |

Every gate has a maximum loop count enforced by the coordinator. No infinite loops. If a gate cannot be resolved within the limit, the coordinator escalates with a full diagnostic: what was tried, what failed, and why.

---

## 9. Evaluation Strategy

### 9.1 Evaluation Layers Already in the Process

The meta-agent's workflow has multiple evaluation layers built into it — they are not a separate "eval framework" but part of the process itself.

**Structural validation** (automated): The Validator specialist checks contract compliance, governance compliance, hardcoding, shared component usage, configuration completeness, and test passage. These are deterministic checks — they pass or fail.

**Human judgment** (the gates): The user reviews the architecture and implementation documents before any code is generated. These are the strongest quality signals. If the architecture is wrong, the user catches it before the Builder wastes effort.

**Consistency enforcement** (automated): The Documentarian ensures cross-document consistency — if a contract changes, all references update. The Builder ensures all configuration values referenced in code exist in the config template.

### 9.2 Operational Metrics — MVP

From day one, the meta-agent tracks operational metrics in the build history. These are not LLM-as-judge evaluations — they are counters.

- **Clarification rounds**: How many rounds of Q&A were needed? Trending down over time means the Spec Analyst is asking better questions or the spec template is improving.
- **Architecture revisions**: How many times was the architecture rejected before approval? Trending down means the Architect is learning from past decisions.
- **Impl doc revisions**: Same signal for the Documentarian.
- **Build fix attempts**: How many validation failures required re-generation? Trending down means the Builder's code quality is improving.
- **Total duration**: How long from spec submission to delivery? Identifies bottlenecks.

These metrics are stored in the metadata framework (`ddlc_shared_metadata.build_summary` and `agent_metadata.execution_log`) as part of operational history. No additional infrastructure needed.

### 9.3 Post-MVP Evaluation

Three evaluation capabilities to add after the core system is working:

**Spec-to-output traceability**: An LLM reads the original spec and the generated agent code and checks: "Does this code fulfill each requirement in the spec?" This is LLM-as-judge evaluation. It reduces the burden on human reviewers by surfacing potential gaps before the human gate.

**Reference spec test suite**: A set of 3–5 reference specs with known-good outputs. When the meta-agent's prompts, models, or logic change, run the reference specs and compare results to the baseline. This is regression testing for the meta-agent itself.

**Contract simulation**: Generate synthetic data matching the input contracts, run it through the generated agent, verify the output matches the output contracts. This tests the agent end-to-end without needing live infrastructure.

---

## 10. Folder Structure

The meta-agent codebase is organized cleanly around the architectural boundaries defined in this document: the deterministic coordinator, isolated domain specialists, the blackboard state model, the knowledge layer (RAG), the unified metadata framework, and the user interface.

```
meta-agent/
├── config/                      # Global configuration and model tier definitions
│   ├── settings.yaml            # Environment, database, and execution settings
│   ├── model_tiers.yaml         # LLM assignments per specialist (speed vs. reasoning)
│   └── safety_valves.yaml       # Max loop iteration limits and timeout thresholds
│
├── data/                        # Templates, schemas, and seed catalogs
│   ├── spec_templates/          # DDLC Agent specification template (YAML)
│   ├── impl_schemas/            # Implementation document schema definitions
│   └── patterns/                # Base architecture pattern catalog definitions
│
├── knowledge/                   # RAG knowledge base raw sources & corpus
│   ├── library_docs/            # Tool & library docs (Snowflake, PySpark, Kafka, etc.)
│   ├── org_standards/           # Corporate data policies, governance rules, security
│   ├── pattern_catalog/         # Documented agent architecture patterns & trade-offs
│   ├── past_decisions/          # Past build architecture proposals & outcomes
│   └── code_conventions/        # Style guides, test conventions, directory conventions
│
├── src/                         # Application source code
│   ├── coordinator/             # LangGraph state machine orchestration (deterministic)
│   │   ├── graph.py             # Top-level workflow graph definition
│   │   ├── edges.py             # Deterministic conditional routing & safety valves
│   │   └── subgraphs/           # Phase-specific LangGraph subgraphs
│   │       ├── understand.py    # Understanding phase subgraph
│   │       ├── research.py      # Research & feasibility subgraph
│   │       ├── design.py        # Architecture design subgraph
│   │       ├── document.py      # Implementation document authoring subgraph
│   │       └── construct.py     # Construction & validation fix-loop subgraph
│   │
│   ├── specialists/             # Domain intelligence nodes (LLM-powered)
│   │   ├── spec_analyst/        # Spec parsing, validation, and gap detection
│   │   ├── researcher/          # Feasibility analysis and tool compatibility
│   │   ├── architect/           # Pattern selection and agent architecture design
│   │   ├── documentarian/       # YAML implementation document generation
│   │   ├── builder/             # Code generation engine
│   │   │   └── strategies/      # Per-runtime generation strategies
│   │   │       ├── langgraph.py # Stateful LangGraph agent generation strategy
│   │   │       ├── pipeline.py  # Linear Python script pipeline generation strategy
│   │   │       └── streaming.py # Event-driven Kafka streaming generation strategy
│   │   └── validator/           # Build verification (contract, governance, test checks)
│   │
│   ├── state/                   # State definitions and blackboard projections
│   │   ├── blackboard.py        # Master blackboard state schema (accumulating, append-only)
│   │   ├── projections.py       # State projection mappers (focused slices per specialist)
│   │   └── checkpointer.py      # PostgreSQL state persistence and snapshot recovery
│   │
│   ├── metadata/                # Metadata framework client & callback hooks
│   │   ├── catalog.py           # Agent and component registry queries (Tier 2)
│   │   ├── lineage.py           # Cross-agent dependencies & contract lineage (Tier 2)
│   │   ├── callbacks.py         # LangGraph callback hooks for operational & audit logging (Tier 1)
│   │   └── dcm_client.py        # Client interface to call the DCM Agent for Tier 3 schema creation
│   │
│   ├── rag/                     # Shared RAG engine (pgvector vector store)
│   │   ├── embeddings.py        # Vector embedding generation (domain-tagged)
│   │   ├── vector_store.py      # pgvector storage, indexing, and similarity search
│   │   └── retriever.py         # Specialist retrieval interfaces filtered by domain
│   │
│   └── ui/                      # Chainlit presentation layer
│       ├── app.py               # Chainlit application entry point & session lifecycle
│       ├── handlers.py          # Streaming event handlers & specialist log streaming
│       └── gates.py             # Interactive human gate approval forms and prompts
│
├── tests/                       # Automated test suites
│   ├── unit/                    # Routing functions, schema validators, projection slices
│   ├── integration/             # Subgraph execution, database checkpoints, RAG queries
│   └── reference_specs/         # Benchmark reference specs for regression verification
│
├── .env.example                 # Template for environment credentials and API keys
├── pyproject.toml               # Python package configuration and dependencies
└── README.md                    # Project overview, setup guide, and architectural index
```

### Folder Responsibilities & Separation of Concerns

- **`config/`**: Houses declarative settings. Centralizes model tier assignments (allowing routing of fast models to Spec Analyst and heavy models to Architect) and safety valves (capping loops) without modifying graph logic.
- **`data/`**: Stores static templates and schemas that define the contracts between phases (spec template and implementation document schemas).
- **`knowledge/`**: The ground-truth document repository ingested into the RAG engine. Organized strictly by domain tag so specialists retrieve only their relevant corpus.
- **`src/coordinator/`**: Contains only deterministic graph logic. No LLM prompts or reasoning live here. It maps state transitions, coordinates subgraphs, and halts at human gates.
- **`src/specialists/`**: Contains LLM-powered domain workers. Each specialist directory is encapsulated with its prompts, Instructor structured output models, and domain tools. The Builder sub-package isolates runtime strategies, making adding a new agent runtime as simple as adding a new strategy file.
- **`src/state/`**: Defines the shared Blackboard schema and the projection functions that produce narrow slices for specialists. Manages Postgres checkpointer connection.
- **`src/metadata/`**: Implements the client interface for the unified 3-tier metadata framework (catalog, lineage, operational, audit). Manages LangGraph callback hooks for automatic logging and provides a client to call the DCM Agent for Tier 3 schema creation during builds.
- **`src/rag/`**: Implements the shared embedding and pgvector retrieval engine, providing filtered semantic queries per domain.
- **`src/ui/`**: Provides the Chainlit chat interface, streaming token output, and UI components for the four human gates.
- **`tests/`**: Contains unit tests for graph edges and projections, integration tests for subgraphs, and reference benchmark specs for end-to-end regression testing.

---

## 11. Decisions Log

All architecture-level decisions made in this document:

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Architecture pattern | Coordinator-Specialist | Separates workflow control (deterministic, no LLM) from domain intelligence (LLM-powered specialists). The approach doc's principle made structural. |
| 2 | Control flow pattern | Hierarchical State Machine | The workflow has nested loops and gates within sequential phases. A flat state machine cannot express this cleanly. Maps directly to LangGraph subgraphs. |
| 3 | Data model pattern | Blackboard | Shared, accumulating, append-only state. Specialists contribute incrementally. Full history preserved. Checkpointed for resumability. |
| 4 | Coordinator-Specialist over Hub-Spoke | Coordinator-Specialist | Hub-spoke implies independent spokes with on-demand dispatch. Our specialists have hard sequential dependencies. Coordinator-specialist captures this accurately. |
| 5 | Coordinator is not an LLM | Deterministic code (graph edges + routing functions) | The coordinator never needs to reason about content. Field lookups are sufficient. Eliminates context window risk in the orchestration layer. |
| 6 | Context management | State projection — each specialist gets a slice, not the whole blackboard | Keeps each specialist's context focused and within token limits. The funnel principle ensures each phase's output is smaller than its input. |
| 7 | Summarization at phase boundaries | Explicit compression when crossing from one phase to the next | Only the approved/resolved output of a phase is projected forward. Rejected revisions, raw Q&A transcripts, and intermediate research are not projected. |
| 8 | RAG architecture | Shared infrastructure, domain-tagged knowledge | One embedding pipeline, one vector store, one retrieval mechanism. Documents tagged by domain. Each specialist queries its relevant domain. |
| 9 | Unified Metadata Framework | 3-tier Postgres schemas (catalog, lineage, operational, audit) | Absorbed standalone 'portfolio memory' into Tier 2. Follows industry metadata platform patterns (DataHub, Unity Catalog). Reusable, domain-specific, and isolated. |
| 10 | Shared component discovery | Semantic search via pgvector on component embeddings | Supported via vector column on `component_registry` within Tier 2 metadata. "Find something like a PII masker" is a semantic query directly on catalog data. |
| 11 | Different agent architectures | Pattern catalog (in RAG) + runtime-agnostic impl docs + Builder generation strategies | The Architect selects from a pattern catalog. The impl docs describe what, not how. The Builder has per-runtime strategies. New runtimes = new strategies, no workflow changes. |
| 12 | Pattern catalog growth | Past architecture decisions stored in RAG after every build | The catalog grows with experience. The tenth build benefits from the patterns of the first nine. Portfolio-level learning. |
| 13 | Evaluation at MVP | Human gates + structural validation + operational metrics | No separate eval framework needed. Human gates are the strongest signal. Counters track improvement over time. |
| 14 | Evaluation post-MVP | Spec-to-output traceability, reference spec test suite, contract simulation | Added when optimizing, not when building. Reduces human review burden and enables regression testing. |
| 15 | Safety valves | Maximum iteration counts on all loops | Enforced by the coordinator. Clarification, architecture revision, impl doc revision, and build fix loops all have configurable maximums. Escalate to human on exhaustion. |
| 16 | Change flow reuse | Maintenance flow reuses Documentation, Construction, and Validation phases | Same specialists, same logic, different entry state. No code duplication between build and change flows. |
| 17 | Metadata write mechanism | LangGraph callback hooks | Keeps metadata writes out of specialist logic. Callback hooks intercept node lifecycle events (start, complete, error) and write metadata as a side effect. |
| 18 | Schema provisioning | DCM Agent (MVP — built first) | Dedicated lightweight CLI agent manages all schema lifecycle. Creates Tiers 1-2, generates Tier 3 per agent. Proves the core stack before the meta-agent is built. See [`metadata-framework/02_dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/02_dcm_agent.md). |
| 19 | Metadata retention | Keep everything | Permanent retention across all tiers for MVP. Archival/aggregation policies deferred until volume demands it. |

---

## 12. What This Document Does NOT Cover

This document deliberately does not cover:

- **State schema specifics** — Field names, types, Pydantic models → a specification document
- **Node implementation details** — How each specialist's nodes are coded → a specification document
- **Database DDL** — Table definitions, indexes, constraints → a specification document
- **Configuration format** — YAML structure, model tier definitions → a specification document
- **The spec template** — What fields the user fills in → `03_spec_template.md`
- **The implementation doc schema** — Full schema for impl docs → `04_impl_doc_schema.md`
- **Builder generation strategies** — How code is generated per runtime → `05_builder.md`
- **Governance enforcement** — How governance flows from spec to agent → `06_governance.md`
- **RAG pipeline** — Ingestion, chunking, retrieval tuning → `07_rag.md`
- **Metadata framework DDL & client** — Column-level DDL, callback implementation, Tier 3 generators → [`design-docs/metadata-framework/01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md)
- **DCM Agent** — Lightweight CLI agent for schema lifecycle management (MVP — built first) → [`design-docs/metadata-framework/02_dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/02_dcm_agent.md)
- **Prompt engineering** — The actual prompts used inside each specialist → implementation detail
- **Chainlit UI design** — Chat interface layout → a design specification
- **Containerization and deployment** — Post-MVP

---

## 13. Next Steps

1. **`03_spec_template.md`** — The YAML template users fill in to describe their DDLC agent
2. **`04_impl_doc_schema.md`** — Full schema for implementation documents
3. **`05_builder.md`** — How the builder reads impl docs and generates agent code per runtime
4. **`06_governance.md`** — Governance enforcement: spec → impl docs → generated agent
5. **[`metadata-framework/01_metadata_framework.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md)** — Unified three-tier metadata framework: catalog, lineage, operational, and audit schemas (iterative — evolves alongside development)
6. **[`metadata-framework/02_dcm_agent.md`](file:///S:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/02_dcm_agent.md)** — DCM Agent: lightweight CLI agent for schema lifecycle management (MVP — **build first**, before the meta-agent)
7. **`07_rag.md`** — RAG pipeline: document ingestion, domain tagging, embedding, retrieval per specialist
