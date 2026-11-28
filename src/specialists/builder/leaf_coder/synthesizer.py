"""Day-0 Leaf Coder synthesizer ('create' mode) for custom tools, unit tests, and prompts."""

from __future__ import annotations

import ast
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.specialists.builder.compiler.generators.graph_gen import parse_tool_handler
from src.specialists.builder.leaf_coder.prompts import (
    build_prompt_template_messages,
    build_tool_messages,
)
from src.specialists.builder.leaf_coder.signature_verifier import (
    verify_prompt_template_ast,
    verify_tool_source,
)
from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.state.projections import LeafCoderProjection


@runtime_checkable
class BuilderRAGProtocol(Protocol):
    async def query_component_docs(
        self, component_or_query: str, query: Optional[str] = None
    ) -> str: ...

    async def query_code_conventions(self, topic: str) -> str: ...


class LeafToolSynthesis(BaseModel):
    """Structured Instructor output for a single custom tool + companion pytest file."""

    tool_code: str = Field(..., min_length=1)
    unit_test_code: str = Field(..., min_length=1)
    summary: str = Field(default="")


class LeafPromptSynthesis(BaseModel):
    """Structured Instructor output for a single stage Jinja2 prompt template."""

    prompt_template: str = Field(..., min_length=1)
    summary: str = Field(default="")


class LeafSynthesisError(RuntimeError):
    """Raised when synthesized leaf code or prompt fails verification after bounded retries."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Leaf Coder verification failed:\n- " + "\n- ".join(errors))


def _validate_tool_bundle(
    bundle: LeafToolSynthesis,
    *,
    func_name: str,
    signature: Optional[str],
    rel_tool_path: str,
    rel_test_path: str,
) -> list[str]:
    errors = verify_tool_source(
        bundle.tool_code,
        expected_func_name=func_name,
        expected_signature=signature,
        filename=rel_tool_path,
    )
    try:
        ast.parse(bundle.unit_test_code, filename=rel_test_path)
    except SyntaxError as exc:
        errors.append(f"SyntaxError in '{rel_test_path}' at line {exc.lineno}: {exc.msg}")
    return errors


class LeafCoderSynthesizer:
    """Synthesizes Day-0 editable Muscle files (`src/tools/`, `src/prompts/`, `tests/unit/`)."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        rag_facade: Optional[BuilderRAGProtocol] = None,
        *,
        max_repair_attempts: int = 2,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.rag_facade = rag_facade
        self.max_repair_attempts = max(0, max_repair_attempts)

    def create_tool(self, projection: LeafCoderProjection, *, rag_context_xml: str = "") -> LeafToolSynthesis:
        """Generate and verify a new custom tool + unit tests in 'create' mode."""
        if not projection.handler:
            raise ValueError(f"Stage '{projection.stage_id}' has no tool handler to synthesize.")
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

        raise LeafSynthesisError(["Unreachable leaf synthesis exit."])

    def create_prompt(
        self,
        projection: LeafCoderProjection,
        *,
        template_ref: str,
        rag_context_xml: str = "",
    ) -> LeafPromptSynthesis:
        """Generate and AST-verify a stage Jinja2 prompt template in 'create' mode."""
        repair_errors: list[str] = []
        for attempt in range(self.max_repair_attempts + 1):
            messages = build_prompt_template_messages(
                projection,
                template_ref=template_ref,
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

        raise LeafSynthesisError(["Unreachable leaf prompt synthesis exit."])
