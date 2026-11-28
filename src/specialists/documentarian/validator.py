"""Deterministic cross-document validator for the 5-file impl_docs/ package (impl_doc_schema.md §10)."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from src.dcm.models import DataContractSchema
from src.metadata.schema_engine import Tier3SchemaClient
from src.specialists.documentarian.models import (
    ALLOWED_BUILTIN_OUTPUT_SCHEMAS,
    AgentManifestDoc,
    ConfigDocument,
    ContractDocument,
    StageDocument,
    TestsDocument,
)
from src.state.blackboard import ImplDocPackage

_CONFIG_REF_RE = re.compile(r"\$\{config\.([A-Za-z0-9_.]+)\}")
_SECRET_REF_RE = re.compile(r"\$\{secrets\.([A-Za-z0-9_]+)\}")


def _deep_merge_dicts(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for k, v in override.items():
        if k in merged and isinstance(merged[k], Mapping) and isinstance(v, Mapping):
            merged[k] = _deep_merge_dicts(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


def _resolve_dotted_path(root: Mapping[str, Any], dotted_path: str) -> tuple[bool, Any]:
    cursor: Any = root
    for part in dotted_path.split("."):
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
        else:
            return False, None
    return True, cursor


def _collect_placeholders(obj: Any, config_refs: set[str], secret_refs: set[str]) -> None:
    if isinstance(obj, str):
        config_refs.update(_CONFIG_REF_RE.findall(obj))
        secret_refs.update(_SECRET_REF_RE.findall(obj))
    elif isinstance(obj, Mapping):
        for v in obj.values():
            _collect_placeholders(v, config_refs, secret_refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_placeholders(item, config_refs, secret_refs)


def validate_impl_doc_package(package: ImplDocPackage) -> list[str]:
    """Validate schema conformance and cross-document referential integrity of an ImplDocPackage.

    Implements impl_doc_schema.md §10.1-10.4 deterministically. Returns an empty list when valid.
    """
    errors: list[str] = []

    # 1. Validate agent.yaml
    agent_doc: AgentManifestDoc | None = None
    try:
        agent_doc = AgentManifestDoc.model_validate(package.agent_yaml)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"agent.yaml ({loc}): {err.get('msg')}")

    # 2. Validate stages/*.yaml
    stage_docs: dict[str, StageDocument] = {}
    for stage_key, raw_stage in package.stages.items():
        try:
            stg_doc = StageDocument.model_validate(raw_stage)
            if stg_doc.stage.id != stage_key:
                errors.append(
                    f"stages[{stage_key!r}]: stage.id '{stg_doc.stage.id}' must match key '{stage_key}'."
                )
            stage_docs[stage_key] = stg_doc
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                errors.append(f"stages/{stage_key}.yaml ({loc}): {err.get('msg')}")

    # 3. Validate contracts/*.yaml
    contract_docs: dict[str, ContractDocument] = {}
    for contract_key, raw_contract in package.contracts.items():
        try:
            ctr_doc = ContractDocument.model_validate(raw_contract)
            if ctr_doc.contract.name != contract_key:
                errors.append(
                    f"contracts[{contract_key!r}]: contract.name '{ctr_doc.contract.name}' "
                    f"must match key '{contract_key}'."
                )
            contract_docs[contract_key] = ctr_doc
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                errors.append(f"contracts/{contract_key}.yaml ({loc}): {err.get('msg')}")

    # 4. Validate config.yaml
    config_doc: ConfigDocument | None = None
    try:
        config_doc = ConfigDocument.model_validate(package.config_yaml)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"config.yaml ({loc}): {err.get('msg')}")

    # 5. Validate tests.yaml
    tests_doc: TestsDocument | None = None
    try:
        tests_doc = TestsDocument.model_validate(package.tests_yaml)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"tests.yaml ({loc}): {err.get('msg')}")

    # 6. Cross-reference integrity (impl_doc_schema.md §10.2)
    if agent_doc is not None:
        pipeline_stage_ids = [edge.stage_id for edge in agent_doc.pipeline.stages]
        pipeline_stage_set = set(pipeline_stage_ids)
        declared_shared = {sc.component_id: sc for sc in agent_doc.dependencies.shared_components}

        for edge in agent_doc.pipeline.stages:
            if edge.stage_id not in package.stages:
                errors.append(
                    f"agent.yaml pipeline references stage '{edge.stage_id}' missing from stages/."
                )
            if edge.next is not None and edge.next not in pipeline_stage_set:
                errors.append(
                    f"agent.yaml pipeline stage '{edge.stage_id}' has invalid next='{edge.next}'."
                )
            if edge.on_error is not None and edge.on_error not in pipeline_stage_set:
                errors.append(
                    f"agent.yaml pipeline stage '{edge.stage_id}' has invalid on_error='{edge.on_error}'."
                )
            if edge.transitions:
                for outcome, target in edge.transitions.items():
                    if target is not None and target not in pipeline_stage_set:
                        errors.append(
                            f"agent.yaml stage '{edge.stage_id}' transition '{outcome}' "
                            f"targets unknown stage '{target}'."
                        )

        for stg_key in package.stages:
            if stg_key not in pipeline_stage_set:
                errors.append(
                    f"Stage '{stg_key}' exists in stages/ but is missing from agent.yaml pipeline.stages."
                )

        for stg_id, stg_doc in stage_docs.items():
            if stg_doc.input.contract and stg_doc.input.contract not in package.contracts:
                errors.append(
                    f"Stage '{stg_id}' input.contract '{stg_doc.input.contract}' not found in contracts/."
                )
            if stg_doc.output.contract and stg_doc.output.contract not in package.contracts:
                errors.append(
                    f"Stage '{stg_id}' output.contract '{stg_doc.output.contract}' not found in contracts/."
                )
            if stg_doc.processing.llm_config is not None:
                llm_cfg = stg_doc.processing.llm_config
                if llm_cfg.output_schema:
                    if (
                        llm_cfg.output_schema not in package.contracts
                        and llm_cfg.output_schema not in ALLOWED_BUILTIN_OUTPUT_SCHEMAS
                    ):
                        errors.append(
                            f"Stage '{stg_id}' llm_config.output_schema '{llm_cfg.output_schema}' "
                            "must match a contract in contracts/ or an allowed wrapper model "
                            f"({sorted(ALLOWED_BUILTIN_OUTPUT_SCHEMAS)})."
                        )
                if not llm_cfg.prompt_template_ref and not llm_cfg.system_prompt:
                    errors.append(
                        f"Stage '{stg_id}' is llm_reasoning and should declare prompt_template_ref or system_prompt."
                    )

            for tool in stg_doc.processing.tools:
                if tool.type == "shared_component" and tool.name not in declared_shared:
                    errors.append(
                        f"Stage '{stg_id}' references shared_component '{tool.name}' "
                        "not declared in agent.yaml dependencies.shared_components."
                    )

        # Optional Tier 3 DCM contract check
        if package.tier3_contract_yaml is not None:
            try:
                t3_schema = DataContractSchema.model_validate(package.tier3_contract_yaml)
                Tier3SchemaClient.validate_schema_name(
                    t3_schema.schema_name,
                    agent_name=agent_doc.agent.name,
                )
            except Exception as exc:
                errors.append(f"tier3_contract_yaml validation failed: {exc}")

    # 7. Tests cross-references
    if tests_doc is not None:
        for suite in tests_doc.tests.contract_tests:
            if suite.contract not in package.contracts:
                errors.append(
                    f"tests.yaml contract_tests references unknown contract '{suite.contract}'."
                )
        for custom in tests_doc.tests.custom_tests:
            if custom.stage and custom.stage not in package.stages:
                errors.append(
                    f"tests.yaml custom_tests '{custom.test_id}' references unknown stage '{custom.stage}'."
                )

    # 8. ${config.*} and ${secrets.*} completeness across all docs
    if config_doc is not None:
        config_refs: set[str] = set()
        secret_refs: set[str] = set()
        _collect_placeholders(package.agent_yaml, config_refs, secret_refs)
        _collect_placeholders(package.stages, config_refs, secret_refs)
        _collect_placeholders(package.contracts, config_refs, secret_refs)
        _collect_placeholders(package.config_yaml, config_refs, secret_refs)

        declared_secrets = {s.name for s in config_doc.config.secrets}
        for sec_name in sorted(secret_refs):
            if sec_name not in declared_secrets:
                errors.append(
                    f"Secret placeholder '${{secrets.{sec_name}}}' is not declared in config.yaml secrets."
                )

        defaults = config_doc.config.defaults
        envs = config_doc.config.environments or {"default": {}}
        for cfg_path in sorted(config_refs):
            ok_default, _ = _resolve_dotted_path(defaults, cfg_path)
            if not ok_default:
                errors.append(
                    f"Config placeholder '${{config.{cfg_path}}}' does not resolve in config.yaml defaults."
                )
                continue
            for env_name, env_overrides in envs.items():
                merged = _deep_merge_dicts(defaults, env_overrides or {})
                ok_env, _ = _resolve_dotted_path(merged, cfg_path)
                if not ok_env:
                    errors.append(
                        f"Config placeholder '${{config.{cfg_path}}}' fails to resolve in environment '{env_name}'."
                    )

    return errors
