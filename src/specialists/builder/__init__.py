"""Builder specialist package (Phase 6 Construction: Deterministic Compiler + LLM Leaf Coder)."""

from src.specialists.builder.compiler import (
    SKELETON_BANNER_LINE,
    CompilationResult,
    CompilerSyntaxError,
    DeterministicScaffoldCompiler,
    has_skeleton_header,
    inspect_muscle_requirements,
    make_skeleton_header,
    validate_python_ast,
)
from src.specialists.builder.leaf_coder import (
    ALLOWED_PROMPT_CONTEXT_VARS,
    BANNED_IMPORTS,
    BuilderRAGProtocol,
    LeafCoderPatcher,
    LeafCoderSynthesizer,
    LeafPromptSynthesis,
    LeafSynthesisError,
    LeafToolSynthesis,
    verify_prompt_template_ast,
    verify_tool_source,
)
from src.specialists.builder.specialist import BuilderSpecialist

__all__ = [
    "ALLOWED_PROMPT_CONTEXT_VARS",
    "BANNED_IMPORTS",
    "BuilderRAGProtocol",
    "BuilderSpecialist",
    "CompilationResult",
    "CompilerSyntaxError",
    "DeterministicScaffoldCompiler",
    "LeafCoderPatcher",
    "LeafCoderSynthesizer",
    "LeafPromptSynthesis",
    "LeafSynthesisError",
    "LeafToolSynthesis",
    "SKELETON_BANNER_LINE",
    "has_skeleton_header",
    "inspect_muscle_requirements",
    "make_skeleton_header",
    "validate_python_ast",
    "verify_prompt_template_ast",
    "verify_tool_source",
]
