"""Day-2 Leaf Coder surgical patcher ('patch' mode preserving 2 AM production hotfixes)."""

from __future__ import annotations

from typing import Optional

from src.specialists.builder.compiler.generators.graph_gen import parse_tool_handler
from src.specialists.builder.leaf_coder.prompts import (
    build_prompt_template_messages,
    build_tool_messages,
)
from src.specialists.builder.leaf_coder.signature_verifier import verify_prompt_template_ast
from src.specialists.builder.leaf_coder.synthesizer import (
    LeafPromptSynthesis,
    LeafSynthesisError,
    LeafToolSynthesis,
    _validate_tool_bundle,
)
from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.state.projections import LeafCoderProjection


class LeafCoderPatcher:
    """Surgically patches existing Leaf tools, tests, and prompts while preserving hotfixes."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        *,
        max_repair_attempts: int = 2,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.max_repair_attempts = max(0, max_repair_attempts)

    def patch_tool(
        self,
        projection: LeafCoderProjection,
        *,
        rag_context_xml: str = "",
    ) -> LeafToolSynthesis:
        """Apply targeted changes to existing_code/existing_tests while keeping hotfixes intact."""
        if projection.mode != "patch" or projection.existing_code is None:
            raise ValueError("LeafCoderPatcher.patch_tool requires mode='patch' and existing_code.")
        if not projection.handler:
            raise ValueError(f"Stage '{projection.stage_id}' missing tool handler.")

        _, func_name = parse_tool_handler(projection.handler)
        rel_tool_path = f"src/{projection.handler.split('::', 1)[0]}"
        mod_stem = rel_tool_path.rsplit("/", 1)[-1].removesuffix(".py")
        rel_test_path = f"tests/unit/test_{mod_stem}.py"

        repair_errors: list[str] = []
        for attempt in range(self.max_repair_attempts + 1):
            messages = build_tool_messages(
                projection,
                rag_context_xml=rag_context_xml,
                repair_errors=repair_errors,
            )
            bundle = self.llm_client.complete_structured(
                specialist_name="builder_leaf_coder",
                response_model=LeafToolSynthesis,
                messages=messages,
            )
            errors = _validate_tool_bundle(
                bundle,
                func_name=func_name,
                signature=projection.signature,
                rel_tool_path=rel_tool_path,
                rel_test_path=rel_test_path,
            )
            if not errors:
                return bundle
            repair_errors = errors
            if attempt >= self.max_repair_attempts:
                raise LeafSynthesisError(errors)

        raise LeafSynthesisError(["Unreachable leaf patch exit."])

    def patch_prompt(
        self,
        projection: LeafCoderProjection,
        *,
        template_ref: str,
        existing_template: str,
        rag_context_xml: str = "",
    ) -> LeafPromptSynthesis:
        """Surgically update an existing Jinja2 prompt template and verify Universal Prompt Contract."""
        repair_errors: list[str] = []
        for attempt in range(self.max_repair_attempts + 1):
            messages = build_prompt_template_messages(
                projection,
                template_ref=template_ref,
                existing_template=existing_template,
                rag_context_xml=rag_context_xml,
                repair_errors=repair_errors,
            )
            bundle = self.llm_client.complete_structured(
                specialist_name="builder_leaf_coder",
                response_model=LeafPromptSynthesis,
                messages=messages,
            )
            errors = verify_prompt_template_ast(template_ref, bundle.prompt_template)
            if not errors:
                return bundle
            repair_errors = errors
            if attempt >= self.max_repair_attempts:
                raise LeafSynthesisError(errors)

        raise LeafSynthesisError(["Unreachable leaf prompt patch exit."])
