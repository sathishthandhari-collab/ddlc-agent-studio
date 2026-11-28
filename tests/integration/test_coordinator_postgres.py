"""Live PostgreSQL integration tests for Step 8 Coordinator & Delivery Node."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

import psycopg
import pytest
import src.coordinator.delivery as delivery_mod
import yaml
from src.coordinator import (
    CoordinatorDeps,
    DeliveryError,
    compile_coordinator_graph,
    node_delivery,
)
from src.core.settings import SafetyValvesConfig, get_env_settings
from src.metadata.catalog import tier2_table
from src.metadata.db import MetadataDatabase, tier1_table
from src.specialists.documentarian.specialist import write_impl_docs_package
from src.state.blackboard import (
    ArchitectureProposal,
    BlackboardState,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    ClarifiedRequirement,
    ImplDocPackage,
    ImplDocRevision,
    ParsedSpecSummary,
    ResearchFindings,
    ValidationReport,
    build_contract_pointer_from_directory,
    initial_blackboard_state,
)
from src.state.checkpointer import (
    delete_thread_checkpoints,
    make_resume_command,
    make_thread_config,
    open_async_checkpointer,
    open_sync_checkpointer,
    run_async_selector,
)

# ---------------------------------------------------------------------------
# PostgreSQL availability & cleanup helpers
# ---------------------------------------------------------------------------


def _live_postgres_uris_or_skip() -> tuple[str, str]:
    """Resolve live Postgres URIs and verify required schemas exist, else skip cleanly."""
    env = get_env_settings()
    meta_uri = getattr(env, "metadata_db_uri", None) or os.environ.get("METADATA_DB_URI")
    ckpt_uri = (
        getattr(env, "checkpointer_db_uri", None)
        or os.environ.get("CHECKPOINTER_DB_URI")
        or meta_uri
    )
    if not meta_uri or not ckpt_uri:
        pytest.skip("PostgreSQL URIs (METADATA_DB_URI / CHECKPOINTER_DB_URI) not configured")

    required_tables = (
        "agent_metadata.agent_run_header",
        "ddlc_shared_metadata.agent_registry",
        "ddlc_shared_metadata.contract_registry",
        "ddlc_shared_metadata.build_summary",
        "ddlc_shared_metadata.stage_gate_record",
        "rag_knowledge.knowledge_documents",
        "rag_knowledge.knowledge_chunks",
    )
    try:
        with psycopg.connect(meta_uri, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                for tbl in required_tables:
                    cur.execute("SELECT to_regclass(%s)", (tbl,))
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        pytest.skip(f"Required PostgreSQL table {tbl} not provisioned")
    except Exception as exc:
        pytest.skip(f"Live PostgreSQL unreachable: {exc}")

    return meta_uri, ckpt_uri


def _cleanup_agent_and_run(
    meta_uri: str,
    ckpt_uri: str,
    *,
    agent_name: str,
    run_id: str,
    thread_id: Optional[str] = None,
) -> None:
    """Clean up all test records across Tier 1, Tier 2, RAG, and checkpointer tables."""
    with psycopg.connect(meta_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT agent_id FROM {tier2_table('agent_registry')} WHERE agent_name = %s",
                (agent_name,),
            )
            agent_ids = [r[0] for r in cur.fetchall()]

            cur.execute(
                """
                DELETE FROM rag_knowledge.knowledge_chunks
                WHERE document_id IN (
                    SELECT document_id FROM rag_knowledge.knowledge_documents
                    WHERE title ILIKE %s OR source_path ILIKE %s
                )
                """,
                (f"%{agent_name}%", f"%{agent_name}%"),
            )
            cur.execute(
                """
                DELETE FROM rag_knowledge.knowledge_documents
                WHERE title ILIKE %s OR source_path ILIKE %s
                """,
                (f"%{agent_name}%", f"%{agent_name}%"),
            )

            cur.execute(
                f"DELETE FROM {tier2_table('stage_gate_record')} WHERE run_id = %s",
                (run_id,),
            )
            cur.execute(
                f"DELETE FROM {tier2_table('build_summary')} WHERE run_id = %s",
                (run_id,),
            )
            for aid in agent_ids:
                cur.execute(
                    f"DELETE FROM {tier2_table('stage_gate_record')} WHERE agent_id = %s",
                    (aid,),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('build_summary')} WHERE agent_id = %s",
                    (aid,),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('component_usage')} WHERE agent_id = %s",
                    (aid,),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('agent_dependency')} WHERE upstream_agent_id = %s OR downstream_agent_id = %s",
                    (aid, aid),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('contract_lineage')} WHERE producer_agent_id = %s OR consumer_agent_id = %s",
                    (aid, aid),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('contract_registry')} WHERE agent_id = %s",
                    (aid,),
                )
                cur.execute(
                    f"DELETE FROM {tier2_table('agent_registry')} WHERE agent_id = %s",
                    (aid,),
                )

            cur.execute(
                f"DELETE FROM {tier1_table('agent_run_header')} WHERE run_id = %s",
                (run_id,),
            )

    if thread_id:
        try:
            delete_thread_checkpoints(ckpt_uri, thread_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Lightweight deterministic test doubles
# ---------------------------------------------------------------------------


def _pg_spec_yaml(agent_name: str) -> str:
    return yaml.safe_dump(
        {
            "agent": {
                "name": agent_name,
                "version": "1.0.0",
                "owner": "pg-integration",
                "description": "Integration test SQL validation agent.",
            },
            "runtime": {"type": "langgraph", "checkpoint": False, "max_retries": 1},
            "stages": [
                {
                    "id": "parse_sql",
                    "name": "Parse SQL AST",
                    "type": "custom",
                    "description": "Parse SQL via sqlglot.",
                    "tool": "sqlglot_verifier",
                }
            ],
            "contracts": {
                "input": {
                    "name": f"{agent_name}_input",
                    "fields": [{"name": "sql_text", "type": "string", "required": True}],
                },
                "output": {
                    "name": f"{agent_name}_output",
                    "fields": [{"name": "is_valid", "type": "boolean", "required": True}],
                },
            },
            "governance": {"pii_handling": "none", "audit_logging": True, "lineage_tracking": True},
        },
        sort_keys=False,
    )


def _pg_impl_doc_pkg(agent_name: str) -> ImplDocPackage:
    return ImplDocPackage(
        agent_yaml={
            "agent": {
                "name": agent_name,
                "display_name": agent_name,
                "version": "1.0.0",
                "description": "PG integration test agent.",
                "owner": "pg-integration",
                "tags": ["pg-it"],
            },
            "architecture": {
                "pattern": "stateful_pipeline_with_gates",
                "runtime": "langgraph",
                "autonomy_level": "autonomous",
            },
            "pipeline": {
                "stages": [{"stage_id": "parse_sql", "next": None, "on_error": None, "transitions": None}]
            },
            "dependencies": {
                "tools": [
                    {
                        "name": "sqlglot_verifier",
                        "version": "1.0.0",
                        "used_by": ["parse_sql"],
                        "description": "SQLGlot AST verifier",
                    }
                ],
                "shared_components": [],
            },
        },
        stages={
            "parse_sql": {
                "stage": {
                    "id": "parse_sql",
                    "name": "Parse SQL",
                    "type": "custom",
                    "description": "Validate SQL syntax.",
                    "autonomy": "autonomous",
                },
                "input": {"contract": f"{agent_name}_input", "source": None},
                "output": {"contract": f"{agent_name}_output", "destination": None},
                "processing": {
                    "processing_type": "deterministic_tool",
                    "llm_config": None,
                    "tools": [
                        {
                            "name": "sqlglot_verifier",
                            "type": "custom_code",
                            "handler": "tools/sqlglot_verifier.py::verify_sql",
                            "signature": "(sql_text: str) -> dict",
                            "purpose": "Validate SQL AST",
                            "access": "read_only",
                            "requires_approval": False,
                            "configuration": {},
                        }
                    ],
                    "safety_valves": {"max_tool_iterations": 2, "max_reflection_retries": 0, "timeout_seconds": 30},
                    "logic_description": "Run sqlglot AST check.",
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
            }
        },
        contracts={
            f"{agent_name}_input": {
                "contract": {
                    "name": f"{agent_name}_input",
                    "version": "1.0.0",
                    "description": "Input SQL",
                    "direction": "input",
                    "storage_mode": "inline",
                    "classification": "internal",
                    "fields": [{"name": "sql_text", "type": "string", "nullable": False, "description": "SQL"}],
                }
            },
            f"{agent_name}_output": {
                "contract": {
                    "name": f"{agent_name}_output",
                    "version": "1.0.0",
                    "description": "Validation verdict",
                    "direction": "output",
                    "storage_mode": "inline",
                    "classification": "internal",
                    "fields": [{"name": "is_valid", "type": "boolean", "nullable": False, "description": "Valid"}],
                }
            },
        },
        config_yaml={"config": {"defaults": {}, "environments": {}, "secrets": []}},
        tests_yaml={
            "tests": {
                "contract_tests": [
                    {
                        "contract": f"{agent_name}_input",
                        "description": "Input schema test",
                        "cases": [
                            {
                                "test_id": "t_in_1",
                                "type": "schema_validation",
                                "description": "Schema check",
                                "auto_generate": True,
                            }
                        ],
                    }
                ],
                "custom_tests": [],
            }
        },
    )


class _PGFakeSpecAnalyst:
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    def analyze(self, projection: Any) -> dict[str, Any]:
        parsed = yaml.safe_load(projection.raw_spec_yaml) if projection.raw_spec_yaml else {}
        summary = ParsedSpecSummary(agent_name=self.agent_name, agent_version="1.0.0")
        rounds = list(getattr(projection, "clarification_rounds", []))
        if any(r.resolved for r in rounds):
            return {
                "current_phase": "understand",
                "spec_validation_status": "valid",
                "has_open_gaps": False,
                "parsed_spec": parsed,
                "parsed_spec_summary": summary,
            }
        return {
            "current_phase": "understand",
            "spec_validation_status": "needs_clarification",
            "has_open_gaps": True,
            "parsed_spec": parsed,
            "parsed_spec_summary": summary,
            "clarification_rounds": [
                ClarificationRound(
                    round_number=1,
                    questions=[
                        ClarificationQuestion(
                            question_id="q_pg_1",
                            category="runtime",
                            field_path="stages[0].dialect",
                            question="Confirm dialect?",
                        )
                    ],
                    answers=[],
                    resolved=False,
                )
            ],
            "clarification_round_count": 1,
        }

    def incorporate_answers(self, projection: Any, answers: list[ClarificationAnswer]) -> dict[str, Any]:
        rounds = list(getattr(projection, "clarification_rounds", []))
        resolved_round = ClarificationRound(
            round_number=1,
            questions=rounds[-1].questions if rounds else [],
            answers=list(answers),
            resolved=True,
        )
        return {
            "current_phase": "understand",
            "spec_validation_status": "valid",
            "has_open_gaps": False,
            "clarification_rounds": [resolved_round],
            "enriched_requirements": [
                ClarifiedRequirement(
                    requirement_id="req_pg_1",
                    source_round=1,
                    category="runtime",
                    summary="Dialect confirmed as postgres.",
                    affected_sections=["stages"],
                )
            ],
        }


class _PGFakeResearcher:
    rag_facade: Any = None
    embedder: Any = None

    def research(self, projection: Any) -> dict[str, Any]:
        return {
            "current_phase": "research",
            "feasibility_verdict": "feasible",
            "research_findings": ResearchFindings(
                verdict="feasible",
                summary="Feasible with deterministic SQLGlot verifier.",
                policy_constraints=["Tier 1 & 2 metadata compliant"],
            ),
        }

    async def aresearch(self, projection: Any) -> dict[str, Any]:
        return self.research(projection)


class _PGFakeArchitect:
    rag_facade: Any = None
    embedder: Any = None

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    def design(self, projection: Any) -> dict[str, Any]:
        return {
            "current_phase": "design",
            "architecture_approved": False,
            "architecture_revision_count": 1,
            "architecture_revisions": [
                ArchitectureProposal(
                    revision_number=1,
                    agent_name=self.agent_name,
                    agent_version="1.0.0",
                    pattern="stateful_pipeline_with_gates",
                    runtime="langgraph",
                    summary="Deterministic state pipeline.",
                    approved=False,
                )
            ],
        }

    async def adesign(self, projection: Any) -> dict[str, Any]:
        return self.design(projection)


class _PGFakeDocumentarian:
    def __init__(self, staging_dir: Path, agent_name: str) -> None:
        self.staging_dir = staging_dir
        self.agent_name = agent_name

    def author_impl_docs(
        self,
        projection: Any,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        target = output_dir or (self.staging_dir / self.agent_name)
        target.mkdir(parents=True, exist_ok=True)
        pkg = _pg_impl_doc_pkg(self.agent_name)
        pointer, _ = write_impl_docs_package(pkg, target)
        pkg = pkg.model_copy(update={"pointer": pointer})
        return {
            "current_phase": "document",
            "impl_docs_pointer": pointer,
            "impl_doc_revisions": [
                ImplDocRevision(revision_number=1, package=pkg, summary="PG integration impl_docs")
            ],
            "impl_doc_revision_count": 1,
            "impl_docs_approved": False,
        }


class _PGFakeBuilder:
    rag_facade: Any = None

    def __init__(self, build_root: Path, agent_name: str) -> None:
        self.build_root = build_root
        self.agent_name = agent_name

    def build(self, projection: Any, *, output_dir: Optional[Path] = None) -> dict[str, Any]:
        target = Path(output_dir) if output_dir else (self.build_root / self.agent_name)
        target.mkdir(parents=True, exist_ok=True)
        (target / "src" / "tools").mkdir(parents=True, exist_ok=True)
        (target / "src" / "tools" / "sqlglot_verifier.py").write_text(
            "def verify_sql(sql_text: str) -> dict:\n    return {'is_valid': True, 'errors': []}\n",
            encoding="utf-8",
        )
        (target / "tests").mkdir(parents=True, exist_ok=True)
        (target / "tests" / "test_basic.py").write_text(
            "def test_ok():\n    assert True\n",
            encoding="utf-8",
        )
        ptr = build_contract_pointer_from_directory(
            contract_name=f"{self.agent_name}_codebase",
            base_dir=target,
        )
        return {
            "current_phase": "construct",
            "generated_code_pointer": ptr,
            "active_fix_instructions": [],
        }

    async def abuild(self, projection: Any, *, output_dir: Optional[Path] = None) -> dict[str, Any]:
        return self.build(projection, output_dir=output_dir)


class _PGFakeValidator:
    def validate(self, projection: Any) -> dict[str, Any]:
        return {
            "validation_passed": True,
            "validation_reports": [ValidationReport(passed=True, attempt_number=1)],
        }


class _PGFakeEmbedder:
    model_name = "text-embedding-3-small"
    dimensions = 1536

    def embed_texts_sync(self, texts: list[str]) -> list[list[float]]:
        return [[0.02] * 1536 for _ in texts]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self.embed_texts_sync(texts)


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


def test_coordinator_postgres_checkpointer_gates_and_atomic_delivery(tmp_path: Path) -> None:
    """Verify real PostgresSaver checkpointing across Gate 1-3 interrupts + atomic Tier 1/2/RAG delivery."""
    meta_uri, ckpt_uri = _live_postgres_uris_or_skip()
    suffix = uuid.uuid4().hex[:8]
    agent_name = f"it_pg_coord_{suffix}"
    run_id = str(uuid.uuid4())
    thread_id = f"it-pg-thread-{suffix}"

    db = MetadataDatabase(db_uri=meta_uri)
    deps = CoordinatorDeps(
        spec_analyst=_PGFakeSpecAnalyst(agent_name),  # type: ignore[arg-type]
        researcher=_PGFakeResearcher(),  # type: ignore[arg-type]
        architect=_PGFakeArchitect(agent_name),  # type: ignore[arg-type]
        documentarian=_PGFakeDocumentarian(tmp_path / "staging_impl", agent_name),  # type: ignore[arg-type]
        builder=_PGFakeBuilder(tmp_path / "staging_build", agent_name),  # type: ignore[arg-type]
        validator=_PGFakeValidator(),  # type: ignore[arg-type]
        safety_valves=SafetyValvesConfig(),
        metadata_db=db,
        embedder=_PGFakeEmbedder(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        staging_root=tmp_path / "staging_impl",
        enable_tier3_migration=False,
        strict_delivery=True,
    )

    try:
        with open_sync_checkpointer(ckpt_uri) as cp:
            graph = compile_coordinator_graph(deps=deps, checkpointer=cp)
            cfg = make_thread_config(thread_id)

            init_state = initial_blackboard_state(
                run_id=run_id,
                thread_id=thread_id,
                raw_spec_yaml=_pg_spec_yaml(agent_name),
            )

            r1 = graph.invoke(init_state, config=cfg)
            assert r1["__interrupt__"][0].value["gate"] == "clarification"

            r2 = graph.invoke(
                make_resume_command({"decision": "approved", "answers": {"q_pg_1": "postgres"}}),
                config=cfg,
            )
            assert r2["__interrupt__"][0].value["gate"] == "architecture_review"

            r3 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg)
            assert r3["__interrupt__"][0].value["gate"] == "impl_doc_review"

            r4 = graph.invoke(make_resume_command({"decision": "approved"}), config=cfg)
            assert "__interrupt__" not in r4
            assert r4["current_phase"] == "completed"
            assert r4["delivery_manifest"] is not None
            assert (tmp_path / "agents" / agent_name / "impl_docs" / "agent.yaml").exists()
            assert (tmp_path / "agents" / agent_name / ".git").exists()

        with psycopg.connect(meta_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT agent_id, agent_name FROM {tier2_table('agent_registry')} WHERE agent_name = %s",
                    (agent_name,),
                )
                agent_row = cur.fetchone()
                assert agent_row is not None and agent_row[1] == agent_name

                cur.execute(
                    f"SELECT outcome FROM {tier2_table('build_summary')} WHERE run_id = %s",
                    (run_id,),
                )
                bs_row = cur.fetchone()
                assert bs_row is not None and bs_row[0] == "success"

                cur.execute(
                    f"SELECT COUNT(*) FROM {tier2_table('stage_gate_record')} WHERE run_id = %s",
                    (run_id,),
                )
                assert cur.fetchone()[0] >= 3

                cur.execute(
                    """
                    SELECT COUNT(*) FROM rag_knowledge.knowledge_documents
                    WHERE title ILIKE %s OR source_path ILIKE %s
                    """,
                    (f"%{agent_name}%", f"%{agent_name}%"),
                )
                assert cur.fetchone()[0] >= 1
    finally:
        _cleanup_agent_and_run(
            meta_uri,
            ckpt_uri,
            agent_name=agent_name,
            run_id=run_id,
            thread_id=thread_id,
        )


def test_coordinator_postgres_delivery_rollback_atomicity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mid-transaction failure rolls back Tier 1, Tier 2, and RAG atomically without touching disk/Git."""
    meta_uri, ckpt_uri = _live_postgres_uris_or_skip()
    suffix = uuid.uuid4().hex[:8]
    agent_name = f"it_pg_rollback_{suffix}"
    run_id = str(uuid.uuid4())

    doc_stub = _PGFakeDocumentarian(tmp_path / "staging_impl", agent_name)
    doc_out = doc_stub.author_impl_docs(type("P", (), {})())
    build_stub = _PGFakeBuilder(tmp_path / "staging_build", agent_name)
    build_out = build_stub.build(type("P", (), {})())
    pkg = doc_out["impl_doc_revisions"][0].package

    state: BlackboardState = {
        **initial_blackboard_state(run_id=run_id, raw_spec_yaml=_pg_spec_yaml(agent_name)),
        "target_agent_name": agent_name,
        "spec_validation_status": "valid",
        "parsed_spec_summary": ParsedSpecSummary(agent_name=agent_name, agent_version="1.0.0"),
        "feasibility_verdict": "feasible",
        "research_findings": ResearchFindings(verdict="feasible", summary="ok"),
        "architecture_approved": True,
        "approved_architecture_version": 1,
        "architecture_revisions": [
            ArchitectureProposal(
                revision_number=1,
                agent_name=agent_name,
                agent_version="1.0.0",
                pattern="stateful_pipeline_with_gates",
                runtime="langgraph",
                summary="ok",
                approved=True,
            )
        ],
        "impl_docs_approved": True,
        "approved_impl_doc_version": 1,
        "impl_docs_pointer": doc_out["impl_docs_pointer"],
        "impl_doc_revisions": [ImplDocRevision(revision_number=1, package=pkg, approved=True)],
        "generated_code_pointer": build_out["generated_code_pointer"],
        "validation_passed": True,
        "validation_reports": [ValidationReport(passed=True, attempt_number=1)],
    }

    def _boom_chunk_vec(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Forced failure after Tier 2 writes inside DB transaction")

    monkeypatch.setattr(delivery_mod, "format_vector_literal", _boom_chunk_vec)

    db = MetadataDatabase(db_uri=meta_uri)
    deps = CoordinatorDeps(
        metadata_db=db,
        embedder=_PGFakeEmbedder(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        agents_output_dir=tmp_path / "agents",
        knowledge_dir=tmp_path / "knowledge",
        enable_tier3_migration=False,
        strict_delivery=True,
    )

    try:
        with pytest.raises(DeliveryError):
            node_delivery(state, deps=deps)

        assert not (tmp_path / "agents" / agent_name).exists()
        with psycopg.connect(meta_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {tier2_table('agent_registry')} WHERE agent_name = %s",
                    (agent_name,),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    f"SELECT COUNT(*) FROM {tier2_table('build_summary')} WHERE run_id = %s",
                    (run_id,),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    """
                    SELECT COUNT(*) FROM rag_knowledge.knowledge_documents
                    WHERE title ILIKE %s OR source_path ILIKE %s
                    """,
                    (f"%{agent_name}%", f"%{agent_name}%"),
                )
                assert cur.fetchone()[0] == 0
    finally:
        _cleanup_agent_and_run(
            meta_uri,
            ckpt_uri,
            agent_name=agent_name,
            run_id=run_id,
        )


def test_coordinator_postgres_async_checkpointer_and_delivery(tmp_path: Path) -> None:
    """Verify async graph.ainvoke + AsyncPostgresSaver + anode_delivery on live Postgres."""
    meta_uri, ckpt_uri = _live_postgres_uris_or_skip()
    suffix = uuid.uuid4().hex[:8]
    agent_name = f"it_pg_async_{suffix}"
    run_id = str(uuid.uuid4())
    thread_id = f"it-pg-async-thread-{suffix}"

    async def _run() -> None:
        db = MetadataDatabase(db_uri=meta_uri)
        deps = CoordinatorDeps(
            spec_analyst=_PGFakeSpecAnalyst(agent_name),  # type: ignore[arg-type]
            researcher=_PGFakeResearcher(),  # type: ignore[arg-type]
            architect=_PGFakeArchitect(agent_name),  # type: ignore[arg-type]
            documentarian=_PGFakeDocumentarian(tmp_path / "staging_impl", agent_name),  # type: ignore[arg-type]
            builder=_PGFakeBuilder(tmp_path / "staging_build", agent_name),  # type: ignore[arg-type]
            validator=_PGFakeValidator(),  # type: ignore[arg-type]
            safety_valves=SafetyValvesConfig(),
            metadata_db=db,
            embedder=_PGFakeEmbedder(),  # type: ignore[arg-type]
            workspace_root=tmp_path,
            agents_output_dir=tmp_path / "agents",
            knowledge_dir=tmp_path / "knowledge",
            staging_root=tmp_path / "staging_impl",
            enable_tier3_migration=False,
            strict_delivery=True,
        )
        async with open_async_checkpointer(ckpt_uri) as cp:
            graph = compile_coordinator_graph(deps=deps, checkpointer=cp)
            cfg = make_thread_config(thread_id)
            init_state = initial_blackboard_state(
                run_id=run_id,
                thread_id=thread_id,
                raw_spec_yaml=_pg_spec_yaml(agent_name),
            )
            r1 = await graph.ainvoke(init_state, config=cfg)
            assert r1["__interrupt__"][0].value["gate"] == "clarification"
            r2 = await graph.ainvoke(
                make_resume_command({"decision": "approved", "answers": {"q_pg_1": "postgres"}}),
                config=cfg,
            )
            assert r2["__interrupt__"][0].value["gate"] == "architecture_review"
            r3 = await graph.ainvoke(make_resume_command({"decision": "approved"}), config=cfg)
            assert r3["__interrupt__"][0].value["gate"] == "impl_doc_review"
            r4 = await graph.ainvoke(make_resume_command({"decision": "approved"}), config=cfg)
            assert r4["current_phase"] == "completed"

    try:
        run_async_selector(_run())
    finally:
        _cleanup_agent_and_run(
            meta_uri,
            ckpt_uri,
            agent_name=agent_name,
            run_id=run_id,
            thread_id=thread_id,
        )
