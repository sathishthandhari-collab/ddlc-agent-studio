"""Comprehensive unit tests for Step 8 Deterministic Coordinator & Delivery Node."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pytest
import yaml
from langgraph.graph import StateGraph
from src.coordinator import (
    CoordinatorDeps,
    DeliveryError,
    anode_delivery,
    build_construct_subgraph,
    build_design_subgraph,
    build_document_subgraph,
    build_research_subgraph,
    build_understand_subgraph,
    compile_coordinator_graph,
    load_impl_docs_package_from_dir,
    node_delivery,
)
from src.coordinator.edges import (
    route_after_architecture_review,
    route_after_build_escalation,
    route_after_clarification_gate,
    route_after_construct_phase,
    route_after_design_phase,
    route_after_document_phase,
    route_after_feasibility_gate,
    route_after_impl_doc_review,
    route_after_research,
    route_after_research_phase,
    route_after_setup,
    route_after_spec_analysis,
    route_after_understand_phase,
    route_after_validation,
)
from src.core.settings import GatesConfig, LoopLimitsConfig, SafetyValvesConfig
from src.specialists.builder.leaf_coder import LeafPromptSynthesis, LeafToolSynthesis
from src.specialists.builder.specialist import BuilderSpecialist
from src.specialists.documentarian.specialist import write_impl_docs_package
from src.specialists.validator.specialist import ValidatorSpecialist
from src.state.blackboard import (
    ArchitectureProposal,
    BlackboardState,
    ChangeRequestContext,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    ClarifiedRequirement,
    ContractPointer,
    ImplDocPackage,
    ImplDocRevision,
    ParsedSpecSummary,
    ResearchFindings,
    StaleStateError,
    ValidationIssue,
    ValidationReport,
    assert_contract_pointer_valid,
    build_contract_pointer_from_directory,
    initial_blackboard_state,
)
from src.state.checkpointer import (
    create_memory_checkpointer,
    make_resume_command,
    make_thread_config,
)

# ---------------------------------------------------------------------------
# Helpers & Test Doubles
# ---------------------------------------------------------------------------


def _valves(
    *,
    max_clarification_rounds: int = 3,
    max_architecture_revisions: int = 3,
    max_impl_doc_revisions: int = 3,
    max_build_fix_attempts: int = 2,
    require_clarification_gate: bool = True,
    require_architecture_approval: bool = True,
    require_impl_doc_approval: bool = True,
    require_destructive_schema_approval: bool = True,
) -> SafetyValvesConfig:
    return SafetyValvesConfig(
        loops=LoopLimitsConfig(
            max_clarification_rounds=max_clarification_rounds,
            max_architecture_revisions=max_architecture_revisions,
            max_impl_doc_revisions=max_impl_doc_revisions,
            max_build_fix_attempts=max_build_fix_attempts,
        ),
        gates=GatesConfig(
            require_clarification_gate=require_clarification_gate,
            require_architecture_approval=require_architecture_approval,
            require_impl_doc_approval=require_impl_doc_approval,
            require_destructive_schema_approval=require_destructive_schema_approval,
        ),
    )


def _canonical_spec_yaml(agent_name: str = "sql_guard_agent") -> str:
    return yaml.safe_dump(
        {
            "agent": {
                "name": agent_name,
                "version": "1.0.0",
                "owner": "data-eng",
                "description": "Validates SQL models via deterministic SQLGlot AST checks.",
            },
            "runtime": {
                "type": "langgraph",
                "checkpoint": False,
                "max_retries": 2,
            },
            "stages": [
                {
                    "id": "parse_sql",
                    "name": "Parse SQL AST",
                    "type": "custom",
                    "description": "Parse incoming SQL with sqlglot and validate dialect rules.",
                    "tool": "sqlglot_verifier",
                }
            ],
            "contracts": {
                "input": {
                    "name": "sql_payload",
                    "fields": [
                        {"name": "sql_text", "type": "string", "required": True},
                        {"name": "dialect", "type": "string", "required": True},
                    ],
                },
                "output": {
                    "name": "validation_verdict",
                    "fields": [
                        {"name": "is_valid", "type": "boolean", "required": True},
                        {"name": "errors", "type": "array", "required": True},
                    ],
                },
            },
            "governance": {
                "pii_handling": "none",
                "audit_logging": True,
                "lineage_tracking": True,
            },
        },
        sort_keys=False,
    )


def _sample_impl_doc_package(
    agent_name: str = "sql_guard_agent",
    *,
    runtime: str = "langgraph",
    version: str = "1.0.0",
    extra_stage: bool = False,
) -> ImplDocPackage:
    pipeline_stages: list[dict[str, Any]] = [
        {
            "stage_id": "parse_sql",
            "next": "format_sql" if extra_stage else None,
            "on_error": None,
            "transitions": None,
        }
    ]
    if extra_stage:
        pipeline_stages.append(
            {
                "stage_id": "format_sql",
                "next": None,
                "on_error": None,
                "transitions": None,
            }
        )

    stages_map: dict[str, dict[str, Any]] = {
        "parse_sql": {
            "stage": {
                "id": "parse_sql",
                "name": "Parse SQL AST",
                "type": "custom",
                "description": "Validate SQL syntax using sqlglot AST parsing.",
                "autonomy": "autonomous",
            },
            "input": {"contract": "sql_payload", "source": None},
            "output": {"contract": "validation_verdict", "destination": None},
            "processing": {
                "processing_type": "deterministic_tool",
                "llm_config": None,
                "tools": [
                    {
                        "name": "sqlglot_verifier",
                        "type": "custom_code",
                        "handler": "tools/sqlglot_verifier.py::verify_sql",
                        "signature": "(sql_text: str, dialect: str = 'postgres') -> dict[str, Any]",
                        "purpose": "Parse SQL syntax via sqlglot AST.",
                        "access": "read_only",
                        "requires_approval": False,
                        "configuration": {},
                    }
                ],
                "safety_valves": {
                    "max_tool_iterations": 3,
                    "max_reflection_retries": 0,
                    "timeout_seconds": 30,
                },
                "logic_description": "Call verify_sql deterministically.",
            },
            "error_strategy": {
                "validation_error": {
                    "action": "fail_fast",
                    "max_retries": 0,
                    "retry_delay_seconds": 0,
                    "escalation": "fail_fast",
                    "log_level": "ERROR",
                }
            },
            "performance": None,
        }
    }
    if extra_stage:
        stages_map["format_sql"] = {
            "stage": {
                "id": "format_sql",
                "name": "Format SQL AST",
                "type": "custom",
                "description": "Normalize SQL output canonically.",
                "autonomy": "autonomous",
            },
            "input": {"contract": "validation_verdict", "source": None},
            "output": {"contract": "validation_verdict", "destination": None},
            "processing": {
                "processing_type": "deterministic_tool",
                "llm_config": None,
                "tools": [
                    {
                        "name": "sqlglot_verifier",
                        "type": "custom_code",
                        "handler": "tools/sqlglot_verifier.py::verify_sql",
                        "signature": "(sql_text: str, dialect: str = 'postgres') -> dict[str, Any]",
                        "purpose": "Normalize SQL syntax.",
                        "access": "read_only",
                        "requires_approval": False,
                        "configuration": {},
                    }
                ],
                "safety_valves": {
                    "max_tool_iterations": 3,
                    "max_reflection_retries": 0,
                    "timeout_seconds": 30,
                },
                "logic_description": "Normalize output payload.",
            },
            "error_strategy": {
                "validation_error": {
                    "action": "fail_fast",
                    "max_retries": 0,
                    "retry_delay_seconds": 0,
                    "escalation": "fail_fast",
                    "log_level": "ERROR",
                }
            },
            "performance": None,
        }

    return ImplDocPackage(
        agent_yaml={
            "agent": {
                "name": agent_name,
                "display_name": "SQL Guard Agent",
                "version": version,
                "description": "Deterministic SQL validation agent.",
                "owner": "data-eng",
                "tags": ["sql", "validation"],
            },
            "architecture": {
                "pattern": "stateful_pipeline_with_gates",
                "runtime": runtime,
                "autonomy_level": "autonomous",
            },
            "pipeline": {"stages": pipeline_stages},
            "dependencies": {
                "tools": [
                    {
                        "name": "sqlglot_verifier",
                        "version": "1.0.0",
                        "used_by": list(stages_map.keys()),
                        "description": "Deterministic SQLGlot AST parser.",
                    }
                ],
                "shared_components": [],
            },
        },
        stages=stages_map,
        contracts={
            "sql_payload": {
                "contract": {
                    "name": "sql_payload",
                    "version": "1.0.0",
                    "description": "Input SQL query and dialect.",
                    "direction": "input",
                    "storage_mode": "inline",
                    "classification": "internal",
                    "fields": [
                        {
                            "name": "sql_text",
                            "type": "string",
                            "nullable": False,
                            "description": "Raw SQL string.",
                        },
                        {
                            "name": "dialect",
                            "type": "string",
                            "nullable": False,
                            "description": "SQL dialect name.",
                        },
                    ],
                }
            },
            "validation_verdict": {
                "contract": {
                    "name": "validation_verdict",
                    "version": "1.0.0",
                    "description": "Validation verdict payload.",
                    "direction": "output",
                    "storage_mode": "inline",
                    "classification": "internal",
                    "fields": [
                        {
                            "name": "is_valid",
                            "type": "boolean",
                            "nullable": False,
                            "description": "Whether SQL parsed cleanly.",
                        },
                        {
                            "name": "errors",
                            "type": "array",
                            "nullable": False,
                            "description": "Validation error list.",
                            "items": {"type": "string"},
                        },
                    ],
                }
            },
        },
        config_yaml={
            "config": {
                "defaults": {"dialect": "postgres"},
                "environments": {"dev": {"dialect": "postgres"}},
                "secrets": [],
            }
        },
        tests_yaml={
            "tests": {
                "contract_tests": [
                    {
                        "contract": "sql_payload",
                        "description": "Schema check for sql_payload",
                        "cases": [
                            {
                                "test_id": "tc_sql_payload_schema",
                                "type": "schema_validation",
                                "description": "Validates sql_payload schema",
                                "auto_generate": True,
                            }
                        ],
                    }
                ],
                "custom_tests": [
                    {
                        "test_id": "test_valid_select",
                        "type": "unit",
                        "description": "Accepts valid SELECT statement",
                        "stage": "parse_sql",
                        "expected_behavior": "Returns is_valid True",
                    }
                ],
            }
        },
    )


def _write_sample_impl_docs(
    target_dir: Path,
    *,
    agent_name: str = "sql_guard_agent",
    version: str = "1.0.0",
    extra_stage: bool = False,
) -> tuple[ImplDocPackage, ContractPointer]:
    pkg = _sample_impl_doc_package(
        agent_name=agent_name,
        version=version,
        extra_stage=extra_stage,
    )
    pointer, _ = write_impl_docs_package(pkg, target_dir)
    pkg = pkg.model_copy(update={"pointer": pointer})
    return pkg, pointer


def _sample_arch(
    agent_name: str = "sql_guard_agent",
    *,
    revision_number: int = 1,
    runtime: str = "langgraph",
    approved: bool = False,
) -> ArchitectureProposal:
    return ArchitectureProposal(
        revision_number=revision_number,
        agent_name=agent_name,
        agent_version="1.0.0",
        pattern="stateful_pipeline_with_gates",
        runtime=runtime,  # type: ignore[arg-type]
        autonomy_level="autonomous",
        summary="Deterministic SQLGlot parser pipeline requires no LLM in workflow control.",
        approved=approved,
    )


def _sample_research(*, verdict: str = "feasible") -> ResearchFindings:
    return ResearchFindings(
        verdict=verdict,  # type: ignore[arg-type]
        summary="SQLGlot supports PostgreSQL AST parsing deterministically.",
        policy_constraints=["Standard Tier 1 audit logging enabled."],
        risks=[] if verdict == "feasible" else ["External connector unavailable"],
    )


def _valid_sqlglot_tool_code() -> str:
    return '''"""Deterministic SQLGlot AST verifier handler."""

from __future__ import annotations

from typing import Any
import sqlglot


def verify_sql(sql_text: str, dialect: str = "postgres") -> dict[str, Any]:
    """Parse SQL payload deterministically and return validation verdict."""
    # PRESERVED-CUSTOM-MARKER
    if not str(sql_text).strip():
        return {"is_valid": False, "errors": ["Empty SQL payload"]}
    try:
        sqlglot.parse_one(sql_text, read=dialect)
        return {"is_valid": True, "errors": []}
    except Exception as exc:
        return {"is_valid": False, "errors": [str(exc)]}
'''


def _valid_sqlglot_unit_test_code() -> str:
    return '''"""Unit test for sqlglot_verifier tool."""

from src.tools.sqlglot_verifier import verify_sql


def test_verify_sql_accepts_select() -> None:
    res = verify_sql("SELECT 1 AS id", dialect="postgres")
    assert res["is_valid"] is True
    assert res["errors"] == []
'''


class _StubLeafCoder:
    """Stub LeafCoderSynthesizer returning deterministic verified tool + test code."""

    def create_tool(self, projection: Any, *, rag_context_xml: str = "") -> LeafToolSynthesis:
        return LeafToolSynthesis(
            tool_code=_valid_sqlglot_tool_code(),
            unit_test_code=_valid_sqlglot_unit_test_code(),
            summary="Deterministic SQLGlot AST verifier tool.",
        )

    def create_prompt(
        self,
        projection: Any,
        *,
        template_ref: str,
        rag_context_xml: str = "",
    ) -> LeafPromptSynthesis:
        return LeafPromptSynthesis(
            prompt_template="Validate input: {{ input_payload }}",
            summary="Prompt template",
        )


class _StubLeafPatcher:
    """Stub LeafCoderPatcher preserving existing custom code during Day-2 updates."""

    def patch_tool(self, projection: Any, *, rag_context_xml: str = "") -> LeafToolSynthesis:
        existing = getattr(projection, "existing_tool_code", None) or _valid_sqlglot_tool_code()
        return LeafToolSynthesis(
            tool_code=existing,
            unit_test_code=_valid_sqlglot_unit_test_code(),
            summary="Patched tool preserving custom comments.",
        )

    def patch_prompt(
        self,
        projection: Any,
        *,
        template_ref: str,
        existing_template: str,
        rag_context_xml: str = "",
    ) -> LeafPromptSynthesis:
        return LeafPromptSynthesis(prompt_template=existing_template, summary="Preserved prompt")


class _FakeSpecAnalyst:
    """Configurable double for SpecAnalystSpecialist."""

    def __init__(
        self,
        *,
        rounds_needed: int = 1,
        reject_spec: bool = False,
        agent_name: str = "sql_guard_agent",
    ) -> None:
        self.rounds_needed = rounds_needed
        self.reject_spec = reject_spec
        self.agent_name = agent_name
        self.analyze_calls = 0
        self.incorporate_calls = 0
        self.change_calls = 0

    def analyze(self, projection: Any) -> dict[str, Any]:
        self.analyze_calls += 1
        if self.reject_spec:
            return {
                "current_phase": "understand",
                "spec_validation_status": "rejected",
                "spec_rejected_errors": ["Missing mandatory governance constraints."],
                "has_open_gaps": False,
            }
        parsed = yaml.safe_load(projection.raw_spec_yaml) if projection.raw_spec_yaml else {}
        summary = ParsedSpecSummary(
            agent_name=self.agent_name,
            agent_version="1.0.0",
            description="Validated SQL guard agent spec.",
            stage_ids=["parse_sql"],
            contract_names=["sql_payload", "validation_verdict"],
        )
        rounds = list(getattr(projection, "clarification_rounds", []))
        current_round = max((r.round_number for r in rounds), default=0) + 1
        if current_round <= self.rounds_needed:
            rnd = ClarificationRound(
                round_number=current_round,
                questions=[
                    ClarificationQuestion(
                        question_id=f"q_{current_round}",
                        category="governance",
                        field_path="governance.retention_days",
                        question=f"Clarify retention window (round {current_round})?",
                    )
                ],
                answers=[],
                resolved=False,
            )
            return {
                "current_phase": "understand",
                "spec_validation_status": "clarify",
                "has_open_gaps": True,
                "clarification_rounds": [rnd],
                "clarification_round_count": current_round,
                "parsed_spec": parsed,
                "parsed_spec_summary": summary,
            }
        return {
            "current_phase": "understand",
            "spec_validation_status": "accepted",
            "has_open_gaps": False,
            "parsed_spec": parsed,
            "parsed_spec_summary": summary,
            "enriched_requirements": [
                ClarifiedRequirement(
                    requirement_id="req_final",
                    source_round=max(1, len(rounds)),
                    category="spec",
                    summary="Validated SQL guard spec cleanly.",
                    affected_sections=["stages", "contracts"],
                )
            ],
        }

    def incorporate_answers(
        self,
        projection: Any,
        answers: list[ClarificationAnswer],
    ) -> dict[str, Any]:
        self.incorporate_calls += 1
        rounds = list(getattr(projection, "clarification_rounds", []))
        last_num = max((r.round_number for r in rounds), default=1)
        resolved_round = ClarificationRound(
            round_number=last_num,
            questions=rounds[-1].questions if rounds else [],
            answers=list(answers),
            resolved=True,
        )
        if last_num < self.rounds_needed:
            return {
                "current_phase": "understand",
                "spec_validation_status": "clarify",
                "has_open_gaps": True,
                "clarification_rounds": [resolved_round],
            }
        return {
            "current_phase": "understand",
            "spec_validation_status": "accepted",
            "has_open_gaps": False,
            "clarification_rounds": [resolved_round],
            "enriched_requirements": [
                ClarifiedRequirement(
                    requirement_id=f"req_{last_num}",
                    source_round=last_num,
                    category="clarification",
                    summary=f"Incorporated {len(answers)} answer(s).",
                    affected_sections=["governance"],
                )
            ],
        }

    def analyze_change_request(self, projection: Any) -> dict[str, Any]:
        self.change_calls += 1
        req = getattr(projection, "change_request", None)
        summary = ParsedSpecSummary(
            agent_name=self.agent_name,
            agent_version="1.0.0",
            description="Change request analysis summary.",
            stage_ids=["parse_sql"],
            contract_names=["sql_payload", "validation_verdict"],
        )
        return {
            "current_phase": "understand",
            "target_agent_name": self.agent_name,
            "spec_validation_status": "accepted",
            "has_open_gaps": False,
            "parsed_spec_summary": summary,
            "change_request": ChangeRequestContext(
                request_text=req.request_text if req else "Add SQL format check",
                agent_id=self.agent_name,
                affected_stages=["parse_sql"],
                affected_contracts=["validation_verdict"],
                changed_impl_files=["stages/stage_parse_sql.yaml"],
            ),
            "enriched_requirements": [
                ClarifiedRequirement(
                    requirement_id="req_chg_1",
                    source_round=1,
                    category="change",
                    summary="Update parse_sql formatting check.",
                    affected_sections=["stages"],
                )
            ],
        }


class _FakeResearcher:
    """Configurable double for ResearcherSpecialist."""

    rag_facade: Any = None
    embedder: Any = None

    def __init__(self, *, verdicts: Optional[list[str]] = None) -> None:
        self.verdicts = list(verdicts or ["feasible"])
        self.calls = 0

    def research(self, projection: Any) -> dict[str, Any]:
        self.calls += 1
        idx = min(self.calls - 1, len(self.verdicts) - 1)
        verdict = self.verdicts[idx]
        return {
            "current_phase": "research",
            "research_findings": _sample_research(verdict=verdict),
            "feasibility_verdict": verdict,
        }

    async def aresearch(self, projection: Any) -> dict[str, Any]:
        return self.research(projection)


class _FakeArchitect:
    """Configurable double for ArchitectSpecialist."""

    rag_facade: Any = None
    embedder: Any = None

    def __init__(self, *, agent_name: str = "sql_guard_agent") -> None:
        self.agent_name = agent_name
        self.calls = 0

    def design(self, projection: Any) -> dict[str, Any]:
        self.calls += 1
        next_rev = int(getattr(projection, "architecture_revision_count", 0)) + 1
        return {
            "current_phase": "design",
            "architecture_revisions": [_sample_arch(self.agent_name, revision_number=next_rev)],
            "architecture_revision_count": next_rev,
            "architecture_approved": False,
        }

    async def adesign(self, projection: Any) -> dict[str, Any]:
        return self.design(projection)


class _FakeDocumentarian:
    """Configurable double for DocumentarianSpecialist writing real impl_docs/ files."""

    def __init__(self, impl_docs_dir: Path, *, agent_name: str = "sql_guard_agent") -> None:
        self.impl_docs_dir = impl_docs_dir
        self.agent_name = agent_name
        self.calls = 0

    def author_impl_docs(
        self,
        projection: Any,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        self.calls += 1
        target = output_dir or (self.impl_docs_dir / self.agent_name)
        next_rev = int(getattr(projection, "impl_doc_revision_count", 0)) + 1
        pkg, ptr = _write_sample_impl_docs(
            target,
            agent_name=self.agent_name,
            version=f"1.0.{next_rev - 1}",
            extra_stage=(next_rev > 1),
        )
        return {
            "current_phase": "document",
            "impl_docs_pointer": ptr,
            "impl_doc_revisions": [
                ImplDocRevision(
                    revision_number=next_rev,
                    package=pkg,
                    summary=f"Revision {next_rev} impl_docs package",
                )
            ],
            "impl_doc_revision_count": next_rev,
            "impl_docs_approved": False,
        }


class _FakeBuilder:
    """Configurable double for BuilderSpecialist emitting runnable agent workspace."""

    rag_facade: Any = None

    def __init__(self, agents_root: Path, *, agent_name: str = "sql_guard_agent") -> None:
        self.agents_root = agents_root
        self.agent_name = agent_name
        self.calls = 0
        self.last_mode = "build"

    def build(
        self,
        projection: Any,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        self.calls += 1
        build_dir = Path(output_dir) if output_dir else (self.agents_root / self.agent_name)
        build_dir.mkdir(parents=True, exist_ok=True)
        src_tools = build_dir / "src" / "tools"
        src_tools.mkdir(parents=True, exist_ok=True)
        handler_path = src_tools / "sqlglot_verifier.py"

        if getattr(projection, "mode", "build") == "change" and handler_path.exists():
            self.last_mode = "patch"
            existing = handler_path.read_text(encoding="utf-8")
            if "# CHANGE-APPLIED" not in existing:
                handler_path.write_text(existing + "\n# CHANGE-APPLIED\n", encoding="utf-8")
        else:
            self.last_mode = "build"
            handler_path.write_text(_valid_sqlglot_tool_code(), encoding="utf-8")

        tests_dir = build_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_agent.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        (build_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{self.agent_name}"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        pointer = build_contract_pointer_from_directory(
            contract_name=f"{self.agent_name}_codebase",
            base_dir=build_dir,
        )
        return {
            "current_phase": "construct",
            "generated_code_pointer": pointer,
            "active_fix_instructions": [],
        }

    async def abuild(
        self,
        projection: Any,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        return self.build(projection, output_dir=output_dir)


class _FakeValidator:
    """Configurable double for ValidatorSpecialist."""

    def __init__(
        self,
        *,
        sequence: Optional[list[tuple[bool, list[ValidationIssue]]]] = None,
    ) -> None:
        self.sequence = list(sequence or [(True, [])])
        self.calls = 0

    def validate(self, projection: Any) -> dict[str, Any]:
        self.calls += 1
        idx = min(self.calls - 1, len(self.sequence) - 1)
        passed, issues = self.sequence[idx]
        report = ValidationReport(
            passed=passed,
            attempt_number=self.calls,
            issues=issues,
            test_summary={"passed": 1 if passed else 0, "failed": 0 if passed else 1},
        )
        return {
            "validation_reports": [report],
            "validation_passed": passed,
        }


class _FakeEmbedder:
    """Deterministic fake embedder supporting both sync and async Delivery paths."""

    model_name = "text-embedding-3-small"
    dimensions = 1536

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def embed_texts_sync(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("LiteLLM embedding upstream unavailable")
        self.calls.append(list(texts))
        return [[0.01] * 1536 for _ in texts]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self.embed_texts_sync(texts)


class _FakeRAGStore:
    """In-memory RAG store double supporting both sync and async upsert paths."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upserted_docs: list[dict[str, Any]] = []

    def upsert_document_and_chunks_sync(self, **kwargs: Any) -> str:
        if self.fail:
            raise RuntimeError("RAG store write failure")
        self.upserted_docs.append(kwargs)
        return str(uuid4())

    async def upsert_document_and_chunks(self, **kwargs: Any) -> str:
        return self.upsert_document_and_chunks_sync(**kwargs)


class _FakeCatalogClient:
    """In-memory CatalogClient double recording Tier 2 registrations."""

    def __init__(self, *, fail_on_register: bool = False) -> None:
        self.fail_on_register = fail_on_register
        self.agents: list[dict[str, Any]] = []
        self.contracts: list[dict[str, Any]] = []
        self.components: list[dict[str, Any]] = []
        self.build_summaries: list[dict[str, Any]] = []

    def get_agent(self, agent_id: str) -> Optional[dict[str, Any]]:
        for a in self.agents:
            if a["agent_id"] == agent_id:
                return a
        return None

    def list_agents(self, *, status: Optional[str] = None) -> list[Any]:
        return []

    def list_contracts(self, *, status: Optional[str] = None) -> list[Any]:
        return []

    def register_agent(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_on_register:
            raise RuntimeError("Simulated Tier 2 DB failure")
        self.agents.append(kwargs)
        return kwargs

    def register_contract(self, **kwargs: Any) -> Any:
        rec = type("ContractRec", (), {"contract_id": uuid4(), **kwargs})()
        self.contracts.append(kwargs)
        return rec

    def register_component(self, **kwargs: Any) -> Any:
        rec = type("ComponentRec", (), {"component_id": uuid4(), **kwargs})()
        self.components.append(kwargs)
        return rec

    def record_build_summary(self, **kwargs: Any) -> dict[str, Any]:
        self.build_summaries.append(kwargs)
        return kwargs


class _FakeLineageClient:
    """In-memory LineageClient double."""

    def __init__(self) -> None:
        self.dependencies: list[dict[str, Any]] = []

    def record_agent_dependency(self, **kwargs: Any) -> dict[str, Any]:
        self.dependencies.append(kwargs)
        return kwargs


# ===========================================================================
# Section A: Deterministic Routing & Safety Valves (`edges.py`)
# ===========================================================================


def test_route_after_setup_and_phase_transitions() -> None:
    base = initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml())
    assert route_after_setup(base) == "understand_phase"

    chg = initial_blackboard_state(raw_spec_yaml="", mode="change")
    assert route_after_setup(chg) == "change_analysis"

    assert route_after_setup({**base, "current_phase": "failed"}) == "abort"
    assert route_after_setup({**base, "current_phase": "aborted"}) == "abort"
    assert route_after_setup({**base, "escalation_required": True}) == "abort"

    assert route_after_understand_phase({**base, "spec_validation_status": "valid"}) == "research_phase"
    assert route_after_understand_phase({**base, "current_phase": "aborted"}) == "abort"
    assert route_after_understand_phase({**base, "spec_validation_status": "rejected"}) == "abort"

    assert route_after_research_phase({**base, "feasibility_verdict": "feasible"}) == "design_phase"
    assert route_after_research_phase({**base, "escalation_required": True}) == "abort"

    assert route_after_design_phase({**base, "architecture_approved": True}) == "document_phase"
    assert route_after_design_phase({**base, "architecture_approved": False}) == "abort"

    assert route_after_document_phase({**base, "impl_docs_approved": True}) == "construct_phase"
    assert route_after_document_phase({**base, "impl_docs_approved": False}) == "abort"

    assert route_after_construct_phase({**base, "validation_passed": True}) == "deliver"
    assert (
        route_after_construct_phase(
            {**base, "validation_passed": False, "current_phase": "document", "impl_docs_approved": False}
        )
        == "document_phase"
    )
    assert route_after_construct_phase({**base, "current_phase": "aborted"}) == "abort"


def test_route_after_spec_analysis_safety_valves_and_gate() -> None:
    valves = _valves(max_clarification_rounds=3, require_clarification_gate=True)
    state: BlackboardState = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "needs_clarification",
        "has_open_gaps": True,
        "clarification_round_count": 2,
    }
    assert route_after_spec_analysis(state, safety_valves=valves) == "gate_clarification"

    assert route_after_spec_analysis({**state, "clarification_round_count": 4}, safety_valves=valves) == "escalate"

    no_gate = _valves(require_clarification_gate=False)
    assert route_after_spec_analysis(state, safety_valves=no_gate) == "done"

    assert route_after_spec_analysis({**state, "spec_validation_status": "rejected"}, safety_valves=valves) == "reject"
    assert route_after_spec_analysis({**state, "has_open_gaps": False}, safety_valves=valves) == "done"

    assert route_after_clarification_gate({**state, "current_phase": "understand"}) == "incorporate_answers"
    assert route_after_clarification_gate({**state, "current_phase": "aborted"}) == "abort"
    assert route_after_clarification_gate({**state, "human_escalated_decision": "abort"}) == "abort"


def test_route_after_research_and_feasibility_gate() -> None:
    base = initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml())
    assert route_after_research({**base, "feasibility_verdict": "feasible"}) == "done"
    assert route_after_research({**base, "feasibility_verdict": "not_feasible"}) == "gate_feasibility"

    assert route_after_feasibility_gate({**base, "human_escalated_decision": "revise"}) == "re_research"
    assert route_after_feasibility_gate({**base, "human_escalated_decision": "approved"}) == "proceed"
    assert route_after_feasibility_gate({**base, "human_escalated_decision": "abort"}) == "abort"
    assert route_after_feasibility_gate({**base, "current_phase": "aborted"}) == "abort"


def test_route_after_architecture_and_impl_doc_review_safety_valves() -> None:
    valves = _valves(max_architecture_revisions=2, max_impl_doc_revisions=2)
    base = initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml())

    assert (
        route_after_architecture_review(
            {**base, "architecture_decision": "approved", "architecture_approved": True},
            safety_valves=valves,
        )
        == "approved"
    )
    assert route_after_architecture_review({**base, "architecture_decision": "abort"}, safety_valves=valves) == "abort"
    assert (
        route_after_architecture_review(
            {**base, "architecture_decision": "revise", "architecture_revision_count": 2},
            safety_valves=valves,
        )
        == "revise"
    )
    assert (
        route_after_architecture_review(
            {**base, "architecture_decision": "revise", "architecture_revision_count": 3},
            safety_valves=valves,
        )
        == "escalate"
    )

    assert (
        route_after_impl_doc_review(
            {**base, "impl_doc_decision": "approved", "impl_docs_approved": True},
            safety_valves=valves,
        )
        == "approved"
    )
    assert route_after_impl_doc_review({**base, "impl_doc_decision": "abort"}, safety_valves=valves) == "abort"
    assert (
        route_after_impl_doc_review(
            {**base, "impl_doc_decision": "revise", "impl_doc_revision_count": 2},
            safety_valves=valves,
        )
        == "revise"
    )
    assert (
        route_after_impl_doc_review(
            {**base, "impl_doc_decision": "revise", "impl_doc_revision_count": 3},
            safety_valves=valves,
        )
        == "escalate"
    )


def test_route_after_validation_and_build_escalation() -> None:
    valves = _valves(max_build_fix_attempts=2)
    base = initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml())

    assert route_after_validation({**base, "validation_passed": True}, safety_valves=valves) == "passed"

    builder_issue = ValidationIssue(
        check_name="ruff_syntax",
        severity="error",
        file_path="src/tools/sqlglot_verifier.py",
        route_target="leaf_coder",
        message="Syntax error",
    )
    assert (
        route_after_validation(
            {
                **base,
                "validation_passed": False,
                "build_fix_attempt_count": 1,
                "validation_reports": [ValidationReport(passed=False, attempt_number=1, issues=[builder_issue])],
            },
            safety_valves=valves,
        )
        == "fix_builder"
    )

    doc_issue = ValidationIssue(
        check_name="contract_consistency",
        severity="error",
        file_path="impl_docs/contracts/sql_payload.yaml",
        route_target="documentarian",
        message="Ambiguous contract field",
    )
    assert (
        route_after_validation(
            {
                **base,
                "validation_passed": False,
                "build_fix_attempt_count": 0,
                "validation_reports": [ValidationReport(passed=False, attempt_number=1, issues=[doc_issue])],
            },
            safety_valves=valves,
        )
        == "fix_documentarian"
    )

    assert (
        route_after_validation(
            {
                **base,
                "validation_passed": False,
                "build_fix_attempt_count": 2,
                "validation_reports": [ValidationReport(passed=False, attempt_number=3, issues=[builder_issue])],
            },
            safety_valves=valves,
        )
        == "escalate"
    )

    assert route_after_build_escalation({**base, "human_escalated_decision": "approved"}) == "revalidate"
    assert route_after_build_escalation({**base, "human_escalated_decision": "revise"}) == "rebuild"
    assert route_after_build_escalation({**base, "human_escalated_decision": "abort"}) == "abort"


# ===========================================================================
# Section B: Phase Subgraphs in Isolation
# ===========================================================================


def _wrap_subgraph(node_name: str, sg: Any) -> Any:
    parent = StateGraph(BlackboardState)
    parent.add_node(node_name, sg)
    parent.set_entry_point(node_name)
    parent.set_finish_point(node_name)
    return parent.compile(checkpointer=create_memory_checkpointer())


def test_understand_subgraph_multi_round_and_abort(tmp_path: Path) -> None:
    spec_analyst = _FakeSpecAnalyst(rounds_needed=2)
    deps = CoordinatorDeps(
        spec_analyst=spec_analyst,  # type: ignore[arg-type]
        safety_valves=_valves(max_clarification_rounds=5),
        workspace_root=tmp_path,
    )
    compiled = _wrap_subgraph("u", build_understand_subgraph(deps))

    cfg = make_thread_config("u-multi-1")
    out1 = compiled.invoke(initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()), config=cfg)
    assert "__interrupt__" in out1
    assert out1["__interrupt__"][0].value["gate"] == "clarification"

    out2 = compiled.invoke(
        make_resume_command({"decision": "approved", "answers": {"q_1": "90"}}),
        config=cfg,
    )
    assert "__interrupt__" in out2

    out3 = compiled.invoke(
        make_resume_command({"decision": "approved", "answers": {"q_2": "postgres"}}),
        config=cfg,
    )
    assert "__interrupt__" not in out3
    assert out3["spec_validation_status"] == "accepted"
    assert len(out3["enriched_requirements"]) >= 1

    cfg_abort = make_thread_config("u-abort-1")
    compiled.invoke(initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()), config=cfg_abort)
    out_abort = compiled.invoke(make_resume_command({"decision": "abort"}), config=cfg_abort)
    assert out_abort["current_phase"] == "aborted"

    rej_deps = CoordinatorDeps(
        spec_analyst=_FakeSpecAnalyst(reject_spec=True),  # type: ignore[arg-type]
        safety_valves=_valves(),
        workspace_root=tmp_path,
    )
    rej_compiled = _wrap_subgraph("u", build_understand_subgraph(rej_deps))
    out_rej = rej_compiled.invoke(
        initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        config=make_thread_config("u-rej-1"),
    )
    assert "__interrupt__" not in out_rej
    assert out_rej["spec_validation_status"] == "rejected"


def test_research_subgraph_feasible_and_escalation_paths(tmp_path: Path) -> None:
    base_state = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "valid",
        "parsed_spec": yaml.safe_load(_canonical_spec_yaml()),
        "parsed_spec_summary": ParsedSpecSummary(agent_name="sql_guard_agent", agent_version="1.0.0"),
        "enriched_requirements": [
            ClarifiedRequirement(requirement_id="req_1", source_round=1, summary="Validate SQL AST")
        ],
    }

    g_ok = _wrap_subgraph(
        "r",
        build_research_subgraph(
            CoordinatorDeps(
                researcher=_FakeResearcher(verdicts=["feasible"]),  # type: ignore[arg-type]
                safety_valves=_valves(),
                workspace_root=tmp_path,
            )
        ),
    )
    res_ok = g_ok.invoke(base_state, config=make_thread_config("r-ok"))
    assert "__interrupt__" not in res_ok
    assert res_ok["feasibility_verdict"] == "feasible"

    g_rev = _wrap_subgraph(
        "r",
        build_research_subgraph(
            CoordinatorDeps(
                researcher=_FakeResearcher(verdicts=["not_feasible", "feasible"]),  # type: ignore[arg-type]
                safety_valves=_valves(),
                workspace_root=tmp_path,
            )
        ),
    )
    cfg = make_thread_config("r-rev")
    out1 = g_rev.invoke(base_state, config=cfg)
    assert out1["__interrupt__"][0].value["gate"] == "feasibility_escalation"
    out2 = g_rev.invoke(
        make_resume_command({"decision": "revise", "guidance": "Use sqlglot fallback parser"}),
        config=cfg,
    )
    assert "__interrupt__" not in out2
    assert out2["feasibility_verdict"] == "feasible"

    g_ab = _wrap_subgraph(
        "r",
        build_research_subgraph(
            CoordinatorDeps(
                researcher=_FakeResearcher(verdicts=["not_feasible"]),  # type: ignore[arg-type]
                safety_valves=_valves(),
                workspace_root=tmp_path,
            )
        ),
    )
    cfg_ab = make_thread_config("r-abort")
    g_ab.invoke(base_state, config=cfg_ab)
    out_ab = g_ab.invoke(make_resume_command({"decision": "abort"}), config=cfg_ab)
    assert out_ab["current_phase"] == "aborted"


def test_design_subgraph_approve_revise_and_safety_valve(tmp_path: Path) -> None:
    deps = CoordinatorDeps(
        architect=_FakeArchitect(),  # type: ignore[arg-type]
        safety_valves=_valves(max_architecture_revisions=2),
        workspace_root=tmp_path,
    )
    g = _wrap_subgraph("d", build_design_subgraph(deps))

    base = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "valid",
        "parsed_spec_summary": ParsedSpecSummary(agent_name="sql_guard_agent", agent_version="1.0.0"),
        "feasibility_verdict": "feasible",
        "research_findings": _sample_research(),
    }

    cfg = make_thread_config("d-rev-app")
    r1 = g.invoke(base, config=cfg)
    assert r1["__interrupt__"][0].value["gate"] == "architecture_review"
    r2 = g.invoke(make_resume_command({"decision": "revise", "feedback": "Add ADR citation"}), config=cfg)
    assert r2["__interrupt__"][0].value["gate"] == "architecture_review"
    r3 = g.invoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert "__interrupt__" not in r3
    assert r3["architecture_approved"] is True
    assert len(r3["architecture_revisions"]) == 2

    cfg_sv = make_thread_config("d-sv")
    g.invoke(base, config=cfg_sv)
    g.invoke(make_resume_command({"decision": "revise", "feedback": "revise 1"}), config=cfg_sv)
    r_lim = g.invoke(make_resume_command({"decision": "revise", "feedback": "revise 2"}), config=cfg_sv)
    assert r_lim["__interrupt__"][0].value["gate"] == "architecture_review"
    r_esc = g.invoke(make_resume_command({"decision": "revise", "feedback": "revise 3"}), config=cfg_sv)
    assert r_esc["architecture_approved"] is False
    assert r_esc["escalation_required"] is True
    assert r_esc["current_phase"] == "failed"


def test_document_subgraph_gate_and_claim_check_tamper(tmp_path: Path) -> None:
    deps = CoordinatorDeps(
        documentarian=_FakeDocumentarian(tmp_path / "staging_impl_docs"),  # type: ignore[arg-type]
        safety_valves=_valves(max_impl_doc_revisions=3),
        workspace_root=tmp_path,
        staging_root=tmp_path / "staging_impl_docs",
    )
    g = _wrap_subgraph("doc", build_document_subgraph(deps))

    base = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "valid",
        "parsed_spec_summary": ParsedSpecSummary(agent_name="sql_guard_agent", agent_version="1.0.0"),
        "architecture_approved": True,
        "approved_architecture_version": 1,
        "architecture_revisions": [_sample_arch(approved=True)],
    }

    cfg = make_thread_config("doc-1")
    d1 = g.invoke(base, config=cfg)
    assert d1["__interrupt__"][0].value["gate"] == "impl_doc_review"
    d2 = g.invoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert "__interrupt__" not in d2
    assert d2["impl_docs_approved"] is True
    pointer = d2["impl_docs_pointer"]
    assert pointer is not None
    assert pointer.frozen is True
    assert_contract_pointer_valid(pointer)

    manifest_file = Path(pointer.storage_uri) / "agent.yaml"
    manifest_file.write_text(manifest_file.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(StaleStateError):
        assert_contract_pointer_valid(pointer)


def test_construct_subgraph_fix_loop_escalation_and_doc_reroute(tmp_path: Path) -> None:
    impl_dir = tmp_path / "impl_docs_src"
    pkg, ptr = _write_sample_impl_docs(impl_dir)
    base_state = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "valid",
        "target_agent_name": "sql_guard_agent",
        "architecture_approved": True,
        "approved_architecture_version": 1,
        "architecture_revisions": [_sample_arch(approved=True)],
        "impl_docs_approved": True,
        "approved_impl_doc_version": 1,
        "impl_docs_pointer": ptr,
        "impl_doc_revisions": [ImplDocRevision(revision_number=1, package=pkg, approved=True)],
    }

    fail_issue = ValidationIssue(
        check_name="ruff_syntax",
        severity="error",
        file_path="src/tools/sqlglot_verifier.py",
        route_target="leaf_coder",
        message="Syntax error",
    )

    b_fix = _FakeBuilder(tmp_path / "agents")
    deps_fix = CoordinatorDeps(
        builder=b_fix,  # type: ignore[arg-type]
        validator=_FakeValidator(sequence=[(False, [fail_issue]), (True, [])]),  # type: ignore[arg-type]
        safety_valves=_valves(max_build_fix_attempts=2),
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
    )
    res_fix = _wrap_subgraph("c", build_construct_subgraph(deps_fix)).invoke(
        base_state, config=make_thread_config("c-fix")
    )
    assert res_fix["validation_passed"] is True
    assert b_fix.calls == 2

    deps_esc = CoordinatorDeps(
        builder=_FakeBuilder(tmp_path / "agents_esc"),  # type: ignore[arg-type]
        validator=_FakeValidator(
            sequence=[(False, [fail_issue]), (False, [fail_issue]), (True, [])]
        ),  # type: ignore[arg-type]
        safety_valves=_valves(max_build_fix_attempts=1),
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents_esc",
    )
    g_esc = _wrap_subgraph("c", build_construct_subgraph(deps_esc))
    cfg_esc = make_thread_config("c-esc")
    r_esc1 = g_esc.invoke(base_state, config=cfg_esc)
    assert r_esc1["__interrupt__"][0].value["gate"] == "build_escalation"
    r_esc2 = g_esc.invoke(make_resume_command({"decision": "approved"}), config=cfg_esc)
    assert r_esc2["validation_passed"] is True

    doc_issue = ValidationIssue(
        check_name="contract_consistency",
        severity="error",
        file_path="impl_docs/contracts/sql_payload.yaml",
        route_target="documentarian",
        message="Ambiguous contract field",
    )
    deps_doc = CoordinatorDeps(
        builder=_FakeBuilder(tmp_path / "agents_doc"),  # type: ignore[arg-type]
        validator=_FakeValidator(sequence=[(False, [doc_issue])]),  # type: ignore[arg-type]
        safety_valves=_valves(max_build_fix_attempts=2),
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents_doc",
    )
    r_doc = _wrap_subgraph("c", build_construct_subgraph(deps_doc)).invoke(
        base_state, config=make_thread_config("c-doc")
    )
    assert r_doc["current_phase"] == "document"
    assert r_doc["impl_docs_approved"] is False
    assert r_doc["validation_passed"] is False


# ===========================================================================
# Section C: Phase 8 Delivery Node (`delivery.py`)
# ===========================================================================


def _ready_delivery_state(tmp_path: Path, *, agent_name: str = "sql_guard_agent") -> BlackboardState:
    impl_dir = tmp_path / "staging_impl_docs" / agent_name
    pkg, impl_ptr = _write_sample_impl_docs(impl_dir, agent_name=agent_name)
    build_dir = tmp_path / "build_staging" / agent_name
    builder = _FakeBuilder(tmp_path / "build_staging", agent_name=agent_name)
    built = builder.build(type("P", (), {"mode": "build"})(), output_dir=build_dir)

    return {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml(agent_name)),
        "target_agent_name": agent_name,
        "spec_validation_status": "valid",
        "parsed_spec_summary": ParsedSpecSummary(agent_name=agent_name, agent_version="1.0.0"),
        "feasibility_verdict": "feasible",
        "research_findings": _sample_research(),
        "architecture_approved": True,
        "approved_architecture_version": 1,
        "architecture_revisions": [_sample_arch(agent_name, approved=True)],
        "impl_docs_approved": True,
        "approved_impl_doc_version": 1,
        "impl_docs_pointer": impl_ptr,
        "impl_doc_revisions": [ImplDocRevision(revision_number=1, package=pkg, approved=True)],
        "generated_code_pointer": built["generated_code_pointer"],
        "validation_passed": True,
        "validation_reports": [ValidationReport(passed=True, attempt_number=1)],
    }


def test_delivery_precondition_failures(tmp_path: Path) -> None:
    state = _ready_delivery_state(tmp_path)
    deps = CoordinatorDeps(workspace_root=tmp_path)

    with pytest.raises(DeliveryError):
        node_delivery({**state, "impl_docs_approved": False}, deps=deps)

    with pytest.raises(DeliveryError):
        node_delivery({**state, "validation_passed": False}, deps=deps)

    with pytest.raises(DeliveryError):
        node_delivery({**state, "generated_code_pointer": None}, deps=deps)


def test_delivery_embed_failure_blocks_before_db_or_git(tmp_path: Path) -> None:
    state = _ready_delivery_state(tmp_path)
    catalog = _FakeCatalogClient()
    rag = _FakeRAGStore()
    deps = CoordinatorDeps(
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=_FakeLineageClient(),  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(fail=True),  # type: ignore[arg-type]
        strict_delivery=True,
    )
    with pytest.raises(DeliveryError, match="embeddings|Delivery"):
        node_delivery(state, deps=deps)
    assert catalog.agents == []
    assert rag.upserted_docs == []
    assert not (tmp_path / "agents" / "sql_guard_agent").exists()


def test_delivery_db_failure_rolls_back_before_git_or_files(tmp_path: Path) -> None:
    state = _ready_delivery_state(tmp_path)
    catalog = _FakeCatalogClient(fail_on_register=True)
    rag = _FakeRAGStore()
    deps = CoordinatorDeps(
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=_FakeLineageClient(),  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(fail=False),  # type: ignore[arg-type]
        strict_delivery=True,
    )
    with pytest.raises(DeliveryError):
        node_delivery(state, deps=deps)
    assert not (tmp_path / "agents" / "sql_guard_agent").exists()
    assert not any((tmp_path / "knowledge").rglob("*.md"))


def test_delivery_two_phase_commit_with_mocks_and_real_git(tmp_path: Path) -> None:
    state = _ready_delivery_state(tmp_path, agent_name="sql_guard_agent")
    catalog = _FakeCatalogClient()
    lineage = _FakeLineageClient()
    rag = _FakeRAGStore()
    embedder = _FakeEmbedder()

    agents_out = tmp_path / "agents"
    knowledge_dir = tmp_path / "knowledge"
    deps = CoordinatorDeps(
        workspace_root=tmp_path,
        agents_output_dir=agents_out,
        knowledge_dir=knowledge_dir,
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=lineage,  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        enable_tier3_migration=False,
        strict_delivery=True,
    )
    out = node_delivery(state, deps=deps)
    assert out["current_phase"] == "completed"
    manifest = out["delivery_manifest"]
    assert manifest is not None
    assert manifest.agent_id == "sql_guard_agent"
    assert manifest.git_commit_sha and len(manifest.git_commit_sha) >= 7
    assert manifest.git_branch and manifest.git_branch.startswith("feat/sql_guard_agent")

    final_agent_dir = agents_out / "sql_guard_agent"
    assert (final_agent_dir / "impl_docs" / "agent.yaml").exists()
    assert (final_agent_dir / "src" / "tools" / "sqlglot_verifier.py").exists()
    assert (final_agent_dir / ".git").exists()

    assert_contract_pointer_valid(manifest.code_pointer)

    adr_files = list((knowledge_dir / "past_decisions").glob("ADR-*.md"))
    assert len(adr_files) == 1
    assert len(embedder.calls) >= 1
    assert len(catalog.agents) == 1
    assert len(catalog.contracts) >= 2
    assert len(catalog.build_summaries) == 1
    assert len(rag.upserted_docs) >= 1

    loaded = load_impl_docs_package_from_dir(final_agent_dir / "impl_docs")
    assert loaded.agent_yaml["agent"]["name"] == "sql_guard_agent"


@pytest.mark.asyncio
async def test_async_delivery_node(tmp_path: Path) -> None:
    state = _ready_delivery_state(tmp_path, agent_name="async_guard_agent")
    deps = CoordinatorDeps(
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        catalog_client=_FakeCatalogClient(),  # type: ignore[arg-type]
        lineage_client=_FakeLineageClient(),  # type: ignore[arg-type]
        rag_store=_FakeRAGStore(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        enable_tier3_migration=False,
        strict_delivery=True,
    )
    out = await anode_delivery(state, deps=deps)
    assert out["current_phase"] == "completed"
    assert out["delivery_manifest"].agent_id == "async_guard_agent"


# ===========================================================================
# Section D: Full E2E Coordinator Flows (Day-0 Build & Day-2 Change)
# ===========================================================================


def _build_e2e_deps(
    tmp_path: Path,
    *,
    agent_name: str = "sql_guard_agent",
    rounds_needed: int = 1,
    valves: Optional[SafetyValvesConfig] = None,
) -> tuple[CoordinatorDeps, _FakeCatalogClient, _FakeRAGStore, _FakeBuilder]:
    agents_out = tmp_path / "agents"
    staging_impl = tmp_path / "staging_impl_docs"
    knowledge_dir = tmp_path / "knowledge"
    catalog = _FakeCatalogClient()
    lineage = _FakeLineageClient()
    rag = _FakeRAGStore()
    embedder = _FakeEmbedder()
    builder = _FakeBuilder(tmp_path / "build_staging", agent_name=agent_name)

    deps = CoordinatorDeps(
        spec_analyst=_FakeSpecAnalyst(rounds_needed=rounds_needed, agent_name=agent_name),  # type: ignore[arg-type]
        researcher=_FakeResearcher(verdicts=["feasible"]),  # type: ignore[arg-type]
        architect=_FakeArchitect(agent_name=agent_name),  # type: ignore[arg-type]
        documentarian=_FakeDocumentarian(staging_impl, agent_name=agent_name),  # type: ignore[arg-type]
        builder=builder,  # type: ignore[arg-type]
        validator=_FakeValidator(sequence=[(True, [])]),  # type: ignore[arg-type]
        safety_valves=valves or _valves(),
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=lineage,  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        agents_output_dir=agents_out,
        knowledge_dir=knowledge_dir,
        staging_root=staging_impl,
        enable_tier3_migration=False,
        strict_delivery=True,
    )
    return deps, catalog, rag, builder


def test_coordinator_full_day0_e2e_sync_with_real_builder_and_validator(tmp_path: Path) -> None:
    """Full Day-0 flow exercising real BuilderSpecialist, LeafCoder stub, and ValidatorSpecialist(run_tests=True)."""
    agent_name = "sql_guard_agent"
    staging_impl = tmp_path / "staging_impl_docs"
    knowledge_dir = tmp_path / "knowledge"
    agents_out = tmp_path / "agents"
    catalog = _FakeCatalogClient()
    lineage = _FakeLineageClient()
    rag = _FakeRAGStore()
    embedder = _FakeEmbedder()

    real_builder = BuilderSpecialist(
        synthesizer=_StubLeafCoder(),  # type: ignore[arg-type]
        patcher=_StubLeafPatcher(),  # type: ignore[arg-type]
    )
    real_validator = ValidatorSpecialist(run_tests=True)

    deps = CoordinatorDeps(
        spec_analyst=_FakeSpecAnalyst(rounds_needed=1, agent_name=agent_name),  # type: ignore[arg-type]
        researcher=_FakeResearcher(verdicts=["feasible"]),  # type: ignore[arg-type]
        architect=_FakeArchitect(agent_name=agent_name),  # type: ignore[arg-type]
        documentarian=_FakeDocumentarian(staging_impl, agent_name=agent_name),  # type: ignore[arg-type]
        builder=real_builder,
        validator=real_validator,
        safety_valves=_valves(),
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=lineage,  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        agents_output_dir=agents_out,
        knowledge_dir=knowledge_dir,
        staging_root=staging_impl,
        enable_tier3_migration=False,
        strict_delivery=True,
    )
    graph = compile_coordinator_graph(deps=deps, checkpointer=create_memory_checkpointer())
    cfg = make_thread_config("e2e-day0-sync")

    r1 = graph.invoke(
        initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml(agent_name)),
        config=cfg,
    )
    assert r1["__interrupt__"][0].value["gate"] == "clarification"

    r2 = graph.invoke(
        make_resume_command({"decision": "approved", "answers": {"q_1": "90"}}),
        config=cfg,
    )
    assert r2["__interrupt__"][0].value["gate"] == "architecture_review"

    r3 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert r3["__interrupt__"][0].value["gate"] == "impl_doc_review"

    r4 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert "__interrupt__" not in r4
    assert r4["current_phase"] == "completed"
    assert r4["validation_passed"] is True
    assert r4["delivery_manifest"] is not None
    assert (agents_out / agent_name / "impl_docs" / "agent.yaml").exists()
    assert len(catalog.agents) == 1
    assert len(rag.upserted_docs) >= 1


@pytest.mark.asyncio
async def test_coordinator_full_day0_e2e_async(tmp_path: Path) -> None:
    deps, catalog, rag, _ = _build_e2e_deps(tmp_path, agent_name="async_e2e_agent")
    graph = compile_coordinator_graph(deps=deps, checkpointer=create_memory_checkpointer())
    cfg = make_thread_config("e2e-day0-async")

    r1 = await graph.ainvoke(
        initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml("async_e2e_agent")),
        config=cfg,
    )
    assert r1["__interrupt__"][0].value["gate"] == "clarification"

    r2 = await graph.ainvoke(
        make_resume_command({"decision": "approved", "answers": {"q_1": "90"}}),
        config=cfg,
    )
    assert r2["__interrupt__"][0].value["gate"] == "architecture_review"

    r3 = await graph.ainvoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert r3["__interrupt__"][0].value["gate"] == "impl_doc_review"

    r4 = await graph.ainvoke(make_resume_command({"decision": "approved"}), config=cfg)
    assert r4["current_phase"] == "completed"
    assert len(catalog.agents) == 1
    assert len(rag.upserted_docs) >= 1


def test_coordinator_full_day2_change_flow_preserves_custom_code_and_commits_new_git_rev(
    tmp_path: Path,
) -> None:
    """Day-2 flow loads impl_docs/ from agents/, routes via change_analysis -> patch -> second Git commit."""
    agent_name = "sql_guard_agent"
    deps, _, _, fake_builder = _build_e2e_deps(tmp_path, agent_name=agent_name, rounds_needed=0)
    graph = compile_coordinator_graph(deps=deps, checkpointer=create_memory_checkpointer())

    cfg0 = make_thread_config("day0-seed")
    r1 = graph.invoke(
        initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml(agent_name)),
        config=cfg0,
    )
    assert r1["__interrupt__"][0].value["gate"] == "architecture_review"
    r2 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg0)
    assert r2["__interrupt__"][0].value["gate"] == "impl_doc_review"
    r3 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg0)
    assert r3["current_phase"] == "completed"
    first_sha = r3["delivery_manifest"].git_commit_sha

    agent_dir = tmp_path / "agents" / agent_name
    handler_file = agent_dir / "src" / "tools" / "sqlglot_verifier.py"
    handler_file.write_text(
        handler_file.read_text(encoding="utf-8") + "\n# HOTFIX-2AM: keep timeout guard\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(agent_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(agent_dir), "commit", "-m", "2am hotfix"],
        check=True,
        capture_output=True,
    )

    cfg2 = make_thread_config("day2-change")
    change_init = initial_blackboard_state(
        raw_spec_yaml="",
        mode="change",
        target_agent_name=agent_name,
        change_request=ChangeRequestContext(
            request_text="Add SQL formatting step to validation output.",
            agent_id=agent_name,
        ),
    )
    c1 = graph.invoke(change_init, config=cfg2)
    assert "__interrupt__" in c1
    assert c1["__interrupt__"][0].value["gate"] == "impl_doc_review"

    c2 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg2)
    assert "__interrupt__" not in c2
    assert c2["current_phase"] == "completed"
    second_sha = c2["delivery_manifest"].git_commit_sha
    assert second_sha != first_sha

    delivered_handler = (agent_dir / "src" / "tools" / "sqlglot_verifier.py").read_text(
        encoding="utf-8"
    )
    assert "# HOTFIX-2AM: keep timeout guard" in delivered_handler
    assert fake_builder.last_mode == "patch"


# ===========================================================================
# Section E: Regression Tests for Verification Findings & Coverage Gaps
# ===========================================================================


def test_manual_gate2_and_gate3_escalate_preserves_reason(tmp_path: Path) -> None:
    """Manual Gate 2/3 'escalate' keeps the user feedback instead of safety-valve boilerplate."""
    g_design = _wrap_subgraph(
        "d",
        build_design_subgraph(
            CoordinatorDeps(
                architect=_FakeArchitect(),  # type: ignore[arg-type]
                safety_valves=_valves(max_architecture_revisions=5),
                workspace_root=tmp_path,
            )
        ),
    )
    design_state = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "accepted",
        "parsed_spec_summary": ParsedSpecSummary(agent_name="sql_guard_agent", agent_version="1.0.0"),
        "feasibility_verdict": "feasible",
        "research_findings": _sample_research(),
    }
    cfg_d = make_thread_config("manual-esc-d")
    g_design.invoke(design_state, config=cfg_d)
    d_out = g_design.invoke(
        make_resume_command({"decision": "escalate", "feedback": "Need security team review."}),
        config=cfg_d,
    )
    assert d_out["escalation_required"] is True
    assert d_out["escalation_reason"] == "Need security team review."

    g_doc = _wrap_subgraph(
        "doc",
        build_document_subgraph(
            CoordinatorDeps(
                documentarian=_FakeDocumentarian(tmp_path / "staging_impl_docs"),  # type: ignore[arg-type]
                safety_valves=_valves(max_impl_doc_revisions=5),
                workspace_root=tmp_path,
                staging_root=tmp_path / "staging_impl_docs",
            )
        ),
    )
    doc_state = {
        **design_state,
        "architecture_approved": True,
        "approved_architecture_version": 1,
        "architecture_revisions": [_sample_arch(approved=True)],
    }
    cfg_doc = make_thread_config("manual-esc-doc")
    g_doc.invoke(doc_state, config=cfg_doc)
    doc_out = g_doc.invoke(
        make_resume_command({"decision": "escalate", "feedback": "Escalate contract ownership."}),
        config=cfg_doc,
    )
    assert doc_out["escalation_required"] is True
    assert doc_out["escalation_reason"] == "Escalate contract ownership."


def test_feasibility_gate_revise_records_guidance_requirement(tmp_path: Path) -> None:
    """Gate 'feasibility_escalation' captures both 'guidance' and 'feedback' into enriched_requirements."""
    base_state = {
        **initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml()),
        "spec_validation_status": "accepted",
        "parsed_spec": yaml.safe_load(_canonical_spec_yaml()),
        "parsed_spec_summary": ParsedSpecSummary(agent_name="sql_guard_agent", agent_version="1.0.0"),
    }
    g_rev = _wrap_subgraph(
        "r",
        build_research_subgraph(
            CoordinatorDeps(
                researcher=_FakeResearcher(verdicts=["not_feasible", "feasible"]),  # type: ignore[arg-type]
                safety_valves=_valves(),
                workspace_root=tmp_path,
            )
        ),
    )
    cfg = make_thread_config("r-guidance")
    g_rev.invoke(base_state, config=cfg)
    out = g_rev.invoke(
        make_resume_command({"decision": "revise", "guidance": "Use sqlglot fallback parser"}),
        config=cfg,
    )
    assert out["feasibility_verdict"] == "feasible"
    summaries = [req.summary for req in out["enriched_requirements"]]
    assert "Use sqlglot fallback parser" in summaries


def test_top_level_construct_documentarian_reroute_bounded_and_halt(tmp_path: Path) -> None:
    """Persistent documentarian validation failures increment build_fix_attempt_count and trip Gate 4."""
    agent_name = "reroute_guard_agent"
    doc_issue = ValidationIssue(
        check_name="contract_consistency",
        severity="error",
        file_path="impl_docs/contracts/sql_payload.yaml",
        route_target="documentarian",
        message="Persistent impl_docs mismatch",
    )
    valves = _valves(
        max_build_fix_attempts=2,
        require_clarification_gate=False,
        require_architecture_approval=False,
        require_impl_doc_approval=False,
    )
    deps = CoordinatorDeps(
        spec_analyst=_FakeSpecAnalyst(rounds_needed=0, agent_name=agent_name),  # type: ignore[arg-type]
        researcher=_FakeResearcher(verdicts=["feasible"]),  # type: ignore[arg-type]
        architect=_FakeArchitect(agent_name=agent_name),  # type: ignore[arg-type]
        documentarian=_FakeDocumentarian(tmp_path / "staging_impl_docs", agent_name=agent_name),  # type: ignore[arg-type]
        builder=_FakeBuilder(tmp_path / "build_staging", agent_name=agent_name),  # type: ignore[arg-type]
        validator=_FakeValidator(sequence=[(False, [doc_issue])]),  # type: ignore[arg-type]
        safety_valves=valves,
        catalog_client=_FakeCatalogClient(),  # type: ignore[arg-type]
        lineage_client=_FakeLineageClient(),  # type: ignore[arg-type]
        rag_store=_FakeRAGStore(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        staging_root=tmp_path / "staging_impl_docs",
        enable_tier3_migration=False,
    )
    graph = compile_coordinator_graph(deps=deps, checkpointer=create_memory_checkpointer())
    cfg = make_thread_config("reroute-bounded")

    # Loops construct -> document twice, then trips Gate 4 escalation on 3rd failure instead of infinite recursion
    r1 = graph.invoke(initial_blackboard_state(raw_spec_yaml=_canonical_spec_yaml(agent_name)), config=cfg)
    assert "__interrupt__" in r1
    assert r1["__interrupt__"][0].value["gate"] == "build_escalation"
    assert r1["build_fix_attempt_count"] == 2

    # Abort at Gate 4 -> top-level halt finalizes cleanly
    r2 = graph.invoke(make_resume_command({"decision": "abort", "feedback": "Stop reroute"}), config=cfg)
    assert "__interrupt__" not in r2
    assert r2["current_phase"] == "aborted"
    assert r2["last_error"] is not None


def test_delivery_tier3_migration_and_new_component_embedding(tmp_path: Path) -> None:
    """Phase 8 invokes Tier3SchemaClient when tier3_contract_yaml is set and embeds new components."""
    state = _ready_delivery_state(tmp_path, agent_name="tier3_agent")
    latest_pkg = state["impl_doc_revisions"][0].package.model_copy(
        update={
            "tier3_contract_yaml": {
                "schema": "ddlc_tier3_agent_metadata",
                "tables": [{"name": "audit_events", "columns": [{"name": "id", "type": "uuid"}]}],
            }
        }
    )
    arch = state["architecture_revisions"][0].model_copy(
        update={
            "new_components_to_create": [
                {
                    "component_name": "sql_ast_normalizer",
                    "component_version": "1.0.0",
                    "description": "Canonical SQL AST normalizer helper",
                    "component_type": "utility",
                }
            ]
        }
    )
    enriched_state: BlackboardState = {
        **state,
        "architecture_revisions": [arch],
        "impl_doc_revisions": [ImplDocRevision(revision_number=1, package=latest_pkg, approved=True)],
    }

    applied_calls: list[dict[str, Any]] = []

    class _FakeTier3Client:
        def apply_migration(self, contract_path: Path, **kwargs: Any) -> None:
            applied_calls.append({"path": contract_path, **kwargs})

    catalog = _FakeCatalogClient()
    rag = _FakeRAGStore()
    embedder = _FakeEmbedder()
    deps = CoordinatorDeps(
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        catalog_client=catalog,  # type: ignore[arg-type]
        lineage_client=_FakeLineageClient(),  # type: ignore[arg-type]
        rag_store=rag,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        tier3_client=_FakeTier3Client(),  # type: ignore[arg-type]
        enable_tier3_migration=True,
        strict_delivery=True,
    )
    out = node_delivery(enriched_state, deps=deps)
    assert out["current_phase"] == "completed"
    assert len(applied_calls) == 1
    assert applied_calls[0]["path"].exists()
    assert len(catalog.components) == 1
    assert catalog.components[0]["component_name"] == "sql_ast_normalizer"
    assert len(catalog.components[0]["embedding"]) == 1536

