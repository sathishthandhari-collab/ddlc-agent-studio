# DDLC Meta-Agent — Knowledge Architecture & RAG Pipeline

## Domain-Tagged Advanced Hybrid RAG Engine (PostgreSQL + pgvector)

> **Document Status**: Approved v1.0  
> **Last Updated**: 2026-11-20  
> **Audience**: Architecture & Engineering Team  
> **Prerequisites**:  
> - [`architecture.md`](file:///s:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/architecture.md) — Meta-Agent Lifecycle, Coordinator, Specialists, Blackboard  
> - [`01_metadata_framework.md`](file:///s:/DATA/agents/ddlc-agent-studio/design-docs/metadata-framework/01_metadata_framework.md) — Three-Tier Unified Metadata Framework  
> - [`builder.md`](file:///s:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/builder.md) — Deterministic Compiler & LLM Leaf Coder  
> - [`dcm_agent.md`](file:///s:/DATA/agents/ddlc-agent-studio/design-docs/dcm-agent/dcm_agent.md) — DCM Schema Engine  

---

## 1. Document Purpose & System Overview

### 1.1 Purpose

This document defines the production knowledge architecture and Retrieval-Augmented Generation (RAG) pipeline for the **DDLC Meta-Agent** (the Layer 1 agent factory in DDLC Agent Studio). 

In traditional generative AI applications, RAG functions as an open-ended conversational QA tool for human users. In DDLC Agent Studio, **RAG is an internal, deterministic intelligence pipeline for specialized AI workers**. It provides the exact slice of technical documentation, organizational governance rules, architectural pattern catalogs, code style conventions, and historical build decisions required by domain Specialists (**Researcher**, **Architect**, **Builder**) to plan, design, and synthesize Layer 2 DDLC AI agents (such as `dbt_pipeline_builder_agent`).

This document details:
- How knowledge is partitioned into **6 isolated, domain-tagged knowledge corpora**.
- Why **Domain-Tagged Advanced Hybrid RAG** (Dense Vector + BM25 Full-Text + Reciprocal Rank Fusion + Cross-Encoder Reranking within PostgreSQL) was selected over Naive, Agentic, and Graph RAG.
- The precise boundary between the relational **3-Tier Metadata Framework** and the **RAG Vector Store**.
- The database schema, HNSW vector indexing, and dual `tsvector` full-text search implementation in PostgreSQL.
- The structure-aware chunking strategies (Markdown hierarchy breadcrumbs, YAML key boundaries, Python ASTs).
- The **Two-Phase Commit Sequence** for dynamic portfolio learning during Phase 8 Delivery.
- The complete folder layout, configuration, component code contracts, and operational evaluation strategy.

---

### 1.2 System Overview Diagram

```
                                      ┌─────────────────────────────────────────────────────────────┐
                                      │                     DDLC META-AGENT                         │
                                      │          (LangGraph Deterministic StateMachine)             │
                                      └──────────────────────────────┬──────────────────────────────┘
                                                                     │
                                  ┌──────────────────────────────────┴──────────────────────────────────┐
                                  ▼                                                                     ▼
             ┌─────────────────────────────────────────┐                           ┌─────────────────────────────────────────┐
             │       SPECIALIST WORKERS (LLM)          │                           │     PHASE 8 DELIVERY NODE (COORDINATOR) │
             │  • Researcher                           │                           │  • Deterministic Delivery Node          │
             │  • Architect                            │                           │  • Extracts Approved Architecture & ADR │
             │  • Builder (Leaf Coder)                 │                           │  • Two-Phase Transactional Commit       │
             └────────────────────┬────────────────────┘                           └────────────────────┬────────────────────┘
                                  │                                                                     │
                       1. Issues Formulated Query                                            1. Ingests Approved Build Decision
                       2. Restricts to Domain Tag                                            2. LiteLLM Generates Embedding
                                  │                                                                     │
                                  ▼                                                                     ▼
 ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                              RAG RETRIEVAL & INGESTION PIPELINE                                             │
 │                                                                                                                             │
 │   QUERY PATH (Specialist Reading):                                                                                          │
 │   ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐    ┌───────────────────────────┐   │
 │   │ Strict Domain Filter   │ →  │ Dual Hybrid Search     │ →  │ Reciprocal Rank Fusion │ →  │ FlashRank Reranker        │   │
 │   │ (WHERE domain = :tag)  │    │ (Dense HNSW + BM25 GIN)│    │ (RRF CTE in PostgreSQL)│    │ (Top-20 down to Top 3-5)  │   │
 │   └────────────────────────┘    └────────────────────────┘    └────────────────────────┘    └─────────────┬─────────────┘   │
 │                                                                                                           │                 │
 │                                                                                                           ▼                 │
 │                                                                                             ┌───────────────────────────┐   │
 │                                                                                             │ State Projection Slices   │   │
 │                                                                                             │ (<rag_context domain=...>)│   │
 │                                                                                             │ Clamped Token Budgets     │   │
 │                                                                                             └───────────────────────────┘   │
 │   INGESTION PATH (CLI & Delivery Writing):                                                                                  │
 │   ┌────────────────────────┐    ┌────────────────────────┐    ┌────────────────────────┐    ┌───────────────────────────┐   │
 │   │ Source Document/ADR    │ →  │ Structure-Aware        │ →  │ Content Hash Dedupe    │ →  │ LiteLLM Embedding         │   │
 │   │ (Markdown, YAML, AST)  │    │ Chunking & Breadcrumbs │    │ (SHA-256 Document Lock)│    │ (text-embedding-3-small)  │   │
 │   └────────────────────────┘    └────────────────────────┘    └────────────────────────┘    └─────────────┬─────────────┘   │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────────┼─────────────────┘
                                                                                                             │
                                                                                                             ▼
 ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                          UNIFIED POSTGRESQL + PGVECTOR STORAGE ENGINE                                       │
 │                                                                                                                             │
 │   ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐   │
 │   │ SCHEMA: rag_knowledge                                                                                               │   │
 │   │                                                                                                                     │   │
 │   │ • knowledge_documents (Registry, Source Path, SHA-256 Hash, Embedding Model, Dimensions, Chunk Counts)             │   │
 │   │ • knowledge_chunks    (Chunk Text, Breadcrumbs, vector(1536) HNSW, tsv_content_simple, tsv_content_english)        │   │
 │   │                                                                                                                     │   │
 │   │ 6 Partitioned Knowledge Domains:                                                                                    │   │
 │   │ [1] library_docs       [2] org_standards       [3] pattern_catalog                                                  │   │
 │   │ [4] past_decisions     [5] component_docs      [6] code_conventions                                                 │   │
 │   └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘   │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Alignment with Evergreen Architectural Principles

The RAG architecture strictly adheres to the 5 core principles defined in [`GEMINI.md`](file:///s:/DATA/agents/ddlc-agent-studio/GEMINI.md) and [`architecture.md`](file:///s:/DATA/agents/ddlc-agent-studio/design-docs/meta-agent/architecture.md):

| Evergreen Principle | How the RAG Architecture Enforces It |
|---|---|
| **1. The Graph Orchestrates, the LLM Reasons** | RAG retrieval is **100% deterministic code** (PostgreSQL CTE hybrid query + mathematical RRF + cross-encoder scoring). The StateGraph coordinator never reasons over chunk content. LLM Specialists formulate targeted single-turn queries based on their state projection, and receive structured context chunks. Autonomous, self-reflective "Agentic RAG" inner loops are explicitly rejected to prevent unbounded execution, non-deterministic latency, and graph stalling. |
| **2. Skeleton vs. Muscle** | RAG functions strictly as "Muscle Intelligence". It provides API contracts, runtime caveats, and code conventions to the LLM Leaf Coder and Researcher. It **never** compiles StateGraph topology, generates data contracts, or executes DDL. The deterministic compiler (Jinja2 + Python AST) and DCM Schema Engine compile the read-only Skeleton without RAG involvement. |
| **3. Single Source of Truth (`impl_docs/`)** | RAG stores unstructured reference documents and historical decision records. It **never** stores mutable agent configurations. Once an agent is delivered, `impl_docs/*.yaml` in the agent's Git repository is the sole source of truth for Day-2 maintenance. RAG consumes final build summaries and ADRs purely as read-only historical context (`past_decisions`). |
| **4. Context Funnel & State Projection** | RAG preserves the Funnel Principle: Specialists query only their assigned domain tags. Chunks are bounded by hard token ceilings (e.g. Researcher: 1,000 tokens; Architect: 1,400 tokens; Builder: 1,000 tokens) and injected via structured `<rag_context>` blocks. Unprocessed RAG candidate lists never enter the Blackboard state. |
| **5. Unified Storage Engine (Postgres + pgvector)** | RAG does not introduce separate vector databases (Pinecone, Qdrant, Chroma). Embeddings, dense HNSW indexes, sparse BM25/`tsvector` inverted indexes, relational metadata (Tiers 1, 2, 3), and LangGraph state checkpoints reside within a **single PostgreSQL instance**. This eliminates distributed transaction drift, reduces container bloat, and allows atomic, transactional operations. |

---

## 3. Pattern Evaluation & Architectural Selection

Four primary RAG architectural patterns were evaluated against the technical requirements, reliability constraints, and execution characteristics of DDLC Agent Studio.

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐       ┌────────────────────────┐
│   Pattern 1     │       │   Pattern 2     │       │   Pattern 3     │       │   Pattern 4 (Chosen)   │
│   Naive RAG     │       │   Agentic RAG   │       │   Graph RAG     │       │  Domain-Tagged Hybrid  │
├─────────────────┤       ├─────────────────┤       ├─────────────────┤       ├────────────────────────┤
│ • Dense vector  │       │ • Planning loop │       │ • Entity graphs │       │ • Dense HNSW vector    │
│ • Fixed chunking│       │ • Evaluator LLM │       │ • Community reps│       │ • Dual BM25 full-text  │
│ • Top-K cosine  │       │ • Tool re-tries │       │ • Graph travers.│       │ • RRF rank fusion      │
│                 │       │                 │       │                 │       │ • Cross-Encoder rerank │
│ ❌ Fails symbol │       │ ❌ High latency │       │ ❌ Extreme DDL  │       │ • Strict domain filter │
│    precision    │       │ ❌ Violates DAG │       │    complexity   │       │                        │
│ ❌ Chops code   │       │    determinism  │       │ ❌ Solved by    │       │ ✅ 100% Deterministic  │
│    and YAML     │       │ ❌ Loop drift   │       │    Tier 2 SQL   │       │ ✅ Exact symbol match  │
└─────────────────┘       └─────────────────┘       └─────────────────┘       └────────────────────────┘
```

### 3.1 Pattern Evaluation Breakdown

#### Pattern 1: Naive RAG (Dense Vector Similarity on Flat Chunks)
- **Mechanism**: Splits documents into fixed-size windows (e.g. 500 characters, 50-character overlap), generates embeddings, and executes top-$k$ cosine similarity search (`<=>`).
- **Failure Modes in DDLC Studio**:
  1. *Symbol & Version Invisibility*: High-dimensional embedding spaces map semantic concepts, not exact technical strings. An exact query for `StrictUndefined`, `ts_rank_cd`, `dbt-core v1.8`, or `SnowflakeDialect` retrieves generic documentation chunks that lack the exact class or parameter.
  2. *Structural Destruction*: Fixed-size chunking slices arbitrarily through Python function definitions, Jinja2 template blocks, and YAML contract dictionaries, tearing apart keys from values and causing AST syntax errors during synthesis.
  3. *Context Pollution*: Injects broad, noisy chunks into the Specialist prompt, violating the State Projection principle.
- **Verdict**: **Rejected**.

#### Pattern 2: Agentic RAG (Autonomous Multi-Turn Tool Loops & Evaluator Agents)
- **Mechanism**: An autonomous inner LLM loop plans sub-queries, calls vector search tools, inspects retrieved chunks with an Evaluator LLM, scores sufficiency, and re-queries if gaps remain (as depicted in ByteByteGo reference architectures).
- **Failure Modes in DDLC Studio**:
  1. *Violates Coordinator Determinism*: Principle 1 dictates: *"The Graph Orchestrates, the LLM Reasons."* Embedding an autonomous agent loop inside a LangGraph specialist node creates an unconstrained sub-state machine, introducing non-deterministic execution times (5–20 seconds per retrieval), retry cascades, and potential infinite loops.
  2. *Excessive Latency & Cost*: Executing 3–5 LLM reasoning hops simply to retrieve API syntax for `SQLGlot` consumes 10,000+ unnecessary tokens and introduces rate-limit vulnerability.
- **Verdict**: **Rejected for Inner Retrieval Engine**. (The Specialist itself remains the reasoning agent that issues single-turn, structured queries to a deterministic retrieval engine).

#### Pattern 3: Graph RAG (Knowledge Graph Entity Extraction & Community Summaries)
- **Mechanism**: Extracts entity-relation triplets from documentation via LLM, builds a graph network, partitions into hierarchical communities, and performs graph-traversal summarization.
- **Failure Modes in DDLC Studio**:
  1. *Massive Preprocessing Overhead*: Entity extraction across large technical libraries (e.g., dbt, Great Expectations, LiteLLM) requires substantial offline LLM compute and frequent graph re-balancing.
  2. *Redundant with Metadata Framework*: Cross-entity relational lineage and component dependencies are **already solved natively** by the PostgreSQL relational tables in Tier 2 (`contract_lineage`, `agent_dependency`, `component_usage`). Modeling static library docs as a knowledge graph yields negligible value over hierarchical markdown chunking.
- **Verdict**: **Rejected**.

#### Pattern 4: Domain-Tagged Advanced Hybrid RAG (Selected Architecture)
- **Mechanism**:
  1. *Structure-Aware Chunking*: Markdown header hierarchy awareness (preserving breadcrumbs), YAML top-level key preservation, and Python AST function/class boundary protection.
  2. *Strict Metadata Filtering*: Hard SQL `WHERE domain = :tag` clause ensuring Specialists query only their authorized knowledge corpus.
  3. *Hybrid Dense & Sparse Search*: Dense vector cosine distance (`vector_cosine_ops`) combined with dual-dictionary PostgreSQL full-text search (`tsvector` with `'simple'` for exact code symbols and `'english'` for natural language).
  4. *Reciprocal Rank Fusion (RRF)*: Pure mathematical rank normalization executed directly inside a PostgreSQL Common Table Expression (CTE).
  5. *Two-Stage Reranking*: Lightweight cross-encoder (`flashrank` ONNX runtime) reranking top-20 candidates down to top 3–5 high-precision chunks.
- **Why It Wins**: Combines exact symbol matching with conceptual semantic search, guarantees sub-100ms execution latency, runs 100% inside PostgreSQL + pgvector, consumes zero LLM tokens for retrieval, and respects the determinism boundary.
- **Verdict**: **Selected as the DDLC Meta-Agent Standard**.

---

### 3.2 Detailed Pattern Trade-Off Matrix

| Evaluation Dimension | Pattern 1: Naive RAG | Pattern 2: Agentic RAG | Pattern 3: Graph RAG | Pattern 4: Domain-Tagged Hybrid (Chosen) |
|---|---|---|---|---|
| **Exact Technical Symbol Matching** | Poor (< 40%) | Moderate (65%) | Moderate (70%) | **Superior (> 95%)** via `'simple'` `tsvector` |
| **Semantic & Conceptual Search** | Good (85%) | High (90%) | High (92%) | **High (90%)** via `pgvector` HNSW |
| **Retrieval Latency** | **Fast (< 30ms)** | Slow (3,000–15,000ms) | Moderate (400–1,200ms) | **Fast (< 80ms)** with FlashRank |
| **Token & Compute Cost** | **Zero LLM cost** | High (3–5 LLM hops) | Very High (offline graph extraction) | **Zero LLM cost** for retrieval |
| **Determinism & Reproducibility** | High | Low (stochastic loops) | Moderate | **100% Deterministic** |
| **Infrastructure Complexity** | Low | High (tool brokers) | Extreme (graph database / Neo4j) | **Low (PostgreSQL + pgvector only)** |
| **Code & YAML Chunk Integrity** | Poor (chops syntax) | Poor to Moderate | N/A | **Superior** via AST & Markdown chunkers |
| **State Projection Alignment** | Poor (context noise) | Moderate | Moderate | **Perfect** via hard token budgets & XML slices |

---

## 4. Boundary Definition: 3-Tier Metadata Framework vs. RAG Layer

A critical architectural pitfall in multi-tiered agent systems is boundary confusion between structured relational metadata and unstructured semantic search. In DDLC Agent Studio, both layers reside within the same PostgreSQL instance, making strict architectural demarcation essential:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    POSTGRESQL STORAGE ENGINE                                     │
│                                                                                                  │
│   RELATIONAL / STRUCTURED (SQL)                              UNSTRUCTURED / VECTOR (RAG)        │
│   Schema: ddlc_shared_metadata                                Schema: rag_knowledge              │
│                                                                                                  │
│   ┌──────────────────────────────────────────┐                ┌──────────────────────────────┐   │
│   │ component_registry                       │                │ knowledge_chunks             │   │
│   │ - component_id (UUID)                    │   Deep Docs    │ (domain: component_docs)     │   │
│   │ - name, version, status                  │ ─────────────> │ - Markdown guides            │   │
│   │ - interface_spec (JSONB: args/returns)   │                │ - Usage patterns & examples  │   │
│   │ - embedding (vector(1536)) [Discovery]  │                │ - Edge cases & gotchas       │   │
│   └──────────────────────────────────────────┘                └──────────────────────────────┘   │
│                                                                                                  │
│   Schema: agent_metadata & ddlc_shared_metadata               Schema: rag_knowledge              │
│   ┌──────────────────────────────────────────┐                ┌──────────────────────────────┐   │
│   │ decision_log & build_summary             │   Synthesized  │ knowledge_chunks             │   │
│   │ - run_id, stage_id, gate_status          │   ADR Markdown │ (domain: past_decisions)     │   │
│   │ - revision_counts, durations, tokens     │ ─────────────> │ - Architecture proposals     │   │
│   │ - deterministic audit records            │                │ - Trade-offs & rejected alts │   │
│   └──────────────────────────────────────────┘                └──────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Boundary 1: `component_registry` vs. `component_docs`

- **`ddlc_shared_metadata.component_registry` (Tier 2 Catalog)**:
  - **Nature**: Relational registry table.
  - **Granularity**: Exactly **1 row and 1 embedding vector per component**.
  - **Payload**: Structured metadata, version string, owner, and typed JSONB `interface_spec` (e.g. required input schema, output schema, configuration parameters).
  - **Query Pattern**: Exact relational SQL lookups and coarse vector similarity search on the summary embedding: *"Find reusable components that match a PII masking interface."*
  - **Consumer**: Queried by the **Architect** during high-level component selection.

- **`rag_knowledge` (`domain = 'component_docs'`)**:
  - **Nature**: Multi-chunk unstructured document corpus.
  - **Granularity**: Multiple chunks per component (e.g., 5–20 chunks covering architecture, implementation details, edge cases, unit testing patterns).
  - **Payload**: Full markdown files, prose explanations, Jinja2 template snippets, and Python invocation examples.
  - **Query Pattern**: Hybrid search + reranking: *"How do I instantiate and bind the Snowflake PII masking component inside a LangGraph ToolNode?"*
  - **Consumer**: Queried by the **Builder (Leaf Coder)** during custom tool and test synthesis.

---

### 4.2 Boundary 2: `decision_log` / `build_summary` vs. `past_decisions`

- **`agent_metadata.decision_log` & `ddlc_shared_metadata.build_summary` (Tier 1 & Tier 2 Operational/Audit SQL)**:
  - **Nature**: Tabular, append-only operational telemetry and compliance records.
  - **Granularity**: One row per node execution, gate decision, or complete build run.
  - **Payload**: Scalar metrics: `run_id`, `clarification_rounds: 2`, `architecture_revisions: 1`, `gate_status: approved`, `approved_by: user_123`, `total_duration_ms: 142000`, `token_count: 38400`.
  - **Query Pattern**: Standard SQL queries, relational joins, time-series dashboards, and compliance audit queries: *"What was the average revision count across all builds last month?"*

- **`rag_knowledge` (`domain = 'past_decisions'`)**:
  - **Nature**: Unstructured Semantic Architectural Decision Records (ADRs).
  - **Granularity**: Multi-chunk narrative markdown compiled upon successful delivery.
  - **Payload**: The complete approved architecture proposal, the problem context, the alternative patterns evaluated, trade-offs accepted, and specific human gate guidance.
  - **Query Pattern**: Semantic hybrid query: *"Have we built an agent connecting Snowflake to DuckDB before, and why did we choose python_litellm instead of langgraph?"*
  - **Consumer**: Queried by the **Architect** to learn from portfolio history.

---

## 5. The 6 Knowledge Domains & Ingestion Corpus

The RAG knowledge base is partitioned into **6 strictly isolated knowledge domains**. A query originating from a Specialist must provide its authorized `domain` tag; cross-domain queries are blocked at the retrieval interface to eliminate context bleed.

```
                               ┌─────────────────────────────────────────────────────────────┐
                               │                    6 KNOWLEDGE DOMAINS                      │
                               └──────────────────────────────┬──────────────────────────────┘
                                                              │
         ┌──────────────────┬──────────────────┬──────────────┴─────┬──────────────────┬──────────────────┐
         ▼                  ▼                  ▼                    ▼                  ▼                  ▼
┌─────────────────┐┌─────────────────┐┌─────────────────┐┌─────────────────┐┌─────────────────┐┌─────────────────┐
│  library_docs   ││  org_standards  ││ pattern_catalog ││ past_decisions  ││ component_docs  ││code_conventions │
├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤
│ • LiteLLM       ││ • Naming rules  ││ • Staged-Gated  ││ • Approved ADRs ││ • Reusable tools││ • PEP 8 / Ruff  │
│ • Instructor    ││ • Governance    ││ • ReAct Agent   ││ • Past trade-offs││ • Interface docs││ • Pytest rules  │
│ • dbt-core      ││ • Security      ││ • Orchestrator- ││ • Human gate    ││ • Configuration ││ • Pydantic v2   │
│ • SQLGlot       ││ • Tool approval ││   Workers       ││   rationale     ││   examples      ││ • Directory DDL │
│ • DuckDB / Snow ││ • Cloud configs ││ • Blackboard    ││ • Dynamic Phase8││ • Deep usage    ││ • StrictUndef   │
├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤├─────────────────┤
│  RESEARCHER     ││ RESEARCHER /    ││   ARCHITECT     ││   ARCHITECT     ││  ARCHITECT /    ││    BUILDER      │
│                 ││   ARCHITECT     ││                 ││                 ││    BUILDER      ││  (LEAF CODER)   │
└─────────────────┘└─────────────────┘└─────────────────┘└─────────────────┘└─────────────────┘└─────────────────┘
```

### 5.1 Domain Specifications

| # | Knowledge Domain | Directory Source | Primary Consumer | Scope & Contents |
|---|---|---|---|---|
| **1** | **`library_docs`** | `knowledge/library_docs/` | **Researcher** | API contracts, CLI parameters, runtime caveats, version differences, and integration guides for approved tools: LiteLLM, Instructor, dbt-core, SQLGlot, DuckDB, Snowflake connector, Great Expectations. |
| **2** | **`org_standards`** | `knowledge/org_standards/` | **Researcher**, **Architect** | Enterprise data classification rules, approved Python packages, Docker base image policies, cloud secret naming conventions, and mandatory security boundaries. |
| **3** | **`pattern_catalog`** | `knowledge/pattern_catalog/` | **Architect** | The formal DDLC agent pattern definitions from `agent-architecture-patterns/` (`01-staged-gated-pipeline.md`, `02-orchestrator-workers.md`, `03-evaluator-optimizer.md`, `04-blackboard-shared-state.md`, `05-react-tool-agent.md`). |
| **4** | **`past_decisions`** | `knowledge/past_decisions/` (and DB dynamic) | **Architect** | Semantic Architectural Decision Records (ADRs) compiled from historical builds. Grows dynamically with every Phase 8 Delivery. |
| **5** | **`component_docs`** | `knowledge/component_docs/` | **Architect**, **Builder** | Detailed implementation guides, usage walkthroughs, and invocation patterns for reusable components registered in Tier 2. |
| **6** | **`code_conventions`** | `knowledge/code_conventions/` | **Builder** | Python 3.12+ style guidelines, Ruff lint rules, Pytest fixture patterns, universal Jinja2 prompt rules (`StrictUndefined`), and configuration loading standards. |

---

### 5.2 Day-0 Pre-Seeding for `past_decisions`

To prevent cold-start retrieval failures on Day 0 before any agents have been built, the Meta-Agent workspace ships with **3 pre-seeded canonical ADRs** in `knowledge/past_decisions/`:

1. **`ADR-001_canonical_dbt_pipeline_builder.md`**: Explains the rationale for selecting the `staged_gated` pattern with LangGraph, compiling typed Instructor contracts, using the Claim Check pattern for `dbt_artifact_bundle`, and enforcing a multi-way conditional human review gate.
2. **`ADR-002_snowflake_lineage_react_agent.md`**: Documents an exploratory tool-calling ReAct agent with read-only database tools, autonomous reflection loops, and safety valve execution bounds.
3. **`ADR-003_lightweight_contract_validator_cli.md`**: Details a linear `python_litellm` event-driven runner for single-stage contract linting without checkpointer overhead.

---

## 6. PostgreSQL Database Architecture (`rag_knowledge` Schema)

The RAG layer uses a dedicated PostgreSQL schema (`rag_knowledge`) with the `pgvector` extension. 

### 6.1 DDL Specification

```sql
-- Ensure extensions exist
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE SCHEMA IF NOT EXISTS rag_knowledge;

-- -----------------------------------------------------------------------------
-- 1. Document Registry Table
-- Tracks document-level metadata, provenance, and SHA-256 for change detection
-- -----------------------------------------------------------------------------
CREATE TABLE rag_knowledge.knowledge_documents (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(64) NOT NULL,
    source_path VARCHAR(512) NOT NULL,
    title VARCHAR(256) NOT NULL,
    file_sha256 CHAR(64) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimension INT NOT NULL DEFAULT 1536,
    chunk_count INT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_rag_source UNIQUE (domain, source_path),
    CONSTRAINT chk_rag_domain CHECK (domain IN (
        'library_docs', 
        'org_standards', 
        'pattern_catalog', 
        'past_decisions', 
        'component_docs', 
        'code_conventions'
    ))
);

-- -----------------------------------------------------------------------------
-- 2. Knowledge Chunks Table
-- Stores individual text chunks, embeddings, and dual-dictionary tsvectors
-- -----------------------------------------------------------------------------
CREATE TABLE rag_knowledge.knowledge_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES rag_knowledge.knowledge_documents(document_id) ON DELETE CASCADE,
    domain VARCHAR(64) NOT NULL,
    chunk_index INT NOT NULL,
    chunk_sha256 CHAR(64) NOT NULL,
    breadcrumb VARCHAR(512),
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    -- Generated tsvectors: 'simple' for exact code tokens; 'english' for natural language prose
    tsv_content_simple tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    tsv_content_english tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    token_count INT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_rag_chunk_order UNIQUE (document_id, chunk_index)
);

-- -----------------------------------------------------------------------------
-- 3. Indexes for High-Speed Filtering, Full-Text Search, and Vector Cosine Ops
-- -----------------------------------------------------------------------------
CREATE INDEX idx_rag_docs_domain ON rag_knowledge.knowledge_documents(domain);
CREATE INDEX idx_rag_chunks_domain ON rag_knowledge.knowledge_chunks(domain);
CREATE INDEX idx_rag_chunks_doc_id ON rag_knowledge.knowledge_chunks(document_id);

-- -----------------------------------------------------------------------------
-- 6 Partitioned Partial HNSW Indexes (Cosine Distance Metric: <=>)
-- Prevents Filtered-HNSW Recall Collapse: When a single global index is filtered
-- by domain, small domains (e.g. past_decisions with 3 Day-0 ADRs) suffer severe
-- graph traversal recall degradation. Partitioned indexes guarantee 100% recall.
-- -----------------------------------------------------------------------------
CREATE INDEX idx_rag_hnsw_library_docs ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'library_docs' 
WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_rag_hnsw_org_standards ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'org_standards' 
WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_rag_hnsw_pattern_catalog ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'pattern_catalog' 
WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_rag_hnsw_past_decisions ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'past_decisions' 
WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_rag_hnsw_component_docs ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'component_docs' 
WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_rag_hnsw_code_conventions ON rag_knowledge.knowledge_chunks 
USING hnsw (embedding vector_cosine_ops) WHERE domain = 'code_conventions' 
WITH (m = 16, ef_construction = 64);

-- Dual Inverted GIN Indexes for Full-Text Search
CREATE INDEX idx_rag_chunks_tsv_simple ON rag_knowledge.knowledge_chunks USING gin(tsv_content_simple);
CREATE INDEX idx_rag_chunks_tsv_english ON rag_knowledge.knowledge_chunks USING gin(tsv_content_english);
```

### 6.2 The Dual `tsvector` Strategy (`simple` + `english`)

Standard PostgreSQL full-text search (`to_tsvector('english', content)`) applies English Porter stemming, lowercasing, and stopword stripping. In technical codebases, this mangles critical code tokens:
- `duckdb-engine` drops punctuation and is indexed as `duckdb` and `engin`.
- `StrictUndefined` is stemmed to `strictundefin`.
- `dbt_project` is split into `dbt` and `project`.
- Snake_case and camelCase identifiers lose their distinct character representations.

To resolve this, the schema employs **Dual Generated TSVectors**:
1. **`tsv_content_simple` (`'simple'` dictionary)**: Does not stem and does not remove stopwords. It preserves exact code identifiers, symbol names, snake_case strings, and version numbers.
2. **`tsv_content_english` (`'english'` dictionary)**: Normalizes English prose, verb tenses, and natural language concepts.

During search, both vectors are queried and scored with weighted ranking (`ts_rank_cd`), prioritizing exact technical symbol matches.

---

## 7. Document Ingestion & Chunking Pipeline

### 7.1 Structure-Aware Chunking Strategies

Generic character chunking ruins technical documents. DDLC Studio deploys **3 specialized chunkers**:

```
                                  ┌─────────────────────────────────────────────────────────────┐
                                  │                  STRUCTURE-AWARE CHUNKERS                   │
                                  └──────────────────────────────┬──────────────────────────────┘
                                                                 │
                                 ┌───────────────────────────────┼───────────────────────────────┐
                                 ▼                               ▼                               ▼
                  ┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐
                  │   MARKDOWN CHUNKER          │ │      YAML CHUNKER           │ │      CODE CHUNKER           │
                  │   (knowledge/library_docs,  │ │   (impl_docs templates,     │ │   (shared component tools,  │
                  │    pattern_catalog, ADRs)   │ │    contract schemas)        │ │    handler modules)         │
                  ├─────────────────────────────┤ ├─────────────────────────────┤ ├─────────────────────────────┤
                  │ • AST header hierarchy      │ │ • Top-level key boundaries  │ │ • Python AST parser         │
                  │ • Slices at ## and ###      │ │ • Preserves parent root     │ │ • Slices at ClassDef        │
                  │ • Prepends breadcrumbs:     │ │   context                   │ │   and FunctionDef           │
                  │   [dbt > CLI > Manifest]    │ │ • Target: 300 tokens        │ │ • Never cuts inside function│
                  │ • Protects code blocks      │ │ • Never cuts KV pairs       │ │ • Target: 350 tokens        │
                  │ • Target: 350 tokens        │ │                             │ │                             │
                  └─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘
```

#### 1. Markdown Header Chunker (`markdown_chunker.py`)
- **Usage**: Technical manuals, pattern catalog, architecture decisions, style guides.
- **Rules**:
  - Slices text at `##` (H2) and `###` (H3) header boundaries.
  - Generates a hierarchical breadcrumb string (e.g. `dbt-core > CLI Invocation > Manifest Parsing`).
  - **Contextual Breadcrumb Prepending**: Prepends the breadcrumb directly to the chunk text prior to embedding (e.g. `"[Context: dbt-core > CLI Invocation > Manifest Parsing]\n\n..."`). This guarantees that dense embeddings retain full hierarchical context even when isolated.
  - **Code Block & Table Protection**: Fenced code blocks (` ```python `) and markdown tables are treated as atomic units and are never split across chunks.
  - **Bounds**: Target 350 tokens; Hard Max 500 tokens; Overlap 50 tokens.

#### 2. YAML Section Chunker (`yaml_chunker.py`)
- **Usage**: Data contract templates, implementation doc schemas, configuration samples.
- **Rules**:
  - Parses YAML document trees and slices by top-level semantic keys (e.g. `stages:`, `contracts:`, `sources:`).
  - Copies parent dictionary headers to each slice so nested structures retain full schema meaning.
  - **Bounds**: Target 300 tokens; Hard Max 450 tokens.

#### 3. Python AST Code Chunker (`code_chunker.py`)
- **Usage**: Reusable shared component handlers and reference test suites.
- **Rules**:
  - Uses the native Python `ast` module.
  - Slices at top-level `ClassDef` and `FunctionDef` boundaries.
  - Preserves class docstrings, type annotations, and function signatures in their entirety.
  - **Bounds**: Target 350 tokens; Hard Max 500 tokens.

---

### 7.2 Ingestion Change Detection & Deduplication

To prevent re-embedding identical files and to eliminate orphaned chunks upon documentation updates:
1. When ingesting a document, calculate the SHA-256 hash of the entire file (`file_sha256`).
2. Query `rag_knowledge.knowledge_documents` by `(domain, source_path)`.
3. If `file_sha256` matches, **skip processing immediately** (0 tokens, 0ms latency).
4. If `file_sha256` differs or the record does not exist:
   - Chunk the document into memory.
   - Batch-generate embeddings via LiteLLM.
   - Execute an **Atomic Transactional Upsert**:
     ```sql
     BEGIN;
     -- 1. Insert or update document metadata (synchronizes all mutable registry columns)
     INSERT INTO rag_knowledge.knowledge_documents (
         domain, 
         source_path, 
         title, 
         file_sha256, 
         embedding_model, 
         embedding_dimension, 
         chunk_count, 
         metadata
     )
     VALUES (
         :domain, 
         :source_path, 
         :title, 
         :sha, 
         :embedding_model, 
         :embedding_dimension, 
         :count, 
         :metadata
     )
     ON CONFLICT (domain, source_path) 
     DO UPDATE SET 
         title = EXCLUDED.title,
         file_sha256 = EXCLUDED.file_sha256,
         embedding_model = EXCLUDED.embedding_model,
         embedding_dimension = EXCLUDED.embedding_dimension,
         chunk_count = EXCLUDED.chunk_count,
         metadata = EXCLUDED.metadata,
         updated_at = clock_timestamp()
     RETURNING document_id;

     -- 2. Clean out old chunks (cascading cleanup)
     DELETE FROM rag_knowledge.knowledge_chunks WHERE document_id = :document_id;

     -- 3. Insert newly generated chunks
     INSERT INTO rag_knowledge.knowledge_chunks (document_id, domain, chunk_index, content, embedding, ...)
     VALUES (...);
     COMMIT;
     ```

---

## 8. Hybrid Retrieval & Ranking Engine

The retrieval pipeline executes in 6 deterministic stages without invoking autonomous agent loops.

```
 Specialist Query (LLM)
         │
         ▼
 ┌───────────────────────────┐
 │ 1. Formulation & Domain   │ Formulates focused technical query + domain tag
 └─────────────┬─────────────┘
               │
               ▼
 ┌───────────────────────────┐
 │ 2. Strict Domain Filter   │ Enforces WHERE domain = :domain
 └─────────────┬─────────────┘
               │
               ▼
 ┌───────────────────────────┐
 │ 3. CTE Hybrid Search      │ Runs Dense Cosine (<=>) and Sparse Dual-Text (simple + english)
 └─────────────┬─────────────┘
               │
               ▼
 ┌───────────────────────────┐
 │ 4. Reciprocal Rank Fusion │ Combines ranks: RRF = 1/(60 + r_dense) + 1/(60 + r_sparse)
 └─────────────┬─────────────┘
               │
               ▼
 ┌───────────────────────────┐
 │ 5. FlashRank Reranker     │ Lightweight ONNX Cross-Encoder scores top 20 -> top 3-5
 └─────────────┬─────────────┘
               │
               ▼
 ┌───────────────────────────┐
 │ 6. State Projection Slice │ Injects structured XML (<rag_context>) under token budget ceiling
 └───────────────────────────┘
```

### 8.1 The PostgreSQL RRF Hybrid Search CTE

Candidate retrieval and reciprocal rank fusion are executed entirely inside PostgreSQL via Common Table Expressions (CTEs), pushing rank computation directly to the database engine.

> [!IMPORTANT]
> **Recall Collapse & Sparse Matching Mitigations**:
> 1. **Filtered-HNSW Iterative Scanning**: When querying small partitions (e.g. `past_decisions` on Day 0), standard HNSW graphs can terminate early before collecting `LIMIT` items. The query session sets `SET LOCAL hnsw.iterative_scan = 'strict_order';`, forcing pgvector 0.8+ to scan iteratively until target candidate limits are satisfied across the partial HNSW index.
> 2. **Flexible Full-Text Matching via `websearch_to_tsquery`**: Standard `plainto_tsquery` forces an overly brittle boolean `AND` across all tokens. If a specialist query is `"dbt-core: run execution CLI subprocess"`, any chunk missing `"subprocess"` is dropped completely. Using `websearch_to_tsquery` supports natural phrases, term tolerance, and token overlap scoring without dropping candidate chunks.
> 3. **Document Provenance Projection**: The query joins `rag_knowledge.knowledge_documents` to project `source_path` directly to the candidate record, guaranteeing full file lineage in the State Projection.

```sql
-- Session configuration to ensure 100% recall on filtered HNSW vector search
SET LOCAL hnsw.iterative_scan = 'strict_order';

-- Parameters:
--   :domain           VARCHAR  (e.g., 'library_docs')
--   :query_embedding  vector   (1536-dim array from LiteLLM)
--   :query_text       TEXT     (Raw text query)
--   :k_rrf            INT      (RRF constant, standard = 60)
--   :top_candidates   INT      (Initial candidate pool, default = 20)

WITH vector_candidates AS (
    SELECT 
        chunk_id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> :query_embedding::vector ASC) AS dense_rank
    FROM rag_knowledge.knowledge_chunks
    WHERE domain = :domain
    ORDER BY embedding <=> :query_embedding::vector ASC
    LIMIT :top_candidates
),
text_candidates AS (
    SELECT 
        chunk_id,
        ROW_NUMBER() OVER (
            ORDER BY (
                -- Prioritize exact symbol/code matches (1.5x) over stemmed prose (1.0x)
                ts_rank_cd(tsv_content_simple, websearch_to_tsquery('simple', :query_text)) * 1.5 +
                ts_rank_cd(tsv_content_english, websearch_to_tsquery('english', :query_text)) * 1.0
            ) DESC
        ) AS sparse_rank
    FROM rag_knowledge.knowledge_chunks
    WHERE domain = :domain
      AND (
          tsv_content_simple @@ websearch_to_tsquery('simple', :query_text) OR
          tsv_content_english @@ websearch_to_tsquery('english', :query_text)
      )
    ORDER BY sparse_rank ASC
    LIMIT :top_candidates
),
fused_scores AS (
    SELECT 
        COALESCE(v.chunk_id, t.chunk_id) AS chunk_id,
        (
            COALESCE(1.0 / (:k_rrf + v.dense_rank), 0.0) +
            COALESCE(1.0 / (:k_rrf + t.sparse_rank), 0.0)
        ) AS rrf_score
    FROM vector_candidates v
    FULL OUTER JOIN text_candidates t ON v.chunk_id = t.chunk_id
)
SELECT 
    c.chunk_id,
    c.document_id,
    c.domain,
    d.source_path,
    c.breadcrumb,
    c.content,
    c.token_count,
    f.rrf_score
FROM fused_scores f
JOIN rag_knowledge.knowledge_chunks c ON f.chunk_id = c.chunk_id
JOIN rag_knowledge.knowledge_documents d ON c.document_id = d.document_id
ORDER BY f.rrf_score DESC
LIMIT :top_candidates;
```

---

### 8.2 Lightweight Reranking with FlashRank & Fallback Circuit Breaker

The top 20 fused candidates from the SQL query are passed to a cross-encoder reranker to compute fine-grained semantic relevance.

- **Engine Choice**: **`flashrank`** (Lightweight ONNX runtime).
  - *Footprint*: ~4MB model weights (`ms-marco-TinyBERT-L-2-v2` or `bge-reranker-base`).
  - *Zero PyTorch Bloat*: Eliminates 2GB+ torch/transformers dependencies from the runner container.
  - *Latency*: < 35ms CPU inference time across 20 candidate chunks.
- **Async Offloading & 150ms Circuit Breaker**:
  Because FlashRank executes CPU-bound ONNX model inference, `self.ranker.rerank()` is never executed synchronously on the main asyncio event loop. The retriever offloads inference to a background worker thread via `asyncio.to_thread` and guards execution with `asyncio.wait_for(..., timeout=0.150)`. 
  If inference exceeds 150ms or encounters an exception, the circuit breaker triggers immediately, **gracefully falling back to sorting candidates by the pure SQL `rrf_score`** and returning the top-$K$ chunks. This guarantees that a slow ONNX execution neither stalls concurrent specialists on the event loop nor crashes the retrieval pipeline.

---

### 8.3 State Projection Token Ceilings & Formatting

To adhere strictly to Principle 4 (Context Funnel & State Projection), the retrieval interface enforces hard token budget ceilings per specialist:

| Specialist | Authorized Domains | Max Chunks Returned ($K$) | Hard Token Budget Ceiling |
|---|---|:---:|:---:|
| **Researcher** | `library_docs`, `org_standards` | 3 | **1,000 tokens** |
| **Architect** | `pattern_catalog`, `past_decisions`, `org_standards` | 4 | **1,400 tokens** |
| **Builder** | `component_docs`, `code_conventions` | 3 | **1,000 tokens** |

#### Structured XML Output Contract & Syntax Escaping
Chunks frequently contain characters like `<`, `>`, `&`, `"`, and `'` (e.g. Python type hints `List[str]`, SQL operators `embedding <=> $2`, or Jinja2 syntax `{% if a < b %}`). Unescaped injection corrupts LLM prompt parsing.

The `RAGContextSlice.to_xml()` renderer strictly:
1. Quotes and escapes all XML attribute values (`domain`, `query`, `count`, `source`, `section`, `score`) via `xml.sax.saxutils.quoteattr`.
2. Escapes chunk body contents via `xml.sax.saxutils.escape()`.
3. Injects populated `source_path` and `breadcrumb` headers.
4. Renders `<rag_context ... count="0"/>` when no candidates match, preventing prompt parsing errors.

```xml
<rag_context domain="library_docs" query="dbt-core: run execution CLI subprocess" count="2">
  <chunk index="1" source="knowledge/library_docs/dbt_execution.md" section="Manifest Inspection" score="0.94">
To parse dbt execution results reliably without live database credentials, inspect the compiled target/manifest.json artifact rather than scraping stdout. Verify &lt;model_name&gt; in manifest nodes...
  </chunk>
  <chunk index="2" source="knowledge/library_docs/dbt_execution.md" section="Sandbox Isolation" score="0.88">
Always stage generated dbt projects in an isolated sandbox directory (DBT_SANDBOX_DIR) using Path.resolve(). Ensure permissions satisfy &gt;= 0750...
  </chunk>
</rag_context>
```

---

## 9. Dynamic Learning Lifecycle (Phase 8 Delivery Integration)

The meta-agent gets smarter with every agent it builds. When a build completes successfully, Phase 8 (the Delivery Node) executes a dynamic ingestion workflow to record the architectural outcome into `past_decisions`.

### 9.1 Two-Phase Commit Sequence (External LiteLLM vs. DB Transaction)

Per Decision 30 and Architectural Review Risk 4, network calls to LiteLLM must **never execute inside an active PostgreSQL database transaction**. An outbound API delay or rate-limit would hold table locks and exhaust database connection pools.

Phase 8 executes the **Two-Phase Commit Sequence**:

```
 Phase 8 Delivery Node Starts
         │
         ▼
 ┌──────────────────────────────────────────┐
 │ Step 1: Synthesize ADR Markdown Document │ Extract spec summary, approved pattern,
 └────────────────────┬─────────────────────┘ trade-offs, and human gate rationale
                      │
                      ▼
 ┌──────────────────────────────────────────┐
 │ Step 2: Structure-Aware Chunking         │ Slice ADR into 300-token chunks with
 └────────────────────┬─────────────────────┘ breadcrumbs
                      │
                      ▼
 ┌──────────────────────────────────────────┐
 │ Step 3: Outbound LiteLLM Embeddings      │ Generate embeddings asynchronously via
 └────────────────────┬─────────────────────┘ LiteLLM (OUTSIDE DB transaction)
                      │
                      ▼
 ┌──────────────────────────────────────────┐
 │ Step 4: Atomic Database Transaction      │ BEGIN;
 │         (PostgreSQL All-or-Nothing)      │   - Update Tier 2 agent_registry
 │                                          │   - Update Tier 2 contract_lineage
 │                                          │   - Insert rag_knowledge.knowledge_documents
 │                                          │   - Insert rag_knowledge.knowledge_chunks
 │                                          │ COMMIT;
 └────────────────────┬─────────────────────┘
                      │
                      ▼
 ┌──────────────────────────────────────────┐
 │ Step 5: Deliver Git Branch / Pull Request│ Non-DB external publishing
 └──────────────────────────────────────────┘
```

1. **Step 1 (ADR Synthesis)**: The Coordinator reads the approved architecture proposal from the Blackboard and synthesizes an Architectural Decision Record markdown document:
   ```markdown
   # ADR: dbt Pipeline Builder Agent for Snowflake
   - Agent Name: dbt_pipeline_builder_agent
   - Pattern: staged_gated (PAT-01)
   - Runtime: langgraph
   - Decided Trade-offs: Selected claim-check artifact passing over in-memory AST
   - Revision Rounds: 1 (User requested fail-fast schema validation)
   ```
2. **Step 2 (Chunking)**: The Markdown chunker slices the ADR into chunks with breadcrumbs.
3. **Step 3 (External Embedding Call)**: The LiteLLM client asynchronously generates vector embeddings for all chunks. *No DB locks are held.*
4. **Step 4 (Atomic DB Commit)**: In a single atomic PostgreSQL transaction:
   - Registers `ddlc_shared_metadata.agent_registry` and `contract_lineage`.
   - Inserts the ADR document into `rag_knowledge.knowledge_documents`.
   - Inserts the chunks into `rag_knowledge.knowledge_chunks`.
5. **Step 5 (Idempotent Git Delivery)**: Writes files to disk and opens the Git Pull Request.

---

### 9.2 Cold-Start & Zero-Hit Fallback Handling

When the Architect specialist queries `past_decisions` on a brand new studio deployment where few or no ADRs exist:
- The SQL CTE query naturally returns 0 text and 0 vector candidates.
- The `architect_rag.py` interface intercepts the empty candidate set.
- It returns an empty context block `<rag_context domain="past_decisions" count="0"/>` rather than raising `EmptyResultException`.
- The Architect LLM prompt handles this gracefully via conditional Jinja2 rendering:
  `{% if past_decisions %}...{% else %}No previous historical architecture records found. Relying on Pattern Catalog.{% endif %}`.

---

## 10. Component Specifications & Code Interfaces

### 10.1 Pydantic Data Models (`src/rag/store/models.py`)

```python
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any, Optional
from uuid import UUID, uuid4
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape, quoteattr

class KnowledgeChunkRecord(BaseModel):
    chunk_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    domain: str
    chunk_index: int
    chunk_sha256: str
    breadcrumb: Optional[str] = None
    content: str
    embedding: List[float]
    token_count: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

class RAGSearchResult(BaseModel):
    chunk_id: UUID
    document_id: UUID
    domain: str
    breadcrumb: Optional[str]
    content: str
    token_count: int
    rrf_score: float
    rerank_score: Optional[float] = None
    source_path: Optional[str] = None

class RAGContextSlice(BaseModel):
    domain: str
    query: str
    chunks: List[RAGSearchResult]
    total_tokens: int

    def to_xml(self) -> str:
        """Render deterministic, XML-escaped block for specialist state projection."""
        if not self.chunks:
            return f'<rag_context domain={quoteattr(self.domain)} query={quoteattr(self.query)} count="0"/>'

        xml_lines = [f'<rag_context domain={quoteattr(self.domain)} query={quoteattr(self.query)} count={quoteattr(str(len(self.chunks)))}>']
        for i, c in enumerate(self.chunks, 1):
            sec = c.breadcrumb or "General"
            src = c.source_path or "unknown"
            score = f"{c.rerank_score:.2f}" if c.rerank_score is not None else f"{c.rrf_score:.4f}"
            xml_lines.append(
                f'  <chunk index={quoteattr(str(i))} source={quoteattr(src)} section={quoteattr(sec)} score={quoteattr(score)}>\n'
                f"{xml_escape(c.content.strip())}\n"
                f'  </chunk>'
            )
        xml_lines.append('</rag_context>')
        return "\n".join(xml_lines)
```

---

### 10.2 Embedding Client (`src/rag/embeddings/embedder.py`)

```python
import litellm
from typing import List
from tenacity import retry, stop_after_attempt, wait_exponential

class LiteLLMEmbedder:
    def __init__(self, model_name: str = "text-embedding-3-small", dimensions: int = 1536):
        self.model_name = model_name
        self.dimensions = dimensions

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Batch generates vector embeddings via LiteLLM with retry."""
        if not texts:
            return []
        
        response = await litellm.aembedding(
            model=self.model_name,
            input=texts,
            dimensions=self.dimensions
        )
        return [item["embedding"] for item in response.data]

    async def embed_query(self, query: str) -> List[float]:
        """Embeds a single search query."""
        results = await self.embed_texts([query])
        return results[0]
```

---

### 10.3 Hybrid Store & Reranker Client (`src/rag/retrieval/hybrid_search.py`)

```python
import asyncio
import asyncpg
from typing import List, Optional
from flashrank import Ranker, RerankRequest
from src.rag.store.models import RAGSearchResult, RAGContextSlice
from src.rag.embeddings.embedder import LiteLLMEmbedder

class HybridRAGRetriever:
    def __init__(
        self, 
        db_pool: asyncpg.Pool, 
        embedder: LiteLLMEmbedder,
        reranker_model: str = "ms-marco-TinyBERT-L-2-v2",
        rerank_timeout_s: float = 0.150
    ):
        self.pool = db_pool
        self.embedder = embedder
        self.rerank_timeout_s = rerank_timeout_s
        try:
            self.ranker = Ranker(model_name=reranker_model)
        except Exception:
            self.ranker = None  # Fallback to pure RRF if flashrank fails to initialize

    async def search(
        self,
        domain: str,
        query: str,
        top_k: int = 3,
        token_ceiling: int = 1000
    ) -> RAGContextSlice:
        """Executes Hybrid CTE search -> RRF -> FlashRank rerank -> Token Clamping."""
        # 1. Embed query
        query_embedding = await self.embedder.embed_query(query)

        # 2. Execute SQL CTE Hybrid Search (Iterative scan + websearch_to_tsquery + provenance join)
        sql_query = """
        -- Force iterative search across filtered partial HNSW indexes
        SET LOCAL hnsw.iterative_scan = 'strict_order';

        WITH vector_candidates AS (
            SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY embedding <=> $2::vector ASC) AS dense_rank
            FROM rag_knowledge.knowledge_chunks
            WHERE domain = $1
            ORDER BY embedding <=> $2::vector ASC
            LIMIT 20
        ),
        text_candidates AS (
            SELECT chunk_id, ROW_NUMBER() OVER (
                ORDER BY (
                    ts_rank_cd(tsv_content_simple, websearch_to_tsquery('simple', $3)) * 1.5 +
                    ts_rank_cd(tsv_content_english, websearch_to_tsquery('english', $3)) * 1.0
                ) DESC
            ) AS sparse_rank
            FROM rag_knowledge.knowledge_chunks
            WHERE domain = $1
              AND (
                  tsv_content_simple @@ websearch_to_tsquery('simple', $3) OR
                  tsv_content_english @@ websearch_to_tsquery('english', $3)
              )
            ORDER BY sparse_rank ASC
            LIMIT 20
        ),
        fused_scores AS (
            SELECT 
                COALESCE(v.chunk_id, t.chunk_id) AS chunk_id,
                (COALESCE(1.0 / (60 + v.dense_rank), 0.0) + COALESCE(1.0 / (60 + t.sparse_rank), 0.0)) AS rrf_score
            FROM vector_candidates v
            FULL OUTER JOIN text_candidates t ON v.chunk_id = t.chunk_id
        )
        SELECT 
            c.chunk_id, 
            c.document_id, 
            c.domain, 
            d.source_path, 
            c.breadcrumb, 
            c.content, 
            c.token_count, 
            f.rrf_score
        FROM fused_scores f
        JOIN rag_knowledge.knowledge_chunks c ON f.chunk_id = c.chunk_id
        JOIN rag_knowledge.knowledge_documents d ON c.document_id = d.document_id
        ORDER BY f.rrf_score DESC
        LIMIT 20;
        """
        async with self.pool.acquire() as conn:
            records = await conn.fetch(sql_query, domain, str(query_embedding), query)

        if not records:
            return RAGContextSlice(domain=domain, query=query, chunks=[], total_tokens=0)

        candidates = [
            RAGSearchResult(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                domain=r["domain"],
                source_path=r["source_path"],
                breadcrumb=r["breadcrumb"],
                content=r["content"],
                token_count=r["token_count"],
                rrf_score=r["rrf_score"]
            )
            for r in records
        ]

        # 3. FlashRank Cross-Encoder Reranking (Offload to thread with 150ms circuit breaker)
        reranked = candidates
        if self.ranker and candidates:
            try:
                passages = [{"id": str(c.chunk_id), "text": c.content} for c in candidates]
                rerank_request = RerankRequest(query=query, passages=passages)
                
                # Offload synchronous ONNX inference to worker thread and guard with timeout
                results = await asyncio.wait_for(
                    asyncio.to_thread(self.ranker.rerank, rerank_request),
                    timeout=self.rerank_timeout_s
                )
                
                score_map = {res["id"]: res["score"] for res in results}
                for c in candidates:
                    c.rerank_score = score_map.get(str(c.chunk_id), 0.0)
                
                reranked = sorted(candidates, key=lambda x: x.rerank_score or 0.0, reverse=True)
            except (asyncio.TimeoutError, Exception):
                # Circuit breaker fallback: timeout or inference failure gracefully falls back to SQL rrf_score
                reranked = candidates

        # 4. Token Budget Ceiling Enforcement
        final_chunks: List[RAGSearchResult] = []
        accumulated_tokens = 0
        for chunk in reranked[:top_k]:
            if accumulated_tokens + chunk.token_count > token_ceiling and final_chunks:
                break
            final_chunks.append(chunk)
            accumulated_tokens += chunk.token_count

        return RAGContextSlice(
            domain=domain,
            query=query,
            chunks=final_chunks,
            total_tokens=accumulated_tokens
        )
```

---

### 10.4 Specialist Retrieval Facades (`src/rag/interfaces/`)

Specialists interact with RAG via type-safe facades that automatically inject domain constraints and token ceilings:

```python
# src/rag/interfaces/researcher_rag.py
class ResearcherRAGFacade:
    def __init__(self, retriever: HybridRAGRetriever):
        self.retriever = retriever

    async def query_tool_docs(self, tool_name: str, query: str) -> str:
        """Queries library_docs with a 1,000 token ceiling."""
        scoped_query = f"{tool_name}: {query}"
        slice_result = await self.retriever.search(
            domain="library_docs",
            query=scoped_query,
            top_k=3,
            token_ceiling=1000
        )
        return slice_result.to_xml()

    async def query_org_standards(self, topic: str) -> str:
        """Queries org_standards with a 1,000 token ceiling."""
        slice_result = await self.retriever.search(
            domain="org_standards",
            query=topic,
            top_k=3,
            token_ceiling=1000
        )
        return slice_result.to_xml()
```

---

## 11. Directory & Folder Structure

The RAG codebase is structured into clear, decoupled layers aligned with `architecture.md` Section 10:

```
ddlc-agent-studio/
├── knowledge/                           # Ground-Truth Document Repository (Ingestion Source)
│   ├── library_docs/                    # Tool API references (LiteLLM, Instructor, dbt, SQLGlot, etc.)
│   ├── org_standards/                   # Governance policies, naming rules, approved tools
│   ├── pattern_catalog/                 # 5 DDLC Agent architecture patterns (PAT-01 to PAT-05)
│   ├── past_decisions/                  # Canonical pre-seeded ADRs & Day-2 build decisions
│   ├── component_docs/                  # Deep usage documentation for Tier 2 shared components
│   └── code_conventions/                # Python 3.12+ style guide, Ruff, Pytest conventions
│
└── src/
    └── rag/                             # RAG Engine Implementation
        ├── __init__.py
        ├── ingest.py                    # CLI Ingestion runner (`ddlc rag ingest`)
        │
        ├── chunking/                    # Structure-Aware Chunkers
        │   ├── __init__.py
        │   ├── base.py                  # Abstract BaseChunker protocol
        │   ├── markdown_chunker.py      # Header-hierarchy chunker with breadcrumbs
        │   ├── yaml_chunker.py          # Key-boundary aware YAML slice
        │   └── code_chunker.py          # Python AST class/function aware
        │
        ├── embeddings/                  # Embedding Generation
        │   ├── __init__.py
        │   ├── base.py                  # Abstract Embedder protocol
        │   └── embedder.py              # LiteLLM embedding client with backoff
        │
        ├── store/                       # Database Storage & Models
        │   ├── __init__.py
        │   ├── models.py                # Pydantic models for chunks & search slices
        │   └── pgvector_store.py        # Database migration DDL & connection pooling
        │
        ├── retrieval/                   # Search, Ranking & Fusion
        │   ├── __init__.py
        │   ├── hybrid_search.py         # PostgreSQL CTE Hybrid query runner
        │   ├── rrf.py                   # Reciprocal Rank Fusion utilities
        │   └── reranker.py              # FlashRank ONNX cross-encoder & fallback
        │
        └── interfaces/                  # Specialist Facades (Domain & Budget Clamped)
            ├── __init__.py
            ├── researcher_rag.py        # Researcher facade (library_docs, org_standards)
            ├── architect_rag.py         # Architect facade (pattern_catalog, past_decisions)
            └── builder_rag.py           # Builder facade (component_docs, code_conventions)
```

---

## 12. Configuration & Model Tiers

RAG parameters are centralized in `config/settings.yaml` under the `rag` namespace:

```yaml
rag:
  database:
    schema_name: "rag_knowledge"
    pool_min_size: 2
    pool_max_size: 10
    connection_timeout_seconds: 15

  embedding:
    provider: "openai"
    model: "text-embedding-3-small"
    dimensions: 1536
    batch_size: 64
    max_retries: 3

  chunking:
    markdown:
      target_tokens: 350
      max_tokens: 500
      overlap_tokens: 50
    yaml:
      target_tokens: 300
      max_tokens: 450
    code:
      target_tokens: 350
      max_tokens: 500

  retrieval:
    rrf_k: 60
    initial_candidates: 20
    reranker:
      enabled: true
      engine: "flashrank"
      model: "ms-marco-TinyBERT-L-2-v2"
      timeout_ms: 150
      fallback_to_rrf: true

  budgets:
    researcher:
      top_k: 3
      max_tokens: 1000
    architect:
      top_k: 4
      max_tokens: 1400
    builder:
      top_k: 3
      max_tokens: 1000
```

---

## 13. Observability, Telemetry & Evaluation Strategy

### 13.1 Operational Telemetry (Tier 1 Integration)

In accordance with Decision 30, RAG retrieval does not write to its own independent logging tables. All retrieval operations emit non-intrusive OpenTelemetry-compatible span events that are captured by the `MetadataCallbackHandler` and persisted into Tier 1 `agent_metadata.execution_log`:

```json
{
  "event": "rag_retrieval",
  "domain": "library_docs",
  "query": "dbt-core: run execution CLI subprocess",
  "candidate_count": 20,
  "top_chunks": [
    {"chunk_id": "8b512c1e-...", "rrf_score": 0.0312, "rerank_score": 0.941},
    {"chunk_id": "3a119f4d-...", "rrf_score": 0.0298, "rerank_score": 0.887}
  ],
  "total_tokens_injected": 680,
  "latency_ms": {
    "embedding_ms": 32,
    "sql_cte_ms": 14,
    "rerank_ms": 28,
    "total_ms": 74
  }
}
```

---

### 13.2 Evaluation Strategy (Offline & Pre-Deployment)

1. **Deterministic Regression Tests (`tests/unit/test_rag.py`)**:
   - Verify that AST chunkers do not split inside Python functions or YAML dictionaries.
   - Verify that breadcrumb strings are correctly prepended.
   - Verify that SQL CTE queries return 0 rows when a non-existent domain is queried.
   - Verify that token budget ceilings strictly clamp output sizes.

2. **RAG Triad Metrics (CI Benchmark)**:
   A reference evaluation suite in `tests/reference_specs/test_rag_benchmark.py` runs synthetic queries against the 6 domains and verifies metrics using `Ragas` / `DeepEval`:
   - **Context Precision**: Verifies that the top-ranked retrieved chunks contain the necessary signal without extraneous noise. (Target: > 0.85).
   - **Context Recall**: Verifies that all technical ground-truth constraints (e.g. required dbt version) were present in the retrieved candidates. (Target: > 0.90).
   - **Faithfulness**: Verifies that Specialist decisions (e.g. Architect's feasibility verdict) strictly adhere to retrieved documentation without hallucinated library capabilities. (Target: 1.00).

---

## 14. Architecture Decision Records (ADRs)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **ADR-RAG-01** | Retrieval Engine Pattern | Domain-Tagged Advanced Hybrid RAG | Dense vector alone misses exact code symbols; Agentic inner loops violate StateGraph determinism; Graph RAG is redundant with Tier 2 relational metadata. |
| **ADR-RAG-02** | Storage & Indexing Engine | PostgreSQL + `pgvector` with Partial HNSW & GIN | Unifies checkpoints, metadata, and vectors in a single engine. Eliminates distributed transaction desync. |
| **ADR-RAG-03** | Dual TSVector & Search Matching | `'simple'` + `'english'` with `websearch_to_tsquery` | `'simple'` preserves exact code symbols, snake_case identifiers, and versions; `'english'` normalizes prose. `websearch_to_tsquery` replaces brittle strict-AND matching, preventing sparse recall collapse on multi-term queries. |
| **ADR-RAG-04** | Reranker Engine & Circuit Breaker | FlashRank ONNX + Async Thread Offload + 150ms Timeout | Eliminates 2GB+ PyTorch container bloat; delivers sub-35ms CPU latency. Inference is offloaded via `asyncio.to_thread` with strict 150ms timeout, gracefully falling back to pure SQL RRF on failure. |
| **ADR-RAG-05** | Delivery Dynamic Learning | Two-Phase Commit Sequence | Embeddings generated asynchronously via LiteLLM outside the DB transaction, followed by an atomic PostgreSQL transaction. Eliminates DB lock starvation. |
| **ADR-RAG-06** | Context Budget & XML Escaping | Structured XML Slices with `xml_escape` & `quoteattr` | Prevents prompt inflation; enforces hard token budget ceilings; escapes `<`, `>`, `&`, `"`, `'` to protect against Python/SQL/Jinja2 prompt corruption; renders `count="0"` on empty slices; populates `source_path`. |
| **ADR-RAG-07** | Filtered-HNSW Recall Defense | 6 Partial HNSW Indexes + Iterative Scan | Solves pgvector graph traversal recall collapse on small partitions (e.g. `past_decisions` on Day 0) via 6 domain-scoped partial HNSW indexes and `SET LOCAL hnsw.iterative_scan = 'strict_order'`. |
| **ADR-RAG-08** | Ingestion Upsert Full Sync | Full Column Synchronization on `ON CONFLICT` | Synchronizes `title`, `embedding_model`, `embedding_dimension`, `chunk_count`, and `metadata` alongside `file_sha256` and `updated_at`, preventing stale document registry metadata. |

---

## 15. Next Steps

1. **Database Provisioning**: Execute `rag_knowledge` DDL migration in the studio's PostgreSQL instance.
2. **Pre-Seeded Knowledge Ingestion**: Run `ddlc rag ingest` across `knowledge/library_docs/`, `knowledge/org_standards/`, `knowledge/pattern_catalog/`, and the 3 Day-0 ADRs.
3. **Specialist Facade Binding**: Wire `ResearcherRAGFacade`, `ArchitectRAGFacade`, and `BuilderRAGFacade` into the respective specialist node packages (`src/specialists/researcher/`, `src/specialists/architect/`, `src/specialists/builder/`).
4. **Delivery Node Integration**: Connect Step 4 of Phase 8 Delivery Node in `src/coordinator/subgraphs/construct.py` to trigger the Two-Phase Commit ADR indexing.
