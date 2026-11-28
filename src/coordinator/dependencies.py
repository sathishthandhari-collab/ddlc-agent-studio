"""Dependency container and lazy runtime wiring for the Meta-Agent Coordinator."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableLambda

from src.core.settings import (
    DEFAULT_PROJECT_ROOT,
    SafetyValvesConfig,
    StudioConfig,
    get_env_settings,
    get_safety_valves_config,
    get_studio_config,
)
from src.metadata.catalog import CatalogClient
from src.metadata.db import MetadataDatabase
from src.metadata.lineage import LineageClient
from src.metadata.schema_engine import Tier3SchemaClient
from src.rag.interfaces import ArchitectRAGFacade, BuilderRAGFacade, ResearcherRAGFacade
from src.rag.retrieval.hybrid_search import HybridRAGRetriever
from src.specialists.architect import ArchitectSpecialist
from src.specialists.builder import BuilderSpecialist
from src.specialists.documentarian import DocumentarianSpecialist
from src.specialists.researcher import ResearcherSpecialist
from src.specialists.spec_analyst import SpecAnalystSpecialist
from src.specialists.validator import ValidatorSpecialist
from src.state.checkpointer import run_async_selector


class _LoopAwareRAGPool:
    """Lazy asyncpg pool wrapper safe across both long-lived async loops and sync bridge loops."""

    def __init__(self, db_uri: str) -> None:
        self._db_uri = db_uri
        self._pool: Optional[Any] = None
        self._pool_loop: Optional[Any] = None

    @staticmethod
    def _normalize_dsn(uri: str) -> str:
        if uri.startswith("postgresql+asyncpg://"):
            return uri.replace("postgresql+asyncpg://", "postgresql://", 1)
        if uri.startswith("postgresql+psycopg://"):
            return uri.replace("postgresql+psycopg://", "postgresql://", 1)
        return uri

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        import asyncio

        import asyncpg
        from pgvector.asyncpg import register_vector

        from src.rag.store.pgvector_store import create_rag_pool

        running_loop = asyncio.get_running_loop()
        if self._pool is None and not running_loop.is_closed():
            self._pool = await create_rag_pool(db_uri=self._db_uri)
            self._pool_loop = running_loop

        if self._pool is not None and self._pool_loop is running_loop and not getattr(self._pool, "_closed", False):
            async with self._pool.acquire() as conn:
                yield conn
            return

        # Sync callers via run_coro_sync() spawn a temporary loop; avoid cross-loop asyncpg pool errors.
        conn = await asyncpg.connect(dsn=self._normalize_dsn(self._db_uri), init=register_vector)
        try:
            yield conn
        finally:
            await conn.close()

    async def aclose(self) -> None:
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            self._pool_loop = None
            await pool.close()


def _has_embedding_credentials() -> bool:
    """Return True if credentials exist for the configured RAG embedding provider."""
    import os

    try:
        emb_cfg = get_studio_config().rag.embedding
        env = get_env_settings()
    except Exception:
        return False

    model = emb_cfg.model
    provider = model.split("/", 1)[0].strip().lower() if "/" in model else emb_cfg.provider.strip().lower()
    checks: dict[str, tuple[tuple[str, ...], str]] = {
        "openrouter": (("OPENROUTER_API_KEY", "OR_API_KEY"), "openrouter_api_key"),
        "openai": (("OPENAI_API_KEY",), "openai_api_key"),
        "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key"),
        "google": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key"),
    }
    spec = checks.get(provider)
    if spec is None:
        return True
    env_vars, settings_attr = spec
    if any(os.environ.get(var, "").strip() for var in env_vars):
        return True
    return bool((getattr(env, settings_attr, None) or "").strip())


def run_coro_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Execute a coroutine synchronously, offloading to a worker thread if a loop is active."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_async_selector(coro)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run_async_selector, coro).result()


def make_dual_node(
    sync_fn: Callable[[Any], dict[str, Any]],
    async_fn: Optional[Callable[[Any], Coroutine[Any, Any, dict[str, Any]]]] = None,
) -> Any:
    """Create a LangGraph node runnable supporting both sync invoke() and async ainvoke()."""
    if async_fn is None:
        return sync_fn
    return RunnableLambda(sync_fn, afunc=async_fn)


@dataclass
class CoordinatorDeps:
    """Injectable dependency container for Coordinator subgraphs, specialists, and Phase 8 Delivery."""

    spec_analyst: Optional[SpecAnalystSpecialist] = None
    researcher: Optional[ResearcherSpecialist] = None
    architect: Optional[ArchitectSpecialist] = None
    documentarian: Optional[DocumentarianSpecialist] = None
    builder: Optional[BuilderSpecialist] = None
    validator: Optional[ValidatorSpecialist] = None

    safety_valves: Optional[SafetyValvesConfig] = None
    studio_config: Optional[StudioConfig] = None

    metadata_db: Optional[MetadataDatabase] = None
    catalog_client: Optional[Any] = None
    lineage_client: Optional[Any] = None
    tier3_client: Optional[Any] = None
    rag_store: Optional[Any] = None
    rag_retriever: Optional[Any] = None
    embedder: Optional[Any] = None
    telemetry_handler: Optional[Any] = None
    git_publisher: Optional[Callable[..., Mapping[str, Any]]] = None

    workspace_root: Optional[Path] = None
    agents_output_dir: Optional[Path] = None
    knowledge_dir: Optional[Path] = None
    staging_root: Optional[Path] = None

    enable_tier2_registration: bool = True
    enable_rag_indexing: bool = True
    enable_rag_retrieval: bool = True
    enable_git_delivery: bool = True
    enable_tier3_migration: bool = True
    strict_delivery: bool = True
    default_owner: str = "ddlc-studio"

    _lazy_db: Optional[MetadataDatabase] = field(default=None, init=False, repr=False)
    _lazy_catalog: Optional[CatalogClient] = field(default=None, init=False, repr=False)
    _lazy_lineage: Optional[LineageClient] = field(default=None, init=False, repr=False)
    _lazy_tier3: Optional[Tier3SchemaClient] = field(default=None, init=False, repr=False)
    _lazy_embedder: Optional[Any] = field(default=None, init=False, repr=False)
    _lazy_rag_pool: Optional[_LoopAwareRAGPool] = field(default=None, init=False, repr=False)
    _lazy_rag_retriever: Optional[HybridRAGRetriever] = field(default=None, init=False, repr=False)

    def get_safety_valves(self) -> SafetyValvesConfig:
        if self.safety_valves is not None:
            return self.safety_valves
        return get_safety_valves_config()

    def get_studio_config(self) -> StudioConfig:
        if self.studio_config is not None:
            return self.studio_config
        return get_studio_config()

    def resolve_workspace_root(self) -> Path:
        return Path(self.workspace_root).resolve() if self.workspace_root else DEFAULT_PROJECT_ROOT

    def resolve_agents_output_dir(self) -> Path:
        if self.agents_output_dir is not None:
            return Path(self.agents_output_dir).resolve()
        rel = self.get_studio_config().app.paths.agents_output_dir
        return (self.resolve_workspace_root() / rel).resolve()

    def resolve_knowledge_dir(self) -> Path:
        if self.knowledge_dir is not None:
            return Path(self.knowledge_dir).resolve()
        rel = self.get_studio_config().app.paths.knowledge_dir
        return (self.resolve_workspace_root() / rel).resolve()

    def resolve_staging_dir(self, run_id: str) -> Path:
        safe_id = str(run_id).strip().replace("/", "_").replace("\\", "_") or "run"
        if self.staging_root is not None:
            return (Path(self.staging_root) / safe_id).resolve()
        rel_sandbox = self.get_studio_config().app.paths.sandbox_dir
        return (self.resolve_workspace_root() / rel_sandbox / "runs" / safe_id).resolve()

    def get_spec_analyst(self) -> SpecAnalystSpecialist:
        if self.spec_analyst is None:
            self.spec_analyst = SpecAnalystSpecialist()
        return self.spec_analyst

    def get_researcher(self) -> ResearcherSpecialist:
        if self.researcher is None:
            rag_facade = self.get_researcher_rag()
            self.researcher = ResearcherSpecialist(
                rag_facade=rag_facade,
                catalog_client=self.get_catalog_client(),
                embedder=self.embedder or (self.get_embedder() if rag_facade is not None else None),
            )
        return self.researcher

    def get_architect(self) -> ArchitectSpecialist:
        if self.architect is None:
            rag_facade = self.get_architect_rag()
            self.architect = ArchitectSpecialist(
                rag_facade=rag_facade,
                catalog_client=self.get_catalog_client(),
                lineage_client=self.get_lineage_client(),
                embedder=self.embedder or (self.get_embedder() if rag_facade is not None else None),
            )
        return self.architect

    def get_documentarian(self) -> DocumentarianSpecialist:
        if self.documentarian is None:
            self.documentarian = DocumentarianSpecialist()
        return self.documentarian

    def get_builder(self) -> BuilderSpecialist:
        if self.builder is None:
            self.builder = BuilderSpecialist(rag_facade=self.get_builder_rag())
        return self.builder

    def get_validator(self) -> ValidatorSpecialist:
        if self.validator is None:
            self.validator = ValidatorSpecialist()
        return self.validator

    def get_metadata_db(self) -> Optional[MetadataDatabase]:
        if self.metadata_db is not None:
            return self.metadata_db
        if self._lazy_db is not None:
            return self._lazy_db
        env = get_env_settings()
        uri = env.metadata_db_uri or env.rag_db_uri
        if not uri:
            return None
        self._lazy_db = MetadataDatabase(db_uri=uri)
        return self._lazy_db

    def get_catalog_client(self) -> Optional[Any]:
        if self.catalog_client is not None:
            return self.catalog_client
        if self._lazy_catalog is not None:
            return self._lazy_catalog
        db = self.get_metadata_db()
        if db is None:
            return None
        self._lazy_catalog = CatalogClient(db=db)
        return self._lazy_catalog

    def get_lineage_client(self) -> Optional[Any]:
        if self.lineage_client is not None:
            return self.lineage_client
        if self._lazy_lineage is not None:
            return self._lazy_lineage
        db = self.get_metadata_db()
        if db is None:
            return None
        self._lazy_lineage = LineageClient(db=db)
        return self._lazy_lineage

    def get_tier3_client(self) -> Optional[Any]:
        if self.tier3_client is not None:
            return self.tier3_client
        if self._lazy_tier3 is not None:
            return self._lazy_tier3
        env = get_env_settings()
        if not env.dcm_migration_db_uri:
            return None
        self._lazy_tier3 = Tier3SchemaClient(
            db_uri=env.dcm_migration_db_uri,
            project_root=self.resolve_workspace_root(),
        )
        return self._lazy_tier3

    def get_embedder(self) -> Any:
        if self.embedder is not None:
            return self.embedder
        if self._lazy_embedder is None:
            from src.rag.embeddings.embedder import LiteLLMEmbedder

            get_env_settings().export_provider_env_vars()
            self._lazy_embedder = LiteLLMEmbedder()
        return self._lazy_embedder

    def get_rag_retriever(self) -> Optional[HybridRAGRetriever]:
        """Return a configured HybridRAGRetriever when RAG DB and embedding credentials exist."""
        if not self.enable_rag_retrieval:
            return None
        if self.rag_retriever is not None:
            return self.rag_retriever
        if self._lazy_rag_retriever is not None:
            return self._lazy_rag_retriever

        env = get_env_settings()
        env.export_provider_env_vars()
        uri = env.rag_db_uri or env.metadata_db_uri
        if not uri:
            return None
        if self.embedder is None and not _has_embedding_credentials():
            return None

        rag_cfg = self.get_studio_config().rag
        pool = _LoopAwareRAGPool(uri)
        self._lazy_rag_pool = pool
        self._lazy_rag_retriever = HybridRAGRetriever(
            db_pool=pool,
            embedder=self.get_embedder(),
            rrf_k=rag_cfg.retrieval.rrf_k,
            initial_candidates=rag_cfg.retrieval.initial_candidates,
            schema_name=rag_cfg.database.schema_name,
            telemetry_handler=self.telemetry_handler,
        )
        return self._lazy_rag_retriever

    def get_researcher_rag(self) -> Optional[ResearcherRAGFacade]:
        retriever = self.get_rag_retriever()
        return ResearcherRAGFacade(retriever) if retriever is not None else None

    def get_architect_rag(self) -> Optional[ArchitectRAGFacade]:
        retriever = self.get_rag_retriever()
        return ArchitectRAGFacade(retriever) if retriever is not None else None

    def get_builder_rag(self) -> Optional[BuilderRAGFacade]:
        retriever = self.get_rag_retriever()
        return BuilderRAGFacade(retriever) if retriever is not None else None

    def close(self) -> None:
        """Close any lazily opened database connection pools."""
        if self._lazy_db is not None:
            self._lazy_db.close()
            self._lazy_db = None
        if self._lazy_rag_pool is not None:
            pool = self._lazy_rag_pool
            self._lazy_rag_pool = None
            try:
                run_coro_sync(pool.aclose())
            except Exception:
                pass
        self._lazy_rag_retriever = None

    async def aclose(self) -> None:
        """Asynchronously close lazily opened RAG and metadata pools."""
        if self._lazy_rag_pool is not None:
            pool = self._lazy_rag_pool
            self._lazy_rag_pool = None
            await pool.aclose()
        self._lazy_rag_retriever = None
        if self._lazy_db is not None:
            self._lazy_db.close()
            self._lazy_db = None
