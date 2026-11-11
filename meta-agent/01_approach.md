# DDLC Meta-Agent — Approach

## LangGraph State Machine with LiteLLM + Instructor + LangChain Utilities

> **Document Status**: Draft v0.4  
> **Last Updated**: 2026-10-31  
> **Audience**: Architecture & Development Team

---

## 1. What We Are Building

We are building a **meta-agent** — an AI system whose job is to build other AI agents.

You hand it a specification document describing a Data Development Lifecycle (DDLC) agent — its purpose, stages, inputs, outputs, and governance rules — and it produces a working, deployable agent. If you later want to change that agent, you describe the change (or edit the spec), and the meta-agent updates the agent accordingly — code, docs, contracts, all of it.

The meta-agent is not a chatbot. It is not a code generator. It is a **structured process** that happens to use LLMs for the intelligent work inside that process.

---

## 2. The Process

The meta-agent follows a two-part process with mandatory human checkpoints.

### Part A — Understanding and Design

1. **Read and understand** the specification document in detail
2. **Ask clarifying questions** — surface ambiguities, missing information, implicit assumptions
3. **Research feasibility** — determine if the requirements are achievable with available tools and patterns
4. **Present a high-level architecture** to the user for approval
5. **Iterate** — if the user requests revisions, incorporate feedback and re-present

Part A ends with an approved design. Nothing has been built yet.

### Part B — Documentation and Construction

6. **Create implementation docs** — detailed specification sheets that become the authoritative source of truth for the agent
7. **Build the agent** — a builder sub-component reads the approved implementation docs and generates the agent's code, configuration, governance hooks, and tests
8. **Validate** — verify the built agent matches the implementation docs
9. **Deliver** — present the working agent

### Maintenance Flow

When a user wants to change an existing agent:

10. **Receive change request** — user describes the change or edits the spec
11. **Analyze impact** — determine which stages, contracts, and downstream agents are affected
12. **Update implementation docs** — modify the source of truth, keeping all contracts in sync
13. **Rebuild affected components** — the builder regenerates only what changed
14. **Validate and deliver**

---

## 3. Why a State Machine

We have already built and discarded two earlier approaches:

- **Single agent + tools** (the LLM decides what to do next): Too unpredictable. It skips steps, forgets context, goes in circles. The workflow needs to be enforced, not suggested.
- **Hybrid phase runner** (coded phases, LLM free within each): Too rigid in the wrong places and too loose in the right ones. A flat list of phases cannot express the real flow — which has branches, loops, parallel tracks, and conditional human checkpoints.

The problem is that our workflow is not a straight line. It looks like this:

```
Understand the spec
    → Do I have questions? → Yes → Ask the user → Did the answers change my understanding? → Loop
    → No questions → Research feasibility
        → Is it feasible? → Yes → Present architecture to user
                                    → Approved? → Create implementation docs → Build
                                    → Revisions requested? → Incorporate feedback → Re-present
                          → No → Explain constraints → Suggest alternatives → Loop
```

This is a **graph**, not a list. It has:

- **Conditional branches**: "Do I have questions?" determines the next step
- **Loops**: Clarification and revision can cycle multiple times
- **Human decision points**: Approval gates where execution pauses and waits for you
- **Parallel possibilities**: Some stages could run concurrently

A state machine is the natural representation of this. And LangGraph is a framework specifically designed to run LLM-powered state machines.

---

## 4. How the State Machine Works

A LangGraph state machine has three concepts:

### State

A single object that carries everything the meta-agent knows at any point in the process. Think of it as the meta-agent's working memory. It accumulates as the process progresses.

When the process starts, the state contains just your spec. By the time the agent is built, the state contains the parsed spec, your answers to clarifying questions, the approved architecture, the implementation docs, and the generated code — the full history of decisions.

The state is **persistent**. If the process crashes, gets interrupted, or you walk away for a week, it picks up exactly where it left off. The state is saved after every step.

### Nodes

Each node is a discrete unit of work. A node receives the current state, does something (usually involving an LLM), and returns an updated state.

Nodes are where the intelligence lives. A node might:

- Read the spec and extract structured information
- Generate a list of clarifying questions
- Research whether a particular tool or library can do what the spec requires
- Draft an architecture document
- Write an implementation document
- Generate agent code from an implementation doc

Each node has a **focused job**. It does not need to know the overall workflow. It just takes state in, does its thing, and puts state out. This is what makes each node independently testable and debuggable.

### Edges

Edges define what happens after a node finishes. They can be:

- **Fixed**: "After node A, always go to node B"
- **Conditional**: "After node A, check the state — if X, go to B; if Y, go to C"
- **Human-gated**: "After node A, pause execution and wait for the user to decide"

Edges are where the workflow logic lives. The LLM inside a node does not decide what happens next — the edges do. This is the key difference from the single-agent approach: **the LLM reasons, the graph orchestrates**.

---

## 5. The Full Process as a Graph

### Part A — Understanding and Design

```
[READ SPEC]
    The meta-agent reads the specification template.
    It extracts: purpose, stages, inputs, outputs, rules, constraints.
    It produces a structured internal representation of the spec.
        │
        ▼
[IDENTIFY GAPS]
    The meta-agent examines the structured spec for:
    • Ambiguities ("what format is the output?")
    • Missing information ("no error handling strategy specified")
    • Contradictions ("stage 3 expects input that stage 2 doesn't produce")
    • Assumptions it is making that it should confirm
        │
        ├── Has questions ──▶ [ASK USER]
        │                        The meta-agent presents its questions.
        │                        Execution PAUSES. You answer.
        │                        Your answers are added to the state.
        │                        ──▶ loops back to [IDENTIFY GAPS]
        │
        └── No questions ──▶ [RESEARCH FEASIBILITY]
                                The meta-agent examines the requirements
                                against what is actually possible.
                                Uses RAG to search library docs, past specs,
                                and existing shared components.
                                    │
                                    ▼
                              [PRESENT ARCHITECTURE]
                                The meta-agent presents:
                                • High-level architecture for the DDLC agent
                                • How it interprets each stage
                                • What tools/libraries it plans to use
                                • Reusable shared components it identified
                                • Trade-offs, risks, and limitations

                                Execution PAUSES. You review.
                                    │
                                    ├── Approved ──▶ Part B
                                    │
                                    └── Revisions ──▶ [INCORPORATE FEEDBACK]
                                            ──▶ loops back to [PRESENT ARCHITECTURE]
```

### Part B — Documentation and Construction

```
[CREATE IMPLEMENTATION DOCS]
    The meta-agent produces YAML implementation docs —
    the authoritative source of truth for the agent.
    Detailed enough that the builder can generate code
    without ambiguity.
        │
        ▼
[REVIEW IMPL DOCS]
    Execution PAUSES. You review.
        │
        ├── Approved ──▶ [BUILD AGENT]
        │
        └── Revisions ──▶ [UPDATE IMPL DOCS]
                              ──▶ loops back to [REVIEW IMPL DOCS]

[BUILD AGENT]
    The builder reads the approved implementation docs
    and generates:
    • Agent code (per project conventions)
    • Configuration files
    • Governance enforcement hooks
    • Test suites derived from data contracts
    • Shared components go to the shared folder
        │
        ▼
[VALIDATE BUILD]
    Checks the generated agent against the implementation docs.
        │
        ├── Valid ──▶ [DELIVER]
        │
        └── Issues found ──▶ [FIX ISSUES]
                                ──▶ loops back to [VALIDATE BUILD]
```

### Maintenance Flow

```
[RECEIVE CHANGE REQUEST]
    User describes the change or edits the spec/impl docs.
        │
        ▼
[ANALYZE IMPACT]
    Which stages, contracts, governance rules, and
    downstream agents are affected?
        │
        ▼
[UPDATE IMPL DOCS]
    Modify the source of truth. Keep all contracts in sync.
        │
        ▼
[REVIEW] → [REBUILD AFFECTED] → [VALIDATE] → [DELIVER]
```

---

## 6. What the LLM Does (and Does Not Do)

### The LLM does:

- **Read and interpret** the natural language parts of your spec
- **Reason** about gaps, ambiguities, and feasibility
- **Ask** intelligent clarifying questions
- **Design** the agent's architecture based on the requirements
- **Write** implementation documents
- **Generate** code from implementation documents
- **Search** for relevant knowledge via RAG (library docs, past specs, shared components)
- **Analyze** change impact across components

### The LLM does NOT:

- **Decide what step comes next** — the graph edges do that
- **Skip steps** — the graph enforces the sequence
- **Proceed without approval** — human-gated edges pause execution
- **Hold the state in its context window** — the state object does that
- **Remember across sessions** — the persisted state and portfolio memory do that

This separation is the entire point. The LLM is a powerful but unreliable reasoner. The state machine is a reliable but unintelligent orchestrator. Together, you get a system that is both intelligent and predictable.

---

## 7. Why This Approach Fits This Problem

### 7.1 The workflow has mandatory human checkpoints

You explicitly require that the meta-agent presents work for your approval before proceeding. These are not optional politeness checks. They are hard gates.

A state machine makes these **structural**. The graph literally cannot advance past an approval node without your input. There is no prompt engineering trick that can bypass it. The edge says "wait for human" and execution stops.

### 7.2 Iteration is a first-class concept

Your process is inherently iterative. You will ask for revisions to the architecture (possibly multiple times), request changes to implementation docs, and come back weeks later to modify an existing agent.

A state machine handles loops naturally. The "revision requested → incorporate feedback → re-present" cycle is just an edge that points backward. The state accumulates each iteration, so the meta-agent has the full history of what was tried and what was rejected.

### 7.3 Each step needs different context

The "understand the spec" step needs your raw spec and domain knowledge. The "build the agent" step needs the approved implementation docs and the project's code conventions. These are very different contexts.

In a state machine, each node gets exactly the context it needs — pulled from the state — and nothing more. This means better LLM performance (focused prompts), lower token costs (smaller context per call), and easier debugging (you can see exactly what each node received and produced).

### 7.4 Persistence and resumability matter

Building a DDLC agent is not a five-minute task. You might start the spec review on Monday, answer clarification questions on Tuesday, approve the architecture on Wednesday. The state machine's state is checkpointed after every node. You can close your laptop, come back whenever, and the process resumes exactly where it paused.

### 7.5 Portfolio management requires persistent memory

The meta-agent manages a portfolio of agents. It needs to know what agents exist, what shared components are available, and what cross-agent dependencies exist. This knowledge persists across builds and informs every new build. The state machine's persistent state layer, combined with the portfolio memory, makes this natural.

### 7.6 Multiple agents with different architectures

Each DDLC agent you build might have a different architecture. The meta-agent's graph does not change — the decisions inside the nodes change. The graph structure (understand → clarify → research → present → build) is the same. The intelligence within each node adapts to the specific spec.

---

## 8. Implementation Docs — The Source of Truth

The implementation docs are the most important artifact the meta-agent produces. They serve two audiences simultaneously:

### For Humans

The docs are readable. Every element has a `description` field written in plain English that explains intent, context, and edge cases. A data engineer can read the doc and understand what the agent does without looking at any code.

### For the Builder

The docs are structured YAML. Every element has typed fields that the builder reads directly. The builder does not "interpret" the docs — it compiles them. Given the same doc, the builder always produces the same output.

### The Format

Pure YAML with rich description fields — the same pattern used by OpenAPI, dbt `schema.yml`, Kubernetes manifests, and Terraform configs. The structure is machine-readable. The descriptions are human-readable. No mixed formats, no parsing ambiguity.

```yaml
agent:
  name: "customer_data_pipeline"
  description: >
    Ingests raw customer CSVs from S3, validates quality,
    masks PII, enriches with derived fields, and publishes
    to the analytics warehouse.
  version: "1.0.0"
  autonomy_level: semi-autonomous

stages:
  - id: ingest
    type: ingestion
    description: >
      Pulls daily CSV drops from the S3 raw bucket.
      Handles schema evolution by logging new columns as warnings.
    autonomy: autonomous
    input:
      source: "s3://data-lake/raw/customers/"
      contract: customer_raw
    output:
      destination: "staging.customer_raw"
      contract: customer_raw
    tools: [s3_reader, database_writer]
```

### The Change Flow

When you want to modify an agent:

1. You describe the change → the meta-agent updates the YAML implementation doc
2. You review the updated doc (human checkpoint)
3. The builder reads the updated doc and regenerates affected components
4. All contracts stay in sync because they are defined in the doc

This is the same pattern as Terraform (`edit .tf → plan → apply`) and dbt (`edit model → run → test`). The doc is the single source of truth. Code is derived from it.

---

## 9. Technology Decisions

### 9.1 Core Stack

| Layer | Technology | Why |
|---|---|---|
| **Orchestration** | LangGraph | State machine with persistence, conditional edges, human-in-the-loop, subgraphs. Built for exactly this pattern. |
| **Model Access** | LiteLLM | Unified API for 100+ LLM providers. Switch models by changing a string. Also handles embedding models for RAG. |
| **Structured Output** | Instructor + Pydantic v2 | Reliable Pydantic objects from LLM responses. Handles retries, validation, partial responses. Central to spec parsing, impl doc generation, and validation. |
| **Tool Definitions** | `langchain-core` `@tool` decorator | Already installed as a LangGraph dependency — zero additional cost. Native integration with LangGraph's `ToolNode`. Each node gets a focused set of tools. |
| **Document Loading** | `langchain-community` document loaders | Pre-built loaders for PDF, web pages, YAML, code, markdown. Used by the RAG ingestion pipeline. |
| **Text Splitting** | `langchain-text-splitters` | Code-aware, markdown-aware, token-aware chunking for RAG. Genuinely hard to replicate well from scratch. |
| **Chat UI** | Chainlit | Open-source, built for conversational AI agents. Native LangGraph integration. Human-in-the-loop support (approval buttons, form inputs). Streaming. Session persistence. |

### 9.1.1 LangChain — What We Use vs. What We Don't

LangGraph depends on `langchain-core`. It is already installed. Rather than fighting this dependency or duplicating what it provides, we use LangChain selectively — as a **utility library**, not as a framework.

**What we use from LangChain:**

| Package | What We Use | Why |
|---|---|---|
| `langchain-core` | `@tool` decorator, `ToolNode`, message types | Already installed via LangGraph. Native tool execution integration. Zero additional dependency. |
| `langchain-text-splitters` | Recursive, token-aware, code-aware, markdown-aware chunking | Small focused package. Good chunking is genuinely complex — respecting document structure, code boundaries, section headers. Rewriting from scratch is weeks of work for no benefit. |
| `langchain-community` | Document loaders (PDF, web, YAML, code) | Pre-built loaders for 100+ document formats. Used in the RAG ingestion pipeline. |

**What we do NOT use from LangChain:**

| LangChain Feature | What We Use Instead | Why |
|---|---|---|
| LLM wrappers (`ChatOpenAI`, `ChatAnthropic`, etc.) | LiteLLM | Provider-agnostic. Switch models by string. One interface for all providers including embeddings. |
| Structured output (`.with_structured_output()`) | Instructor + Pydantic | Better retry logic, validation, and partial response handling for complex Pydantic models. |
| Chains / LCEL | LangGraph nodes | Our nodes are the composition layer. No need for chain abstractions. |
| Agents (`create_react_agent`, etc.) | LangGraph graph | Our state machine IS the agent. LangChain's agent abstractions add nothing. |
| Memory classes | LangGraph state + Postgres | LangChain's memory is deprecated in favor of LangGraph's state. Portfolio memory is relational. |
| Full RAG chains | Custom retrieval on pgvector | We use LangChain for ingestion utilities (loading, splitting) but write retrieval ourselves for full control over what gets injected into prompts. |

### 9.2 Model Strategy

Tiered — different models for different nodes based on the cognitive load:

| Node Type | Model Tier | Examples |
|---|---|---|
| Heavy reasoning (spec analysis, architecture design, gap identification) | Tier 1 — Gemini Pro/Argon | Understanding ambiguous specs, designing architectures, analyzing change impact |
| Standard work (doc generation, code generation, research) | Tier 2 — Open-source large | Writing implementation docs, generating agent code, feasibility research |
| Mechanical tasks (formatting, file writing, validation checks) | Tier 3 — Open-source small/fast | Reformatting outputs, running validation checks, updating doc sections |

LiteLLM makes this a configuration choice per node. The model name is a string in the node's config — no code changes to switch.

### 9.3 Generated Agent Runtime

The agents the meta-agent builds are **not** locked into one framework. The runtime depends on the agent's complexity:

| Agent Complexity | Runtime | When |
|---|---|---|
| Multi-stage, stateful, has governance gates and human checkpoints | LangGraph | Complex DDLC pipelines with conditional routing, approval gates, error recovery branches |
| Linear, straightforward, no human-in-the-loop | Python + LiteLLM | Simple extract-transform-load agents, single-stage processors, monitoring agents |

The implementation doc specifies the agent's stages, autonomy levels, and governance requirements. The builder uses this to decide the appropriate runtime.

### 9.4 Spec Input Format

Predefined YAML template. The user fills in the template with their requirements. The Spec Parser node validates the filled template against a Pydantic schema.

The template provides structure (required fields, valid options, examples) while allowing natural language in description fields. This minimizes the LLM's interpretation burden — it validates and enriches rather than deciphering free-form text.

---

## 10. Persistence and Memory

### 10.1 One Database: Postgres + pgvector

A single Postgres instance handles all persistence needs:

| Need | How Postgres Handles It |
|---|---|
| **LangGraph state checkpoints** | `langgraph-checkpoint-postgres` — first-party LangGraph support. Automatic checkpointing after every node. Resume from any point. |
| **Agent portfolio registry** | Relational tables — agent metadata, versions, ownership, status. Standard SQL queries. |
| **Component inventory** | Relational tables — shared components, which agents use them, version history. |
| **Dependency tracking** | Junction tables and foreign keys — Agent A produces → Agent B consumes. Change impact queries via SQL joins. |
| **Semantic search** | pgvector extension — vector columns with similarity search. "Find shared components similar to X." |
| **Build history** | Relational tables — what was built, when, from which spec version, by which model. |

No separate vector database. No graph database. One database, fully open-source, handles relational data, state persistence, and vector search.

### 10.2 Portfolio Memory

The meta-agent maintains awareness of all agents it has built:

- **Agent registry**: Every agent's metadata, current version, implementation docs, and status
- **Shared component index**: What reusable components exist, what they do, which agents use them
- **Cross-agent dependencies**: Which agents produce data that other agents consume
- **Decision history**: Why certain architectural choices were made (stored as part of state checkpoints)

When building a new agent, the meta-agent queries portfolio memory to:

- **Find reusable components** — semantic search via pgvector ("do I already have a PII masker?")
- **Avoid duplication** — check the shared component index before generating new code
- **Understand dependencies** — know if the new agent's outputs will feed existing agents

### 10.3 RAG — External Knowledge Retrieval

The meta-agent needs external knowledge during research and building:

- Library and tool documentation (how does `great_expectations` work?)
- Organizational standards and conventions
- Past specs and implementation patterns
- API references for tools being wired into agents

**RAG is built on the existing stack — no additional framework needed:**

1. **Load**: `langchain-community` document loaders handle diverse formats (PDF, web, YAML, code, markdown)
2. **Chunk**: `langchain-text-splitters` provide code-aware, markdown-aware, token-aware splitting
3. **Embed**: LiteLLM embedding models (same library used for LLM calls)
4. **Store**: pgvector in Postgres (same database used for state and portfolio memory)
5. **Retrieve**: Custom retrieval function — embed the query → pgvector similarity search → top-k relevant chunks → inject into the LLM's prompt

We use LangChain for the ingestion utilities (steps 1-2) because document loading and intelligent chunking are genuinely complex. We write retrieval ourselves (step 5) for full control over what gets injected into each node's prompt.

This can be extended later with hybrid search (vector + keyword), reranking, or multi-index retrieval if the knowledge base grows large enough to need it.

---

## 11. General Rules (Enforced Across All Builds)

These rules apply to every agent the meta-agent builds:

| Rule | How It Is Enforced |
|---|---|
| **No duplication** | Before generating any component, the builder searches the shared component index (semantic search via pgvector). If a match exists, it reuses. If similar, it generalizes the existing component. |
| **No hardcoding** | All configuration values (paths, connection strings, thresholds, model names) go into config files. The builder generates code that reads from config, never embeds values. Validated during the build validation step. |
| **Shared folder for shared features** | Components used by more than one agent live in a shared folder. The builder places them there and updates the component index. Per-agent code references shared components, never copies them. |
| **Leverage existing libraries** | The builder uses established open-source libraries for standard tasks (logging, validation, profiling, file I/O) rather than writing custom implementations. The research node identifies the right libraries during feasibility analysis. |
| **Per-agent architecture can vary** | The implementation doc determines each agent's architecture. The builder selects the runtime (LangGraph or Python + LiteLLM) based on the agent's requirements. No forced uniformity. |

---

## 12. What "Building the Meta-Agent" Means

In concrete terms, building this meta-agent means:

1. **Define the state schema** — What data accumulates as the process runs? (The spec, questions, answers, architecture, approvals, impl docs, generated code, validation results, portfolio references.)

2. **Define the nodes** — What are the discrete units of work? Each node is a Python function that takes state in and returns updated state. Inside each function, we call LiteLLM/Instructor with a focused prompt and LangChain-defined tools.

3. **Define the edges** — What connects the nodes? Which edges are conditional? Which edges are human-gated?

4. **Define the tools** — What can the LLM use within each node? (Read files, write files, search for libraries, run code, query portfolio memory, search shared components.) Tools are defined using `langchain-core`'s `@tool` decorator for native LangGraph integration. Different nodes may have different tool sets.

5. **Build the portfolio memory layer** — Postgres schema for agent registry, component index, dependency tracking. Pgvector for semantic search.

6. **Build the RAG layer** — Document ingestion pipeline (LangChain loaders + splitters), embedding storage (LiteLLM + pgvector), retrieval functions.

7. **Build the Chainlit interface** — Chat UI that connects to LangGraph, renders streaming responses, presents approval gates as interactive elements.

8. **Build the spec template** — The YAML template users fill in to describe their agent.

9. **Build the builder** — The sub-component that reads implementation docs and generates agent code.

10. **Test** — Each node can be tested independently by constructing a state, running the node, and checking the output state.

---

## 13. What This Document Does NOT Cover

This document deliberately does not cover:

- **What the nodes are specifically** — That is architecture
- **What the state schema looks like** — That is architecture
- **The implementation doc YAML schema** — That is a design specification
- **How the builder generates code** — That is a compiler/generator design question
- **The spec template fields** — That is a design specification
- **Postgres schema design** — That is architecture
- **Chainlit UI layout** — That is a design specification
- **Specific tool implementations** — That is architecture
- **Containerization and deployment** — That is post-MVP

These are all addressed in subsequent documents.

---

## 14. Decisions Log

All approach-level decisions made during this phase:

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Orchestration framework | LangGraph | State machine with persistence, conditional edges, human-in-the-loop. Designed for this pattern. |
| 2 | Model access | LiteLLM | Provider-agnostic, switch models by string, also handles embeddings. |
| 3 | Structured output | Instructor + Pydantic v2 | Reliable typed objects from LLMs. Retries and validation built in. |
| 4 | Tool definitions | `langchain-core` `@tool` + `ToolNode` | Already installed via LangGraph. Native integration. Zero additional dependency cost. |
| 5 | Document loading (RAG) | `langchain-community` loaders | Pre-built for 100+ formats. Used in RAG ingestion pipeline. |
| 6 | Text splitting (RAG) | `langchain-text-splitters` | Code-aware, markdown-aware, token-aware chunking. Small focused package. Hard to replicate well. |
| 7 | LangChain scope | Utility library only | Use `langchain-core` (tools), `langchain-text-splitters` (chunking), `langchain-community` (loaders). Do NOT use chains, agents, LCEL, memory, LLM wrappers, or full RAG chains. |
| 8 | Chat UI | Chainlit | Open-source, agent-native, LangGraph integration, human-in-the-loop support. |
| 9 | Model strategy | Tiered | Open-source for bulk work, Gemini for heavy reasoning. Per-node config via LiteLLM. |
| 10 | Generated agent runtime | Spec-dependent | LangGraph for complex agents, Python + LiteLLM for simple ones. Builder decides based on impl doc. |
| 11 | Spec input format | Predefined YAML template | User fills template. Minimizes LLM interpretation. Pydantic-validated. |
| 12 | Implementation doc format | Pure YAML with rich descriptions | Human-readable (description fields) + machine-parseable (typed fields). OpenAPI/dbt pattern. Single source of truth. |
| 13 | State persistence | Postgres | LangGraph checkpoint support. Relational queries. Concurrent access. |
| 14 | Semantic search / vectors | pgvector (Postgres extension) | No separate vector DB. Enables shared component search and RAG retrieval. |
| 15 | Portfolio memory | Postgres relational + pgvector semantic | Agent registry, component index, dependency tracking, semantic search. One database. |
| 16 | RAG ingestion | LangChain loaders + splitters → LiteLLM embeddings → pgvector | LangChain for loading/chunking, LiteLLM for embedding, pgvector for storage. Custom retrieval for full prompt control. |
| 17 | RAG retrieval | Custom Python on pgvector | Full control over what gets injected into each node's prompt. No framework abstraction over retrieval. |
| 18 | Graph DB | Not for MVP | Postgres handles dependency tracking via junction tables. Reconsider at scale. |
| 19 | Containerization | Post-MVP | Design for containerizability (config-driven, no hardcoded paths). Dockerize later. |
| 20 | Approaches tried and rejected | Single agent + tools; Hybrid phase runner | Single agent: too unpredictable. Hybrid: too rigid in wrong places, too loose in right ones. |

---

## 15. Next Steps

1. **`meta-agent-architecture.md`** — The meta-agent's architecture: state schema, node inventory, edge map, tool sets per node
2. **`meta-agent-spec_template.md`** — The YAML template users fill in to describe their DDLC agent
3. **`metaagent-impl-doc-schema.md`** — Full schema for implementation documents: all supported constructs and fields
4. **`builder.md`** — How the builder reads impl docs and generates agent code
5. **`governance.md`** — How governance rules flow from spec → impl docs → generated agent enforcement
6. **`portfolio_memory.md`** — Portfolio memory design: agent registry, component index, dependency tracking, semantic search
7. **`rag.md`** — RAG pipeline: document ingestion, embedding, retrieval
8. **`implementation-docs/`** — directory of all implementation documents
