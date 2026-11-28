"""Deterministic LangGraph and Python+LiteLLM workflow context assembler."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from src.specialists.builder.compiler.generators.contract_gen import to_pascal_case

_CONFIG_EXPR_RE = re.compile(r"^\$\{config\.([A-Za-z0-9_.]+)\}$")


def _config_path_to_settings_expr(path_str: str) -> str:
    parts = [
        "schema_name" if p == "schema" else p
        for p in path_str.split(".")
    ]
    return "settings." + ".".join(parts)


def resolve_config_ref_expr(raw_value: Any) -> str:
    """Convert '${config.a.b.schema}' into 'settings.a.b.schema_name' or repr literal."""
    if isinstance(raw_value, str):
        match = _CONFIG_EXPR_RE.match(raw_value.strip())
        if match:
            return _config_path_to_settings_expr(match.group(1))
    return repr(raw_value)


def parse_tool_handler(handler: str) -> tuple[str, str]:
    """Parse 'tools/sqlglot_verifier.py::validate_sql_ast' -> ('src.tools.sqlglot_verifier', 'validate_sql_ast')."""
    mod_part, func_name = handler.strip().split("::", 1)
    mod_no_ext = mod_part[:-3] if mod_part.endswith(".py") else mod_part
    dotted = "src." + mod_no_ext.strip("/").replace("/", ".")
    return dotted, func_name.strip()


@dataclass
class CompiledToolRef:
    name: str
    tool_type: str
    module_path: Optional[str] = None
    func_name: Optional[str] = None
    usage_type: str = "import"
    access: str = "read_only"
    configuration: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompiledStageSpec:
    stage_id: str
    stage_name: str
    stage_type: str
    description: str
    autonomy: str
    processing_type: str
    input_contract: Optional[str] = None
    input_model: Optional[str] = None
    output_contract: Optional[str] = None
    output_model: Optional[str] = None
    output_storage_mode: str = "inline"
    model_tier: str = "reasoning"
    prompt_template_name: Optional[str] = None
    system_prompt: Optional[str] = None
    llm_output_schema: Optional[str] = None
    llm_output_model: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096
    max_reflection_retries: int = 3
    max_tool_iterations: int = 5
    source_uri_expr: Optional[str] = None
    source_schema: Optional[str] = None
    destination_uri_expr: Optional[str] = None
    custom_tools: list[CompiledToolRef] = field(default_factory=list)
    shared_tools: list[CompiledToolRef] = field(default_factory=list)
    builtin_tools: list[CompiledToolRef] = field(default_factory=list)
    next_stage: Optional[str] = None
    on_error_stage: Optional[str] = None
    transitions: dict[str, Optional[str]] = field(default_factory=dict)


def build_graph_context(
    agent_yaml: Mapping[str, Any],
    stages: Mapping[str, Mapping[str, Any]],
    contracts: Mapping[str, Mapping[str, Any]],
    config_yaml: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble deterministic stage wiring and import metadata for LangGraph or Python+LiteLLM."""
    agent_sec = agent_yaml.get("agent") or {}
    arch_sec = agent_yaml.get("architecture") or {}
    pipeline_stages = list((agent_yaml.get("pipeline") or {}).get("stages") or [])
    deps_sec = agent_yaml.get("dependencies") or {}

    shared_usage_map = {
        str(sc.get("component_id")): str(sc.get("usage_type") or "import")
        for sc in (deps_sec.get("shared_components") or [])
        if isinstance(sc, Mapping) and sc.get("component_id")
    }

    contract_storage_modes: dict[str, str] = {}
    for ckey, craw in contracts.items():
        cbody = craw.get("contract") if isinstance(craw.get("contract"), Mapping) else craw
        contract_storage_modes[str(cbody.get("name") or ckey)] = str(
            cbody.get("storage_mode") or "inline"
        )

    cfg_body = config_yaml.get("config") if isinstance(config_yaml.get("config"), Mapping) else config_yaml
    defaults = dict(cfg_body.get("defaults") or {})
    config_top_keys = [
        ("schema_name" if k == "schema" else str(k), str(k))
        for k in defaults.keys()
    ]

    compiled_stages: list[CompiledStageSpec] = []
    custom_imports: dict[tuple[str, str], None] = {}
    contract_models_to_import: dict[str, None] = {}
    llm_stages_with_prompts: list[tuple[str, str]] = []
    has_claim_check = False
    has_human_gate = False

    for edge in pipeline_stages:
        sid = str(edge["stage_id"])
        raw_stg = stages[sid]
        hdr = raw_stg.get("stage") or {}
        in_sec = raw_stg.get("input") or {}
        out_sec = raw_stg.get("output") or {}
        proc_sec = raw_stg.get("processing") or {}
        llm_cfg = proc_sec.get("llm_config") or {}
        valves = proc_sec.get("safety_valves") or {}

        in_contract = in_sec.get("contract")
        out_contract = out_sec.get("contract")
        in_model = to_pascal_case(in_contract) if in_contract else None
        out_model = to_pascal_case(out_contract) if out_contract else None
        if in_model:
            contract_models_to_import[in_model] = None
        if out_model:
            contract_models_to_import[out_model] = None

        llm_output_schema = llm_cfg.get("output_schema")
        llm_output_model = (
            "GeneratedFileSet"
            if llm_output_schema == "GeneratedFileSet"
            else (to_pascal_case(llm_output_schema) if llm_output_schema else out_model)
        )
        if llm_output_model:
            contract_models_to_import[llm_output_model] = None
            if llm_output_model == "GeneratedFileSet":
                contract_models_to_import["GeneratedFile"] = None

        out_storage_mode = contract_storage_modes.get(out_contract or "", "inline")
        if out_storage_mode == "claim_check" or llm_output_schema == "GeneratedFileSet":
            has_claim_check = True

        proc_type = str(proc_sec.get("processing_type") or "deterministic_tool")
        if proc_type == "human_gate" or hdr.get("autonomy") == "human_required":
            has_human_gate = True

        prompt_ref = llm_cfg.get("prompt_template_ref")
        prompt_filename: Optional[str] = None
        if prompt_ref:
            prompt_filename = str(prompt_ref).split("/")[-1]
            llm_stages_with_prompts.append((sid, prompt_filename))

        custom_tools: list[CompiledToolRef] = []
        shared_tools: list[CompiledToolRef] = []
        builtin_tools: list[CompiledToolRef] = []

        for tool in proc_sec.get("tools") or []:
            if not isinstance(tool, Mapping):
                continue
            tname = str(tool.get("name") or "")
            ttype = str(tool.get("type") or "builtin")
            if ttype == "custom_code" and tool.get("handler"):
                mod_path, fn_name = parse_tool_handler(str(tool["handler"]))
                custom_imports[(mod_path, fn_name)] = None
                custom_tools.append(
                    CompiledToolRef(
                        name=tname,
                        tool_type=ttype,
                        module_path=mod_path,
                        func_name=fn_name,
                        access=str(tool.get("access") or "read_only"),
                        configuration=dict(tool.get("configuration") or {}),
                    )
                )
            elif ttype == "shared_component":
                shared_tools.append(
                    CompiledToolRef(
                        name=tname,
                        tool_type=ttype,
                        usage_type=shared_usage_map.get(tname, "import"),
                        access=str(tool.get("access") or "read_only"),
                        configuration=dict(tool.get("configuration") or {}),
                    )
                )
            else:
                builtin_tools.append(
                    CompiledToolRef(
                        name=tname,
                        tool_type=ttype,
                        access=str(tool.get("access") or "read_only"),
                        configuration=dict(tool.get("configuration") or {}),
                    )
                )

        source_sec = in_sec.get("source") or {}
        dest_sec = out_sec.get("destination") or {}

        compiled_stages.append(
            CompiledStageSpec(
                stage_id=sid,
                stage_name=str(hdr.get("name") or sid),
                stage_type=str(hdr.get("type") or "custom"),
                description=str(hdr.get("description") or "").strip(),
                autonomy=str(hdr.get("autonomy") or "autonomous"),
                processing_type=proc_type,
                input_contract=in_contract,
                input_model=in_model,
                output_contract=out_contract,
                output_model=out_model,
                output_storage_mode=out_storage_mode,
                model_tier=str(llm_cfg.get("model_tier") or "reasoning"),
                prompt_template_name=prompt_filename,
                system_prompt=llm_cfg.get("system_prompt"),
                llm_output_schema=llm_output_schema,
                llm_output_model=llm_output_model,
                temperature=float(llm_cfg.get("temperature") or 0.0),
                max_tokens=int(llm_cfg.get("max_tokens") or 4096),
                max_reflection_retries=int(valves.get("max_reflection_retries") or 3),
                max_tool_iterations=int(valves.get("max_tool_iterations") or 5),
                source_uri_expr=(
                    resolve_config_ref_expr(source_sec["uri"])
                    if "uri" in source_sec
                    else None
                ),
                source_schema=source_sec.get("schema"),
                destination_uri_expr=(
                    resolve_config_ref_expr(dest_sec["uri"])
                    if "uri" in dest_sec
                    else None
                ),
                custom_tools=custom_tools,
                shared_tools=shared_tools,
                builtin_tools=builtin_tools,
                next_stage=edge.get("next"),
                on_error_stage=edge.get("on_error"),
                transitions=dict(edge.get("transitions") or {}),
            )
        )

    entry_stage_id = compiled_stages[0].stage_id if compiled_stages else ""
    first_input_contract = compiled_stages[0].input_contract if compiled_stages else None
    last_output_contract = compiled_stages[-1].output_contract if compiled_stages else None
    stage_by_id = {s.stage_id: s for s in compiled_stages}

    tool_deps = [
        f"{t.get('name')}{t.get('version') or ''}"
        for t in (deps_sec.get("tools") or [])
        if isinstance(t, Mapping) and t.get("name")
    ]

    return {
        "agent_name": str(agent_sec.get("name") or "ddlc_agent"),
        "display_name": str(agent_sec.get("display_name") or "DDLC Agent"),
        "agent_version": str(agent_sec.get("version") or "1.0.0"),
        "agent_description": str(agent_sec.get("description") or "").strip(),
        "owner": str(agent_sec.get("owner") or "data-engineering"),
        "pattern": str(arch_sec.get("pattern") or "stateful_pipeline_with_gates"),
        "runtime": str(arch_sec.get("runtime") or "langgraph"),
        "autonomy_level": str(arch_sec.get("autonomy_level") or "semi_autonomous"),
        "stages": compiled_stages,
        "stage_by_id": stage_by_id,
        "entry_stage_id": entry_stage_id,
        "first_input_contract": first_input_contract,
        "last_output_contract": last_output_contract,
        "custom_imports": list(custom_imports.keys()),
        "contract_models_to_import": sorted(contract_models_to_import.keys()),
        "llm_stages_with_prompts": llm_stages_with_prompts,
        "config_top_keys": config_top_keys,
        "has_claim_check": has_claim_check,
        "has_human_gate": has_human_gate,
        "tool_deps": tool_deps,
        "shared_components": list(deps_sec.get("shared_components") or []),
        "has_llm_stages": any(s.processing_type == "llm_reasoning" for s in compiled_stages),
    }
