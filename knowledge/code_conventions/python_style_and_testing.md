# Python 3.12+ Style, Config & Testing Conventions

Authoritative coding standards for the Meta-Agent (`src/`) and generated Layer 2 DDLC agents (`agents/`).

---

## 1. Python Version, Typing & Ruff Lint Rules

All studio and generated code targets **Python 3.12+** (`requires-python = ">=3.12"` in `pyproject.toml`):

- **Modern Typing**: Include `from __future__ import annotations` where forward references appear; leverage Python 3.12 native type parameter syntax (e.g., `def _load_yaml_model[T: BaseModel](path: Path, model_cls: type[T]) -> T`) and standard `pydantic` v2 annotations.
- **UTC Timestamps (Decision 31)**: Never call deprecated `datetime.utcnow()`. Always use timezone-aware UTC timestamps:
  ```python
  from datetime import datetime, timezone
  now = datetime.now(timezone.utc)
  ```
- **Ruff Configuration (`pyproject.toml`)**:
  ```toml
  [tool.ruff]
  target-version = "py312"
  line-length = 100
  src = ["src", "tests", "alembic"]

  [tool.ruff.lint]
  select = ["E", "F", "I", "W"]
  ignore = ["E501"]
  ```
  Run checks locally with `.venv/Scripts/ruff.exe check src tests alembic`.

---

## 2. Lazy Configuration Loading (`get_settings()`)

Never eagerly evaluate `Settings()` or open database connections at module import time when defaults or `.env` files might be absent during zero-infra unit tests (Decision 31):

- **Nested `BaseSettings` (`src/core/config.py`)**: Use `SettingsConfigDict(env_prefix="AGENT_", env_nested_delimiter="__", env_file=".env", extra="ignore")` so `${config.a.b.c}` maps 1:1 to `settings.a.b.c`.
- **Studio YAML Loaders (`src/core/settings.py`)**: Strict YAML models inherit `_StrictYamlModel` (`ConfigDict(extra="forbid")`) so typos in `config/*.yaml` fail fast.
- **`@lru_cache` Factories**: Expose lazy, cacheable getters (`get_env_settings()`, `get_studio_config()`, `get_settings()`) with test-safe defaults:

```python
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class AgentSettings(BaseSettings, FrameworkRuntimeSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )
    ENVIRONMENT: str = "development"

@lru_cache()
def get_settings() -> AgentSettings:
    """Lazy loader supporting isolated zero-infra unit testing."""
    return AgentSettings()
```

---

## 3. Skeleton vs. Muscle Code Boundaries

Generated agent repositories separate compiler-owned orchestration from editable leaf logic:

- **Skeleton Files (`# DO NOT EDIT`)**: `src/models/contracts_pydantic.py`, `src/core/config.py`, `src/core/telemetry.py`, `src/graph.py`, and `tests/contracts/test_contracts.py` are emitted deterministically by the Scaffold Compiler and verified via `ast.parse`. Never hand-edit them.
- **Muscle Files (Editable)**: Custom tools (`src/tools/<mod>.py`), prompt templates (`src/prompts/stage_<id>.jinja2`), and unit tests (`tests/unit/test_<mod>.py`) are bound via `processing.tools[].handler: "tools/<mod>.py::<func>"` with explicit `signature` annotations.
- **Leaf Coder Modes**: Day-0 builds run `create` mode; Day-2 updates run `patch` mode (receiving existing tool and test source code) to preserve manual 2 AM production hotfixes (Decision 25).

---

## 4. Fail-Fast Transactions vs. Passive Telemetry (Decision 30)

Maintain a strict boundary between critical state writes and observability callbacks:

- **Fail-Fast Transactional Writes**: `CatalogClient`, `LineageClient`, `node_deliver_stage`, and RAG Phase 8 ingestion execute inside explicit `with self._db.transaction() as conn:` blocks and raise immediately on validation, Git, or SQL errors.
- **Non-Blocking Telemetry Callbacks**: `MetadataCallbackHandler` sets `raise_error = False`, wraps every LangChain/LangGraph hook (`on_chain_start`, `on_llm_end`, `on_tool_error`) in `try/except`, logs warnings via `_warn()`, and never aborts graph execution.

---

## 5. Zero-Infra Pytest Conventions

Configured in `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = [".", "src"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

- **Zero-Infra Unit Tests**: Unit suites (`tests/unit/`, `tests/metadata/`, `tests/dcm/`) must pass in milliseconds with **no running PostgreSQL instance or LLM API keys**.
- **Test Doubles & Cache Clearing**: Use `tmp_path`, `monkeypatch`, `get_env_settings.cache_clear()` (or `get_settings.cache_clear()`), and lightweight connection/pool stubs (`_RecordingConnection`, `_FakePool`, `_FailingPool`) matching `tests/metadata/test_callbacks.py`.
- **Live DB Integration Gate**: Guard optional live PostgreSQL integration tests with `pytest.skip(...)` when `METADATA_DB_URI` is unset or unreachable (`_open_live_db_or_skip()`).
