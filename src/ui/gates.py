"""Interactive human-in-the-loop gate renderers, parsers, and Chainlit handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import chainlit as cl
import yaml

DEFAULT_GATE_TIMEOUT_SECONDS = 3600

_IMPL_DOC_FILE_ORDER: tuple[str, ...] = (
    "agent.yaml",
    "stages.yaml",
    "contracts.yaml",
    "governance.yaml",
    "config.yaml",
    "tests.yaml",
    "tier3_contract.yaml",
)


def uri_to_local_path(uri_or_path: Optional[str]) -> Optional[Path]:
    """Convert a file:// URI or filesystem path string to a local Path, or None."""
    raw = str(uri_or_path or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("file://"):
        parsed = urlparse(raw)
        path_part = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in {"localhost", ""}:
            combined = f"//{parsed.netloc}{path_part}"
            return Path(url2pathname(combined))
        return Path(url2pathname(path_part))
    return Path(raw)


def load_impl_doc_previews(
    storage_uri: Optional[str],
    *,
    max_chars_per_file: int = 1800,
) -> dict[str, str]:
    """Read YAML implementation document previews from ``storage_uri`` cleanly."""
    base_dir = uri_to_local_path(storage_uri)
    if base_dir is None or not base_dir.is_dir():
        return {}

    previews: dict[str, str] = {}
    ordered_names = list(_IMPL_DOC_FILE_ORDER)
    extra_yaml = sorted(
        p.name
        for p in base_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml"} and p.name not in _IMPL_DOC_FILE_ORDER
    )
    for filename in [*ordered_names, *extra_yaml]:
        file_path = base_dir / filename
        if not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if max_chars_per_file > 0 and len(text) > max_chars_per_file:
            text = text[:max_chars_per_file].rstrip() + "\n# ... (truncated for preview)"
        previews[filename] = text
    return previews


def _question_id_lookup(questions: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], list[str]]:
    canonical_ids: list[str] = []
    aliases: dict[str, str] = {}
    for idx, item in enumerate(questions, start=1):
        qid = str(item.get("question_id") or f"q{idx}").strip()
        canonical_ids.append(qid)
        aliases[qid.lower()] = qid
        aliases[str(idx)] = qid
        aliases[f"q{idx}"] = qid
        aliases[f"q-{idx}"] = qid
        aliases[f"q-{idx:03d}"] = qid
        field_path = str(item.get("field_path") or "").strip().lower()
        if field_path:
            aliases[field_path] = qid
    return aliases, canonical_ids


def parse_clarification_answers(
    raw_text: str,
    questions: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Parse user clarification replies from JSON, YAML, numbered lines, or prose."""
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        return {}

    lowered = cleaned.lower()
    if lowered in {"default", "defaults", "use defaults", "approve", "approve defaults"}:
        return {}

    aliases, canonical_ids = _question_id_lookup(questions)

    # 1. Try JSON or YAML object mapping first.
    if cleaned.startswith("{") or ":" in cleaned:
        try:
            loaded = json.loads(cleaned) if cleaned.startswith("{") else yaml.safe_load(cleaned)
        except Exception:
            loaded = None
        if isinstance(loaded, Mapping):
            mapped: dict[str, str] = {}
            for key, val in loaded.items():
                key_norm = str(key).strip().lower()
                target_id = aliases.get(key_norm)
                if target_id is None and len(canonical_ids) == 1:
                    target_id = canonical_ids[0]
                if target_id is not None and val is not None:
                    ans = str(val).strip()
                    if ans:
                        mapped[target_id] = ans
            if mapped:
                return mapped

    # 2. Try line-by-line prefixed format like "Q-001: ...", "1. ...", "[q1] ...".
    line_answers: dict[str, str] = {}
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        sep_idx = -1
        for sep in (":", ")", "."):
            pos = line.find(sep)
            if 0 < pos <= 24:
                prefix = line[:pos].strip("[]()- ").lower()
                if prefix in aliases:
                    target_id = aliases[prefix]
                    value = line[pos + 1 :].strip()
                    if value:
                        line_answers[target_id] = value
                    sep_idx = pos
                    break
        if sep_idx == -1 and " - " in line:
            prefix, value = line.split(" - ", 1)
            prefix_norm = prefix.strip("[]() ").lower()
            if prefix_norm in aliases and value.strip():
                line_answers[aliases[prefix_norm]] = value.strip()

    if line_answers:
        return line_answers

    # 3. Single question or non-empty multi-line / single free-text fallback.
    non_empty_lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if len(canonical_ids) == 1:
        return {canonical_ids[0]: cleaned}
    if len(non_empty_lines) == len(canonical_ids) and non_empty_lines:
        return dict(zip(canonical_ids, non_empty_lines, strict=False))
    if canonical_ids:
        return {canonical_ids[0]: cleaned}
    return {}


def render_clarification_prompt(payload: Mapping[str, Any]) -> str:
    """Render Gate 1 (Spec Clarification) markdown message."""
    round_num = payload.get("round_number", 1)
    max_rounds = payload.get("max_rounds", 3)
    agent_name = payload.get("agent_name") or "New DDLC Agent"
    questions = list(payload.get("questions") or [])

    lines = [
        f"### Gate 1: Specification Clarification ({agent_name})",
        f"**Round:** `{round_num} / {max_rounds}`",
        "",
    ]
    if not questions:
        lines.append("No open clarification questions remain. Choose an option below to proceed.")
        return "\n".join(lines)

    lines.append("Please review the open specification gaps below:")
    lines.append("")
    for idx, q in enumerate(questions, start=1):
        qid = q.get("question_id") or f"q{idx}"
        category = q.get("category") or "general"
        field_path = q.get("field_path") or "unspecified"
        question_text = q.get("question") or ""
        suggested = q.get("suggested_default")
        impact = q.get("impact_area")
        lines.append(f"**{idx}. `{qid}`** ({category} · `{field_path}`)")
        lines.append(f"- Question: {question_text}")
        if suggested:
            lines.append(f"- Suggested default: `{suggested}`")
        if impact:
            lines.append(f"- Impact: {impact}")
        lines.append("")

    lines.append(
        "_Click **Approve Suggested Defaults** to accept all defaults automatically, "
        "or **Answer Questions** to supply custom responses._"
    )
    return "\n".join(lines)


def render_feasibility_prompt(payload: Mapping[str, Any]) -> str:
    """Render Gate 1b (Feasibility Escalation) markdown message."""
    verdict = payload.get("verdict", "not_feasible")
    summary = payload.get("summary") or "Researcher flagged technical or policy constraints."
    constraints = list(payload.get("policy_constraints") or [])
    risks = list(payload.get("risks") or [])

    lines = [
        "### Gate 1b: Technical Feasibility Escalation",
        f"**Verdict:** `{verdict}`",
        "",
        f"**Summary:** {summary}",
    ]
    if constraints:
        lines.extend(["", "**Policy / Compliance Constraints:**"])
        lines.extend(f"- {item}" for item in constraints)
    if risks:
        lines.extend(["", "**Identified Risks:**"])
        lines.extend(f"- {item}" for item in risks)
    lines.extend(
        [
            "",
            "_Choose **Revise Constraints** to amend the request, "
            "**Override & Continue** to accept risk, or **Abort** to stop cleanly._",
        ]
    )
    return "\n".join(lines)


def render_architecture_prompt(payload: Mapping[str, Any]) -> str:
    """Render Gate 2 (Architecture Review) markdown message."""
    rev = payload.get("revision_number", 1)
    max_rev = payload.get("max_revisions", 3)
    agent_name = payload.get("agent_name") or "agent"
    pattern = payload.get("pattern") or "unknown"
    runtime = payload.get("runtime") or "unknown"
    summary = payload.get("summary") or ""
    proposal = payload.get("proposal") or {}

    lines = [
        f"### Gate 2: Architecture Review (`{agent_name}`)",
        f"**Revision:** `{rev} / {max_rev}` · **Pattern:** `{pattern}` · **Runtime:** `{runtime}`",
    ]
    if summary:
        lines.extend(["", summary])

    stages = list(proposal.get("stages") or [])
    if stages:
        lines.extend(["", "**Proposed Stages:**", "| Stage | Type | Autonomy | Tools |", "| --- | --- | --- | --- |"])
        for stg in stages:
            sid = stg.get("stage_id") or stg.get("id") or ""
            stype = stg.get("stage_type") or ""
            auto = stg.get("autonomy") or ""
            tools = ", ".join(str(t) for t in (stg.get("tools") or [])) or "-"
            lines.append(f"| `{sid}` | `{stype}` | `{auto}` | {tools} |")

    reused = list(proposal.get("reused_components") or [])
    new_comps = list(proposal.get("new_components_to_create") or [])
    contracts = list(proposal.get("contracts_to_define") or [])
    tier3_required = bool(proposal.get("tier_3_schema_required"))
    tier3_tables = list(proposal.get("tier_3_tables") or [])
    risks = list(proposal.get("risks_and_tradeoffs") or [])

    if reused:
        reused_labels = [
            f"`{c.get('component_name') or c.get('component_id')}` ({c.get('usage_type', 'import')})"
            for c in reused
            if isinstance(c, Mapping)
        ]
        if reused_labels:
            lines.extend(["", f"**Reused Tier 2 Components:** {', '.join(reused_labels)}"])
    if new_comps:
        new_labels = [
            f"`{c.get('name') or c.get('component_name') or c}`"
            for c in new_comps
        ]
        lines.extend(["", f"**New Components:** {', '.join(new_labels)}"])
    if contracts:
        lines.extend(["", "**Contracts:** " + ", ".join(f"`{c}`" for c in contracts)])
    if tier3_required or tier3_tables:
        tbl_str = ", ".join(f"`{t}`" for t in tier3_tables) if tier3_tables else "yes"
        lines.extend(["", f"**Tier 3 Metadata Tables:** {tbl_str}"])
    if risks:
        lines.extend(["", "**Tradeoffs & Risks:**"])
        lines.extend(f"- {r}" for r in risks)

    return "\n".join(lines)


def render_impl_doc_prompt(
    payload: Mapping[str, Any],
    *,
    previews: Optional[Mapping[str, str]] = None,
) -> str:
    """Render Gate 3 (Implementation Docs Review + Claim Check SHA-256) markdown message."""
    rev = payload.get("revision_number", 1)
    max_rev = payload.get("max_revisions", 3)
    agent_name = payload.get("agent_name") or "agent"
    summary = payload.get("summary") or ""
    stages = list(payload.get("stages") or [])
    contracts = list(payload.get("contracts") or [])
    has_tier3 = bool(payload.get("has_tier3_contract"))
    sha256 = payload.get("impl_docs_sha256")
    storage_uri = payload.get("storage_uri")

    lines = [
        f"### Gate 3: Implementation Docs Freeze (`{agent_name}`)",
        f"**Revision:** `{rev} / {max_rev}` · **Tier 3 Contract:** `{'yes' if has_tier3 else 'no'}`",
    ]
    if sha256:
        lines.append(f"**Claim Check SHA-256:** `{sha256}`")
    if storage_uri:
        lines.append(f"**Location:** `{storage_uri}`")
    if summary:
        lines.extend(["", summary])
    if stages:
        lines.extend(["", "**Stages:** " + ", ".join(f"`{s}`" for s in stages)])
    if contracts:
        lines.extend(["", "**Contracts:** " + ", ".join(f"`{c}`" for c in contracts)])

    resolved_previews = (
        previews if previews is not None else load_impl_doc_previews(storage_uri, max_chars_per_file=900)
    )
    if resolved_previews:
        file_list = ", ".join(f"`{name}`" for name in resolved_previews)
        lines.extend(["", f"**Generated `impl_docs/` Files:** {file_list}"])
        if "agent.yaml" in resolved_previews:
            lines.extend(["", "```yaml", "# agent.yaml (preview)", resolved_previews["agent.yaml"], "```"])

    return "\n".join(lines)


def render_build_escalation_prompt(payload: Mapping[str, Any]) -> str:
    """Render Gate 4 (Build / Validation Escalation) markdown message."""
    attempts = payload.get("build_fix_attempt_count", 0)
    max_attempts = payload.get("max_build_fix_attempts", 3)
    code_uri = payload.get("code_storage_uri") or "unknown"
    fix_instructions = list(payload.get("fix_instructions") or [])
    issues = list(payload.get("issues") or [])

    lines = [
        "### Gate 4: Build & Validation Escalation",
        f"**Fix Attempts:** `{attempts} / {max_attempts}`",
        f"**Generated Code URI:** `{code_uri}`",
    ]
    if fix_instructions:
        lines.extend(["", "**Latest Fix Instructions:**"])
        lines.extend(f"- {item}" for item in fix_instructions)
    if issues:
        lines.extend(["", "**Validation Issues:**"])
        for iss in issues[:12]:
            if isinstance(iss, Mapping):
                sev = str(iss.get("severity") or "error").upper()
                check = iss.get("check_name") or iss.get("code") or "check"
                msg = iss.get("message") or str(iss)
                fpath = iss.get("file_path")
                loc = f" (`{fpath}`)" if fpath else ""
                lines.append(f"- **[{sev}] `{check}`**{loc}: {msg}")
            else:
                lines.append(f"- {iss}")
    lines.extend(
        [
            "",
            "_If you fixed the code on disk directly, choose **Re-validate Code**. "
            "Otherwise choose **Send Fix Feedback** or **Abort**._",
        ]
    )
    return "\n".join(lines)


def render_gate_prompt(payload: Mapping[str, Any]) -> str:
    """Render the appropriate markdown prompt for any Coordinator gate payload."""
    gate = str(payload.get("gate") or "")
    renderers = {
        "clarification": render_clarification_prompt,
        "feasibility_escalation": render_feasibility_prompt,
        "architecture_review": render_architecture_prompt,
        "impl_doc_review": render_impl_doc_prompt,
        "build_escalation": render_build_escalation_prompt,
    }
    renderer = renderers.get(gate)
    if renderer is None:
        return f"### Human Gate (`{gate or 'unknown'}`)\n\n```json\n{json.dumps(dict(payload), indent=2, default=str)}\n```"
    return renderer(payload)


def _extract_action_name(response: Any) -> Optional[str]:
    if response is None:
        return None
    if isinstance(response, Mapping):
        name = response.get("name")
        if name:
            return str(name)
        payload = response.get("payload")
        if isinstance(payload, Mapping) and payload.get("decision"):
            return str(payload["decision"])
    attr_name = getattr(response, "name", None)
    return str(attr_name) if attr_name else None


def _extract_user_output(response: Any) -> Optional[str]:
    if response is None:
        return None
    if isinstance(response, Mapping):
        if "output" in response and response["output"] is not None:
            return str(response["output"])
        if "content" in response and response["content"] is not None:
            return str(response["content"])
        return None
    out = getattr(response, "output", None)
    return str(out) if out is not None else None


async def _ask_action(
    content: str,
    actions: list[cl.Action],
    *,
    timeout: int = DEFAULT_GATE_TIMEOUT_SECONDS,
) -> Optional[str]:
    res = await cl.AskActionMessage(
        content=content,
        actions=actions,
        timeout=timeout,
        raise_on_timeout=False,
    ).send()
    return _extract_action_name(res)


async def _ask_feedback_text(
    prompt: str,
    *,
    timeout: int = DEFAULT_GATE_TIMEOUT_SECONDS,
) -> Optional[str]:
    res = await cl.AskUserMessage(
        content=prompt,
        timeout=timeout,
        raise_on_timeout=False,
    ).send()
    text = _extract_user_output(res)
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


async def _handle_clarification_gate(
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    questions = list(payload.get("questions") or [])
    prompt = render_clarification_prompt(payload)
    actions = [
        cl.Action(
            name="approve_defaults",
            payload={"decision": "approved"},
            label="Approve Suggested Defaults",
            tooltip="Accept suggested defaults for all questions",
        ),
        cl.Action(
            name="answer_questions",
            payload={"decision": "answer"},
            label="Answer Questions",
            tooltip="Provide custom answers to clarification questions",
        ),
        cl.Action(
            name="abort",
            payload={"decision": "abort"},
            label="Abort",
            tooltip="Stop the build",
        ),
    ]
    choice = await _ask_action(prompt, actions, timeout=timeout)
    if choice is None:
        return None
    if choice == "abort":
        return {
            "decision": "abort",
            "answers": {},
            "reviewer": reviewer,
            "feedback": "Aborted at specification clarification gate.",
        }
    if choice == "approve_defaults":
        return {
            "decision": "approved",
            "answers": {},
            "reviewer": reviewer,
            "feedback": None,
        }

    if questions:
        id_hint = ", ".join(f"`{q.get('question_id')}`" for q in questions if q.get("question_id"))
        answer_prompt = (
            "Enter your clarification answers (for example, `q1: your answer` per line, "
            f"YAML/JSON, or one line per question for {id_hint}). "
            "Type `defaults` to accept suggested defaults."
        )
        raw_reply = await _ask_feedback_text(answer_prompt, timeout=timeout)
        if raw_reply is None:
            return None
        answers = parse_clarification_answers(raw_reply, questions)
    else:
        answers = {}

    return {
        "decision": "approved",
        "answers": answers,
        "reviewer": reviewer,
        "feedback": None,
    }


async def _handle_feasibility_gate(
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    prompt = render_feasibility_prompt(payload)
    actions = [
        cl.Action(
            name="revise",
            payload={"decision": "revise"},
            label="Revise Constraints",
            tooltip="Send clarification/constraint changes and re-evaluate feasibility",
        ),
        cl.Action(
            name="approved",
            payload={"decision": "approved"},
            label="Override & Continue",
            tooltip="Proceed to Architecture Design despite warnings",
        ),
        cl.Action(
            name="abort",
            payload={"decision": "abort"},
            label="Abort Build",
            tooltip="Abort this build safely",
        ),
    ]
    choice = await _ask_action(prompt, actions, timeout=timeout)
    if choice is None:
        return None
    if choice == "revise":
        feedback = await _ask_feedback_text(
            "Provide revised requirements, approved library alternatives, or policy exceptions:",
            timeout=timeout,
        )
        if feedback is None:
            return None
        return {"decision": "revise", "reviewer": reviewer, "feedback": feedback}
    if choice == "approved":
        return {
            "decision": "approved",
            "reviewer": reviewer,
            "feedback": "Human override at technical feasibility gate.",
        }
    return {
        "decision": "abort",
        "reviewer": reviewer,
        "feedback": "Aborted at technical feasibility gate.",
    }


async def _handle_architecture_gate(
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    prompt = render_architecture_prompt(payload)
    actions = [
        cl.Action(
            name="approved",
            payload={"decision": "approved"},
            label="Approve Architecture",
            tooltip="Lock architecture and generate implementation docs",
        ),
        cl.Action(
            name="revise",
            payload={"decision": "revise"},
            label="Request Revision",
            tooltip="Send design feedback to Architect",
        ),
        cl.Action(
            name="escalate",
            payload={"decision": "escalate"},
            label="Escalate to Spec Analyst",
            tooltip="Return upstream to clarify specification",
        ),
        cl.Action(
            name="abort",
            payload={"decision": "abort"},
            label="Abort Build",
            tooltip="Cancel this run",
        ),
    ]
    choice = await _ask_action(prompt, actions, timeout=timeout)
    if choice is None:
        return None
    if choice == "approved":
        return {"decision": "approved", "reviewer": reviewer, "feedback": None}
    if choice in {"revise", "escalate"}:
        verb = "architecture revision" if choice == "revise" else "upstream spec escalation"
        feedback = await _ask_feedback_text(
            f"Describe your feedback for {verb}:",
            timeout=timeout,
        )
        if feedback is None:
            return None
        return {"decision": choice, "reviewer": reviewer, "feedback": feedback}
    return {
        "decision": "abort",
        "reviewer": reviewer,
        "feedback": "Aborted at architecture review gate.",
    }


async def _handle_impl_doc_gate(
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    storage_uri = payload.get("storage_uri")
    previews = load_impl_doc_previews(storage_uri, max_chars_per_file=2400)
    prompt = render_impl_doc_prompt(payload, previews=previews)

    while True:
        actions = [
            cl.Action(
                name="approved",
                payload={"decision": "approved"},
                label="Approve & Freeze Impl Docs",
                tooltip="Freeze implementation docs and start deterministic code build",
            ),
            cl.Action(
                name="revise",
                payload={"decision": "revise"},
                label="Request Revision",
                tooltip="Send feedback to Documentarian",
            ),
            cl.Action(
                name="escalate",
                payload={"decision": "escalate"},
                label="Escalate to Architecture",
                tooltip="Return upstream to Architect",
            ),
        ]
        if previews:
            actions.append(
                cl.Action(
                    name="inspect_yaml",
                    payload={"decision": "inspect_yaml"},
                    label="Inspect Full YAML Files",
                    tooltip="Display full impl_docs/*.yaml previews before deciding",
                )
            )
        actions.append(
            cl.Action(
                name="abort",
                payload={"decision": "abort"},
                label="Abort Build",
                tooltip="Cancel this run",
            )
        )

        choice = await _ask_action(prompt, actions, timeout=timeout)
        if choice is None:
            return None
        if choice == "inspect_yaml":
            yaml_sections = [
                f"#### `{name}`\n```yaml\n{content}\n```"
                for name, content in previews.items()
            ]
            await cl.Message(
                content="\n\n".join(yaml_sections) or "No YAML files found at storage URI."
            ).send()
            continue
        if choice == "approved":
            return {"decision": "approved", "reviewer": reviewer, "feedback": None}
        if choice in {"revise", "escalate"}:
            verb = "impl_docs revision" if choice == "revise" else "architecture escalation"
            feedback = await _ask_feedback_text(
                f"Describe your feedback for {verb}:",
                timeout=timeout,
            )
            if feedback is None:
                return None
            return {"decision": choice, "reviewer": reviewer, "feedback": feedback}
        return {
            "decision": "abort",
            "reviewer": reviewer,
            "feedback": "Aborted at implementation docs review gate.",
        }


async def _handle_build_escalation_gate(
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    prompt = render_build_escalation_prompt(payload)
    actions = [
        cl.Action(
            name="approved",
            payload={"decision": "approved"},
            label="Re-validate Code",
            tooltip="Re-run Validator against current files on disk",
        ),
        cl.Action(
            name="revise",
            payload={"decision": "revise"},
            label="Send Fix Feedback",
            tooltip="Instruct Builder to repair and re-validate generated code",
        ),
        cl.Action(
            name="abort",
            payload={"decision": "abort"},
            label="Abort Build",
            tooltip="Stop the build cleanly",
        ),
    ]
    choice = await _ask_action(prompt, actions, timeout=timeout)
    if choice is None:
        return None
    if choice == "approved":
        return {
            "decision": "approved",
            "reviewer": reviewer,
            "feedback": "Human verified manual fixes on disk; re-validate.",
        }
    if choice == "revise":
        feedback = await _ask_feedback_text(
            "Provide concrete fix instructions for the Builder:",
            timeout=timeout,
        )
        if feedback is None:
            return None
        return {"decision": "revise", "reviewer": reviewer, "feedback": feedback}
    return {
        "decision": "abort",
        "reviewer": reviewer,
        "feedback": "Aborted at build escalation gate.",
    }


async def handle_gate_interrupt(
    payload: Mapping[str, Any],
    *,
    reviewer: str = "studio-user",
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS,
) -> Optional[dict[str, Any]]:
    """Present the interactive Chainlit UI prompt for a Coordinator interrupt payload.

    Returns a Coordinator-compatible resume payload dictionary, or ``None`` if the
    prompt timed out so the caller can preserve the active checkpoint untouched.
    """
    gate = str(payload.get("gate") or "").strip().lower()
    if gate == "clarification":
        return await _handle_clarification_gate(payload, reviewer=reviewer, timeout=timeout_seconds)
    if gate == "feasibility_escalation":
        return await _handle_feasibility_gate(payload, reviewer=reviewer, timeout=timeout_seconds)
    if gate == "architecture_review":
        return await _handle_architecture_gate(payload, reviewer=reviewer, timeout=timeout_seconds)
    if gate == "impl_doc_review":
        return await _handle_impl_doc_gate(payload, reviewer=reviewer, timeout=timeout_seconds)
    if gate == "build_escalation":
        return await _handle_build_escalation_gate(payload, reviewer=reviewer, timeout=timeout_seconds)

    # Generic fallback for any unrecognized gate type.
    choice = await _ask_action(
        render_gate_prompt(payload),
        [
            cl.Action(name="approved", payload={"decision": "approved"}, label="Approve"),
            cl.Action(name="abort", payload={"decision": "abort"}, label="Abort"),
        ],
        timeout=timeout_seconds,
    )
    if choice is None:
        return None
    return {
        "decision": choice,
        "reviewer": reviewer,
        "feedback": None if choice == "approved" else f"Aborted at gate {gate or 'unknown'}",
    }
