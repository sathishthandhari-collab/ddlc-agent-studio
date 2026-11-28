"""Narrow prompt builders for LLM Leaf Coder create and patch modes (builder.md §5.3)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Optional

from src.state.projections import LeafCoderProjection

LEAF_CREATE_TOOL_SYSTEM_PROMPT = """\
You are the LLM Leaf Coder for DDLC Agent Studio operating in CREATE mode (Day-0).
Synthesize an isolated, production-ready Python tool module and a fast zero-infra pytest unit suite.

Rules:
- Implement the exact top-level function name and parameter signature specified by handler and signature.
- Target Python 3.12+; use timezone-aware datetime.now(timezone.utc), never datetime.utcnow().
- Never hardcode credentials, API keys, or URIs.
- Never import banned libraries (pyspark, pandera, langchain_community).
- Write fast, deterministic unit tests that run offline without live DBs or network calls.
"""

LEAF_PATCH_TOOL_SYSTEM_PROMPT = """\
You are the LLM Leaf Coder for DDLC Agent Studio operating in PATCH mode (Day-2).
Surgically update existing_code and existing_tests to satisfy updated contracts, logic_description,
or fix_diagnostics.

CRITICAL SAFETY RULE (Decision 25):
- Preserve ALL existing edge-case handlers, boundary conditions, production hotfixes, and tests
  unless they directly contradict fix_diagnostics.
- Match the exact function name and parameter signature specified by handler and signature.
- Never import banned libraries or use datetime.utcnow().
"""

LEAF_PROMPT_SYSTEM_PROMPT = """\
You are the LLM Leaf Coder authoring an editable Jinja2 prompt template (src/prompts/stage_<id>.jinja2).

CRITICAL INTERFACE RULE (Universal Prompt Contract - Decision 28):
- The ONLY allowed top-level template variables are: input, diagnostics, feedback, config.
- Never reference any other top-level variable (e.g., never {{ source_profile }} or {{ stage_name }}).
- Access contract fields via {{ input.<field> }} and config settings via {{ config.<key> }}.
- Guard reflection/feedback sections with {% if diagnostics %} and {% if feedback %}.
"""


def build_tool_messages(
    projection: LeafCoderProjection,
    *,
    rag_context_xml: str = "",
    repair_errors: Sequence[str] = (),
) -> list[dict[str, str]]:
    sys_prompt = (
        LEAF_PATCH_TOOL_SYSTEM_PROMPT
        if projection.mode == "patch"
        else LEAF_CREATE_TOOL_SYSTEM_PROMPT
    )
    payload = {
        "mode": projection.mode,
        "stage_id": projection.stage_id,
        "tool_name": projection.tool_name,
        "handler": projection.handler,
        "signature": projection.signature,
        "logic_description": projection.logic_description,
        "tools": list(projection.tools),
        "input_contract": projection.input_contract,
        "output_contract": projection.output_contract,
        "existing_code": projection.existing_code,
        "existing_tests": projection.existing_tests,
        "fix_diagnostics": list(projection.fix_diagnostics),
        "repair_errors": list(repair_errors),
        "rag_conventions": rag_context_xml,
    }
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": json.dumps(payload, indent=2, default=str)},
    ]


def build_prompt_template_messages(
    projection: LeafCoderProjection,
    *,
    template_ref: str,
    existing_template: Optional[str] = None,
    rag_context_xml: str = "",
    repair_errors: Sequence[str] = (),
) -> list[dict[str, str]]:
    payload = {
        "mode": projection.mode,
        "stage_id": projection.stage_id,
        "template_ref": template_ref,
        "logic_description": projection.logic_description,
        "input_contract": projection.input_contract,
        "output_contract": projection.output_contract,
        "existing_template": existing_template,
        "fix_diagnostics": list(projection.fix_diagnostics),
        "repair_errors": list(repair_errors),
        "rag_conventions": rag_context_xml,
    }
    return [
        {"role": "system", "content": LEAF_PROMPT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, indent=2, default=str)},
    ]
