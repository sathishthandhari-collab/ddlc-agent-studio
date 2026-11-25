"""Unit tests for DDLC Agent Studio YAML configuration and lazy env settings."""

from pathlib import Path

from src.core.settings import (
    RuntimeEnvSettings,
    get_env_settings,
    get_model_tiers_config,
    get_safety_valves_config,
    get_studio_config,
)


def test_studio_config_loads_and_validates() -> None:
    config = get_studio_config()
    assert config.app.name == "ddlc-agent-studio"
    assert config.database.schemas.tier1 == "agent_metadata"
    assert config.database.schemas.tier2 == "ddlc_shared_metadata"
    assert config.database.schemas.rag == "rag_knowledge"
    assert config.rag.embedding.dimensions == 1536
    assert config.rag.retrieval.rrf_k == 60
    assert config.rag.budgets["architect"].max_tokens == 1400


def test_model_tiers_config_references_valid_tiers() -> None:
    config = get_model_tiers_config()
    for specialist_name, assignment in config.specialists.items():
        assert assignment.tier in config.tiers, (
            f"Specialist '{specialist_name}' references unknown tier '{assignment.tier}'"
        )


def test_safety_valves_config_has_positive_bounds() -> None:
    config = get_safety_valves_config()
    assert config.loops.max_clarification_rounds >= 1
    assert config.loops.max_architecture_revisions >= 1
    assert config.loops.max_impl_doc_revisions >= 1
    assert config.loops.max_build_fix_attempts >= 1
    assert config.timeouts.rag_rerank_ms == 150


def test_env_settings_lazy_defaults_without_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_env_settings.cache_clear()
    settings = RuntimeEnvSettings()
    assert settings.ddlc_env == "development"
    assert settings.metadata_db_uri is None
