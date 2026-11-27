"""Unit and live PostgreSQL integration tests for src/metadata/callbacks.py (Tier 1)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from src.core.settings import get_env_settings
from src.metadata.callbacks import MetadataCallbackHandler, extract_llm_telemetry
from src.metadata.db import MetadataDatabase


class _RecordingCursor:
    def __init__(self, row: Optional[dict[str, Any]] = None) -> None:
        self._row = row

    def fetchone(self) -> Optional[dict[str, Any]]:
        return self._row


class _RecordingConnection:
    """Minimal psycopg Connection stub recording SQL statements and params."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.next_fetchone: Optional[dict[str, Any]] = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, sql: str, params: Optional[dict[str, Any]] = None) -> _RecordingCursor:
        self.statements.append((sql, params or {}))
        return _RecordingCursor(self.next_fetchone)


class _FakePool:
    def __init__(self, conn: _RecordingConnection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[_RecordingConnection]:
        yield self._conn


class _FailingPool:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[Any]:
        raise self._exc
        yield  # pragma: no cover


class _SavepointOnlyPool:
    """Wraps an uncommitted Connection so db.transaction() uses SAVEPOINT without committing."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[psycopg.Connection[Any]]:
        yield self._conn


def _open_live_db_or_skip() -> str:
    uri = get_env_settings().metadata_db_uri
    if not uri:
        pytest.skip("METADATA_DB_URI is not configured.")
    try:
        probe = psycopg.connect(uri, connect_timeout=2)
        probe.close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable in current environment: {exc}")
    return uri


class TestLLMTokenExtraction:
    def test_extracts_langchain_usage_metadata_across_generations(self) -> None:
        msg1 = AIMessage(
            content="step 1",
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 45,
                "total_tokens": 165,
                "input_token_details": {"cache_read": 30},
                "output_token_details": {"reasoning": 15},
            },
            response_metadata={"model_name": "anthropic/claude-3-5-sonnet-20241022"},
        )
        msg2 = AIMessage(
            content="step 2",
            usage_metadata={
                "input_tokens": 80,
                "output_tokens": 25,
                "total_tokens": 105,
                "input_token_details": {"cache_read": 10},
                "output_token_details": {"reasoning": 5},
            },
        )
        result = LLMResult(generations=[[ChatGeneration(message=msg1)], [ChatGeneration(message=msg2)]])
        telemetry = extract_llm_telemetry(result)
        assert telemetry["model_provider"] == "anthropic"
        assert telemetry["model_used"] == "claude-3-5-sonnet-20241022"
        assert telemetry["input_token_count"] == 200
        assert telemetry["output_token_count"] == 70
        assert telemetry["cached_token_count"] == 40
        assert telemetry["reasoning_token_count"] == 20

    def test_extracts_openai_and_litellm_llm_output(self) -> None:
        result = LLMResult(
            generations=[[Generation(text="ok")]],
            llm_output={
                "model_name": "openai/o3-mini",
                "token_usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 250,
                    "prompt_tokens_details": {"cached_tokens": 128},
                    "completion_tokens_details": {"reasoning_tokens": 190},
                },
            },
        )
        telemetry = extract_llm_telemetry(result)
        assert telemetry["model_provider"] == "openai"
        assert telemetry["model_used"] == "o3-mini"
        assert telemetry["input_token_count"] == 500
        assert telemetry["output_token_count"] == 250
        assert telemetry["cached_token_count"] == 128
        assert telemetry["reasoning_token_count"] == 190

    def test_extracts_anthropic_usage_dict(self) -> None:
        result = LLMResult(
            generations=[[Generation(text="done")]],
            llm_output={
                "model": "claude-3-7-sonnet-20250219",
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 90,
                    "cache_read_input_tokens": 220,
                },
            },
        )
        telemetry = extract_llm_telemetry(result)
        assert telemetry["model_provider"] == "anthropic"
        assert telemetry["model_used"] == "claude-3-7-sonnet-20250219"
        assert telemetry["input_token_count"] == 300
        assert telemetry["output_token_count"] == 90
        assert telemetry["cached_token_count"] == 220
        assert telemetry["reasoning_token_count"] == 0


class TestMetadataCallbackHandlerUnit:
    def test_span_tree_sequence_and_rollup_sql(self) -> None:
        conn = _RecordingConnection()
        db = MetadataDatabase(pool=_FakePool(conn))
        handler = MetadataCallbackHandler(
            agent_id="meta-agent",
            agent_version="0.1.0",
            initiator_id="architect@example.com",
            initiator_type="human",
            trigger_type="manual",
            db=db,
        )

        run_id = handler.start_run(run_parameters={"api_key": "sk-secretKey1234567890"})
        assert run_id is not None

        chain_lc_id = uuid4()
        llm_lc_id = uuid4()
        tool_lc_id = uuid4()

        handler.on_chain_start(
            {"name": "Coordinator"},
            {"spec": "customer pipeline", "auth": "Bearer myTokenSecret999"},
            run_id=chain_lc_id,
            metadata={"langgraph_node": "architect", "trace_id": "trace-abc"},
        )
        handler.on_chat_model_start(
            {"id": ["langchain", "ChatLiteLLM"], "kwargs": {"model": "anthropic/claude-3-5-sonnet"}},
            [[HumanMessage(content="Design architecture")]],
            run_id=llm_lc_id,
            parent_run_id=chain_lc_id,
        )
        handler.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="Plan ready",
                                usage_metadata={
                                    "input_tokens": 111,
                                    "output_tokens": 22,
                                    "total_tokens": 133,
                                },
                            )
                        )
                    ]
                ]
            ),
            run_id=llm_lc_id,
            parent_run_id=chain_lc_id,
        )
        handler.on_tool_start(
            {"name": "rag_retriever"},
            "query patterns",
            run_id=tool_lc_id,
            parent_run_id=chain_lc_id,
        )
        handler.on_tool_end("3 hits", run_id=tool_lc_id, parent_run_id=chain_lc_id)
        handler.on_chain_end({"selected": "staged_gated_pipeline"}, run_id=chain_lc_id)

        conn.next_fetchone = {
            "run_id": run_id,
            "status": "completed",
            "total_input_tokens": 111,
            "total_output_tokens": 22,
            "total_cost_usd": Decimal("0.002500"),
            "duration_ms": 150,
            "completed_at": datetime.now(timezone.utc),
        }
        summary = handler.complete_run("completed", total_cost_usd=0.0025)
        assert summary is not None
        assert summary["total_input_tokens"] == 111

        # Verify SQL statements recorded: start_run + 3 span starts + 3 span ends + complete_run
        assert len(conn.statements) == 8
        start_sql, start_params = conn.statements[0]
        assert "INSERT INTO" in start_sql and "agent_run_header" in start_sql
        assert "sk-secretKey1234567890" not in str(start_params["run_parameters"].obj)

        chain_start_params = conn.statements[1][1]
        llm_start_params = conn.statements[2][1]
        tool_start_params = conn.statements[4][1]

        # Monotonic event_sequence 1, 2, 3 and valid parent_log_id hierarchy
        assert chain_start_params["event_sequence"] == 1
        assert chain_start_params["parent_log_id"] is None
        assert chain_start_params["step_name"] == "architect"
        assert chain_start_params["step_type"] == "node"
        assert "myTokenSecret999" not in str(chain_start_params["details"].obj)

        assert llm_start_params["event_sequence"] == 2
        assert llm_start_params["parent_log_id"] == chain_start_params["log_id"]
        assert llm_start_params["step_type"] == "llm_call"
        assert llm_start_params["model_provider"] == "anthropic"

        assert tool_start_params["event_sequence"] == 3
        assert tool_start_params["parent_log_id"] == chain_start_params["log_id"]
        assert tool_start_params["step_type"] == "tool_call"

        rollup_sql, rollup_params = conn.statements[7]
        assert "SUM(input_token_count)" in rollup_sql
        assert "SUM(output_token_count)" in rollup_sql
        assert rollup_params["is_terminal"] is True
        assert rollup_params["completed_at"] is not None

    def test_error_sanitizes_and_truncates_trace(self) -> None:
        conn = _RecordingConnection()
        db = MetadataDatabase(pool=_FakePool(conn))
        handler = MetadataCallbackHandler(db=db, run_id=uuid4())

        lc_id = uuid4()
        handler.on_tool_start({"name": "http_client"}, "call", run_id=lc_id)
        try:
            raise RuntimeError("Bad auth Bearer secretJwtToken99999")
        except RuntimeError as exc:
            handler.on_tool_error(exc, run_id=lc_id)

        err_sql, err_params = conn.statements[-1]
        assert "UPDATE" in err_sql
        assert err_params["status"] == "failed"
        assert err_params["error_type"] == "RuntimeError"
        assert "secretJwtToken99999" not in err_params["error_message"]
        assert "secretJwtToken99999" not in err_params["stack_trace"]

    def test_non_blocking_resilience_swallows_db_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = MetadataDatabase(pool=_FailingPool(psycopg.OperationalError("connection refused")))
        handler = MetadataCallbackHandler(db=db, agent_id="meta-agent")

        with caplog.at_level(logging.WARNING):
            run_id = handler.start_run()
            assert run_id is not None

            lc_id = uuid4()
            handler.on_chain_start({"name": "node_a"}, {"x": 1}, run_id=lc_id)
            handler.on_llm_start({"name": "llm"}, ["hi"], run_id=uuid4(), parent_run_id=lc_id)
            handler.on_llm_end(LLMResult(generations=[]), run_id=uuid4())
            handler.on_chain_error(ValueError("boom"), run_id=lc_id)
            handler.on_tool_start({"name": "t"}, "in", run_id=uuid4())
            handler.on_tool_end("out", run_id=uuid4())
            handler.on_tool_error(RuntimeError("tool err"), run_id=uuid4())
            assert handler.record_artifact(
                artifact_name="a.md",
                artifact_kind="document",
                storage_uri="file:///tmp/a.md",
                checksum_sha256="0" * 64,
            ) is None
            assert handler.link_artifact_manifest(artifact_id=uuid4(), role="output") is None
            assert handler.record_decision(
                decision_type="routing",
                decision_status="pending",
            ) is None
            assert handler.record_audit_event(
                action="gate_approval",
                actor_id="user",
                actor_type="human",
                target_entity_type="gate",
            ) is None
            assert handler.record_retry_span(
                retry_of_log_id=uuid4(),
                attempt_number=2,
                step_name="retry_node",
            ) is None
            assert handler.complete_run("completed") is None

        assert len(caplog.records) >= 5

    def test_decision_constraint_validation(self) -> None:
        conn = _RecordingConnection()
        handler = MetadataCallbackHandler(db=MetadataDatabase(pool=_FakePool(conn)), run_id=uuid4())

        # Missing rationale/decided_by violates chk_decision_resolution
        with pytest.raises(ValueError, match="chk_decision_resolution"):
            handler.record_decision(
                decision_type="approval",
                decision_status="decided",
                decision_value="approved",
            )

        # Confidence out of [0.000, 1.000]
        with pytest.raises(ValueError, match="confidence_score"):
            handler.record_decision(
                decision_type="routing",
                decision_status="pending",
                confidence_score=1.5,
            )

        # Pending decision succeeds cleanly without decided_by/rationale
        dec_id = handler.record_decision(
            decision_type="approval",
            decision_status="pending",
            details={"gate": "architecture"},
        )
        assert isinstance(dec_id, UUID)

    def test_disabled_handler_performs_zero_db_calls(self) -> None:
        conn = _RecordingConnection()
        handler = MetadataCallbackHandler(
            db=MetadataDatabase(pool=_FakePool(conn)),
            enabled=False,
        )
        handler.start_run()
        handler.on_chain_start({"name": "n"}, {}, run_id=uuid4())
        handler.on_chain_end({}, run_id=uuid4())
        handler.complete_run("completed")
        assert conn.statements == []


class TestTier1LivePostgresIntegration:
    def test_end_to_end_run_spans_artifacts_decisions_and_rollup(self) -> None:
        uri = _open_live_db_or_skip()
        with MetadataDatabase(db_uri=uri) as db:
            handler = MetadataCallbackHandler(
                db=db,
                agent_id=f"test-meta-{uuid4().hex[:8]}",
                agent_version="0.1.0",
                initiator_id="pytest@ddlc.local",
                initiator_type="system",
                trigger_type="manual",
            )
            run_id = handler.start_run(run_parameters={"api_key": "sk-liveRedactedKey12345678"})
            assert run_id is not None

            chain_lc_id = uuid4()
            llm_lc_id = uuid4()
            tool_lc_id = uuid4()
            artifact_id: Optional[UUID] = None

            try:
                handler.on_chain_start(
                    {"name": "SpecAnalyst"},
                    {"prompt": "build pipeline"},
                    run_id=chain_lc_id,
                    metadata={"langgraph_node": "spec_analyst"},
                )
                handler.on_chat_model_start(
                    {"id": ["langchain", "ChatOpenAI"], "kwargs": {"model": "gpt-4o-mini"}},
                    [[HumanMessage(content="Analyze spec")]],
                    run_id=llm_lc_id,
                    parent_run_id=chain_lc_id,
                )
                handler.on_llm_end(
                    LLMResult(
                        generations=[
                            [
                                ChatGeneration(
                                    message=AIMessage(
                                        content="Analyzed",
                                        usage_metadata={
                                            "input_tokens": 150,
                                            "output_tokens": 60,
                                            "total_tokens": 210,
                                            "input_token_details": {"cache_read": 25},
                                            "output_token_details": {"reasoning": 10},
                                        },
                                    )
                                )
                            ]
                        ]
                    ),
                    run_id=llm_lc_id,
                    parent_run_id=chain_lc_id,
                )
                handler.on_tool_start(
                    {"name": "schema_check"},
                    "check",
                    run_id=tool_lc_id,
                    parent_run_id=chain_lc_id,
                )
                handler.on_tool_error(RuntimeError("transient error"), run_id=tool_lc_id, parent_run_id=chain_lc_id)

                # Record a retry span referencing the failed tool span
                with db.connection() as conn:
                    failed_row = conn.execute(
                        "SELECT log_id FROM agent_metadata.execution_log WHERE run_id = %s AND step_type = 'tool_call'",
                        (run_id,),
                    ).fetchone()
                assert failed_row is not None

                retry_log_id = handler.record_retry_span(
                    retry_of_log_id=failed_row["log_id"],
                    attempt_number=2,
                    step_name="schema_check",
                    step_type="tool_call",
                    status="completed",
                    details={"retried": True},
                )
                assert retry_log_id is not None

                handler.on_chain_end({"status": "ok"}, run_id=chain_lc_id)

                # Register artifact & link BoM manifest
                artifact_id = handler.record_artifact(
                    artifact_name="blueprint.yaml",
                    artifact_kind="config",
                    storage_uri="file:///tmp/blueprint.yaml",
                    checksum_sha256="a" * 64,
                    byte_size=512,
                    mime_type="application/yaml",
                    produced_run_id=run_id,
                    produced_log_id=retry_log_id,
                )
                assert artifact_id is not None
                manifest_id = handler.link_artifact_manifest(
                    artifact_id=artifact_id,
                    role="output",
                    associated_log_id=retry_log_id,
                    associated_phase="design",
                )
                assert manifest_id is not None

                # Pending -> Decided lifecycle on decision_log
                dec_id = handler.record_decision(
                    decision_type="approval",
                    decision_status="pending",
                    log_id=retry_log_id,
                    details={"gate": "architecture"},
                )
                assert dec_id is not None
                resolved_dec_id = handler.record_decision(
                    decision_id=dec_id,
                    decision_type="approval",
                    decision_status="decided",
                    decision_value="approved",
                    decided_by="reviewer@ddlc.local",
                    decided_by_type="human",
                    rationale="Matches Tier 1/2 contracts.",
                    alternatives_considered=["reject", "revise"],
                    confidence_score=1.0,
                    log_id=retry_log_id,
                )
                assert resolved_dec_id == dec_id

                rollup = handler.complete_run("completed", total_cost_usd=0.00125)
                assert rollup is not None
                assert rollup["status"] == "completed"
                assert rollup["total_input_tokens"] == 150
                assert rollup["total_output_tokens"] == 60
                assert rollup["duration_ms"] >= 0
                assert rollup["completed_at"] is not None

                with db.connection() as conn:
                    hdr = conn.execute(
                        "SELECT run_parameters, completed_at, started_at FROM agent_metadata.agent_run_header WHERE run_id = %s",
                        (run_id,),
                    ).fetchone()
                    assert hdr is not None
                    assert hdr["run_parameters"]["api_key"] == "[REDACTED]"
                    assert hdr["completed_at"] >= hdr["started_at"]

                    logs = conn.execute(
                        "SELECT event_sequence, step_type, status, cached_token_count, reasoning_token_count "
                        "FROM agent_metadata.execution_log WHERE run_id = %s ORDER BY event_sequence ASC",
                        (run_id,),
                    ).fetchall()
                    assert [r["event_sequence"] for r in logs] == [1, 2, 3, 4]
                    assert logs[1]["cached_token_count"] == 25
                    assert logs[1]["reasoning_token_count"] == 10
                    assert logs[2]["status"] == "failed"
                    assert logs[3]["status"] == "completed"
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM agent_metadata.artifact_manifest WHERE run_id = %s",
                        (run_id,),
                    )
                    if artifact_id is not None:
                        conn.execute(
                            "DELETE FROM agent_metadata.artifact_registry WHERE artifact_id = %s",
                            (artifact_id,),
                        )
                    conn.execute(
                        "DELETE FROM agent_metadata.agent_run_header WHERE run_id = %s",
                        (run_id,),
                    )

    def test_audit_log_append_and_immutability_inside_rolled_back_tx(self) -> None:
        uri = _open_live_db_or_skip()
        outer_conn = psycopg.connect(uri, autocommit=False, row_factory=dict_row)
        register_vector(outer_conn)
        try:
            # Start outer transaction; nothing will ever be committed.
            outer_conn.execute("SELECT 1")
            db = MetadataDatabase(pool=_SavepointOnlyPool(outer_conn))
            handler = MetadataCallbackHandler(db=db)

            audit_id = handler.record_audit_event(
                run_id=None,
                action="gate_override",
                actor_id="security-reviewer",
                actor_type="human",
                target_entity_type="gate",
                target_entity_id="arch-gate-1",
                outcome="success",
                before_state={"status": "pending", "token": "Bearer secret12345"},
                after_state={"status": "approved"},
                ip_address="127.0.0.1",
                user_agent="pytest-suite",
                details={"reason": "emergency signoff"},
            )
            assert audit_id is not None

            row = outer_conn.execute(
                "SELECT action, before_state, ip_address FROM agent_metadata.audit_log WHERE audit_id = %s",
                (audit_id,),
            ).fetchone()
            assert row is not None
            assert row["action"] == "gate_override"
            assert row["before_state"]["token"] == "[REDACTED]"

            # Verify DB trigger blocks DELETE on audit_log inside a savepoint
            with pytest.raises(psycopg.errors.RaiseException, match="SEC-01"):
                with outer_conn.transaction():
                    outer_conn.execute(
                        "DELETE FROM agent_metadata.audit_log WHERE audit_id = %s",
                        (audit_id,),
                    )
        finally:
            outer_conn.rollback()
            outer_conn.close()


class TestCallbackHardeningEdgeCases:
    def test_concurrent_threads_create_single_run_header_and_unique_sequences(self) -> None:
        import threading

        conn = _RecordingConnection()
        handler = MetadataCallbackHandler(db=MetadataDatabase(pool=_FakePool(conn)))
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                lc_id = uuid4()
                handler.on_chain_start({"name": f"node_{idx}"}, {"step": idx}, run_id=lc_id)
                handler.on_chain_end({"ok": True}, run_id=lc_id)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        run_inserts = [
            s for s in conn.statements if 'INSERT INTO "agent_metadata"."agent_run_header"' in s[0]
        ]
        assert len(run_inserts) == 1
        span_inserts = [
            s for s in conn.statements if 'INSERT INTO "agent_metadata"."execution_log"' in s[0]
        ]
        assert len(span_inserts) == 4
        seqs = sorted(p["event_sequence"] for _, p in span_inserts)
        assert seqs == [1, 2, 3, 4]

    def test_orphaned_end_and_error_spans_use_fallback_insert(self) -> None:
        conn = _RecordingConnection()
        run_id = uuid4()
        handler = MetadataCallbackHandler(
            run_id=run_id,
            db=MetadataDatabase(pool=_FakePool(conn)),
        )
        handler.on_tool_end("tool_out", run_id=uuid4(), name="orphan_tool")
        handler.on_llm_error(RuntimeError("sk-orphanSecret1234567890"), run_id=uuid4())

        inserts = [
            s for s in conn.statements if 'INSERT INTO "agent_metadata"."execution_log"' in s[0]
        ]
        assert len(inserts) == 2
        assert inserts[0][1]["step_name"] == "orphan_tool"
        assert inserts[0][1]["status"] == "completed"
        assert inserts[1][1]["status"] == "failed"
        assert "[REDACTED]" in inserts[1][1]["error_message"]

    def test_non_terminal_complete_run_keeps_completed_at_null(self) -> None:
        conn = _RecordingConnection()
        conn.next_fetchone = {
            "run_id": uuid4(),
            "status": "paused",
            "total_input_tokens": 10,
            "total_output_tokens": 5,
            "duration_ms": None,
            "completed_at": None,
        }
        handler = MetadataCallbackHandler(
            run_id=uuid4(),
            db=MetadataDatabase(pool=_FakePool(conn)),
        )
        res = handler.complete_run("paused")
        assert res is not None
        sql, params = conn.statements[-1]
        assert "ELSE NULL" in sql
        assert params["is_terminal"] is False

    def test_invalid_inputs_and_retry_span_serialization_are_non_blocking(self) -> None:
        conn = _RecordingConnection()
        handler = MetadataCallbackHandler(db=MetadataDatabase(pool=_FakePool(conn)))

        # Invalid initiator_type must NOT poison self.agent_run_id or insert
        assert handler.start_run(initiator_type="alien") is None
        assert handler.agent_run_id is None
        assert handler.start_run(trigger_type="telepathy") is None
        assert handler.agent_run_id is None

        # Non-JSON / bad run_id in record_retry_span must warn and return None without raising
        handler.agent_run_id = uuid4()
        assert (
            handler.record_retry_span(
                retry_of_log_id="not-a-uuid",
                step_name="retry_node",
                step_type="node",
                attempt_number=2,
            )
            is None
        )

    def test_oversized_details_truncated_end_to_end_in_callback(self) -> None:
        conn = _RecordingConnection()
        handler = MetadataCallbackHandler(
            run_id=uuid4(),
            db=MetadataDatabase(pool=_FakePool(conn)),
        )
        big_inputs = {"blob": "x" * 70_000}
        handler.on_chain_start({"name": "big_stage"}, big_inputs, run_id=uuid4())

        _, params = conn.statements[-1]
        details_obj = params["details"].obj
        assert details_obj["_truncated"] is True
        assert "artifact_registry" in details_obj["storage_hint"]
