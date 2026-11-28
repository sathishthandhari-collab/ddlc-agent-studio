"""Researcher specialist — RAG-grounded feasibility evaluation and Tier 2 component discovery."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.specialists.researcher.prompts import build_research_messages
from src.state.blackboard import (
    BuildLogEntry,
    FeasibilityStatus,
    ResearchFindings,
    ToolEvaluation,
)
from src.state.checkpointer import run_async_selector
from src.state.projections import ResearcherProjection


@runtime_checkable
class ResearcherRAGProtocol(Protocol):
    async def query_tool_docs(self, tool_name: str, query: str) -> str: ...

    async def query_org_standards(self, topic: str) -> str: ...


class ResearchAnalysisOutput(BaseModel):
    """Structured Instructor output emitted by the Researcher specialist."""

    verdict: FeasibilityStatus
    summary: str = Field(..., min_length=1)
    tool_evaluations: list[ToolEvaluation] = Field(default_factory=list)
    policy_constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def _extract_rag_sources(xml_slices: list[str]) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for raw_xml in xml_slices:
        if not raw_xml or not raw_xml.strip():
            continue
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError:
            continue
        for chunk_el in root.findall(".//chunk"):
            src = (chunk_el.attrib.get("source") or chunk_el.attrib.get("source_path") or "").strip()
            if src and src not in seen:
                seen.add(src)
                sources.append(src)
    return sources


def _collect_requested_tools(parsed_spec: Mapping[str, Any]) -> list[str]:
    tools: list[str] = []
    seen: set[str] = set()

    def _add(item: Any) -> None:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            tools.append(text)

    constraints = parsed_spec.get("constraints") or {}
    for lib in constraints.get("required_libraries") or []:
        _add(lib)

    for stg in parsed_spec.get("stages") or []:
        if isinstance(stg, Mapping):
            for t in stg.get("tools") or []:
                _add(t)

    infra = parsed_spec.get("infrastructure") or {}
    for endpoint in list(infra.get("sources") or []) + list(infra.get("destinations") or []):
        if isinstance(endpoint, Mapping) and endpoint.get("system"):
            _add(endpoint["system"])

    return tools


def _detect_avoided_conflicts(parsed_spec: Mapping[str, Any]) -> list[str]:
    constraints = parsed_spec.get("constraints") or {}
    avoided = {
        str(lib).strip().lower(): str(lib).strip()
        for lib in (constraints.get("avoided_libraries") or [])
        if str(lib).strip()
    }
    if not avoided:
        return []

    conflicts: list[str] = []
    for lib in constraints.get("required_libraries") or []:
        key = str(lib).strip().lower()
        if key in avoided:
            conflicts.append(
                f"Library '{lib}' appears in both constraints.required_libraries and constraints.avoided_libraries."
            )

    for stg in parsed_spec.get("stages") or []:
        if not isinstance(stg, Mapping):
            continue
        for tool in stg.get("tools") or []:
            key = str(tool).strip().lower()
            if key in avoided:
                conflicts.append(
                    f"Stage '{stg.get('id')}' requests '{tool}', which is banned in constraints.avoided_libraries."
                )
    return conflicts


def _match_catalog_components(
    parsed_spec: Mapping[str, Any],
    available_components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministically match requested or capability-relevant Tier 2 components."""
    if not available_components:
        return []

    cross = parsed_spec.get("cross_agent") or {}
    requested_names = {
        str(sc.get("name") or "").strip().lower()
        for sc in (cross.get("shared_components") or [])
        if isinstance(sc, Mapping)
    }
    stage_tokens: set[str] = set()
    for stg in parsed_spec.get("stages") or []:
        if isinstance(stg, Mapping):
            for t in stg.get("tools") or []:
                stage_tokens.add(str(t).strip().lower())
            if stg.get("type"):
                stage_tokens.add(str(stg["type"]).strip().lower())

    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for comp in available_components:
        cid = str(comp.get("component_id") or "")
        cname = str(comp.get("component_name") or "").lower()
        ctype = str(comp.get("component_type") or "").lower()
        desc = str(comp.get("description") or "").lower()

        is_hit = (
            cname in requested_names
            or cname in stage_tokens
            or ("discovery" in stage_tokens and ctype == "connector")
            or ("delivery" in stage_tokens and "git" in desc)
        )
        if is_hit and (cid or cname) not in seen_ids:
            seen_ids.add(cid or cname)
            matched.append(dict(comp))
    return matched


class ResearcherSpecialist:
    """Evaluates technical feasibility against RAG library_docs/org_standards and Tier 2 catalog."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        rag_facade: Optional[ResearcherRAGProtocol] = None,
        catalog_client: Optional[Any] = None,
        embedder: Optional[Any] = None,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.rag_facade = rag_facade
        self.catalog_client = catalog_client
        self.embedder = embedder

    def _assemble_findings(
        self,
        *,
        analysis: ResearchAnalysisOutput,
        avoided_conflicts: list[str],
        candidates: list[dict[str, Any]],
        rag_sources: list[str],
    ) -> dict[str, Any]:
        verdict = analysis.verdict
        policy_constraints = list(analysis.policy_constraints)
        for conflict in avoided_conflicts:
            if conflict not in policy_constraints:
                policy_constraints.append(conflict)
        if avoided_conflicts and verdict == "feasible":
            verdict = "not_feasible"

        findings = ResearchFindings(
            verdict=verdict,
            summary=analysis.summary,
            tool_evaluations=list(analysis.tool_evaluations),
            policy_constraints=policy_constraints,
            risks=list(analysis.risks),
            candidate_shared_components=candidates,
            rag_sources=rag_sources,
        )
        return {
            "current_phase": "research",
            "research_findings": findings,
            "feasibility_verdict": findings.verdict,
            "build_logs": [
                BuildLogEntry(
                    phase="research",
                    actor="researcher",
                    action="evaluate_feasibility",
                    status="completed",
                    details={
                        "verdict": findings.verdict,
                        "tools_evaluated": len(findings.tool_evaluations),
                        "candidate_components": len(candidates),
                        "rag_sources": rag_sources,
                    },
                )
            ],
        }

    async def aresearch(self, projection: ResearcherProjection) -> dict[str, Any]:
        """Async RAG-grounded feasibility evaluation."""
        parsed = projection.parsed_spec
        requested_tools = _collect_requested_tools(parsed)
        avoided_conflicts = _detect_avoided_conflicts(parsed)

        tool_rag_slices: dict[str, str] = {}
        org_standards_xml = ""
        raw_xml_list: list[str] = []

        if self.rag_facade is not None:
            agent_desc = (parsed.get("agent") or {}).get("description") or "DDLC agent pipeline"
            for tool_name in requested_tools[:6]:
                xml_slice = await self.rag_facade.query_tool_docs(
                    tool_name,
                    f"capabilities limitations compatibility for {agent_desc}",
                )
                tool_rag_slices[tool_name] = xml_slice
                raw_xml_list.append(xml_slice)

            classification = (parsed.get("governance") or {}).get("data_classification") or "internal"
            org_standards_xml = await self.rag_facade.query_org_standards(
                f"architecture governance security naming {classification}"
            )
            raw_xml_list.append(org_standards_xml)

        candidates = _match_catalog_components(parsed, projection.available_components)
        if self.catalog_client is not None and self.embedder is not None:
            agent_desc = str((parsed.get("agent") or {}).get("description") or "DDLC shared component")
            # Two-phase sequence: embed query outside DB transaction/connection
            query_vec = await self.embedder.embed_query(agent_desc)
            semantic_hits = self.catalog_client.search_components_semantic(query_vec, top_k=5)
            existing_names = {c.get("component_name") for c in candidates}
            for hit in semantic_hits:
                hit_dict = hit.model_dump(mode="json", exclude={"embedding"})
                if hit_dict.get("component_name") not in existing_names:
                    candidates.append(hit_dict)
                    existing_names.add(hit_dict.get("component_name"))

        messages = build_research_messages(
            projection,
            tool_rag_slices=tool_rag_slices,
            org_standards_xml=org_standards_xml,
            avoided_conflicts=avoided_conflicts,
            candidate_components=candidates,
        )
        analysis = await self.llm_client.acomplete_structured(
            specialist_name="researcher",
            response_model=ResearchAnalysisOutput,
            messages=messages,
        )
        rag_sources = _extract_rag_sources(raw_xml_list)
        return self._assemble_findings(
            analysis=analysis,
            avoided_conflicts=avoided_conflicts,
            candidates=candidates,
            rag_sources=rag_sources,
        )

    def research(self, projection: ResearcherProjection) -> dict[str, Any]:
        """Synchronous feasibility evaluation (avoids event loop overhead when no async facade is bound)."""
        if self.rag_facade is None and self.embedder is None:
            parsed = projection.parsed_spec
            avoided_conflicts = _detect_avoided_conflicts(parsed)
            candidates = _match_catalog_components(parsed, projection.available_components)
            messages = build_research_messages(
                projection,
                tool_rag_slices={},
                org_standards_xml="",
                avoided_conflicts=avoided_conflicts,
                candidate_components=candidates,
            )
            analysis = self.llm_client.complete_structured(
                specialist_name="researcher",
                response_model=ResearchAnalysisOutput,
                messages=messages,
            )
            return self._assemble_findings(
                analysis=analysis,
                avoided_conflicts=avoided_conflicts,
                candidates=candidates,
                rag_sources=[],
            )
        return run_async_selector(self.aresearch(projection))
