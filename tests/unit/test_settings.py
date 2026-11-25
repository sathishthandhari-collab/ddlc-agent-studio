"""Unit tests for DDLC Agent Studio YAML configuration and lazy env settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.settings import (
    ModelTiersConfig,
    RuntimeEnvSettings,
    SafetyValvesConfig,
    StudioConfig,
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


def test_env_settings_lazy_defaults_without_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("METADATA_DB_URI", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    get_env_settings.cache_clear()
    settings = RuntimeEnvSettings(_env_file=tmp_path / ".missing.env")
    assert settings.ddlc_env == "development"
    assert settings.metadata_db_uri is None
    assert settings.openai_api_key is None
    assert settings.github_token is None


def test_env_settings_loads_root_env_from_any_cwd_and_validates_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "DDLC_ENV=staging\nOPENAI_API_KEY=sk-test-123\nGITHUB_TOKEN=ghp_abc\n",
        encoding="utf-8",
    )
    subdir = tmp_path / "nested"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    loaded = RuntimeEnvSettings(_env_file=fake_env)
    assert loaded.ddlc_env == "staging"
    assert loaded.openai_api_key == "sk-test-123"
    assert loaded.github_token == "ghp_abc"

    with pytest.raises(ValidationError):
        RuntimeEnvSettings(_env_file=fake_env, ddlc_env="invalid_env")  # type: ignore[arg-type]


def test_yaml_loaders_reject_missing_files_extra_keys_and_unknown_tiers(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        get_studio_config(config_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        get_model_tiers_config(config_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        get_safety_valves_config(config_dir=tmp_path)

    # Reject unknown keys via extra="forbid"
    with pytest.raises(ValidationError):
        SafetyValvesConfig.model_validate({"unknown_section": {"foo": 1}})
    with pytest.raises(ValidationError):
        StudioConfig.model_validate({"app": {"name": "x", "version": "1", "environment": "dev"}})

    # Reject specialist referencing an undeclared tier
    with pytest.raises(ValidationError, match="unknown tier"):
        ModelTiersConfig.model_validate(
            {
                "tiers": {"fast": {"model": "gpt-4o-mini"}},
                "specialists": {"architect": {"tier": "ultra_heavy"}},
            }
        )
