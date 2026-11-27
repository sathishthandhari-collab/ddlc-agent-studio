"""LiteLLM asynchronous embedding client with batching and exponential backoff."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Optional

from tenacity import (
    AsyncRetrying,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.settings import get_env_settings, get_studio_config


class EmbeddingConfigError(RuntimeError):
    """Raised immediately when embedding provider credentials or config are invalid."""


_PROVIDER_CREDENTIAL_SPECS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "openai": (("OPENAI_API_KEY",), "openai_api_key", "OPENAI_API_KEY"),
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key", "GEMINI_API_KEY"),
    "google": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key", "GEMINI_API_KEY"),
    "openrouter": (
        ("OPENROUTER_API_KEY", "OR_API_KEY"),
        "openrouter_api_key",
        "OPENROUTER_API_KEY",
    ),
}


def _import_litellm() -> Any:
    """Lazy-import LiteLLM with offline-safe local cost-map resolution."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    return litellm


class LiteLLMEmbedder:
    """Batching LiteLLM embedding client implementing the BaseEmbedder protocol."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        dimensions: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_retries: Optional[int] = None,
        provider: Optional[str] = None,
    ) -> None:
        needs_cfg = any(
            val is None for val in (model_name, dimensions, batch_size, max_retries)
        ) or (provider is None and "/" not in (model_name or ""))
        cfg = get_studio_config().rag.embedding if needs_cfg else None

        self.model_name: str = model_name if model_name is not None else cfg.model  # type: ignore[union-attr]
        self.dimensions: int = int(
            dimensions if dimensions is not None else cfg.dimensions  # type: ignore[union-attr]
        )
        self.batch_size: int = int(
            batch_size if batch_size is not None else cfg.batch_size  # type: ignore[union-attr]
        )
        self.max_retries: int = int(
            max_retries if max_retries is not None else cfg.max_retries  # type: ignore[union-attr]
        )
        if provider is not None:
            self.provider: str = provider
        elif "/" in self.model_name:
            self.provider = self.model_name.split("/", 1)[0]
        else:
            self.provider = cfg.provider  # type: ignore[union-attr]

        if self.dimensions <= 0:
            raise ValueError(f"dimensions must be positive, got {self.dimensions}.")
        if self.batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {self.batch_size}.")
        if self.max_retries <= 0:
            raise ValueError(f"max_retries must be positive, got {self.max_retries}.")

        if self.provider.strip().lower() == "openrouter" and not self.model_name.startswith("openrouter/"):
            self.model_name = f"openrouter/{self.model_name}"

    def _validate_provider_credentials(self) -> None:
        """Fail fast with EmbeddingConfigError when required provider credentials are absent."""
        provider_norm = self.provider.strip().lower()
        spec = _PROVIDER_CREDENTIAL_SPECS.get(provider_norm)
        if spec is None:
            return

        env_vars, settings_attr, target_env = spec
        env_settings = get_env_settings()
        if provider_norm == "openrouter":
            env_settings.export_provider_env_vars(
                keys=("OPENROUTER_API_BASE", "OR_SITE_URL", "OR_APP_NAME")
            )

        if any(os.environ.get(var, "").strip() for var in env_vars):
            return

        settings_key = (getattr(env_settings, settings_attr, None) or "").strip()
        if settings_key:
            os.environ[target_env] = settings_key
            return

        raise EmbeddingConfigError(
            f"{target_env} is not configured for embedding provider '{self.provider}'. "
            f"Set {target_env} in the environment or .env before calling embed_texts()."
        )

    async def _embed_batch(self, litellm_mod: Any, batch: list[str]) -> list[list[float]]:
        """Execute a single LiteLLM embedding request with exponential retry on transient failures."""
        auth_exc_type = getattr(litellm_mod, "AuthenticationError", None)
        non_retryable: tuple[type[BaseException], ...] = (
            (EmbeddingConfigError, auth_exc_type)
            if isinstance(auth_exc_type, type) and issubclass(auth_exc_type, BaseException)
            else (EmbeddingConfigError,)
        )

        response: Any = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_not_exception_type(non_retryable),
            reraise=True,
        ):
            with attempt:
                try:
                    response = await litellm_mod.aembedding(
                        model=self.model_name,
                        input=batch,
                        dimensions=self.dimensions,
                    )
                except non_retryable as exc:
                    if isinstance(exc, EmbeddingConfigError):
                        raise
                    raise EmbeddingConfigError(
                        f"Authentication failed for embedding provider '{self.provider}' "
                        f"and model '{self.model_name}': {exc}"
                    ) from exc

        items = list(response.data)
        if items and isinstance(items[0], dict) and "index" in items[0]:
            items.sort(key=lambda item: item["index"])
        elif items and hasattr(items[0], "index") and getattr(items[0], "index") is not None:
            items.sort(key=lambda item: item.index)

        vectors: list[list[float]] = []
        for item in items:
            raw_vec = item["embedding"] if isinstance(item, dict) else item.embedding
            vectors.append([float(val) for val in raw_vec])

        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Embedding count mismatch: sent {len(batch)} texts, received {len(vectors)} vectors."
            )
        return vectors

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Batch-generate vector embeddings via LiteLLM with retry."""
        if not texts:
            return []

        self._validate_provider_credentials()
        litellm_mod = _import_litellm()

        text_list = list(texts)
        all_vectors: list[list[float]] = []
        for start in range(0, len(text_list), self.batch_size):
            batch = text_list[start : start + self.batch_size]
            batch_vectors = await self._embed_batch(litellm_mod, batch)
            all_vectors.extend(batch_vectors)
        return all_vectors

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single search query."""
        results = await self.embed_texts([query])
        return results[0]
