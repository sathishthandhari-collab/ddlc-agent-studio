"""Deterministic configuration context assembler: config.yaml -> nested Pydantic BaseSettings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from src.specialists.builder.compiler.generators.contract_gen import to_pascal_case

_SECRET_PLACEHOLDER_RE = re.compile(r"\$\{secrets\.([A-Za-z0-9_]+)\}")


@dataclass
class ConfigFieldSpec:
    attr_name: str
    annotation: str
    default_expr: str
    alias: Optional[str] = None


@dataclass
class ConfigModelSpec:
    class_name: str
    fields: list[ConfigFieldSpec] = field(default_factory=list)


def _sanitize_default_value(val: Any) -> Any:
    """Ensure no raw secret placeholders leak into runtime URI/string crashes in offline tests."""
    if isinstance(val, str):
        # Replace ${secrets.foo} with safe placeholder token so default URLs remain syntactically valid
        return _SECRET_PLACEHOLDER_RE.sub(lambda m: f"<{m.group(1)}>", val)
    return val


def _scalar_annotation(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, list):
        return "List[Any]"
    return "str"


def _build_nested_config_models(
    mapping: Mapping[str, Any],
    prefix: str,
    emitted: dict[str, ConfigModelSpec],
    ordered: list[ConfigModelSpec],
) -> list[ConfigFieldSpec]:
    fields: list[ConfigFieldSpec] = []
    for raw_key, raw_val in mapping.items():
        attr_name = "schema_name" if raw_key == "schema" else str(raw_key)
        alias = "schema" if raw_key == "schema" else None

        if isinstance(raw_val, Mapping):
            sub_class_name = f"{to_pascal_case(prefix + '_' + str(raw_key))}Config"
            if sub_class_name not in emitted:
                sub_fields = _build_nested_config_models(
                    raw_val, f"{prefix}_{raw_key}", emitted, ordered
                )
                model_spec = ConfigModelSpec(class_name=sub_class_name, fields=sub_fields)
                emitted[sub_class_name] = model_spec
                ordered.append(model_spec)
            fields.append(
                ConfigFieldSpec(
                    attr_name=attr_name,
                    annotation=sub_class_name,
                    default_expr=f"Field(default_factory={sub_class_name})",
                    alias=alias,
                )
            )
        else:
            sanitized = _sanitize_default_value(raw_val)
            ann = _scalar_annotation(sanitized)
            if isinstance(sanitized, list):
                default_expr = "Field(default_factory=list)"
            elif alias is not None:
                default_expr = f"Field(default={sanitized!r}, alias={alias!r})"
            else:
                default_expr = f"Field(default={sanitized!r})"
            fields.append(
                ConfigFieldSpec(
                    attr_name=attr_name,
                    annotation=ann,
                    default_expr=default_expr,
                    alias=alias,
                )
            )
    return fields


def build_config_context(config_yaml: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble nested BaseSettings context from impl_docs/config.yaml."""
    cfg_body = config_yaml.get("config") if isinstance(config_yaml.get("config"), Mapping) else config_yaml
    defaults = dict(cfg_body.get("defaults") or {})
    secrets = list(cfg_body.get("secrets") or [])

    emitted: dict[str, ConfigModelSpec] = {}
    ordered_models: list[ConfigModelSpec] = []
    top_fields = _build_nested_config_models(defaults, "", emitted, ordered_models)

    secret_fields: list[dict[str, Any]] = []
    for sec in secrets:
        if isinstance(sec, Mapping) and sec.get("name"):
            sec_name = str(sec["name"]).strip()
            env_key = sec_name.upper()
            secret_fields.append(
                {
                    "attr_name": env_key,
                    "env_key": env_key,
                    "description": str(sec.get("description") or ""),
                    "required": bool(sec.get("required", True)),
                }
            )

    return {
        "nested_models": ordered_models,
        "top_fields": top_fields,
        "secret_fields": secret_fields,
        "defaults": defaults,
    }
