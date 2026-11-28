"""Shared LiteLLM + Instructor structured output client for Meta-Agent specialists."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.settings import (
    ModelTierSpec,
    SpecialistModelAssignment,
    get_env_settings,
    get_model_tiers_config,
    get_safety_valves_config,
)


class LLMConfigError(RuntimeError):
    """Raised immediately when required LLM provider credentials or config are missing."""


class SpecialistLLMError(RuntimeError):
    """Raised when structured LLM generation fails after bounded retries."""


@dataclass(frozen=True)
class ResolvedSpecialistModel:
    """Resolved LiteLLM model parameters for a named specialist."""

    specialist_name: str
    tier_name: str
    model: str
    temperature: float
    max_tokens: int
    max_retries: int
    timeout_seconds: int


@runtime_checkable
class StructuredLLMClient(Protocol):
    """Protocol for structured Pydantic extraction used by all specialists."""

    def complete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        """Synchronously generate a validated Pydantic instance from messages."""

    async def acomplete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        """Asynchronously generate a validated Pydantic instance from messages."""


_PROVIDER_CREDENTIAL_SPECS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "openrouter": (
        ("OPENROUTER_API_KEY", "OR_API_KEY"),
        "openrouter_api_key",
        "OPENROUTER_API_KEY",
    ),
    "openai": (("OPENAI_API_KEY",), "openai_api_key", "OPENAI_API_KEY"),
    "anthropic": (("ANTHROPIC_API_KEY",), "anthropic_api_key", "ANTHROPIC_API_KEY"),
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key", "GEMINI_API_KEY"),
    "google": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini_api_key", "GEMINI_API_KEY"),
}


def resolve_specialist_model(specialist_name: str) -> ResolvedSpecialistModel:
    """Resolve model tier, temperature, token limit, and retries from YAML config."""
    tiers_cfg = get_model_tiers_config()
    valves_cfg = get_safety_valves_config()

    assignment: Optional[SpecialistModelAssignment] = tiers_cfg.specialists.get(specialist_name)
    if assignment is None:
        raise LLMConfigError(
            f"Unknown specialist '{specialist_name}' in config/model_tiers.yaml. "
            f"Available: {sorted(tiers_cfg.specialists.keys())}"
        )

    tier_spec: ModelTierSpec = tiers_cfg.tiers[assignment.tier]
    return ResolvedSpecialistModel(
        specialist_name=specialist_name,
        tier_name=assignment.tier,
        model=tier_spec.model,
        temperature=0.0 if tier_spec.temperature is None else float(tier_spec.temperature),
        max_tokens=4096 if tier_spec.max_tokens is None else int(tier_spec.max_tokens),
        max_retries=int(assignment.max_retries),
        timeout_seconds=int(valves_cfg.timeouts.llm_call_seconds),
    )


def _infer_provider(model_slug: str) -> Optional[str]:
    if "/" in model_slug:
        return model_slug.split("/", 1)[0].strip().lower()
    lower = model_slug.lower()
    if lower.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
        return "openai"
    if "claude" in lower:
        return "anthropic"
    if "gemini" in lower:
        return "gemini"
    return None


def ensure_provider_credentials(model_slug: str) -> None:
    """Export .env provider keys and fail fast if the required provider key is absent."""
    provider = _infer_provider(model_slug)
    if provider is None:
        return
    spec = _PROVIDER_CREDENTIAL_SPECS.get(provider)
    if spec is None:
        return

    env_vars, settings_attr, target_env = spec
    env_settings = get_env_settings()
    if provider == "openrouter":
        env_settings.export_provider_env_vars(
            keys=("OPENROUTER_API_BASE", "OR_SITE_URL", "OR_APP_NAME")
        )

    if any(os.environ.get(var, "").strip() for var in env_vars):
        return

    settings_val = (getattr(env_settings, settings_attr, None) or "").strip()
    if settings_val:
        os.environ[target_env] = settings_val
        return

    raise LLMConfigError(
        f"{target_env} is not configured for provider '{provider}' (model '{model_slug}'). "
        f"Set {target_env} in your environment or .env file."
    )


def _import_litellm_and_instructor() -> tuple[Any, Any]:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import instructor
    import litellm

    return litellm, instructor


def _transient_exception_types(litellm_mod: Any) -> tuple[type[BaseException], ...]:
    names = (
        "RateLimitError",
        "Timeout",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
    )
    found: list[type[BaseException]] = []
    for name in names:
        exc_type = getattr(litellm_mod, name, None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            found.append(exc_type)
    return tuple(found)


def _fatal_exception_types(litellm_mod: Any) -> tuple[type[BaseException], ...]:
    names = ("AuthenticationError", "BadRequestError", "PermissionDeniedError")
    found: list[type[BaseException]] = []
    for name in names:
        exc_type = getattr(litellm_mod, name, None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            found.append(exc_type)
    return tuple(found)


class LiteLLMInstructorClient:
    """Lazy, retry-hardened LiteLLM + Instructor structured output client."""

    def __init__(self, *, transport_attempts: int = 3) -> None:
        if transport_attempts < 1:
            raise ValueError("transport_attempts must be >= 1.")
        self.transport_attempts = transport_attempts

    def complete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        resolved = resolve_specialist_model(specialist_name)
        ensure_provider_credentials(resolved.model)
        litellm_mod, instructor_mod = _import_litellm_and_instructor()

        client = instructor_mod.from_litellm(litellm_mod.completion)
        eff_temp = resolved.temperature if temperature is None else float(temperature)
        eff_max_tokens = resolved.max_tokens if max_tokens is None else int(max_tokens)
        eff_retries = resolved.max_retries if max_retries is None else int(max_retries)
        transient = _transient_exception_types(litellm_mod)
        fatal = _fatal_exception_types(litellm_mod)
        msg_list = [dict(m) for m in messages]

        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self.transport_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(transient) if transient else retry_if_exception_type(()),
                reraise=True,
            ):
                with attempt:
                    return client.chat.completions.create(
                        model=resolved.model,
                        response_model=response_model,
                        messages=msg_list,
                        temperature=eff_temp,
                        max_tokens=eff_max_tokens,
                        max_retries=eff_retries,
                        timeout=resolved.timeout_seconds,
                    )
        except LLMConfigError:
            raise
        except fatal as exc:
            raise LLMConfigError(
                f"LLM provider rejected request for specialist '{specialist_name}' "
                f"(model={resolved.model}): {exc}"
            ) from exc
        except Exception as exc:
            raise SpecialistLLMError(
                f"Structured generation failed for specialist '{specialist_name}' "
                f"(model={resolved.model}, response_model={response_model.__name__}): {exc}"
            ) from exc

        raise SpecialistLLMError(f"Unreachable retry exit for specialist '{specialist_name}'.")

    async def acomplete_structured[T: BaseModel](
        self,
        *,
        specialist_name: str,
        response_model: type[T],
        messages: Sequence[Mapping[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        resolved = resolve_specialist_model(specialist_name)
        ensure_provider_credentials(resolved.model)
        litellm_mod, instructor_mod = _import_litellm_and_instructor()

        client = instructor_mod.from_litellm(litellm_mod.acompletion)
        eff_temp = resolved.temperature if temperature is None else float(temperature)
        eff_max_tokens = resolved.max_tokens if max_tokens is None else int(max_tokens)
        eff_retries = resolved.max_retries if max_retries is None else int(max_retries)
        transient = _transient_exception_types(litellm_mod)
        fatal = _fatal_exception_types(litellm_mod)
        msg_list = [dict(m) for m in messages]

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.transport_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(transient) if transient else retry_if_exception_type(()),
                reraise=True,
            ):
                with attempt:
                    return await client.chat.completions.create(
                        model=resolved.model,
                        response_model=response_model,
                        messages=msg_list,
                        temperature=eff_temp,
                        max_tokens=eff_max_tokens,
                        max_retries=eff_retries,
                        timeout=resolved.timeout_seconds,
                    )
        except LLMConfigError:
            raise
        except fatal as exc:
            raise LLMConfigError(
                f"LLM provider rejected request for specialist '{specialist_name}' "
                f"(model={resolved.model}): {exc}"
            ) from exc
        except Exception as exc:
            raise SpecialistLLMError(
                f"Async structured generation failed for specialist '{specialist_name}' "
                f"(model={resolved.model}, response_model={response_model.__name__}): {exc}"
            ) from exc

        raise SpecialistLLMError(f"Unreachable retry exit for specialist '{specialist_name}'.")
