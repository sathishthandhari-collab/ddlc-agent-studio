"""Tier 1 passive operational telemetry and LangChain/LangGraph callback handler."""

from __future__ import annotations

import ipaddress
import itertools
import logging
import threading
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from src.metadata.db import (
    MetadataDatabase,
    get_telemetry_config,
    guard_inline_jsonb,
    sanitize_text,
    tier1_table,
    to_jsonb,
    truncate_stack_trace,
)

logger = logging.getLogger(__name__)


def _warn(msg: str, *args: Any) -> None:
    """Emit telemetry warning even if Alembic's fileConfig disabled existing loggers."""
    if logger.disabled:
        logger.disabled = False
    logger.warning(msg, *args)

_UNSET: Any = object()

INITIATOR_TYPES = frozenset({"human", "agent", "system", "schedule"})
TRIGGER_TYPES = frozenset({"manual", "api", "schedule", "event", "parent_run"})
RUN_STATUSES = frozenset({"running", "completed", "failed", "cancelled", "paused", "timed_out"})
RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
STEP_TYPES = frozenset({"node", "tool_call", "llm_call", "gate", "system"})
STEP_STATUSES = frozenset({"started", "completed", "failed", "skipped", "timed_out"})
STEP_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped", "timed_out"})
MANIFEST_ROLES = frozenset({"input", "output", "intermediate"})
AUDIT_ACTOR_TYPES = frozenset({"human", "agent", "service_account", "system"})
AUDIT_OUTCOMES = frozenset({"success", "denied", "failed", "error"})
DECISION_STATUSES = frozenset({"pending", "decided", "rejected", "cancelled"})
DECISION_ACTOR_TYPES = frozenset({"human", "agent", "rule_engine"})


def _truncate_str(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= max_len else text[:max_len]


def _coerce_uuid(value: Optional[UUID | str]) -> Optional[UUID]:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _infer_provider_and_model(
    raw_model: Optional[str],
    explicit_provider: Optional[str] = None,
    serialized: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Normalize provider and model names across LiteLLM/OpenAI/Anthropic/Google."""
    provider = explicit_provider.strip().lower() if explicit_provider else None
    model = raw_model.strip() if raw_model else None

    if model and "/" in model:
        prefix, _, remainder = model.partition("/")
        if prefix and remainder:
            provider = provider or prefix.lower()
            model = remainder

    if provider is None and model:
        lower = model.lower()
        if "claude" in lower:
            provider = "anthropic"
        elif lower.startswith(("gpt-", "o1", "o3", "o4", "text-embedding", "chatgpt-")):
            provider = "openai"
        elif "gemini" in lower:
            provider = "google"
        elif "ollama" in lower:
            provider = "ollama"

    if provider is None and isinstance(serialized, dict):
        cls_id = serialized.get("id")
        if isinstance(cls_id, list) and cls_id:
            cls_name = str(cls_id[-1]).lower()
            if "anthropic" in cls_name:
                provider = "anthropic"
            elif "openai" in cls_name:
                provider = "openai"
            elif "google" in cls_name or "gemini" in cls_name:
                provider = "google"
            elif "ollama" in cls_name:
                provider = "ollama"
            elif "litellm" in cls_name:
                provider = "litellm"

    return _truncate_str(provider, 50), _truncate_str(model, 100)


def _as_non_negative_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _extract_tokens_from_dict(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    """Extract (input, output, cached, reasoning) counts from provider usage dicts."""
    in_tok = _as_non_negative_int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("prompt_token_count")
    )
    out_tok = _as_non_negative_int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("candidates_token_count")
    )

    in_details = usage.get("input_token_details") or usage.get("prompt_tokens_details") or {}
    out_details = usage.get("output_token_details") or usage.get("completion_tokens_details") or {}

    cached_tok = _as_non_negative_int(
        (in_details.get("cache_read") if isinstance(in_details, dict) else None)
        or (in_details.get("cached_tokens") if isinstance(in_details, dict) else None)
        or usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or usage.get("cached_content_token_count")
    )
    reasoning_tok = _as_non_negative_int(
        (out_details.get("reasoning") if isinstance(out_details, dict) else None)
        or (out_details.get("reasoning_tokens") if isinstance(out_details, dict) else None)
        or usage.get("reasoning_tokens")
        or usage.get("thoughts_token_count")
    )
    return in_tok, out_tok, cached_tok, reasoning_tok


def extract_llm_telemetry(
    response: Any,
    *,
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None,
) -> dict[str, Any]:
    """Extract normalized provider, model, and token counts from LLMResult or dict payloads."""
    llm_output = getattr(response, "llm_output", None)
    if llm_output is None and isinstance(response, dict):
        llm_output = response.get("llm_output") or response

    generations = getattr(response, "generations", None)
    if generations is None and isinstance(response, dict):
        generations = response.get("generations")

    raw_model: Optional[str] = None
    explicit_provider: Optional[str] = None

    if isinstance(llm_output, dict):
        raw_model = llm_output.get("model_name") or llm_output.get("model")
        explicit_provider = llm_output.get("model_provider") or llm_output.get("provider")

    msg_in = msg_out = msg_cached = msg_reasoning = 0
    found_message_usage = False
    resp_meta_usage: Optional[dict[str, Any]] = None

    if isinstance(generations, list):
        for gen_group in generations:
            if not isinstance(gen_group, list):
                continue
            for gen in gen_group:
                message = getattr(gen, "message", None)
                if message is None and isinstance(gen, dict):
                    message = gen.get("message")

                resp_meta = getattr(message, "response_metadata", None) if message is not None else None
                if resp_meta is None and isinstance(message, dict):
                    resp_meta = message.get("response_metadata")
                if isinstance(resp_meta, dict):
                    raw_model = raw_model or resp_meta.get("model_name") or resp_meta.get("model") or resp_meta.get("model_id")
                    explicit_provider = explicit_provider or resp_meta.get("model_provider") or resp_meta.get("provider")
                    if resp_meta_usage is None:
                        candidate = resp_meta.get("token_usage") or resp_meta.get("usage") or resp_meta.get("usage_metadata")
                        if isinstance(candidate, dict):
                            resp_meta_usage = candidate

                usage_meta = getattr(message, "usage_metadata", None) if message is not None else None
                if usage_meta is None and isinstance(message, dict):
                    usage_meta = message.get("usage_metadata")
                if isinstance(usage_meta, dict) and usage_meta:
                    found_message_usage = True
                    i, o, c, r = _extract_tokens_from_dict(usage_meta)
                    msg_in += i
                    msg_out += o
                    msg_cached += c
                    msg_reasoning += r

    if not found_message_usage:
        dict_usage: Optional[dict[str, Any]] = None
        if isinstance(llm_output, dict):
            candidate = llm_output.get("token_usage") or llm_output.get("usage") or llm_output.get("usage_metadata")
            if isinstance(candidate, dict):
                dict_usage = candidate
        if dict_usage is None:
            dict_usage = resp_meta_usage
        if isinstance(dict_usage, dict):
            msg_in, msg_out, msg_cached, msg_reasoning = _extract_tokens_from_dict(dict_usage)

    provider, model = _infer_provider_and_model(
        raw_model or fallback_model,
        explicit_provider or fallback_provider,
    )
    return {
        "model_provider": provider,
        "model_used": model,
        "input_token_count": msg_in,
        "output_token_count": msg_out,
        "cached_token_count": msg_cached,
        "reasoning_token_count": msg_reasoning,
    }


def _extract_step_name(
    serialized: Optional[dict[str, Any]],
    metadata: Optional[dict[str, Any]],
    kwargs: dict[str, Any],
    default: str,
) -> str:
    candidates: list[Any] = [kwargs.get("name")]
    if isinstance(metadata, dict):
        candidates.append(metadata.get("langgraph_node"))
        candidates.append(metadata.get("step_name"))
    if isinstance(serialized, dict):
        candidates.append(serialized.get("name"))
        cls_id = serialized.get("id")
        if isinstance(cls_id, list) and cls_id:
            candidates.append(cls_id[-1])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _truncate_str(candidate.strip(), 100) or default
    return _truncate_str(default, 100) or "step"


def _extract_trace_ids(
    metadata: Optional[dict[str, Any]],
    kwargs: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    trace_id = kwargs.get("trace_id")
    span_id = kwargs.get("span_id")
    if isinstance(metadata, dict):
        trace_id = trace_id or metadata.get("trace_id") or metadata.get("otel_trace_id")
        span_id = span_id or metadata.get("span_id") or metadata.get("otel_span_id")
    return (
        _truncate_str(str(trace_id), 64) if trace_id else None,
        _truncate_str(str(span_id), 32) if span_id else None,
    )


def _format_exception(error: BaseException) -> tuple[str, str, Optional[str]]:
    err_type = _truncate_str(type(error).__name__, 100) or "Exception"
    err_msg = sanitize_text(str(error)) or err_type
    tb_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    trace = truncate_stack_trace(tb_text)
    return err_type, err_msg, trace


@dataclass
class _SpanContext:
    log_id: UUID
    step_name: str
    step_type: str
    started_at: datetime
    details: dict[str, Any] = field(default_factory=dict)
    model_provider: Optional[str] = None
    model_used: Optional[str] = None


class MetadataCallbackHandler(BaseCallbackHandler):
    """Non-blocking LangChain/LangGraph callback handler writing Tier 1 telemetry.

    Follows Decision 30: passive telemetry failures log a warning and never abort
    or corrupt graph execution.
    """

    raise_error = False

    def __init__(
        self,
        *,
        run_id: Optional[UUID | str] = None,
        agent_id: str = "meta-agent",
        agent_version: str = "0.1.0",
        operation_kind: str = "standard",
        initiator_id: str = "system",
        initiator_type: str = "system",
        trigger_type: str = "manual",
        source_revision: Optional[str] = None,
        parent_run_id: Optional[UUID | str] = None,
        db: Optional[MetadataDatabase] = None,
        db_uri: Optional[str] = None,
        pool: Optional[ConnectionPool] = None,
        enabled: Optional[bool] = None,
        initial_sequence: int = 1,
    ) -> None:
        super().__init__()
        self.agent_run_id: Optional[UUID] = _coerce_uuid(run_id)
        self.agent_id = agent_id
        self.agent_version = agent_version
        self.operation_kind = operation_kind
        self.initiator_id = initiator_id
        self.initiator_type = initiator_type
        self.trigger_type = trigger_type
        self.source_revision = source_revision
        self.parent_agent_run_id: Optional[UUID] = _coerce_uuid(parent_run_id)

        self._db = db if db is not None else MetadataDatabase(db_uri=db_uri, pool=pool)
        self._enabled_override = enabled
        self._lock = threading.RLock()
        self._spans: dict[UUID, _SpanContext] = {}
        self._sequence_counter = itertools.count(max(1, initial_sequence))

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        try:
            return get_telemetry_config().enabled
        except Exception:
            return True

    def _next_sequence(self) -> int:
        with self._lock:
            return next(self._sequence_counter)

    def _ensure_run_id(self) -> Optional[UUID]:
        with self._lock:
            if self.agent_run_id is not None:
                return self.agent_run_id
            return self.start_run()

    # -----------------------------------------------------------------------
    # Run Lifecycle (agent_metadata.agent_run_header)
    # -----------------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: Optional[UUID | str] = None,
        parent_run_id: Optional[UUID | str] = _UNSET,
        agent_id: Optional[str] = None,
        agent_version: Optional[str] = None,
        operation_kind: Optional[str] = None,
        initiator_id: Optional[str] = None,
        initiator_type: Optional[str] = None,
        trigger_type: Optional[str] = None,
        source_revision: Optional[str] = _UNSET,
        run_parameters: Optional[dict[str, Any]] = None,
    ) -> Optional[UUID]:
        """Insert a running header record in agent_metadata.agent_run_header."""
        try:
            resolved_run_id = _coerce_uuid(run_id) or uuid4()
            eff_initiator_type = initiator_type or self.initiator_type
            eff_trigger_type = trigger_type or self.trigger_type
            if eff_initiator_type not in INITIATOR_TYPES:
                _warn("Invalid initiator_type %r; skipping start_run telemetry.", eff_initiator_type)
                return None
            if eff_trigger_type not in TRIGGER_TYPES:
                _warn("Invalid trigger_type %r; skipping start_run telemetry.", eff_trigger_type)
                return None
            eff_parent = (
                self.parent_agent_run_id
                if parent_run_id is _UNSET
                else _coerce_uuid(parent_run_id)
            )
        except Exception as exc:
            _warn("Invalid arguments for start_run telemetry: %s", exc)
            return None

        with self._lock:
            self.agent_run_id = resolved_run_id
            self._sequence_counter = itertools.count(1)
            self._spans.clear()

        if not self.enabled:
            return resolved_run_id

        eff_rev = self.source_revision if source_revision is _UNSET else source_revision

        try:
            guarded_params = guard_inline_jsonb(run_parameters or {})
            sql = f"""
                INSERT INTO {tier1_table("agent_run_header")} (
                    run_id, parent_run_id, agent_id, agent_version, operation_kind,
                    initiator_id, initiator_type, trigger_type, source_revision,
                    status, run_parameters
                )
                VALUES (
                    %(run_id)s, %(parent_run_id)s, %(agent_id)s, %(agent_version)s, %(operation_kind)s,
                    %(initiator_id)s, %(initiator_type)s, %(trigger_type)s, %(source_revision)s,
                    'running', %(run_parameters)s
                )
                ON CONFLICT (run_id) DO NOTHING
            """
            with self._db.transaction() as conn:
                conn.execute(
                    sql,
                    {
                        "run_id": resolved_run_id,
                        "parent_run_id": eff_parent,
                        "agent_id": _truncate_str(agent_id or self.agent_id, 100),
                        "agent_version": _truncate_str(agent_version or self.agent_version, 50),
                        "operation_kind": _truncate_str(operation_kind or self.operation_kind, 50),
                        "initiator_id": _truncate_str(initiator_id or self.initiator_id, 100),
                        "initiator_type": eff_initiator_type,
                        "trigger_type": eff_trigger_type,
                        "source_revision": _truncate_str(eff_rev, 100),
                        "run_parameters": Jsonb(guarded_params),
                    },
                )
        except Exception as exc:
            _warn("Failed to record agent_run_header start_run: %s", exc)
        return resolved_run_id

    def complete_run(
        self,
        status: str = "completed",
        *,
        run_id: Optional[UUID | str] = None,
        total_cost_usd: Optional[Decimal | float] = None,
        completed_at: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically roll up execution_log tokens and finalize agent_run_header."""
        if not self.enabled:
            return None

        try:
            target_run_id = _coerce_uuid(run_id) or self.agent_run_id
        except Exception as exc:
            _warn("Invalid run_id in complete_run: %s", exc)
            return None

        if target_run_id is None:
            _warn("complete_run called with no active run_id; skipping telemetry.")
            return None

        if status not in RUN_STATUSES:
            _warn("Invalid run status %r in complete_run; skipping telemetry.", status)
            return None

        is_terminal = status in RUN_TERMINAL_STATUSES
        eff_completed_at = completed_at
        if is_terminal and eff_completed_at is None:
            eff_completed_at = datetime.now(timezone.utc)

        try:
            sql = f"""
                WITH agg AS (
                    SELECT
                        COALESCE(SUM(input_token_count), 0)::INTEGER AS in_tok,
                        COALESCE(SUM(output_token_count), 0)::INTEGER AS out_tok
                    FROM {tier1_table("execution_log")}
                    WHERE run_id = %(run_id)s
                )
                UPDATE {tier1_table("agent_run_header")} AS h
                SET total_input_tokens = agg.in_tok,
                    total_output_tokens = agg.out_tok,
                    total_cost_usd = COALESCE(%(total_cost_usd)s, h.total_cost_usd),
                    status = %(status)s,
                    completed_at = CASE
                        WHEN %(is_terminal)s
                            THEN GREATEST(%(completed_at)s::timestamptz, h.started_at)
                        ELSE NULL
                    END,
                    duration_ms = CASE
                        WHEN %(is_terminal)s
                            THEN GREATEST(
                                0,
                                ROUND(
                                    EXTRACT(
                                        EPOCH FROM (
                                            GREATEST(%(completed_at)s::timestamptz, h.started_at)
                                            - h.started_at
                                        )
                                    ) * 1000
                                )::BIGINT
                            )
                        ELSE NULL
                    END
                FROM agg
                WHERE h.run_id = %(run_id)s
                RETURNING h.run_id, h.status, h.total_input_tokens, h.total_output_tokens,
                          h.total_cost_usd, h.duration_ms, h.completed_at
            """
            cost_param = Decimal(str(total_cost_usd)) if total_cost_usd is not None else None
            with self._db.transaction() as conn:
                row = conn.execute(
                    sql,
                    {
                        "run_id": target_run_id,
                        "status": status,
                        "is_terminal": is_terminal,
                        "completed_at": eff_completed_at,
                        "total_cost_usd": cost_param,
                    },
                ).fetchone()
            return dict(row) if row is not None else None
        except Exception as exc:
            _warn("Failed to complete_run telemetry rollup for %s: %s", target_run_id, exc)
            return None

    # -----------------------------------------------------------------------
    # Shared Execution Span Persistence (agent_metadata.execution_log)
    # -----------------------------------------------------------------------

    def _start_span(
        self,
        *,
        lc_run_id: UUID,
        parent_run_id: Optional[UUID],
        step_name: str,
        step_type: str,
        details: dict[str, Any],
        metadata: Optional[dict[str, Any]] = None,
        kwargs: Optional[dict[str, Any]] = None,
        model_provider: Optional[str] = None,
        model_used: Optional[str] = None,
    ) -> Optional[UUID]:
        if not self.enabled:
            return None

        resolved_type = step_type if step_type in STEP_TYPES else "node"
        agent_run_id = self._ensure_run_id()
        if agent_run_id is None:
            return None

        with self._lock:
            parent_span = self._spans.get(parent_run_id) if parent_run_id is not None else None
            parent_log_id = parent_span.log_id if parent_span is not None else None
            seq = next(self._sequence_counter)

        log_id = uuid4()
        started_at = datetime.now(timezone.utc)
        trace_id, span_id = _extract_trace_ids(metadata, kwargs or {})
        guarded_details = guard_inline_jsonb(details)
        if not isinstance(guarded_details, dict):
            guarded_details = {"value": guarded_details}

        sql = f"""
            INSERT INTO {tier1_table("execution_log")} (
                log_id, run_id, parent_log_id, event_sequence, trace_id, span_id,
                step_name, step_type, status, started_at, model_provider, model_used, details
            )
            VALUES (
                %(log_id)s, %(run_id)s, %(parent_log_id)s, %(event_sequence)s, %(trace_id)s, %(span_id)s,
                %(step_name)s, %(step_type)s, 'started', %(started_at)s, %(model_provider)s, %(model_used)s, %(details)s
            )
        """
        with self._db.transaction() as conn:
            conn.execute(
                sql,
                {
                    "log_id": log_id,
                    "run_id": agent_run_id,
                    "parent_log_id": parent_log_id,
                    "event_sequence": seq,
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "step_name": _truncate_str(step_name, 100),
                    "step_type": resolved_type,
                    "started_at": started_at,
                    "model_provider": _truncate_str(model_provider, 50),
                    "model_used": _truncate_str(model_used, 100),
                    "details": Jsonb(guarded_details),
                },
            )

        with self._lock:
            self._spans[lc_run_id] = _SpanContext(
                log_id=log_id,
                step_name=step_name,
                step_type=resolved_type,
                started_at=started_at,
                details=guarded_details,
                model_provider=model_provider,
                model_used=model_used,
            )
        return log_id

    def _finish_span(
        self,
        *,
        lc_run_id: UUID,
        parent_run_id: Optional[UUID],
        fallback_step_name: str,
        fallback_step_type: str,
        status: str,
        end_details: Optional[dict[str, Any]] = None,
        model_provider: Optional[str] = None,
        model_used: Optional[str] = None,
        input_token_count: int = 0,
        output_token_count: int = 0,
        cached_token_count: int = 0,
        reasoning_token_count: int = 0,
        error: Optional[BaseException] = None,
    ) -> None:
        if not self.enabled:
            return

        agent_run_id = self.agent_run_id
        if agent_run_id is None:
            return

        with self._lock:
            span = self._spans.pop(lc_run_id, None)
            parent_span = self._spans.get(parent_run_id) if parent_run_id is not None else None

        completed_at = datetime.now(timezone.utc)
        err_type: Optional[str] = None
        err_msg: Optional[str] = None
        stack_trace: Optional[str] = None
        if error is not None:
            err_type, err_msg, stack_trace = _format_exception(error)

        if span is not None:
            merged_details = dict(span.details)
            if end_details:
                merged_details.update(end_details)
            guarded_details = guard_inline_jsonb(merged_details)
            if not isinstance(guarded_details, dict):
                guarded_details = {"value": guarded_details}

            eff_provider = model_provider or span.model_provider
            eff_model = model_used or span.model_used

            sql = f"""
                UPDATE {tier1_table("execution_log")}
                SET status = %(status)s,
                    completed_at = GREATEST(%(completed_at)s::timestamptz, started_at),
                    duration_ms = GREATEST(
                        0,
                        ROUND(
                            EXTRACT(
                                EPOCH FROM (
                                    GREATEST(%(completed_at)s::timestamptz, started_at)
                                    - started_at
                                )
                            ) * 1000
                        )::INTEGER
                    ),
                    model_provider = COALESCE(%(model_provider)s, model_provider),
                    model_used = COALESCE(%(model_used)s, model_used),
                    input_token_count = %(input_token_count)s,
                    output_token_count = %(output_token_count)s,
                    cached_token_count = %(cached_token_count)s,
                    reasoning_token_count = %(reasoning_token_count)s,
                    error_type = %(error_type)s,
                    error_message = %(error_message)s,
                    stack_trace = %(stack_trace)s,
                    details = %(details)s
                WHERE run_id = %(run_id)s AND log_id = %(log_id)s
            """
            with self._db.transaction() as conn:
                conn.execute(
                    sql,
                    {
                        "run_id": agent_run_id,
                        "log_id": span.log_id,
                        "status": status,
                        "completed_at": completed_at,
                        "model_provider": _truncate_str(eff_provider, 50),
                        "model_used": _truncate_str(eff_model, 100),
                        "input_token_count": input_token_count,
                        "output_token_count": output_token_count,
                        "cached_token_count": cached_token_count,
                        "reasoning_token_count": reasoning_token_count,
                        "error_type": err_type,
                        "error_message": err_msg,
                        "stack_trace": stack_trace,
                        "details": Jsonb(guarded_details),
                    },
                )
            return

        # Fallback: end event arrived without a tracked start span; insert terminal span directly.
        guarded_details = guard_inline_jsonb(end_details or {})
        if not isinstance(guarded_details, dict):
            guarded_details = {"value": guarded_details}
        seq = self._next_sequence()
        parent_log_id = parent_span.log_id if parent_span is not None else None
        insert_sql = f"""
            INSERT INTO {tier1_table("execution_log")} (
                log_id, run_id, parent_log_id, event_sequence, step_name, step_type,
                status, started_at, completed_at, duration_ms,
                model_provider, model_used,
                input_token_count, output_token_count, cached_token_count, reasoning_token_count,
                error_type, error_message, stack_trace, details
            )
            VALUES (
                %(log_id)s, %(run_id)s, %(parent_log_id)s, %(event_sequence)s, %(step_name)s, %(step_type)s,
                %(status)s, %(ts)s, %(ts)s, 0,
                %(model_provider)s, %(model_used)s,
                %(input_token_count)s, %(output_token_count)s, %(cached_token_count)s, %(reasoning_token_count)s,
                %(error_type)s, %(error_message)s, %(stack_trace)s, %(details)s
            )
        """
        with self._db.transaction() as conn:
            conn.execute(
                insert_sql,
                {
                    "log_id": uuid4(),
                    "run_id": agent_run_id,
                    "parent_log_id": parent_log_id,
                    "event_sequence": seq,
                    "step_name": _truncate_str(fallback_step_name, 100),
                    "step_type": fallback_step_type if fallback_step_type in STEP_TYPES else "node",
                    "status": status,
                    "ts": completed_at,
                    "model_provider": _truncate_str(model_provider, 50),
                    "model_used": _truncate_str(model_used, 100),
                    "input_token_count": input_token_count,
                    "output_token_count": output_token_count,
                    "cached_token_count": cached_token_count,
                    "reasoning_token_count": reasoning_token_count,
                    "error_type": err_type,
                    "error_message": err_msg,
                    "stack_trace": stack_trace,
                    "details": Jsonb(guarded_details),
                },
            )

    # -----------------------------------------------------------------------
    # LangChain BaseCallbackHandler Lifecycle Hooks
    # -----------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            step_name = _extract_step_name(serialized, metadata, kwargs, default="chain")
            raw_type = metadata.get("step_type") if isinstance(metadata, dict) else None
            step_type = raw_type if isinstance(raw_type, str) and raw_type in STEP_TYPES else "node"
            details: dict[str, Any] = {"inputs": inputs}
            if tags:
                details["tags"] = tags
            if metadata:
                details["metadata"] = metadata
            self._start_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                step_name=step_name,
                step_type=step_type,
                details=details,
                metadata=metadata,
                kwargs=kwargs,
            )
        except Exception as exc:
            _warn("Telemetry error in on_chain_start (%s): %s", run_id, exc)

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name="chain",
                fallback_step_type="node",
                status="completed",
                end_details={"outputs": outputs},
            )
        except Exception as exc:
            _warn("Telemetry error in on_chain_end (%s): %s", run_id, exc)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name="chain",
                fallback_step_type="node",
                status="failed",
                error=error,
            )
        except Exception as exc:
            _warn("Telemetry error in on_chain_error (%s): %s", run_id, exc)

    def _handle_llm_start(
        self,
        serialized: dict[str, Any],
        payload_key: str,
        payload_value: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        kwargs: dict[str, Any],
    ) -> None:
        inv_params = kwargs.get("invocation_params") or {}
        ser_kwargs = serialized.get("kwargs") if isinstance(serialized, dict) else {}
        if not isinstance(inv_params, dict):
            inv_params = {}
        if not isinstance(ser_kwargs, dict):
            ser_kwargs = {}

        raw_model = (
            inv_params.get("model_name")
            or inv_params.get("model")
            or ser_kwargs.get("model_name")
            or ser_kwargs.get("model")
            or (metadata.get("ls_model_name") if isinstance(metadata, dict) else None)
        )
        explicit_provider = (
            (metadata.get("ls_provider") if isinstance(metadata, dict) else None)
            or inv_params.get("custom_llm_provider")
            or inv_params.get("provider")
        )
        provider, model = _infer_provider_and_model(
            raw_model,
            explicit_provider=explicit_provider,
            serialized=serialized,
        )
        step_name = _extract_step_name(
            serialized,
            metadata,
            kwargs,
            default=model or "llm_call",
        )
        details: dict[str, Any] = {payload_key: payload_value}
        if tags:
            details["tags"] = tags
        if metadata:
            details["metadata"] = metadata

        self._start_span(
            lc_run_id=run_id,
            parent_run_id=parent_run_id,
            step_name=step_name,
            step_type="llm_call",
            details=details,
            metadata=metadata,
            kwargs=kwargs,
            model_provider=provider,
            model_used=model,
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._handle_llm_start(
                serialized,
                "prompts",
                prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                kwargs=kwargs,
            )
        except Exception as exc:
            _warn("Telemetry error in on_llm_start (%s): %s", run_id, exc)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._handle_llm_start(
                serialized,
                "messages",
                messages,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=metadata,
                kwargs=kwargs,
            )
        except Exception as exc:
            _warn("Telemetry error in on_chat_model_start (%s): %s", run_id, exc)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            with self._lock:
                existing = self._spans.get(run_id)
            telemetry = extract_llm_telemetry(
                response,
                fallback_provider=existing.model_provider if existing else None,
                fallback_model=existing.model_used if existing else None,
            )
            end_details: dict[str, Any] = {}
            if hasattr(response, "generations"):
                end_details["generation_count"] = sum(
                    len(g) for g in response.generations if isinstance(g, list)
                )
            if getattr(response, "llm_output", None) is not None:
                end_details["llm_output"] = response.llm_output

            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name=telemetry["model_used"] or "llm_call",
                fallback_step_type="llm_call",
                status="completed",
                end_details=end_details,
                model_provider=telemetry["model_provider"],
                model_used=telemetry["model_used"],
                input_token_count=telemetry["input_token_count"],
                output_token_count=telemetry["output_token_count"],
                cached_token_count=telemetry["cached_token_count"],
                reasoning_token_count=telemetry["reasoning_token_count"],
            )
        except Exception as exc:
            _warn("Telemetry error in on_llm_end (%s): %s", run_id, exc)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name="llm_call",
                fallback_step_type="llm_call",
                status="failed",
                error=error,
            )
        except Exception as exc:
            _warn("Telemetry error in on_llm_error (%s): %s", run_id, exc)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        inputs: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            step_name = _extract_step_name(serialized, metadata, kwargs, default="tool_call")
            details: dict[str, Any] = {"input_str": input_str}
            if inputs is not None:
                details["inputs"] = inputs
            if tags:
                details["tags"] = tags
            if metadata:
                details["metadata"] = metadata
            self._start_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                step_name=step_name,
                step_type="tool_call",
                details=details,
                metadata=metadata,
                kwargs=kwargs,
            )
        except Exception as exc:
            _warn("Telemetry error in on_tool_start (%s): %s", run_id, exc)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name="tool_call",
                fallback_step_type="tool_call",
                status="completed",
                end_details={"output": output},
            )
        except Exception as exc:
            _warn("Telemetry error in on_tool_end (%s): %s", run_id, exc)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            self._finish_span(
                lc_run_id=run_id,
                parent_run_id=parent_run_id,
                fallback_step_name="tool_call",
                fallback_step_type="tool_call",
                status="failed",
                error=error,
            )
        except Exception as exc:
            _warn("Telemetry error in on_tool_error (%s): %s", run_id, exc)

    # -----------------------------------------------------------------------
    # Explicit Tier 1 Telemetry, Retry, Artifact, Decision, and Audit APIs
    # -----------------------------------------------------------------------

    def record_retry_span(
        self,
        *,
        retry_of_log_id: UUID | str,
        attempt_number: int,
        step_name: str,
        step_type: str = "node",
        status: str = "started",
        run_id: Optional[UUID | str] = None,
        parent_log_id: Optional[UUID | str] = None,
        log_id: Optional[UUID | str] = None,
        lc_run_id: Optional[UUID | str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        error: Optional[BaseException] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> Optional[UUID]:
        """Record an explicit retry attempt linked via (run_id, retry_of_log_id)."""
        if attempt_number < 1:
            raise ValueError("attempt_number must be >= 1.")
        if step_type not in STEP_TYPES:
            raise ValueError(f"Invalid step_type {step_type!r}; expected one of {sorted(STEP_TYPES)}.")
        if status not in STEP_STATUSES:
            raise ValueError(f"Invalid status {status!r}; expected one of {sorted(STEP_STATUSES)}.")

        if not self.enabled:
            return None

        try:
            target_run_id = _coerce_uuid(run_id) or self.agent_run_id
            if target_run_id is None:
                _warn("record_retry_span called without active run_id; skipping.")
                return None

            new_log_id = _coerce_uuid(log_id) or uuid4()
            now = datetime.now(timezone.utc)
            is_terminal = status in STEP_TERMINAL_STATUSES
            err_type: Optional[str] = None
            err_msg: Optional[str] = None
            stack_trace: Optional[str] = None
            if error is not None:
                err_type, err_msg, stack_trace = _format_exception(error)

            guarded_details = guard_inline_jsonb(details or {})
            if not isinstance(guarded_details, dict):
                guarded_details = {"value": guarded_details}

            seq = self._next_sequence()
            sql = f"""
                INSERT INTO {tier1_table("execution_log")} (
                    log_id, run_id, parent_log_id, retry_of_log_id, attempt_number,
                    event_sequence, trace_id, span_id, step_name, step_type, status,
                    started_at, completed_at, duration_ms,
                    error_type, error_message, stack_trace, details
                )
                VALUES (
                    %(log_id)s, %(run_id)s, %(parent_log_id)s, %(retry_of_log_id)s, %(attempt_number)s,
                    %(event_sequence)s, %(trace_id)s, %(span_id)s, %(step_name)s, %(step_type)s, %(status)s,
                    %(started_at)s, %(completed_at)s, %(duration_ms)s,
                    %(error_type)s, %(error_message)s, %(stack_trace)s, %(details)s
                )
            """
            with self._db.transaction() as conn:
                conn.execute(
                    sql,
                    {
                        "log_id": new_log_id,
                        "run_id": target_run_id,
                        "parent_log_id": _coerce_uuid(parent_log_id),
                        "retry_of_log_id": _coerce_uuid(retry_of_log_id),
                        "attempt_number": attempt_number,
                        "event_sequence": seq,
                        "trace_id": _truncate_str(trace_id, 64),
                        "span_id": _truncate_str(span_id, 32),
                        "step_name": _truncate_str(step_name, 100),
                        "step_type": step_type,
                        "status": status,
                        "started_at": now,
                        "completed_at": now if is_terminal else None,
                        "duration_ms": 0 if is_terminal else None,
                        "error_type": err_type,
                        "error_message": err_msg,
                        "stack_trace": stack_trace,
                        "details": Jsonb(guarded_details),
                    },
                )
            if lc_run_id is not None and not is_terminal:
                with self._lock:
                    self._spans[_coerce_uuid(lc_run_id)] = _SpanContext(
                        log_id=new_log_id,
                        step_name=step_name,
                        step_type=step_type,
                        started_at=now,
                        details=guarded_details,
                    )
            return new_log_id
        except Exception as exc:
            _warn("Failed to record retry span %s: %s", new_log_id, exc)
            return None

    def record_artifact(
        self,
        *,
        artifact_name: str,
        artifact_kind: str,
        storage_uri: str,
        checksum_sha256: str,
        artifact_version: str = "1.0.0",
        byte_size: Optional[int] = None,
        mime_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        produced_run_id: Optional[UUID | str] = _UNSET,
        produced_log_id: Optional[UUID | str] = None,
        artifact_id: Optional[UUID | str] = None,
    ) -> Optional[UUID]:
        """Register an immutable artifact in agent_metadata.artifact_registry."""
        if byte_size is not None and byte_size < 0:
            raise ValueError("byte_size must be >= 0.")
        if len(checksum_sha256) > 64:
            raise ValueError("checksum_sha256 exceeds VARCHAR(64) limit.")

        if not self.enabled:
            return None

        eff_run_id = (
            self.agent_run_id
            if produced_run_id is _UNSET
            else _coerce_uuid(produced_run_id)
        )
        eff_log_id = _coerce_uuid(produced_log_id)
        if eff_run_id is None and eff_log_id is not None:
            raise ValueError("produced_log_id requires produced_run_id to satisfy fk_artifact_step.")

        new_artifact_id = _coerce_uuid(artifact_id) or uuid4()
        try:
            guarded_meta = guard_inline_jsonb(metadata or {})
            if not isinstance(guarded_meta, dict):
                guarded_meta = {"value": guarded_meta}
            sql = f"""
                INSERT INTO {tier1_table("artifact_registry")} (
                    artifact_id, produced_run_id, produced_log_id, artifact_name,
                    artifact_kind, artifact_version, storage_uri, checksum_sha256,
                    byte_size, mime_type, metadata
                )
                VALUES (
                    %(artifact_id)s, %(produced_run_id)s, %(produced_log_id)s, %(artifact_name)s,
                    %(artifact_kind)s, %(artifact_version)s, %(storage_uri)s, %(checksum_sha256)s,
                    %(byte_size)s, %(mime_type)s, %(metadata)s
                )
                RETURNING artifact_id
            """
            with self._db.transaction() as conn:
                row = conn.execute(
                    sql,
                    {
                        "artifact_id": new_artifact_id,
                        "produced_run_id": eff_run_id,
                        "produced_log_id": eff_log_id,
                        "artifact_name": _truncate_str(artifact_name, 255),
                        "artifact_kind": _truncate_str(artifact_kind, 50),
                        "artifact_version": _truncate_str(artifact_version, 50),
                        "storage_uri": sanitize_text(storage_uri),
                        "checksum_sha256": checksum_sha256,
                        "byte_size": byte_size,
                        "mime_type": _truncate_str(mime_type, 100),
                        "metadata": Jsonb(guarded_meta),
                    },
                ).fetchone()
            return row["artifact_id"] if row else new_artifact_id
        except Exception as exc:
            _warn("Failed to record artifact %s: %s", artifact_name, exc)
            return None

    def link_artifact_manifest(
        self,
        *,
        artifact_id: UUID | str,
        role: str,
        run_id: Optional[UUID | str] = None,
        associated_log_id: Optional[UUID | str] = None,
        associated_phase: Optional[str] = None,
        manifest_id: Optional[UUID | str] = None,
    ) -> Optional[UUID]:
        """Link an artifact to a run/span Bill of Materials in artifact_manifest."""
        if role not in MANIFEST_ROLES:
            raise ValueError(f"Invalid manifest role {role!r}; expected one of {sorted(MANIFEST_ROLES)}.")

        if not self.enabled:
            return None

        target_run_id = _coerce_uuid(run_id) or self.agent_run_id
        if target_run_id is None:
            _warn("link_artifact_manifest called without active run_id; skipping.")
            return None

        new_manifest_id = _coerce_uuid(manifest_id) or uuid4()
        try:
            sql = f"""
                INSERT INTO {tier1_table("artifact_manifest")} (
                    manifest_id, run_id, artifact_id, associated_log_id, role, associated_phase
                )
                VALUES (
                    %(manifest_id)s, %(run_id)s, %(artifact_id)s, %(associated_log_id)s, %(role)s, %(associated_phase)s
                )
                ON CONFLICT (run_id, artifact_id, role) DO UPDATE
                SET associated_log_id = COALESCE(EXCLUDED.associated_log_id, {tier1_table("artifact_manifest")}.associated_log_id),
                    associated_phase = COALESCE(EXCLUDED.associated_phase, {tier1_table("artifact_manifest")}.associated_phase)
                RETURNING manifest_id
            """
            with self._db.transaction() as conn:
                row = conn.execute(
                    sql,
                    {
                        "manifest_id": new_manifest_id,
                        "run_id": target_run_id,
                        "artifact_id": _coerce_uuid(artifact_id),
                        "associated_log_id": _coerce_uuid(associated_log_id),
                        "role": role,
                        "associated_phase": _truncate_str(associated_phase, 50),
                    },
                ).fetchone()
            return row["manifest_id"] if row else new_manifest_id
        except Exception as exc:
            _warn("Failed to link artifact manifest (%s, %s): %s", artifact_id, role, exc)
            return None

    def record_decision(
        self,
        *,
        decision_type: str,
        decision_status: str = "decided",
        decision_value: Optional[str] = None,
        decided_by: Optional[str] = None,
        decided_by_type: Optional[str] = None,
        rationale: Optional[str] = None,
        alternatives_considered: Optional[Sequence[Any]] = None,
        confidence_score: Optional[Decimal | float] = None,
        details: Optional[dict[str, Any]] = None,
        run_id: Optional[UUID | str] = None,
        log_id: Optional[UUID | str] = None,
        decision_id: Optional[UUID | str] = None,
    ) -> Optional[UUID]:
        """Record or resolve a decision point in agent_metadata.decision_log."""
        if decision_status not in DECISION_STATUSES:
            raise ValueError(
                f"Invalid decision_status {decision_status!r}; expected one of {sorted(DECISION_STATUSES)}."
            )
        if decided_by_type is not None and decided_by_type not in DECISION_ACTOR_TYPES:
            raise ValueError(
                f"Invalid decided_by_type {decided_by_type!r}; expected one of {sorted(DECISION_ACTOR_TYPES)}."
            )
        if decision_status == "decided" and any(
            v is None for v in (decision_value, decided_by, decided_by_type, rationale)
        ):
            raise ValueError(
                "chk_decision_resolution requires decision_value, decided_by, "
                "decided_by_type, and rationale when decision_status == 'decided'."
            )
        if confidence_score is not None:
            score_dec = Decimal(str(confidence_score))
            if score_dec < Decimal("0.000") or score_dec > Decimal("1.000"):
                raise ValueError("confidence_score must be between 0.000 and 1.000.")
        else:
            score_dec = None

        if not self.enabled:
            return None

        target_run_id = _coerce_uuid(run_id) or self.agent_run_id
        if target_run_id is None:
            _warn("record_decision called without active run_id; skipping.")
            return None

        new_decision_id = _coerce_uuid(decision_id) or uuid4()
        try:
            guarded_details = guard_inline_jsonb(details or {})
            if not isinstance(guarded_details, dict):
                guarded_details = {"value": guarded_details}
            alt_jsonb = (
                to_jsonb(list(alternatives_considered), guard_size=True)
                if alternatives_considered is not None
                else None
            )
            tbl = tier1_table("decision_log")
            sql = f"""
                INSERT INTO {tbl} (
                    decision_id, run_id, log_id, decision_status, decision_type,
                    decision_value, decided_by, decided_by_type, rationale,
                    alternatives_considered, confidence_score, details
                )
                VALUES (
                    %(decision_id)s, %(run_id)s, %(log_id)s, %(decision_status)s, %(decision_type)s,
                    %(decision_value)s, %(decided_by)s, %(decided_by_type)s, %(rationale)s,
                    %(alternatives_considered)s, %(confidence_score)s, %(details)s
                )
                ON CONFLICT (decision_id) DO UPDATE
                SET decision_status = EXCLUDED.decision_status,
                    decision_value = EXCLUDED.decision_value,
                    decided_by = EXCLUDED.decided_by,
                    decided_by_type = EXCLUDED.decided_by_type,
                    rationale = EXCLUDED.rationale,
                    alternatives_considered = COALESCE(EXCLUDED.alternatives_considered, {tbl}.alternatives_considered),
                    confidence_score = COALESCE(EXCLUDED.confidence_score, {tbl}.confidence_score),
                    details = EXCLUDED.details
                RETURNING decision_id
            """
            with self._db.transaction() as conn:
                row = conn.execute(
                    sql,
                    {
                        "decision_id": new_decision_id,
                        "run_id": target_run_id,
                        "log_id": _coerce_uuid(log_id),
                        "decision_status": decision_status,
                        "decision_type": _truncate_str(decision_type, 50),
                        "decision_value": sanitize_text(decision_value),
                        "decided_by": _truncate_str(decided_by, 100),
                        "decided_by_type": decided_by_type,
                        "rationale": sanitize_text(rationale),
                        "alternatives_considered": alt_jsonb,
                        "confidence_score": score_dec,
                        "details": Jsonb(guarded_details),
                    },
                ).fetchone()
            return row["decision_id"] if row else new_decision_id
        except Exception as exc:
            _warn("Failed to record decision %s: %s", decision_type, exc)
            return None

    def record_audit_event(
        self,
        *,
        action: str,
        actor_id: str,
        actor_type: str,
        target_entity_type: str,
        outcome: str = "success",
        target_entity_id: Optional[str] = None,
        run_id: Optional[UUID | str] = _UNSET,
        correlation_id: Optional[str] = None,
        before_state: Optional[dict[str, Any]] = None,
        after_state: Optional[dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
        audit_id: Optional[UUID | str] = None,
    ) -> Optional[UUID]:
        """Append an immutable compliance/security audit record to agent_metadata.audit_log."""
        if actor_type not in AUDIT_ACTOR_TYPES:
            raise ValueError(
                f"Invalid actor_type {actor_type!r}; expected one of {sorted(AUDIT_ACTOR_TYPES)}."
            )
        if outcome not in AUDIT_OUTCOMES:
            raise ValueError(
                f"Invalid outcome {outcome!r}; expected one of {sorted(AUDIT_OUTCOMES)}."
            )
        if ip_address is not None:
            ipaddress.ip_address(ip_address)

        if not self.enabled:
            return None

        eff_run_id = (
            self.agent_run_id
            if run_id is _UNSET
            else _coerce_uuid(run_id)
        )
        new_audit_id = _coerce_uuid(audit_id) or uuid4()

        try:
            guarded_details = guard_inline_jsonb(details or {})
            if not isinstance(guarded_details, dict):
                guarded_details = {"value": guarded_details}
            before_jsonb = to_jsonb(before_state, guard_size=True) if before_state is not None else None
            after_jsonb = to_jsonb(after_state, guard_size=True) if after_state is not None else None
            eff_occurred_at = occurred_at or datetime.now(timezone.utc)

            sql = f"""
                INSERT INTO {tier1_table("audit_log")} (
                    audit_id, run_id, correlation_id, occurred_at,
                    action, actor_id, actor_type, target_entity_type, target_entity_id,
                    outcome, before_state, after_state, ip_address, user_agent, details
                )
                VALUES (
                    %(audit_id)s, %(run_id)s, %(correlation_id)s, %(occurred_at)s,
                    %(action)s, %(actor_id)s, %(actor_type)s, %(target_entity_type)s, %(target_entity_id)s,
                    %(outcome)s, %(before_state)s, %(after_state)s, %(ip_address)s, %(user_agent)s, %(details)s
                )
                RETURNING audit_id
            """
            with self._db.transaction() as conn:
                row = conn.execute(
                    sql,
                    {
                        "audit_id": new_audit_id,
                        "run_id": eff_run_id,
                        "correlation_id": _truncate_str(correlation_id, 100),
                        "occurred_at": eff_occurred_at,
                        "action": _truncate_str(action, 100),
                        "actor_id": _truncate_str(actor_id, 100),
                        "actor_type": actor_type,
                        "target_entity_type": _truncate_str(target_entity_type, 50),
                        "target_entity_id": _truncate_str(target_entity_id, 100),
                        "outcome": outcome,
                        "before_state": before_jsonb,
                        "after_state": after_jsonb,
                        "ip_address": ip_address,
                        "user_agent": sanitize_text(user_agent),
                        "details": Jsonb(guarded_details),
                    },
                ).fetchone()
            return row["audit_id"] if row else new_audit_id
        except Exception as exc:
            _warn("Failed to append audit_log event %s: %s", action, exc)
            return None
