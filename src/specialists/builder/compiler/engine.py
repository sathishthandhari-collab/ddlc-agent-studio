"""Deterministic Scaffold Compiler orchestrator (Tier 1: Zero-LLM Jinja2 + AST)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.specialists.builder.compiler.ast_validator import (
    make_skeleton_header,
    validate_python_ast,
)
from src.specialists.builder.compiler.format import (
    format_directory_with_ruff,
    normalize_python_source,
)
from src.specialists.builder.compiler.generators.config_gen import build_config_context
from src.specialists.builder.compiler.generators.contract_gen import build_contracts_context
from src.specialists.builder.compiler.generators.graph_gen import build_graph_context
from src.specialists.documentarian.specialist import write_impl_docs_package
from src.state.blackboard import ImplDocPackage

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class CompilationResult:
    """Summary of deterministic Skeleton files emitted to disk."""

    output_dir: Path
    runtime: str
    generated_files: list[str] = field(default_factory=list)
    skeleton_files: list[str] = field(default_factory=list)
    custom_tool_hooks: list[dict[str, Any]] = field(default_factory=list)
    prompt_templates_needed: list[dict[str, Any]] = field(default_factory=list)


def _create_compiler_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["repr"] = repr
    return env


def inspect_muscle_requirements(package: ImplDocPackage) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministically inspect stages/*.yaml for custom_code hooks and prompt templates."""
    tool_hooks: list[dict[str, Any]] = []
    prompt_targets: list[dict[str, Any]] = []
    seen_handlers: set[str] = set()

    pipeline_order = [
        str(edge["stage_id"])
        for edge in (package.agent_yaml.get("pipeline") or {}).get("stages") or []
        if "stage_id" in edge
    ]
    ordered_stage_ids = pipeline_order + [
        sid for sid in sorted(package.stages.keys()) if sid not in pipeline_order
    ]

    for stage_id in ordered_stage_ids:
        stg = package.stages.get(stage_id)
        if not stg:
            continue
        proc = stg.get("processing") or {}
        proc_type = proc.get("processing_type")

        for tool in proc.get("tools") or []:
            if tool.get("type") == "custom_code" and tool.get("handler"):
                handler = str(tool["handler"]).strip()
                if handler not in seen_handlers:
                    seen_handlers.add(handler)
                    tool_hooks.append(
                        {
                            "stage_id": stage_id,
                            "tool_name": str(tool.get("name") or ""),
                            "handler": handler,
                            "signature": tool.get("signature"),
                            "purpose": tool.get("purpose"),
                        }
                    )

        llm_cfg = proc.get("llm_config") or {}
        if proc_type == "llm_reasoning" and llm_cfg.get("prompt_template_ref"):
            prompt_targets.append(
                {
                    "stage_id": stage_id,
                    "prompt_template_ref": str(llm_cfg["prompt_template_ref"]).strip(),
                    "output_schema": llm_cfg.get("output_schema"),
                    "input_contract": (stg.get("input") or {}).get("contract"),
                }
            )

    return tool_hooks, prompt_targets


class DeterministicScaffoldCompiler:
    """Zero-LLM Jinja2 + AST compiler generating the read-only Skeleton codebase."""

    def __init__(self, *, run_ruff_format: bool = False) -> None:
        self.env = _create_compiler_jinja_env()
        self.run_ruff_format = run_ruff_format

    def _render_and_write(
        self,
        *,
        template_rel: str,
        context: dict[str, Any],
        output_dir: Path,
        rel_path: str,
        is_python: bool = True,
        is_skeleton: bool = True,
    ) -> str:
        target_path = (output_dir / rel_path).resolve()
        resolved_root = output_dir.resolve()
        if not target_path.is_relative_to(resolved_root):
            raise ValueError(f"Output path traversal disallowed: {rel_path!r}")

        render_ctx = {**context}
        if is_skeleton and is_python:
            render_ctx["header"] = make_skeleton_header(rel_path)
        rendered = self.env.get_template(template_rel).render(**render_ctx)

        if is_python:
            rendered = normalize_python_source(rendered)
            validate_python_ast(rendered, filename=rel_path)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(rendered, encoding="utf-8")
        return rel_path

    def compile(
        self,
        package: ImplDocPackage,
        output_dir: Path,
        *,
        write_impl_docs: bool = True,
    ) -> CompilationResult:
        """Deterministically compile impl_docs/ into an AST-verified agent Skeleton."""
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)

        # Ensure standard Python package markers & Muscle directories exist
        for pkg_init in (
            "src/__init__.py",
            "src/models/__init__.py",
            "src/core/__init__.py",
            "src/tools/__init__.py",
            "tests/__init__.py",
            "tests/contracts/__init__.py",
            "tests/unit/__init__.py",
            "tests/integration/__init__.py",
        ):
            init_file = root / pkg_init
            init_file.parent.mkdir(parents=True, exist_ok=True)
            if not init_file.exists():
                init_file.write_text('"""Generated package."""\n', encoding="utf-8")

        (root / "src" / "prompts").mkdir(parents=True, exist_ok=True)

        if write_impl_docs:
            write_impl_docs_package(package, root / "impl_docs")

        contracts_ctx = build_contracts_context(package.contracts, package.stages)
        config_ctx = build_config_context(package.config_yaml)
        graph_ctx = build_graph_context(
            package.agent_yaml,
            package.stages,
            package.contracts,
            package.config_yaml,
        )

        runtime = graph_ctx["runtime"]
        generated_files: list[str] = []
        skeleton_files: list[str] = []

        # 1. Common Skeleton modules
        common_targets = [
            ("common/contracts_pydantic.py.jinja2", contracts_ctx, "src/models/contracts_pydantic.py", True, True),
            ("common/config.py.jinja2", config_ctx, "src/core/config.py", True, True),
            ("common/telemetry.py.jinja2", graph_ctx, "src/core/telemetry.py", True, True),
            ("common/test_contracts.py.jinja2", contracts_ctx, "tests/contracts/test_contracts.py", True, True),
            ("common/pyproject.toml.jinja2", graph_ctx, "pyproject.toml", False, False),
            ("common/env_example.jinja2", config_ctx | graph_ctx, ".env.example", False, False),
            ("common/readme.md.jinja2", graph_ctx, "README.md", False, False),
        ]
        for tmpl, ctx, rel_path, is_py, is_skel in common_targets:
            written = self._render_and_write(
                template_rel=tmpl,
                context=ctx,
                output_dir=root,
                rel_path=rel_path,
                is_python=is_py,
                is_skeleton=is_skel,
            )
            generated_files.append(written)
            if is_skel:
                skeleton_files.append(written)

        # 2. Runtime-specific orchestration Skeleton
        if runtime == "langgraph":
            runtime_targets = [
                ("langgraph/graph.py.jinja2", graph_ctx, "src/graph.py", True, True),
                ("langgraph/main.py.jinja2", graph_ctx, "src/main.py", True, True),
                ("langgraph/test_agent_graph.py.jinja2", graph_ctx, "tests/integration/test_agent_graph.py", True, True),
            ]
        elif runtime == "python_litellm":
            runtime_targets = [
                ("python_litellm/pipeline.py.jinja2", graph_ctx, "src/pipeline.py", True, True),
                ("python_litellm/main.py.jinja2", graph_ctx, "src/main.py", True, True),
                ("python_litellm/test_pipeline.py.jinja2", graph_ctx, "tests/integration/test_pipeline.py", True, True),
            ]
        else:
            raise ValueError(f"Unsupported runtime strategy: {runtime!r}")

        for tmpl, ctx, rel_path, is_py, is_skel in runtime_targets:
            written = self._render_and_write(
                template_rel=tmpl,
                context=ctx,
                output_dir=root,
                rel_path=rel_path,
                is_python=is_py,
                is_skeleton=is_skel,
            )
            generated_files.append(written)
            if is_skel:
                skeleton_files.append(written)

        if self.run_ruff_format:
            format_directory_with_ruff(root)

        tool_hooks, prompt_targets = inspect_muscle_requirements(package)
        return CompilationResult(
            output_dir=root,
            runtime=runtime,
            generated_files=generated_files,
            skeleton_files=skeleton_files,
            custom_tool_hooks=tool_hooks,
            prompt_templates_needed=prompt_targets,
        )
