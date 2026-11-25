"""Shared PostgreSQL connection pooling, transaction helpers, and telemetry sanitization."""

from __future__ import annotations

import json
import re
import threading
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, overload
from uuid import UUID

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from src.core.settings import (
    DatabasePoolConfig,
    DatabaseSchemasConfig,
    TelemetryConfig,
    get_env_settings,
    get_studio_config,
)

REDACTED_VALUE = "[REDACTED]"
_TRUNCATION_MARKER = "\n... [TRUNCATED]"


def _close_pool_quietly(pool: ConnectionPool) -> None:
    """Close a connection pool during finalization without raising."""
    try:
        if not pool.closed:
            pool.close(timeout=2.0)
    except Exception:
        pass


def get_schema_names() -> DatabaseSchemasConfig:
    """Return configured schema names from config/settings.yaml."""
    return get_studio_config().database.schemas


def get_telemetry_config() -> TelemetryConfig:
    """Return configured telemetry sanitization/truncation limits."""
    return get_studio_config().telemetry


def _validate_sql_identifier(identifier: str) -> str:
    """Guard against unsafe dynamic schema identifiers."""
    if not identifier.isidentifier():
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def qualified_table(schema_name: str, table_name: str) -> str:
    """Return a safely quoted schema-qualified table reference."""
    return f'"{_validate_sql_identifier(schema_name)}"."{_validate_sql_identifier(table_name)}"'


def tier1_table(table_name: str) -> str:
    """Return the configured Tier 1 schema-qualified table reference."""
    return qualified_table(get_schema_names().tier1, table_name)


def tier2_table(table_name: str) -> str:
    """Return the configured Tier 2 schema-qualified table reference."""
    return qualified_table(get_schema_names().tier2, table_name)


class MetadataDatabase:
    """Lazy, thread-safe PostgreSQL connection pool and transaction manager."""

    def __init__(
        self,
        db_uri: Optional[str] = None,
        pool: Optional[ConnectionPool] = None,
        *,
        register_pgvector: bool = True,
        pool_config: Optional[DatabasePoolConfig] = None,
    ) -> None:
        self._db_uri = db_uri
        self._pool = pool
        self._owns_pool = pool is None
        self._register_pgvector = register_pgvector
        self._pool_config = pool_config
        self._lock = threading.Lock()
        self._finalizer: Optional[weakref.finalize] = None

    def _resolve_uri(self) -> str:
        uri = self._db_uri or get_env_settings().metadata_db_uri
        if not uri:
            raise ValueError(
                "Metadata database URI is required (pass db_uri or set METADATA_DB_URI)."
            )
        return uri

    def _resolve_pool_config(self) -> DatabasePoolConfig:
        return self._pool_config or get_studio_config().database.pool

    def _configure_connection(self, conn: Connection[Any]) -> None:
        """Register pgvector adapters and apply statement timeout per connection."""
        pool_cfg = self._resolve_pool_config()
        if self._register_pgvector:
            register_vector(conn)
        if pool_cfg.statement_timeout_ms > 0:
            conn.execute(f"SET statement_timeout = {int(pool_cfg.statement_timeout_ms)}")
        # ConnectionPool requires configure() to leave connection in IDLE state.
        conn.commit()

    def get_pool(self) -> ConnectionPool:
        """Lazily create and open the underlying psycopg ConnectionPool."""
        if self._pool is not None:
            return self._pool

        with self._lock:
            if self._pool is not None:
                return self._pool

            uri = self._resolve_uri()
            pool_cfg = self._resolve_pool_config()
            pool = ConnectionPool(
                conninfo=uri,
                min_size=pool_cfg.min_size,
                max_size=pool_cfg.max_size,
                timeout=float(pool_cfg.connection_timeout_seconds),
                open=False,
                configure=self._configure_connection,
                kwargs={"row_factory": dict_row},
            )
            pool.open(wait=False)
            self._pool = pool
            self._owns_pool = True
            self._finalizer = weakref.finalize(self, _close_pool_quietly, pool)
            return pool

    @contextmanager
    def connection(self, timeout: Optional[float] = None) -> Iterator[Connection[Any]]:
        """Yield a pooled connection with automatic commit/rollback on exit."""
        pool = self.get_pool()
        with pool.connection(timeout=timeout) as conn:
            yield conn

    @contextmanager
    def transaction(self, timeout: Optional[float] = None) -> Iterator[Connection[Any]]:
        """Yield a pooled connection inside an explicit database transaction."""
        with self.connection(timeout=timeout) as conn:
            with conn.transaction():
                yield conn

    def close(self, timeout: float = 5.0) -> None:
        """Close the owned pool cleanly."""
        with self._lock:
            pool = self._pool
            if pool is not None and self._owns_pool:
                self._pool = None
                if self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = None
                if not pool.closed:
                    pool.close(timeout=timeout)

    def __enter__(self) -> MetadataDatabase:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


_default_db_lock = threading.Lock()
_default_db: Optional[MetadataDatabase] = None


def get_default_db() -> MetadataDatabase:
    """Return the shared lazy MetadataDatabase singleton."""
    global _default_db
    if _default_db is not None:
        return _default_db
    with _default_db_lock:
        if _default_db is None:
            _default_db = MetadataDatabase()
        return _default_db


def close_default_db(timeout: float = 5.0) -> None:
    """Close and reset the shared default MetadataDatabase."""
    global _default_db
    with _default_db_lock:
        if _default_db is not None:
            _default_db.close(timeout=timeout)
            _default_db = None


def create_pool(
    db_uri: Optional[str] = None,
    *,
    register_pgvector: bool = True,
    pool_config: Optional[DatabasePoolConfig] = None,
) -> ConnectionPool:
    """Create and open a standalone psycopg ConnectionPool from Studio settings."""
    db = MetadataDatabase(
        db_uri=db_uri,
        register_pgvector=register_pgvector,
        pool_config=pool_config,
    )
    pool = db.get_pool()
    # Detach ownership from the temporary MetadataDatabase wrapper so caller manages lifecycle.
    db._owns_pool = False
    if db._finalizer is not None:
        db._finalizer.detach()
        db._finalizer = None
    return pool


@contextmanager
def connection(
    db_uri: Optional[str] = None,
    pool: Optional[ConnectionPool] = None,
    *,
    timeout: Optional[float] = None,
) -> Iterator[Connection[Any]]:
    """Context manager yielding a connection from an explicit or default pool."""
    if pool is not None:
        with pool.connection(timeout=timeout) as conn:
            yield conn
        return

    if db_uri is not None:
        with MetadataDatabase(db_uri=db_uri) as db:
            with db.connection(timeout=timeout) as conn:
                yield conn
        return

    with get_default_db().connection(timeout=timeout) as conn:
        yield conn


@contextmanager
def transaction(
    db_uri: Optional[str] = None,
    pool: Optional[ConnectionPool] = None,
    *,
    timeout: Optional[float] = None,
) -> Iterator[Connection[Any]]:
    """Context manager yielding a connection inside an explicit transaction."""
    with connection(db_uri=db_uri, pool=pool, timeout=timeout) as conn:
        with conn.transaction():
            yield conn


# ---------------------------------------------------------------------------
# Telemetry Sanitization, Stack Truncation, and 64KB Inline JSONB Guards
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _compile_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile and cache configured redaction regexes."""
    return tuple(re.compile(pattern) for pattern in patterns if pattern)


def _resolve_patterns(
    redact_patterns: Optional[Sequence[str]] = None,
) -> tuple[re.Pattern[str], ...]:
    raw = (
        tuple(redact_patterns)
        if redact_patterns is not None
        else tuple(get_telemetry_config().redact_patterns)
    )
    return _compile_patterns(raw)


@overload
def sanitize_text(
    text: str,
    *,
    redact_patterns: Optional[Sequence[str]] = None,
) -> str: ...


@overload
def sanitize_text(
    text: None,
    *,
    redact_patterns: Optional[Sequence[str]] = None,
) -> None: ...


def sanitize_text(
    text: Optional[str],
    *,
    redact_patterns: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Redact configured secret regex patterns from a text string."""
    if text is None:
        return None
    if not text:
        return text

    result = text
    for pattern in _resolve_patterns(redact_patterns):
        result = pattern.sub(REDACTED_VALUE, result)
    return result


def _sanitize_value(
    value: Any,
    patterns: tuple[re.Pattern[str], ...],
    seen: set[int],
) -> Any:
    """Recursively sanitize and normalize arbitrary Python values for JSONB."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        result = value
        for pattern in patterns:
            result = pattern.sub(REDACTED_VALUE, result)
        return result
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        return _sanitize_value(decoded, patterns, seen)
    if isinstance(value, BaseModel):
        return _sanitize_value(value.model_dump(mode="json"), patterns, seen)

    obj_id = id(value)
    if obj_id in seen:
        return "[CIRCULAR_REF]"

    if isinstance(value, dict):
        seen.add(obj_id)
        try:
            sanitized_dict: dict[str, Any] = {}
            for k, v in value.items():
                key_str = _sanitize_value(str(k), patterns, seen)
                sanitized_dict[key_str] = _sanitize_value(v, patterns, seen)
            return sanitized_dict
        finally:
            seen.remove(obj_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(obj_id)
        try:
            return [_sanitize_value(item, patterns, seen) for item in value]
        finally:
            seen.remove(obj_id)

    return _sanitize_value(str(value), patterns, seen)


def sanitize_payload(
    payload: Any,
    *,
    redact_patterns: Optional[Sequence[str]] = None,
) -> Any:
    """Recursively redact sensitive regex patterns and normalize for JSON serialization."""
    patterns = _resolve_patterns(redact_patterns)
    return _sanitize_value(payload, patterns, set())


def truncate_stack_trace(
    trace: Optional[str],
    *,
    max_bytes: Optional[int] = None,
    redact_patterns: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Sanitize and cap stack trace UTF-8 byte length at stack_trace_max_bytes."""
    if trace is None:
        return None

    sanitized = sanitize_text(trace, redact_patterns=redact_patterns)
    limit = (
        max_bytes
        if max_bytes is not None
        else get_telemetry_config().stack_trace_max_bytes
    )
    if limit <= 0:
        return ""

    encoded = sanitized.encode("utf-8")
    if len(encoded) <= limit:
        return sanitized

    marker_bytes = len(_TRUNCATION_MARKER.encode("utf-8"))
    if limit <= marker_bytes:
        return encoded[:limit].decode("utf-8", errors="ignore")

    prefix = encoded[: limit - marker_bytes].decode("utf-8", errors="ignore")
    return prefix + _TRUNCATION_MARKER


def _json_byte_length(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def guard_inline_jsonb(
    payload: Any,
    *,
    max_bytes: Optional[int] = None,
    redact_patterns: Optional[Sequence[str]] = None,
    artifact_id: Optional[str] = None,
    storage_uri: Optional[str] = None,
) -> Any:
    """Sanitize a payload and enforce telemetry.inline_payload_max_bytes.

    Payloads whose compact UTF-8 JSON representation exceeds the limit are replaced
    with a bounded summary notice directing callers to `agent_metadata.artifact_registry`.
    """
    if payload is None:
        return None

    sanitized = sanitize_payload(payload, redact_patterns=redact_patterns)
    limit = (
        max_bytes
        if max_bytes is not None
        else get_telemetry_config().inline_payload_max_bytes
    )
    if limit <= 0:
        return {}

    byte_size = _json_byte_length(sanitized)
    if byte_size <= limit:
        return sanitized

    top_keys: Optional[list[str]] = None
    if isinstance(sanitized, dict):
        top_keys = [str(k)[:64] for k in list(sanitized.keys())[:20]]

    tier1_artifact_table = f"{get_schema_names().tier1}.artifact_registry"
    notice: dict[str, Any] = {
        "_truncated": True,
        "_reason": "inline_payload_max_bytes_exceeded",
        "original_byte_size": byte_size,
        "max_inline_bytes": limit,
        "storage_hint": (
            f"Payload exceeded inline JSONB limit; persist full payload to "
            f"{tier1_artifact_table} and reference artifact_id/storage_uri."
        ),
        "artifact_registry_ref": {
            "table": tier1_artifact_table,
            "artifact_id": sanitize_text(artifact_id, redact_patterns=redact_patterns),
            "storage_uri": sanitize_text(storage_uri, redact_patterns=redact_patterns),
        },
    }
    if top_keys is not None:
        notice["top_level_keys"] = top_keys

    if _json_byte_length(notice) <= limit:
        return notice

    notice.pop("top_level_keys", None)
    if _json_byte_length(notice) <= limit:
        return notice

    compact_notice: dict[str, Any] = {
        "_truncated": True,
        "artifact_registry": tier1_artifact_table,
        "original_byte_size": byte_size,
    }
    if _json_byte_length(compact_notice) <= limit:
        return compact_notice

    minimal: dict[str, Any] = {"_truncated": True}
    return minimal if _json_byte_length(minimal) <= limit else {}


def to_jsonb(
    payload: Any,
    *,
    guard_size: bool = True,
    max_bytes: Optional[int] = None,
    redact_patterns: Optional[Sequence[str]] = None,
    artifact_id: Optional[str] = None,
    storage_uri: Optional[str] = None,
) -> Optional[Jsonb]:
    """Sanitize (and optionally size-guard) a Python object into a psycopg Jsonb wrapper."""
    if payload is None:
        return None
    processed = (
        guard_inline_jsonb(
            payload,
            max_bytes=max_bytes,
            redact_patterns=redact_patterns,
            artifact_id=artifact_id,
            storage_uri=storage_uri,
        )
        if guard_size
        else sanitize_payload(payload, redact_patterns=redact_patterns)
    )
    return Jsonb(processed)
