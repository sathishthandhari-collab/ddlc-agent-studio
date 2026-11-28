"""Prompt builders for the Documentarian specialist."""

from __future__ import annotations

import json
from collections.abc import Sequence

from src.state.projections import DocumentarianProjection

DOCUMENTARIAN_SYSTEM_PROMPT = """\
You are the Documentarian specialist for DDLC Agent Studio (Phase 5 Documentation).
Translate the approved ArchitectureProposal into the complete, unambiguous 5-file
runtime-agnostic YAML implementation document suite (impl_docs/):
  1. agent.yaml (identity, pattern, runtime, pipeline topology, shared_components, metadata)
  2. stages/stage_<id>.yaml (one per stage: input, output, processing_type, tools, error_strategy)
  3. contracts/<name>.yaml (explicit type, explicit nullable bool, description, quality_rules, storage_mode)
  4. config.yaml (defaults, environments, secrets — zero hardcoded credentials/URIs)
  5. tests.yaml (contract_tests + custom_tests)

Critical invariants:
- Stages specify WHAT, not HOW, and match pipeline.stages[].stage_id 1:1.
- Every llm_reasoning stage must set llm_config.prompt_template_ref = "prompts/stage_<id>.jinja2"
  and output_schema to a contract name (or "GeneratedFileSet" when generating multi-file bundles staged via Claim Check).
- Every custom Python tool must use type="custom_code", handler="tools/<mod>.py::<func>",
  explicit typed signature, and non-empty processing.logic_description.
- Every shared_component tool must also be declared in agent.yaml dependencies.shared_components with usage_type.
- Every ${config.*} and ${secrets.*} reference must resolve cleanly in config.yaml.
- Never embed plaintext passwords, tokens, or hardcoded API keys.
"""


def build_documentarian_messages(
    projection: DocumentarianProjection,
    *,
    repair_errors: Sequence[str] = (),
) -> list[dict[str, str]]:
    user_payload = {
        "mode": projection.mode,
        "approved_architecture": (
            projection.approved_architecture.model_dump(mode="json")
            if projection.approved_architecture is not None
            else None
        ),
        "shared_component_specs": list(projection.shared_component_specs),
        "current_impl_docs": (
            projection.current_impl_docs.model_dump(mode="json")
            if projection.current_impl_docs is not None
            else None
        ),
        "impl_doc_feedback": projection.impl_doc_feedback,
        "contract_validation_feedback": list(projection.contract_validation_feedback),
        "change_request": (
            projection.change_request.model_dump(mode="json")
            if projection.change_request is not None
            else None
        ),
        "validation_repair_errors": list(repair_errors),
    }
    return [
        {"role": "system", "content": DOCUMENTARIAN_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, indent=2, default=str)},
    ]
