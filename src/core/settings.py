"""Lazy, typed configuration and environment loaders for DDLC Agent Studio."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeEnvSettings(BaseSettings):
    """Environment-backed secrets and database connection URIs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ddlc_env: Literal["development", "staging", "production"] = "development"
    checkpointer_db_uri: Optional[str] = None
    metadata_db_uri: Optional[str] = None
    rag_db_uri: Optional[str] = None
    dcm_migration_db_uri: Optional[str] = None
    dbt_sandbox_dir: str = "./sandbox/dbt_dev"


class AppPathsConfig(BaseModel):
    contracts_dir: str = "contracts"
    knowledge_dir: str = "knowledge"
    agents_output_dir: str = "agents"
    shared_dir: str = "_shared"
    sandbox_dir: str = "sandbox"


class AppConfig(BaseModel):
    name: str
    version: str
    environment: str
    paths: AppPathsConfig = Field(default_factory=AppPathsConfig)


class DatabaseSchemasConfig(BaseModel):
    tier1: str = "agent_metadata"
    tier2: str = "ddlc_shared_metadata"
    rag: str = "rag_knowledge"


class DatabasePoolConfig(BaseModel):
    min_size: int = 2
    max_size: int = 10
    connection_timeout_seconds: int = 15
    statement_timeout_ms: int = 30000


class DatabaseConfig(BaseModel):
    schemas: DatabaseSchemasConfig = Field(default_factory=DatabaseSchemasConfig)
    pool: DatabasePoolConfig = Field(default_factory=DatabasePoolConfig)


class TelemetryConfig(BaseModel):
    enabled: bool = True
    stack_trace_max_bytes: int = 32768
    inline_payload_max_bytes: int = 65536
    redact_patterns: List[str] = Field(default_factory=list)


class RAGDatabaseConfig(BaseModel):
    schema_name: str = "rag_knowledge"
    pool_min_size: int = 2
    pool_max_size: int = 10
    connection_timeout_seconds: int = 15


class RAGEmbeddingConfig(BaseModel):
    provider: str = "openai"
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    batch_size: int = 64
    max_retries: int = 3


class ChunkBoundsConfig(BaseModel):
    target_tokens: int
    max_tokens: int
    overlap_tokens: int = 0


class RAGChunkingConfig(BaseModel):
    markdown: ChunkBoundsConfig
    yaml: ChunkBoundsConfig
    code: ChunkBoundsConfig


class RAGRerankerConfig(BaseModel):
    enabled: bool = True
    engine: str = "flashrank"
    model: str = "ms-marco-TinyBERT-L-2-v2"
    timeout_ms: int = 150
    fallback_to_rrf: bool = True


class RAGRetrievalConfig(BaseModel):
    rrf_k: int = 60
    initial_candidates: int = 20
    reranker: RAGRerankerConfig = Field(default_factory=RAGRerankerConfig)


class SpecialistBudgetConfig(BaseModel):
    top_k: int
    max_tokens: int


class RAGConfig(BaseModel):
    database: RAGDatabaseConfig = Field(default_factory=RAGDatabaseConfig)
    embedding: RAGEmbeddingConfig = Field(default_factory=RAGEmbeddingConfig)
    chunking: RAGChunkingConfig
    retrieval: RAGRetrievalConfig = Field(default_factory=RAGRetrievalConfig)
    budgets: Dict[str, SpecialistBudgetConfig]


class StudioConfig(BaseModel):
    """Typed representation of config/settings.yaml."""

    app: AppConfig
    database: DatabaseConfig
    telemetry: TelemetryConfig
    rag: RAGConfig


class ModelTierSpec(BaseModel):
    description: str = ""
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    dimensions: Optional[int] = None


class SpecialistModelAssignment(BaseModel):
    tier: str
    max_retries: int = 2


class ModelTiersConfig(BaseModel):
    """Typed representation of config/model_tiers.yaml."""

    tiers: Dict[str, ModelTierSpec]
    specialists: Dict[str, SpecialistModelAssignment]


class LoopLimitsConfig(BaseModel):
    max_clarification_rounds: int = 5
    max_architecture_revisions: int = 5
    max_impl_doc_revisions: int = 5
    max_build_fix_attempts: int = 3


class TimeoutsConfig(BaseModel):
    node_execution_seconds: int = 180
    llm_call_seconds: int = 90
    global_run_seconds: int = 1800
    rag_rerank_ms: int = 150


class GatesConfig(BaseModel):
    require_clarification_gate: bool = True
    require_architecture_approval: bool = True
    require_impl_doc_approval: bool = True
    require_destructive_schema_approval: bool = True


class SafetyValvesConfig(BaseModel):
    """Typed representation of config/safety_valves.yaml."""

    loops: LoopLimitsConfig = Field(default_factory=LoopLimitsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)


def _load_yaml_model[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    """Read a YAML file and validate it against a Pydantic model."""
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle) or {}
    return model_cls.model_validate(raw)


@lru_cache(maxsize=1)
def get_env_settings() -> RuntimeEnvSettings:
    """Return cached environment-backed runtime settings."""
    return RuntimeEnvSettings()


@lru_cache(maxsize=4)
def get_studio_config(config_dir: Optional[Path] = None) -> StudioConfig:
    """Load and validate config/settings.yaml."""
    base_dir = config_dir or (DEFAULT_PROJECT_ROOT / "config")
    return _load_yaml_model(base_dir / "settings.yaml", StudioConfig)


@lru_cache(maxsize=4)
def get_model_tiers_config(config_dir: Optional[Path] = None) -> ModelTiersConfig:
    """Load and validate config/model_tiers.yaml."""
    base_dir = config_dir or (DEFAULT_PROJECT_ROOT / "config")
    return _load_yaml_model(base_dir / "model_tiers.yaml", ModelTiersConfig)


@lru_cache(maxsize=4)
def get_safety_valves_config(config_dir: Optional[Path] = None) -> SafetyValvesConfig:
    """Load and validate config/safety_valves.yaml."""
    base_dir = config_dir or (DEFAULT_PROJECT_ROOT / "config")
    return _load_yaml_model(base_dir / "safety_valves.yaml", SafetyValvesConfig)
