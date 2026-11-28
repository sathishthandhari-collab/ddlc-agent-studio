"""Builder specialist — Dual-Tier orchestration (Deterministic Scaffold Compiler + LLM Leaf Coder)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from src.core.settings import DEFAULT_PROJECT_ROOT, get_studio_config
from src.specialists.builder.compiler.engine import DeterministicScaffoldCompiler
from src.specialists.builder.leaf_coder.patcher import LeafCoderPatcher
from src.specialists.builder.leaf_coder.synthesizer import (
    BuilderRAGProtocol,
    LeafCoderSynthesizer,
)
from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.state.blackboard import (
    BuildLogEntry,
    ImplDocPackage,
    build_contract_pointer_from_directory,
)
from src.state.checkpointer import run_async_selector
from src.state.projections import BuilderProjection, LeafCoderProjection


def _resolve_contained_path(base_dir: Path, rel_path: str) -> Path:
    resolved_base = base_dir.resolve()
    target = (resolved_base / rel_path).resolve()
    if not target.is_relative_to(resolved_base):
        raise ValueError(f"Path traversal disallowed in Builder output: {rel_path!r}")
    return target


def _build_leaf_projection(
    builder_proj: BuilderProjection,
    *,
    stage_id: str,
    tool_name: Optional[str] = None,
    mode: Literal["create", "patch"] = "create",
    existing_code: Optional[str] = None,
    existing_tests: Optional[str] = None,
) -> LeafCoderProjection:
    pkg: ImplDocPackage = builder_proj.approved_impl_docs
    stage_doc = pkg.stages[stage_id]
    processing = stage_doc.get("processing") or {}
    tools = list(processing.get("tools") or [])

    selected_tool: Optional[dict[str, Any]] = None
    if tool_name is not None:
        for t in tools:
            if isinstance(t, dict) and t.get("name") == tool_name:
                selected_tool = t
                break
    else:
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "custom_code" and t.get("handler"):
                selected_tool = t
                break

    in_name = (stage_doc.get("input") or {}).get("contract")
    out_name = (stage_doc.get("output") or {}).get("contract")

    return LeafCoderProjection(
        run_id=builder_proj.run_id,
        mode=mode,
        stage_id=stage_id,
        tool_name=str(selected_tool.get("name")) if selected_tool else tool_name,
        handler=selected_tool.get("handler") if selected_tool else None,
        signature=selected_tool.get("signature") if selected_tool else None,
        logic_description=processing.get("logic_description"),
        tools=tools,
        input_contract=pkg.contracts.get(in_name) if in_name else None,
        output_contract=pkg.contracts.get(out_name) if out_name else None,
        existing_code=existing_code if mode == "patch" else None,
        existing_tests=existing_tests if mode == "patch" else None,
        fix_diagnostics=list(builder_proj.fix_instructions),
    )


def _should_rebuild_stage(
    projection: BuilderProjection,
    *,
    stage_id: str,
    target_rel_path: str,
    file_exists: bool,
) -> bool:
    """In change/fix mode, rebuild only missing files or targeted stages; keep untouched Muscle safe."""
    if not file_exists:
        return True
    if projection.build_fix_attempt_count > 0 and projection.fix_instructions:
        joined = " ".join(projection.fix_instructions).lower()
        file_name = target_rel_path.rsplit("/", 1)[-1].lower()
        if stage_id.lower() in joined or file_name in joined:
            return True
        all_stage_ids = set(projection.approved_impl_docs.stages.keys())
        if any(other_id.lower() in joined for other_id in all_stage_ids if other_id != stage_id):
            return False
        return True
    if projection.mode == "change" and projection.changed_stages:
        return stage_id in projection.changed_stages
    return projection.mode == "build"


class BuilderSpecialist:
    """Executes Phase 6 Construction (Tier 1 Deterministic Compiler + Tier 2 LLM Leaf Coder)."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        rag_facade: Optional[BuilderRAGProtocol] = None,
        *,
        compiler: Optional[DeterministicScaffoldCompiler] = None,
        synthesizer: Optional[LeafCoderSynthesizer] = None,
        patcher: Optional[LeafCoderPatcher] = None,
    ) -> None:
        client = llm_client or LiteLLMInstructorClient()
        self.compiler = compiler or DeterministicScaffoldCompiler()
        self.rag_facade = rag_facade
        self.synthesizer = synthesizer or LeafCoderSynthesizer(client, rag_facade=rag_facade)
        self.patcher = patcher or LeafCoderPatcher(client)

    def _resolve_target_dir(
        self,
        projection: BuilderProjection,
        output_dir: Optional[Path],
    ) -> Path:
        if output_dir is not None:
            return Path(output_dir)
        agent_name = (
            projection.target_agent_name
            or (projection.approved_impl_docs.agent_yaml.get("agent") or {}).get("name")
            or "generated_agent"
        )
        rel_agents_dir = get_studio_config().app.paths.agents_output_dir
        return DEFAULT_PROJECT_ROOT / rel_agents_dir / str(agent_name)

    def _fetch_rag_conventions(self, topic: str) -> str:
        if self.rag_facade is None:
            return ""
        return run_async_selector(self.rag_facade.query_code_conventions(topic))

    async def _afetch_rag_conventions(self, topic: str) -> str:
        if self.rag_facade is None:
            return ""
        return await self.rag_facade.query_code_conventions(topic)

    def build(
        self,
        projection: BuilderProjection,
        *,
        output_dir: Optional[Path] = None,
        rag_context_xml: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synchronously compile Skeleton and synthesize/patch Muscle files on disk."""
        target_dir = self._resolve_target_dir(projection, output_dir)
        pkg = projection.approved_impl_docs

        # Tier 1: Deterministic Scaffold Compilation (always runs; <250ms, 0 LLM tokens)
        compile_res = self.compiler.compile(pkg, target_dir, write_impl_docs=True)

        conv_xml = (
            rag_context_xml
            if rag_context_xml is not None
            else (
                self._fetch_rag_conventions("python style testing jinja2 prompt contract")
                if (compile_res.custom_tool_hooks or compile_res.prompt_templates_needed)
                else ""
            )
        )

        leaf_files_written: list[str] = []
        leaf_modes_used: dict[str, str] = {}

        # Tier 2A: Custom Python tool handlers + unit tests
        for hook in compile_res.custom_tool_hooks:
            stage_id = str(hook["stage_id"])
            tool_name = str(hook["tool_name"])
            handler = str(hook["handler"])
            mod_rel = handler.split("::", 1)[0].strip()
            tool_rel = f"src/{mod_rel}"
            mod_stem = mod_rel.rsplit("/", 1)[-1].removesuffix(".py")
            test_rel = f"tests/unit/test_{mod_stem}.py"

            tool_path = _resolve_contained_path(target_dir, tool_rel)
            test_path = _resolve_contained_path(target_dir, test_rel)
            exists_on_disk = tool_path.is_file()

            if not _should_rebuild_stage(
                projection,
                stage_id=stage_id,
                target_rel_path=tool_rel,
                file_exists=exists_on_disk,
            ):
                continue

            mode: Literal["create", "patch"] = "patch" if exists_on_disk else "create"
            existing_code = tool_path.read_text(encoding="utf-8") if exists_on_disk else None
            existing_tests = test_path.read_text(encoding="utf-8") if test_path.is_file() else None

            leaf_proj = _build_leaf_projection(
                projection,
                stage_id=stage_id,
                tool_name=tool_name,
                mode=mode,
                existing_code=existing_code,
                existing_tests=existing_tests,
            )
            if mode == "patch":
                bundle = self.patcher.patch_tool(leaf_proj, rag_context_xml=conv_xml)
            else:
                bundle = self.synthesizer.create_tool(leaf_proj, rag_context_xml=conv_xml)

            tool_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.parent.mkdir(parents=True, exist_ok=True)
            tool_path.write_text(bundle.tool_code.rstrip() + "\n", encoding="utf-8")
            test_path.write_text(bundle.unit_test_code.rstrip() + "\n", encoding="utf-8")
            leaf_files_written.extend([tool_rel, test_rel])
            leaf_modes_used[tool_rel] = mode

        # Tier 2B: Editable Jinja2 prompt templates (Universal Prompt Contract)
        for p_target in compile_res.prompt_templates_needed:
            stage_id = str(p_target["stage_id"])
            prompt_ref = str(p_target["prompt_template_ref"])
            prompt_rel = prompt_ref if prompt_ref.startswith("src/") else f"src/{prompt_ref}"
            prompt_path = _resolve_contained_path(target_dir, prompt_rel)
            exists_on_disk = prompt_path.is_file()

            if not _should_rebuild_stage(
                projection,
                stage_id=stage_id,
                target_rel_path=prompt_rel,
                file_exists=exists_on_disk,
            ):
                continue

            mode = "patch" if exists_on_disk else "create"
            existing_tmpl = prompt_path.read_text(encoding="utf-8") if exists_on_disk else None
            leaf_proj = _build_leaf_projection(
                projection,
                stage_id=stage_id,
                mode="create",  # prompt template patch uses existing_template directly
            )
            if mode == "patch" and existing_tmpl is not None:
                p_bundle = self.patcher.patch_prompt(
                    leaf_proj,
                    template_ref=prompt_ref,
                    existing_template=existing_tmpl,
                    rag_context_xml=conv_xml,
                )
            else:
                p_bundle = self.synthesizer.create_prompt(
                    leaf_proj,
                    template_ref=prompt_ref,
                    rag_context_xml=conv_xml,
                )

            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(p_bundle.prompt_template.rstrip() + "\n", encoding="utf-8")
            leaf_files_written.append(prompt_rel)
            leaf_modes_used[prompt_rel] = mode

        agent_meta = pkg.agent_yaml.get("agent") or {}
        agent_name = str(agent_meta.get("name") or projection.target_agent_name or "generated_agent")
        agent_version = str(agent_meta.get("version") or "1.0.0")
        code_pointer = build_contract_pointer_from_directory(
            contract_name=f"{agent_name}_codebase",
            base_dir=target_dir,
            contract_version=agent_version,
            metadata={
                "runtime": compile_res.runtime,
                "skeleton_files": compile_res.skeleton_files,
                "leaf_files": leaf_files_written,
            },
        )

        return {
            "current_phase": "construct",
            "generated_code_pointer": code_pointer,
            "active_fix_instructions": [],
            "build_logs": [
                BuildLogEntry(
                    phase="construct",
                    actor="builder",
                    action="compile_and_synthesize",
                    status="completed",
                    details={
                        "output_dir": str(target_dir.resolve()),
                        "runtime": compile_res.runtime,
                        "skeleton_count": len(compile_res.skeleton_files),
                        "leaf_files_written": leaf_files_written,
                        "leaf_modes_used": leaf_modes_used,
                        "payload_sha256": code_pointer.payload_sha256,
                    },
                )
            ],
        }

    async def abuild(
        self,
        projection: BuilderProjection,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Async Builder entrypoint pre-fetching BuilderRAGFacade conventions before build()."""
        conv_xml = await self._afetch_rag_conventions(
            "python style testing jinja2 prompt contract"
        )
        return self.build(projection, output_dir=output_dir, rag_context_xml=conv_xml)
