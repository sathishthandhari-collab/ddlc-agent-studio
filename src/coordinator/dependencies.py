"""Dependency container and lazy runtime wiring for the Meta-Agent Coordinator."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
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
from src.specialists.architect import ArchitectSpecialist
from src.specialists.builder import BuilderSpecialist
from src.specialists.documentarian import DocumentarianSpecialist
from src.specialists.researcher import ResearcherSpecialist
from src.specialists.spec_analyst import SpecAnalystSpecialist
from src.specialists.validator import ValidatorSpecialist
from src.state.checkpointer import run_async_selector


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
    embedder: Optional[Any] = None
    git_publisher: Optional[Callable[..., Mapping[str, Any]]] = None

    workspace_root: Optional[Path] = None
    agents_output_dir: Optional[Path] = None
    knowledge_dir: Optional[Path] = None
    staging_root: Optional[Path] = None

    enable_tier2_registration: bool = True
    enable_rag_indexing: bool = True
    enable_git_delivery: bool = True
    enable_tier3_migration: bool = True
    strict_delivery: bool = True
    default_owner: str = "ddlc-studio"

    _lazy_db: Optional[MetadataDatabase] = field(default=None, init=False, repr=False)
    _lazy_catalog: Optional[CatalogClient] = field(default=None, init=False, repr=False)
    _lazy_lineage: Optional[LineageClient] = field(default=None, init=False, repr=False)
    _lazy_tier3: Optional[Tier3SchemaClient] = field(default=None, init=False, repr=False)
    _lazy_embedder: Optional[Any] = field(default=None, init=False, repr=False)

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
            self.researcher = ResearcherSpecialist()
        return self.researcher

    def get_architect(self) -> ArchitectSpecialist:
        if self.architect is None:
            self.architect = ArchitectSpecialist(
                catalog_client=self.get_catalog_client(),
                lineage_client=self.get_lineage_client(),
                embedder=self.embedder,
            )
        return self.architect

    def get_documentarian(self) -> DocumentarianSpecialist:
        if self.documentarian is None:
            self.documentarian = DocumentarianSpecialist()
        return self.documentarian

    def get_builder(self) -> BuilderSpecialist:
        if self.builder is None:
            self.builder = BuilderSpecialist()
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

            self._lazy_embedder = LiteLLMEmbedder()
        return self._lazy_embedder

    def close(self) -> None:
        """Close any lazily opened database connection pools."""
        if self._lazy_db is not None:
            self._lazy_db.close()
            self._lazy_db = None
