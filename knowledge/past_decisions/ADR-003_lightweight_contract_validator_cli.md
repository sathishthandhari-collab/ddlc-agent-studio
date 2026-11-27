# ADR-003: Lightweight Contract Validator CLI Agent (`contract_validator_cli_agent`)

- **ADR ID**: `ADR-003`
- **Status**: Approved (Canonical Reference ADR for `python_litellm`)
- **Pattern**: Lightweight Event / CLI Runner (`lightweight_event_cli_agent`, single-pass variant of `PAT-01` two-pass design)
- **Runtime**: `python_litellm` (linear sequential runner + Typer CLI, zero checkpointer)
- **Primary Consumers**: Architect Specialist, Builder Specialist

---

## Context

Developers and CI/CD pipelines need a fast, stateless CLI and webhook validator (`contract_validator_cli_agent`) that inspects proposed edits to YAML data contracts (`contracts/*.yaml` or `impl_docs/contracts/*.yaml`) during Pull Requests and local pre-commit checks.

The agent must:

1. Parse YAML contract definitions deterministically and verify structural schema rules, naming conventions (`snake_case` fields, semantic versions), and classification tag completeness (`public | internal | confidential | restricted`).
2. Run a focused, single-turn semantic quality check on plain-English `description` fields, `quality_rules` clarity, and potential backward-compatibility risks against a baseline contract snapshot.
3. Return machine-readable lint diagnostics (JSON / SARIF summary and non-zero CI exit codes on `error` severity) in **under 5 seconds** without provisioning PostgreSQL checkpoint tables or pausing for interactive human gates.

---

## Decision

Select the **`lightweight_event_cli_agent`** pattern compiled via the **`python_litellm`** runtime generation strategy (`architecture.pattern: lightweight_event_cli_agent`, `architecture.runtime: python_litellm`, `architecture.autonomy_level: autonomous`):

1. **Linear Sequential Runner (`src/pipeline.py` + `src/main.py`)**: Compile a deterministic `LiteLLMAgentRunner` invoked by a Typer CLI (`src/main.py`), eliminating LangGraph `StateGraph` compilation and `PostgresSaver` checkpoint persistence.
2. **Two-Pass Deterministic-First Linting**: Execute pure Python Pydantic v2 + regex + diff checks first (`parse_and_rule_lint`), resolving ~90% of schema, enum, and breaking-change violations at zero token cost in `< 20 ms`.
3. **Single-Turn Instructor Semantic Review**: Invoke `instructor.from_litellm(litellm.completion)` on `model_tier: fast` (`temperature: 0.0`, wrapped with `@tenacity.retry` for transient API errors) once per contract to evaluate semantic clarity and ambiguous rule phrasing, outputting a validated `ContractLintReport`.
4. **Stateless Execution & Optional Telemetry**: Rely on CI stdout/JSON exit codes as the primary delivery mechanism; avoid mandatory database locks or `CHECKPOINTER_DB_URI` requirements so local developer pre-commit runs succeed completely offline/zero-infra for deterministic checks.

---

## Runtime & Contracts

### Stage Pipeline (`python_litellm` Strategy)

| Stage ID | Type | Processing Type | Autonomy | Mechanism |
|---|---|---|---|---|
| `parse_contract` | `discovery` | `deterministic_tool` | `autonomous` | PyYAML `safe_load` + Pydantic validation + deterministic breaking-change diff |
| `semantic_lint` | `reasoning` | `llm_reasoning` | `autonomous` | `litellm.completion` + Instructor (`model_tier: fast`, `temperature: 0.0`) |
| `emit_report` | `delivery` | `deterministic_tool` | `autonomous` | Merges rule + semantic findings, writes JSON/SARIF report, sets CLI exit code |

### Contracts (Inline Storage Mode)

All contracts use `storage_mode: inline` since YAML contract files and lint diagnostics are small (`< 50 KB`) and never persisted across graph checkpoints:

1. `contract_lint_request` (`input`): `contract_path`, `baseline_contract_path` (optional for PR diff), `strict_mode` (`bool`).
2. `parsed_contract_ast` (`internal`): `contract_name`, `contract_version`, `classification`, `rule_violations: List[LintFinding]`, `schema_diff_summary`.
3. `contract_lint_report` (`output`): `contract_name`, `passed` (`bool`), `error_count`, `warning_count`, `findings: List[LintFinding]`, `execution_ms`.

### Error Strategy & Resilience

- `on_yaml_parse_error`: `fail_fast` (exits immediately with syntax line/column diagnostics; never sends malformed YAML to the LLM).
- `on_llm_transient_error`: `retry` via `@tenacity.retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))` on `RateLimitError` / `Timeout`; optional `--deterministic-only` flag degrades gracefully to deterministic rules when offline.

---

## Trade-offs & Rejected Alternatives

### Rejected: `langgraph` Runtime with `PostgresSaver`
- **Why Rejected**: Setting up `PostgresSaver` checkpoint connections (`CHECKPOINTER_DB_URI`) for a 2-second stateless pre-commit/CI linter adds unnecessary infrastructure coupling, connection pool latency, and operational complexity when there are no cyclic loops, Claim Check artifact bundles, or `interrupt()` human gates.
- **Accepted Trade-off**: Cannot pause mid-run for interactive human edits or resume after process termination. Re-running the CLI from scratch takes `< 5` seconds and is completely idempotent.

### Rejected: Evaluator-Optimizer Refinement Loop (`PAT-03`)
- **Why Rejected**: `contract_validator_cli_agent` diagnoses defects for a human author or CI system; it does not auto-rewrite contracts in a multi-round generator-critic loop. Iterating would triple latency and cost for zero value.

### Rejected: Pure LLM Validation Without Deterministic First Pass
- **Why Rejected**: Asking an LLM to verify `snake_case` regexes, required YAML keys, or column type compatibility is slow, costly, and prone to false positives (`PAT-01` §12.4 anti-pattern: *"LLM used for work a rule could do"*).

---

## Human Gate Rationale

### Why Runtime Human Gates Are Intentionally Omitted
- **Placement Compliance (`PAT-01` §9.1)**: This agent performs strictly `read_only` inspection and emits advisory lint findings without modifying Git branches, opening Pull Requests, applying DDL migrations (`dcm apply`), or writing to production systems.
- **Accountability Preservation**: Adding an interactive `interrupt()` gate to a CI linter would break non-interactive automation while adding zero governance value. Instead, human accountability resides naturally in the Git Pull Request approval workflow where CI surfaces `contract_lint_report` findings before merge.
