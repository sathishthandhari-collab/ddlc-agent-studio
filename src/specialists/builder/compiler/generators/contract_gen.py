"""Deterministic contract context assembler: contracts/*.yaml -> Pydantic v2 models + contract tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")


def to_pascal_case(name: str) -> str:
    """Convert snake_case or hyphenated identifier to PascalCase."""
    parts = [p for p in _NON_ALNUM_RE.split(name.strip()) if p]
    if not parts:
        return "Model"
    return "".join(p[0].upper() + p[1:] for p in parts)


def _scalar_py_type(logical_type: str) -> str:
    mapping = {
        "string": "str",
        "integer": "int",
        "float": "float",
        "boolean": "bool",
        "timestamp": "datetime",
        "datetime": "datetime",
        "date": "date",
        "json": "Dict[str, Any]",
        "binary": "bytes",
        "object": "Dict[str, Any]",
    }
    return mapping.get(logical_type.strip().lower(), "Any")


@dataclass
class CompiledFieldSpec:
    name: str
    annotation: str
    default_expr: str
    field_args: list[str] = field(default_factory=list)
    description: str = ""
    not_empty_validator: bool = False
    sample_valid: Any = "sample"
    sample_invalid_rule: Optional[tuple[str, Any]] = None


@dataclass
class CompiledModelSpec:
    class_name: str
    docstring: str
    contract_name: Optional[str] = None
    contract_version: Optional[str] = None
    storage_mode: str = "inline"
    fields: list[CompiledFieldSpec] = field(default_factory=list)


def _parse_quality_rules(
    raw_rules: list[Any],
    base_type: str,
) -> tuple[Optional[str], list[str], bool, Optional[tuple[str, Any]]]:
    """Translate YAML quality_rules into Literal[], Field constraints, validator flags, and test fixtures."""
    literal_override: Optional[str] = None
    field_args: list[str] = []
    not_empty = False
    invalid_sample: Optional[tuple[str, Any]] = None

    for rule_item in raw_rules or []:
        if isinstance(rule_item, Mapping):
            rule_kind = str(rule_item.get("rule") or "").strip().lower()
            if rule_kind == "allowed_values":
                vals = list(rule_item.get("values") or [])
                if vals:
                    literal_override = f"Literal[{', '.join(repr(v) for v in vals)}]"
                    invalid_sample = ("allowed_values", "__invalid_enum_value__")
            elif rule_kind == "range":
                if rule_item.get("min") is not None:
                    field_args.append(f"ge={rule_item['min']!r}")
                    if isinstance(rule_item["min"], (int, float)):
                        invalid_sample = ("range", rule_item["min"] - 1)
                if rule_item.get("max") is not None:
                    field_args.append(f"le={rule_item['max']!r}")
                    if invalid_sample is None and isinstance(rule_item["max"], (int, float)):
                        invalid_sample = ("range", rule_item["max"] + 1)
            elif rule_kind == "regex":
                pattern = str(rule_item.get("pattern") or "")
                if pattern:
                    field_args.append(f"pattern={pattern!r}")
                    invalid_sample = ("regex", "___INVALID_PATTERN___")
            elif rule_kind == "not_empty":
                if base_type == "str":
                    field_args.append("min_length=1")
                    not_empty = True
                    invalid_sample = ("not_empty", "   ")
        elif isinstance(rule_item, str):
            lower = rule_item.lower()
            if "not be empty" in lower and base_type == "str":
                field_args.append("min_length=1")
                not_empty = True
                invalid_sample = ("not_empty", "   ")
            elif "at least one" in lower and base_type.startswith("List["):
                field_args.append("min_length=1")

    return literal_override, field_args, not_empty, invalid_sample


def _sample_value_for_field(
    field_name: str,
    logical_type: str,
    raw_rules: list[Any],
    nested_model_name: Optional[str] = None,
    submodels_by_name: Optional[Mapping[str, CompiledModelSpec]] = None,
    is_array: bool = False,
    array_elem_type: str = "string",
) -> Any:
    for rule_item in raw_rules or []:
        if isinstance(rule_item, Mapping):
            rk = str(rule_item.get("rule") or "").lower()
            if rk == "allowed_values" and rule_item.get("values"):
                val = list(rule_item["values"])[0]
                return [val] if is_array else val
            if rk == "range":
                mn = rule_item.get("min")
                mx = rule_item.get("max")
                if mn is not None:
                    return mn if logical_type == "integer" else float(mn)
                if mx is not None:
                    return mx if logical_type == "integer" else float(mx)
            if rk == "regex":
                pat = str(rule_item.get("pattern") or "")
                if "[a-f0-9]{64}" in pat:
                    return "a" * 64
                if "[a-f0-9]{40}" in pat:
                    return "b" * 40
                return "sample_match"

    if is_array:
        if nested_model_name and submodels_by_name and nested_model_name in submodels_by_name:
            sub = submodels_by_name[nested_model_name]
            item_dict = {f.name: f.sample_valid for f in sub.fields}
            return [item_dict]
        scalar = _sample_value_for_field(field_name, array_elem_type, [])
        return [scalar]

    if nested_model_name and submodels_by_name and nested_model_name in submodels_by_name:
        sub = submodels_by_name[nested_model_name]
        return {f.name: f.sample_valid for f in sub.fields}

    lt = logical_type.strip().lower()
    if lt == "integer":
        return 1
    if lt == "float":
        return 1.0
    if lt == "boolean":
        return True
    if lt in {"timestamp", "datetime"}:
        return "2026-01-01T00:00:00+00:00"
    if lt == "date":
        return "2026-01-01"
    if lt == "json":
        return {"key": "value"}
    if lt == "binary":
        return b"sample"
    if lt == "object":
        return {"key": "value"}
    if "sha256" in field_name:
        return "a" * 64
    return f"sample_{field_name}"


def _compile_fields_recursive(
    raw_fields: list[Mapping[str, Any]],
    parent_class: str,
    emitted_submodels: dict[str, CompiledModelSpec],
    ordered_submodels: list[CompiledModelSpec],
) -> list[CompiledFieldSpec]:
    compiled_fields: list[CompiledFieldSpec] = []

    for fld in raw_fields:
        fname = str(fld["name"])
        ftype = str(fld.get("type") or "string").strip().lower()
        nullable = bool(fld.get("nullable", False))
        desc = str(fld.get("description") or "").strip()
        default_val = fld.get("default")
        raw_rules = list(fld.get("quality_rules") or [])
        items_spec = fld.get("items")
        nested_fields = fld.get("fields") or []

        nested_model_name: Optional[str] = None
        is_array = ftype == "array"
        array_elem_type = "string"

        if ftype == "array":
            if isinstance(items_spec, Mapping):
                item_type = str(items_spec.get("type") or "string").strip().lower()
                array_elem_type = item_type
                item_fields = list(items_spec.get("fields") or [])
                if item_type == "object" and item_fields:
                    sub_name = str(
                        items_spec.get("model_name")
                        or f"{parent_class}{to_pascal_case(fname)}Item"
                    )
                    nested_model_name = sub_name
                    if sub_name not in emitted_submodels:
                        sub_compiled = _compile_fields_recursive(
                            item_fields, sub_name, emitted_submodels, ordered_submodels
                        )
                        sub_model = CompiledModelSpec(
                            class_name=sub_name,
                            docstring=str(items_spec.get("description") or f"Nested model {sub_name}."),
                            fields=sub_compiled,
                        )
                        emitted_submodels[sub_name] = sub_model
                        ordered_submodels.append(sub_model)
                    base_py_type = f"List[{sub_name}]"
                else:
                    base_py_type = f"List[{_scalar_py_type(item_type)}]"
            else:
                base_py_type = "List[Any]"
        elif ftype == "object" and nested_fields:
            sub_name = str(
                (items_spec.get("model_name") if isinstance(items_spec, Mapping) else None)
                or fld.get("model_name")
                or f"{parent_class}{to_pascal_case(fname)}"
            )
            nested_model_name = sub_name
            if sub_name not in emitted_submodels:
                sub_compiled = _compile_fields_recursive(
                    list(nested_fields), sub_name, emitted_submodels, ordered_submodels
                )
                sub_model = CompiledModelSpec(
                    class_name=sub_name,
                    docstring=desc or f"Nested model {sub_name}.",
                    fields=sub_compiled,
                )
                emitted_submodels[sub_name] = sub_model
                ordered_submodels.append(sub_model)
            base_py_type = sub_name
        elif ftype == "object" and isinstance(items_spec, Mapping) and items_spec.get("fields"):
            sub_name = str(
                items_spec.get("model_name") or f"{parent_class}{to_pascal_case(fname)}"
            )
            nested_model_name = sub_name
            if sub_name not in emitted_submodels:
                sub_compiled = _compile_fields_recursive(
                    list(items_spec["fields"]), sub_name, emitted_submodels, ordered_submodels
                )
                sub_model = CompiledModelSpec(
                    class_name=sub_name,
                    docstring=str(items_spec.get("description") or desc or f"Nested model {sub_name}."),
                    fields=sub_compiled,
                )
                emitted_submodels[sub_name] = sub_model
                ordered_submodels.append(sub_model)
            base_py_type = sub_name
        else:
            base_py_type = _scalar_py_type(ftype)

        literal_override, field_args, not_empty, invalid_sample = _parse_quality_rules(
            raw_rules, base_py_type
        )
        eff_type = literal_override or base_py_type
        annotation = f"Optional[{eff_type}]" if nullable else eff_type

        if default_val == "${now}":
            default_expr = "default_factory=lambda: datetime.now(timezone.utc)"
        elif default_val == "${today}":
            default_expr = "default_factory=date.today"
        elif default_val is not None:
            default_expr = f"default={default_val!r}"
        elif nullable:
            default_expr = "default=None"
        elif ftype == "array":
            default_expr = "default_factory=list"
        else:
            default_expr = "..."

        sample_valid = _sample_value_for_field(
            fname,
            ftype,
            raw_rules,
            nested_model_name=nested_model_name,
            submodels_by_name=emitted_submodels,
            is_array=is_array,
            array_elem_type=array_elem_type,
        )

        compiled_fields.append(
            CompiledFieldSpec(
                name=fname,
                annotation=annotation,
                default_expr=default_expr,
                field_args=field_args,
                description=desc,
                not_empty_validator=not_empty,
                sample_valid=sample_valid,
                sample_invalid_rule=invalid_sample,
            )
        )

    return compiled_fields


def build_contracts_context(
    contracts: Mapping[str, Mapping[str, Any]],
    stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build deterministic template context for contracts_pydantic.py and test_contracts.py."""
    emitted_submodels: dict[str, CompiledModelSpec] = {}
    ordered_submodels: list[CompiledModelSpec] = []
    contract_models: list[CompiledModelSpec] = []

    for contract_key in sorted(contracts.keys()):
        raw = contracts[contract_key]
        body = raw.get("contract") if isinstance(raw.get("contract"), Mapping) else raw
        cname = str(body.get("name") or contract_key)
        cver = str(body.get("version") or "1.0.0")
        storage_mode = str(body.get("storage_mode") or "inline")
        class_name = to_pascal_case(cname)
        raw_fields = list(body.get("fields") or [])

        compiled_fields = _compile_fields_recursive(
            raw_fields, class_name, emitted_submodels, ordered_submodels
        )
        contract_models.append(
            CompiledModelSpec(
                class_name=class_name,
                docstring=f"Autogenerated Pydantic model for contract: {cname} v{cver}.",
                contract_name=cname,
                contract_version=cver,
                storage_mode=storage_mode,
                fields=compiled_fields,
            )
        )

    include_generated_fileset = False
    for stg in stages.values():
        proc = stg.get("processing") or {}
        llm_cfg = proc.get("llm_config") or {}
        if llm_cfg.get("output_schema") == "GeneratedFileSet":
            include_generated_fileset = True
            break

    return {
        "submodels": ordered_submodels,
        "contract_models": contract_models,
        "include_generated_fileset": include_generated_fileset,
    }
