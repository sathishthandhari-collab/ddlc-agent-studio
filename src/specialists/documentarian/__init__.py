"""Documentarian specialist package (Phase 5 Documentation & Gate 3)."""

from src.specialists.documentarian.models import (
    ALLOWED_BUILTIN_OUTPUT_SCHEMAS,
    AgentManifestDoc,
    ConfigDocument,
    ContractDocument,
    StageDocument,
    TestsDocument,
)
from src.specialists.documentarian.specialist import (
    DocumentarianSpecialist,
    ImplDocSynthesisOutput,
    ImplDocValidationError,
    synthesis_to_package,
    write_impl_docs_package,
)
from src.specialists.documentarian.validator import validate_impl_doc_package

__all__ = [
    "ALLOWED_BUILTIN_OUTPUT_SCHEMAS",
    "AgentManifestDoc",
    "ConfigDocument",
    "ContractDocument",
    "DocumentarianSpecialist",
    "ImplDocSynthesisOutput",
    "ImplDocValidationError",
    "StageDocument",
    "TestsDocument",
    "synthesis_to_package",
    "validate_impl_doc_package",
    "write_impl_docs_package",
]
