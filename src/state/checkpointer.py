"""PostgreSQL LangGraph checkpointer lifecycle, schema verification, and thread recovery."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import weakref
from collections.abc import AsyncIterator, Collection, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from psycopg import AsyncConnection, Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from pydantic import BaseModel, Field

from src.core.settings import DatabasePoolConfig, get_env_settings, get_studio_config
from src.state.blackboard import BLACKBOARD_MSGPACK_ALLOWLIST, verify_state_pointers

logger = logging.getLogger(__name__)

CHECKPOINTER_TABLES: tuple[str, ...] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


class CheckpointerConfigError(ValueError):
    """Raised when CHECKPOINTER_DB_URI or required pool config is missing or invalid."""


class CheckpointerSetupError(RuntimeError):
    """Raised when checkpoint tables are missing and cannot be initialized."""


class CheckpointerSchemaStatus(BaseModel):
    """Readiness and migration state of PostgreSQL LangGraph checkpoint tables."""

    ready: bool
    current_version: int
    expected_version: int
    existing_tables: list[str] = Field(default_factory=list)
    missing_tables: list[str] = Field(default_factory=list)


class CheckpointSummary(BaseModel):
    """Lightweight summary entry for thread checkpoint history inspection."""

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: str
    parent_checkpoint_id: Optional[str] = None
    run_id: Optional[str] = None
    current_phase: Optional[str] = None
    mode: Optional[str] = None
    step: Optional[int] = None
    source: Optional[str] = None
    has_pending_writes: bool = False
    pending_tasks: list[str] = Field(default_factory=list)
    next_nodes: list[str] = Field(default_factory=list)
    timestamp: Optional[str] = None


class ThreadRecoverySnapshot(BaseModel):
    """Recovered Blackboard snapshot and interrupt state for thread resumption."""

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: str
    parent_checkpoint_id: Optional[str] = None
    state: dict[str, Any] = Field(default_factory=dict)
    next_nodes: list[str] = Field(default_factory=list)
    pending_interrupts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    pointer_integrity: dict[str, bool] = Field(default_factory=dict)
    resumable: bool = False


def normalize_checkpointer_uri(uri: str) -> str:
    """Normalize SQLAlchemy-style PostgreSQL URIs to standard libpq ``postgresql://``."""
    trimmed = uri.strip()
    if trimmed.startswith("postgresql+psycopg://"):
        return trimmed.replace("postgresql+psycopg://", "postgresql://", 1)
    if trimmed.startswith("postgresql+asyncpg://"):
        return trimmed.replace("postgresql+asyncpg://", "postgresql://", 1)
    if trimmed.startswith("postgres://"):
        return trimmed.replace("postgres://", "postgresql://", 1)
    return trimmed


def resolve_checkpointer_db_uri(
    db_uri: Optional[str] = None,
    *,
    admin: bool = False,
) -> str:
    """Resolve the checkpointer PostgreSQL URI lazily from explicit arg or ``.env``."""
    if db_uri is not None and db_uri.strip():
        return normalize_checkpointer_uri(db_uri)

    env = get_env_settings()
    if admin:
        uri = env.dcm_migration_db_uri or env.checkpointer_db_uri or env.metadata_db_uri
        if not uri:
            raise CheckpointerConfigError(
                "Admin or runtime checkpointer URI is required "
                "(set DCM_MIGRATION_DB_URI or CHECKPOINTER_DB_URI)."
            )
        return normalize_checkpointer_uri(uri)

    uri = env.checkpointer_db_uri or env.metadata_db_uri
    if not uri:
        raise CheckpointerConfigError(
            "CHECKPOINTER_DB_URI is required (pass db_uri or set CHECKPOINTER_DB_URI in .env)."
        )
    return normalize_checkpointer_uri(uri)


def expected_migration_version() -> int:
    """Return the latest migration index expected by installed ``PostgresSaver``."""
    return len(PostgresSaver.MIGRATIONS) - 1


def create_blackboard_serializer(
    *,
    extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
    pickle_fallback: bool = False,
) -> JsonPlusSerializer:
    """Create a ``JsonPlusSerializer`` explicitly allowlisting Blackboard Pydantic models."""
    combined: list[tuple[str, ...]] = list(BLACKBOARD_MSGPACK_ALLOWLIST)
    if extra_allowlist:
        for item in extra_allowlist:
            tup = tuple(item)
            if tup not in combined:
                combined.append(tup)
    return JsonPlusSerializer(
        pickle_fallback=pickle_fallback,
        allowed_msgpack_modules=combined,
    )


def apply_allowlist_to_checkpointer[S: BaseCheckpointSaver[Any]](
    checkpointer: S,
    extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
) -> S:
    """Attach the Blackboard msgpack allowlist to an existing checkpointer instance."""
    combined: list[tuple[str, ...]] = list(BLACKBOARD_MSGPACK_ALLOWLIST)
    if extra_allowlist:
        for item in extra_allowlist:
            tup = tuple(item)
            if tup not in combined:
                combined.append(tup)
    with_allowlist = getattr(checkpointer, "with_allowlist", None)
    if callable(with_allowlist):
        return with_allowlist(combined)
    return checkpointer


def create_memory_checkpointer(
    *,
    extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
    pickle_fallback: bool = False,
) -> InMemorySaver:
    """Create a zero-infra in-memory checkpointer using the exact Blackboard serializer."""
    serde = create_blackboard_serializer(
        extra_allowlist=extra_allowlist,
        pickle_fallback=pickle_fallback,
    )
    return InMemorySaver(serde=serde)


# ---------------------------------------------------------------------------
# Schema Verification & Privilege-Safe Idempotent Setup
# ---------------------------------------------------------------------------


def _inspect_schema_on_connection(conn: Connection[Any]) -> CheckpointerSchemaStatus:
    expected = expected_migration_version()
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%(tables)s)
        """,
        {"tables": list(CHECKPOINTER_TABLES)},
    ).fetchall()

    existing_set: set[str] = set()
    for row in rows:
        name = row["table_name"] if isinstance(row, Mapping) else row[0]
        existing_set.add(str(name))

    existing = [t for t in CHECKPOINTER_TABLES if t in existing_set]
    missing = [t for t in CHECKPOINTER_TABLES if t not in existing_set]

    current_version = -1
    if "checkpoint_migrations" in existing_set:
        ver_row = conn.execute(
            "SELECT COALESCE(MAX(v), -1) AS max_v FROM public.checkpoint_migrations"
        ).fetchone()
        if ver_row is not None:
            current_version = int(
                ver_row["max_v"] if isinstance(ver_row, Mapping) else ver_row[0]
            )

    ready = (not missing) and (current_version >= expected)
    return CheckpointerSchemaStatus(
        ready=ready,
        current_version=current_version,
        expected_version=expected,
        existing_tables=existing,
        missing_tables=missing,
    )


def check_checkpointer_schema(
    db_uri: Optional[str] = None,
    *,
    conn: Optional[Connection[Any]] = None,
) -> CheckpointerSchemaStatus:
    """Verify whether LangGraph checkpoint tables and migrations are present in PostgreSQL.

    Uses read-only ``SELECT`` queries so least-privilege runtime roles (``ddlc_app``)
    can verify readiness without needing DDL ``CREATE`` privileges on ``public``.
    """
    if conn is not None:
        return _inspect_schema_on_connection(conn)

    uri = resolve_checkpointer_db_uri(db_uri, admin=False)
    timeout_s = max(1, int(get_studio_config().database.pool.connection_timeout_seconds))
    with Connection.connect(
        uri,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=timeout_s,
    ) as opened:
        return _inspect_schema_on_connection(opened)


def ensure_checkpointer_setup(
    db_uri: Optional[str] = None,
    *,
    admin_db_uri: Optional[str] = None,
) -> CheckpointerSchemaStatus:
    """Ensure checkpoint tables exist and migrations are current.

    Checks readiness first using ``db_uri`` (read-only); skips DDL completely when
    already up to date so least-privilege runtime roles never fail with
    ``InsufficientPrivilege``. Runs ``PostgresSaver.setup()`` only when needed.
    """
    status = check_checkpointer_schema(db_uri=db_uri)
    if status.ready:
        return status

    setup_uri = (
        normalize_checkpointer_uri(admin_db_uri)
        if admin_db_uri and admin_db_uri.strip()
        else resolve_checkpointer_db_uri(db_uri, admin=True)
    )
    try:
        with PostgresSaver.from_conn_string(setup_uri) as saver:
            saver.setup()
    except InsufficientPrivilege as exc:
        raise CheckpointerSetupError(
            "Checkpoint tables are missing or outdated and the active PostgreSQL role lacks "
            "DDL privileges on schema 'public'. Run setup using DCM_MIGRATION_DB_URI (ddlc_admin)."
        ) from exc

    updated = check_checkpointer_schema(db_uri=setup_uri)
    if not updated.ready:
        raise CheckpointerSetupError(
            f"Checkpointer setup completed but schema remains incomplete: {updated.model_dump()}"
        )
    return updated


# ---------------------------------------------------------------------------
# Pooled Lifecycle Managers (Sync & Async)
# ---------------------------------------------------------------------------


def _close_sync_pool_quietly(pool: ConnectionPool) -> None:
    try:
        if not pool.closed:
            pool.close(timeout=2.0)
    except Exception:
        pass


class PostgresCheckpointerManager:
    """Lazy, thread-safe connection pool manager for sync and async LangGraph savers."""

    def __init__(
        self,
        db_uri: Optional[str] = None,
        *,
        pool_config: Optional[DatabasePoolConfig] = None,
        extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
        pickle_fallback: bool = False,
        sync_pool: Optional[ConnectionPool] = None,
        async_pool: Optional[AsyncConnectionPool] = None,
    ) -> None:
        self._db_uri = db_uri
        self._pool_config = pool_config
        self._extra_allowlist = extra_allowlist
        self._pickle_fallback = pickle_fallback
        self._sync_pool = sync_pool
        self._async_pool = async_pool
        self._owns_sync_pool = sync_pool is None
        self._owns_async_pool = async_pool is None
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._finalizer: Optional[weakref.finalize] = None

    def _resolve_uri(self) -> str:
        return resolve_checkpointer_db_uri(self._db_uri, admin=False)

    def _resolve_pool_config(self) -> DatabasePoolConfig:
        return self._pool_config or get_studio_config().database.pool

    def build_serializer(self) -> JsonPlusSerializer:
        """Return the configured ``JsonPlusSerializer`` for Blackboard state."""
        return create_blackboard_serializer(
            extra_allowlist=self._extra_allowlist,
            pickle_fallback=self._pickle_fallback,
        )

    def _configure_sync_conn(self, conn: Connection[Any]) -> None:
        pool_cfg = self._resolve_pool_config()
        if pool_cfg.statement_timeout_ms > 0:
            conn.execute(f"SET statement_timeout = {int(pool_cfg.statement_timeout_ms)}")

    async def _configure_async_conn(self, conn: AsyncConnection[Any]) -> None:
        pool_cfg = self._resolve_pool_config()
        if pool_cfg.statement_timeout_ms > 0:
            await conn.execute(f"SET statement_timeout = {int(pool_cfg.statement_timeout_ms)}")

    def get_sync_pool(self) -> ConnectionPool:
        """Lazily open and return the synchronous ``ConnectionPool`` for ``PostgresSaver``."""
        if self._sync_pool is not None:
            return self._sync_pool

        with self._lock:
            if self._sync_pool is not None:
                return self._sync_pool

            uri = self._resolve_uri()
            pool_cfg = self._resolve_pool_config()
            pool = ConnectionPool(
                conninfo=uri,
                min_size=pool_cfg.min_size,
                max_size=pool_cfg.max_size,
                timeout=float(pool_cfg.connection_timeout_seconds),
                open=False,
                configure=self._configure_sync_conn,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            pool.open(wait=True, timeout=float(pool_cfg.connection_timeout_seconds))
            self._sync_pool = pool
            self._owns_sync_pool = True
            self._finalizer = weakref.finalize(self, _close_sync_pool_quietly, pool)
            return pool

    async def get_async_pool(self) -> AsyncConnectionPool:
        """Lazily open and return the ``AsyncConnectionPool`` for ``AsyncPostgresSaver``."""
        if self._async_pool is not None:
            return self._async_pool

        if sys.platform == "win32":
            running_loop = asyncio.get_running_loop()
            proactor_cls = getattr(asyncio, "ProactorEventLoop", None)
            if proactor_cls is not None and isinstance(running_loop, proactor_cls):
                raise CheckpointerConfigError(
                    "psycopg async checkpointer requires SelectorEventLoop on Windows, "
                    "not ProactorEventLoop. Run with asyncio.SelectorEventLoop or call "
                    "run_async_selector() / WindowsSelectorEventLoopPolicy."
                )

        if self._async_lock is None:
            self._async_lock = asyncio.Lock()

        async with self._async_lock:
            if self._async_pool is not None:
                return self._async_pool

            uri = self._resolve_uri()
            pool_cfg = self._resolve_pool_config()
            pool = AsyncConnectionPool(
                conninfo=uri,
                min_size=pool_cfg.min_size,
                max_size=pool_cfg.max_size,
                timeout=float(pool_cfg.connection_timeout_seconds),
                open=False,
                configure=self._configure_async_conn,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            await pool.open(wait=True, timeout=float(pool_cfg.connection_timeout_seconds))
            self._async_pool = pool
            self._owns_async_pool = True
            return pool

    def get_sync_saver(
        self,
        *,
        verify_setup: bool = False,
        admin_db_uri: Optional[str] = None,
    ) -> PostgresSaver:
        """Return a pooled ``PostgresSaver`` configured with Blackboard strict serde."""
        if verify_setup:
            ensure_checkpointer_setup(self._db_uri, admin_db_uri=admin_db_uri)
        pool = self.get_sync_pool()
        return PostgresSaver(conn=pool, serde=self.build_serializer())

    async def get_async_saver(
        self,
        *,
        verify_setup: bool = False,
        admin_db_uri: Optional[str] = None,
    ) -> AsyncPostgresSaver:
        """Return a pooled ``AsyncPostgresSaver`` configured with Blackboard strict serde."""
        if verify_setup:
            ensure_checkpointer_setup(self._db_uri, admin_db_uri=admin_db_uri)
        pool = await self.get_async_pool()
        return AsyncPostgresSaver(conn=pool, serde=self.build_serializer())

    def close(self, timeout: float = 5.0) -> None:
        """Close the owned synchronous pool cleanly."""
        with self._lock:
            pool = self._sync_pool
            if pool is not None and self._owns_sync_pool:
                self._sync_pool = None
                if self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = None
                if not pool.closed:
                    pool.close(timeout=timeout)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Close both owned async and sync pools cleanly."""
        async_pool = self._async_pool
        if async_pool is not None and self._owns_async_pool:
            self._async_pool = None
            if not async_pool.closed:
                await async_pool.close(timeout=timeout)
        self.close(timeout=timeout)

    def __enter__(self) -> PostgresCheckpointerManager:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> PostgresCheckpointerManager:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()


@contextmanager
def open_sync_checkpointer(
    db_uri: Optional[str] = None,
    *,
    verify_setup: bool = False,
    admin_db_uri: Optional[str] = None,
    extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
    pickle_fallback: bool = False,
) -> Iterator[PostgresSaver]:
    """Scoped context manager yielding a pooled ``PostgresSaver``."""
    with PostgresCheckpointerManager(
        db_uri=db_uri,
        extra_allowlist=extra_allowlist,
        pickle_fallback=pickle_fallback,
    ) as manager:
        yield manager.get_sync_saver(verify_setup=verify_setup, admin_db_uri=admin_db_uri)


@asynccontextmanager
async def open_async_checkpointer(
    db_uri: Optional[str] = None,
    *,
    verify_setup: bool = False,
    admin_db_uri: Optional[str] = None,
    extra_allowlist: Optional[Collection[tuple[str, ...]]] = None,
    pickle_fallback: bool = False,
) -> AsyncIterator[AsyncPostgresSaver]:
    """Scoped async context manager yielding a pooled ``AsyncPostgresSaver``."""
    async with PostgresCheckpointerManager(
        db_uri=db_uri,
        extra_allowlist=extra_allowlist,
        pickle_fallback=pickle_fallback,
    ) as manager:
        yield await manager.get_async_saver(
            verify_setup=verify_setup,
            admin_db_uri=admin_db_uri,
        )


# ---------------------------------------------------------------------------
# Thread Config, History Inspection & Checkpoint Recovery Helpers
# ---------------------------------------------------------------------------


def make_thread_config(
    thread_id: str,
    *,
    checkpoint_ns: str = "",
    checkpoint_id: Optional[str] = None,
    callbacks: Optional[Sequence[Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a LangGraph ``RunnableConfig`` targeting a checkpointed thread."""
    if not thread_id or not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string.")
    configurable: dict[str, Any] = {"thread_id": thread_id.strip()}
    if checkpoint_ns:
        configurable["checkpoint_ns"] = checkpoint_ns
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id

    config: dict[str, Any] = {"configurable": configurable}
    if callbacks is not None:
        config["callbacks"] = list(callbacks)
    if metadata is not None:
        config["metadata"] = dict(metadata)
    return config


def run_async_selector[T](coro: Coroutine[Any, Any, T]) -> T:
    """Execute an async coroutine using ``SelectorEventLoop`` on Windows (psycopg async safe)."""
    if sys.platform == "win32":
        import selectors

        return asyncio.run(
            coro,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coro)


def make_resume_command(decision_payload: Any) -> Command:
    """Wrap a human gate decision payload in a LangGraph ``Command(resume=...)``."""
    return Command(resume=decision_payload)


def _extract_next_nodes(checkpoint: Mapping[str, Any]) -> list[str]:
    """Infer pending next node names from active ``branch:to:<node>`` channels."""
    channel_values = checkpoint.get("channel_values") or {}
    next_nodes = [
        key.removeprefix("branch:to:")
        for key in channel_values
        if isinstance(key, str) and key.startswith("branch:to:")
    ]
    return sorted(set(next_nodes))


def _extract_blackboard_state(channel_values: Mapping[str, Any]) -> dict[str, Any]:
    """Return user-visible Blackboard keys excluding LangGraph internal routing channels."""
    return {
        k: v
        for k, v in channel_values.items()
        if isinstance(k, str) and not k.startswith("__") and not k.startswith("branch:")
    }


def _serialize_interrupt_item(task_id: str, item: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "id": getattr(item, "id", None),
        "value": getattr(item, "value", item),
    }


def _extract_pending_interrupts(
    pending_writes: Optional[Sequence[tuple[str, str, Any]]],
) -> list[dict[str, Any]]:
    """Extract pending ``__interrupt__`` payloads stored on a paused checkpoint."""
    interrupts: list[dict[str, Any]] = []
    if not pending_writes:
        return interrupts

    for task_id, channel, value in pending_writes:
        if channel != "__interrupt__":
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                interrupts.append(_serialize_interrupt_item(str(task_id), item))
        else:
            interrupts.append(_serialize_interrupt_item(str(task_id), value))
    return interrupts


def _tuple_to_summary(tup: CheckpointTuple) -> CheckpointSummary:
    cfg = tup.config.get("configurable") or {}
    parent_cfg = (tup.parent_config or {}).get("configurable") or {}
    checkpoint = tup.checkpoint or {}
    channel_values = checkpoint.get("channel_values") or {}
    meta = dict(tup.metadata or {})
    pending_writes = tup.pending_writes or []
    pending_tasks = sorted({str(task_id) for task_id, _, _ in pending_writes})

    return CheckpointSummary(
        thread_id=str(cfg.get("thread_id") or ""),
        checkpoint_ns=str(cfg.get("checkpoint_ns") or ""),
        checkpoint_id=str(cfg.get("checkpoint_id") or checkpoint.get("id") or ""),
        parent_checkpoint_id=parent_cfg.get("checkpoint_id"),
        run_id=channel_values.get("run_id"),
        current_phase=channel_values.get("current_phase"),
        mode=channel_values.get("mode"),
        step=meta.get("step"),
        source=meta.get("source"),
        has_pending_writes=bool(pending_writes),
        pending_tasks=pending_tasks,
        next_nodes=_extract_next_nodes(checkpoint),
        timestamp=checkpoint.get("ts"),
    )


def _tuple_to_recovery_snapshot(
    tup: CheckpointTuple,
    *,
    verify_pointers: bool = False,
) -> ThreadRecoverySnapshot:
    cfg = tup.config.get("configurable") or {}
    parent_cfg = (tup.parent_config or {}).get("configurable") or {}
    checkpoint = tup.checkpoint or {}
    raw_channel_values = checkpoint.get("channel_values") or {}
    state = _extract_blackboard_state(raw_channel_values)
    next_nodes = _extract_next_nodes(checkpoint)
    pending_interrupts = _extract_pending_interrupts(tup.pending_writes)
    pointer_integrity = verify_state_pointers(state) if verify_pointers else {}

    return ThreadRecoverySnapshot(
        thread_id=str(cfg.get("thread_id") or ""),
        checkpoint_ns=str(cfg.get("checkpoint_ns") or ""),
        checkpoint_id=str(cfg.get("checkpoint_id") or checkpoint.get("id") or ""),
        parent_checkpoint_id=parent_cfg.get("checkpoint_id"),
        state=state,
        next_nodes=next_nodes,
        pending_interrupts=pending_interrupts,
        metadata=dict(tup.metadata or {}),
        pointer_integrity=pointer_integrity,
        resumable=bool(next_nodes or pending_interrupts),
    )


def get_thread_history(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
    *,
    checkpoint_ns: str = "",
    limit: int = 20,
) -> list[CheckpointSummary]:
    """List checkpoint summaries for ``thread_id`` (most recent first)."""
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    cfg = make_thread_config(thread_id, checkpoint_ns=checkpoint_ns)
    return [_tuple_to_summary(tup) for tup in checkpointer.list(cfg, limit=limit)]


async def aget_thread_history(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
    *,
    checkpoint_ns: str = "",
    limit: int = 20,
) -> list[CheckpointSummary]:
    """Async version of ``get_thread_history``."""
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    cfg = make_thread_config(thread_id, checkpoint_ns=checkpoint_ns)
    summaries: list[CheckpointSummary] = []
    async for tup in checkpointer.alist(cfg, limit=limit):
        summaries.append(_tuple_to_summary(tup))
    return summaries


def recover_thread_state(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
    *,
    checkpoint_id: Optional[str] = None,
    checkpoint_ns: str = "",
    verify_pointers: bool = False,
) -> Optional[ThreadRecoverySnapshot]:
    """Recover the latest (or specific) Blackboard snapshot and interrupts for a thread."""
    cfg = make_thread_config(
        thread_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_id=checkpoint_id,
    )
    tup = checkpointer.get_tuple(cfg)
    if tup is None:
        return None
    return _tuple_to_recovery_snapshot(tup, verify_pointers=verify_pointers)


async def arecover_thread_state(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
    *,
    checkpoint_id: Optional[str] = None,
    checkpoint_ns: str = "",
    verify_pointers: bool = False,
) -> Optional[ThreadRecoverySnapshot]:
    """Async version of ``recover_thread_state``."""
    cfg = make_thread_config(
        thread_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_id=checkpoint_id,
    )
    tup = await checkpointer.aget_tuple(cfg)
    if tup is None:
        return None
    return _tuple_to_recovery_snapshot(tup, verify_pointers=verify_pointers)


def delete_thread_checkpoints(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
) -> None:
    """Delete all stored checkpoints and writes for ``thread_id``."""
    if not thread_id or not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string.")
    checkpointer.delete_thread(thread_id.strip())


async def adelete_thread_checkpoints(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: str,
) -> None:
    """Async version of ``delete_thread_checkpoints``."""
    if not thread_id or not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string.")
    await checkpointer.adelete_thread(thread_id.strip())
