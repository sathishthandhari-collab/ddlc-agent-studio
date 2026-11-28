"""Architect specialist — pattern selection, Tier 2 component reuse, and Day-2 impact analysis."""

from __future__ import annotations

from typing import Any, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.specialists.architect.prompts import build_architecture_messages
from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.state.blackboard import (
    ArchitectureProposal,
    BuildLogEntry,
    StageDesignSpec,
)
from src.state.checkpointer import run_async_selector
from src.state.projections import ArchitectProjection

ALLOWED_PATTERNS: frozenset[str] = frozenset(
    {
        "stateful_pipeline_with_gates",
        "react_tool_agent",
        "multi_step_planner_agent",
        "lightweight_event_cli_agent",
        "multi_source_lineage_inspector",
    }
)


@runtime_checkable
class ArchitectRAGProtocol(Protocol):
    async def query_patterns(self, query: str) -> str: ...

    async def query_past_decisions(self, query: str) -> str: ...

    async def query_org_standards(self, topic: str) -> str: ...

    async def query_component_docs(
        self, component_or_query: str, query: Optional[str] = None
    ) -> str: ...


class ArchitectureDesignDraft(BaseModel):
    """Structured Instructor output produced by the Architect specialist."""

    pattern: Literal[
        "stateful_pipeline_with_gates",
        "react_tool_agent",
        "multi_step_planner_agent",
        "lightweight_event_cli_agent",
        "multi_source_lineage_inspector",
    ]
    runtime: Literal["langgraph", "python_litellm"]
    autonomy_level: Literal["autonomous", "semi_autonomous", "human_required"] = "semi_autonomous"
    summary: str = Field(..., min_length=1)
    stages: list[StageDesignSpec] = Field(default_factory=list)
    contracts_to_define: list[str] = Field(default_factory=list)
    reused_components: list[dict[str, Any]] = Field(default_factory=list)
    new_components_to_create: list[dict[str, Any]] = Field(default_factory=list)
    risks_and_tradeoffs: list[str] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    tier_3_schema_required: bool = False
    tier_3_tables: list[str] = Field(default_factory=list)


def _enforce_runtime_and_components(
    draft: ArchitectureDesignDraft,
    projection: ArchitectProjection,
) -> ArchitectureProposal:
    """Deterministically enforce runtime safety rules and Tier 2 usage_type integrity."""
    summary = projection.parsed_spec_summary
    requires_langgraph = (
        draft.autonomy_level in {"semi_autonomous", "human_required"}
        or summary.autonomy_level in {"semi_autonomous", "human_required"}
        or bool(summary.approval_gates)
        or any(
            s.autonomy == "human_required" or s.stage_type == "human_review"
            for s in draft.stages
        )
    )
    runtime: Literal["langgraph", "python_litellm"] = (
        "langgraph" if requires_langgraph else draft.runtime
    )
    pattern = draft.pattern if draft.pattern in ALLOWED_PATTERNS else "stateful_pipeline_with_gates"
    if runtime == "langgraph" and pattern == "lightweight_event_cli_agent":
        pattern = "stateful_pipeline_with_gates"

    normalized_reused: list[dict[str, Any]] = []
    for comp in draft.reused_components:
        entry = dict(comp)
        usage = str(entry.get("usage_type") or "import").strip().lower()
        if usage not in {"import", "extend", "wrap"}:
            usage = "import"
        entry["usage_type"] = usage
        normalized_reused.append(entry)

    next_rev = projection.architecture_revision_count + 1
    return ArchitectureProposal(
        revision_number=next_rev,
        agent_name=summary.agent_name,
        agent_version=summary.agent_version,
        pattern=pattern,
        runtime=runtime,
        autonomy_level=draft.autonomy_level,
        summary=draft.summary,
        stages=list(draft.stages),
        contracts_to_define=list(draft.contracts_to_define or summary.contract_names),
        reused_components=normalized_reused,
        new_components_to_create=list(draft.new_components_to_create),
        risks_and_tradeoffs=list(draft.risks_and_tradeoffs),
        alternatives_considered=list(draft.alternatives_considered),
        tier_3_schema_required=draft.tier_3_schema_required,
        tier_3_tables=list(draft.tier_3_tables),
        revision_feedback_addressed=projection.architecture_feedback,
        approved=False,
    )


class ArchitectSpecialist:
    """Designs DDLC agent architectures using RAG patterns/ADRs and Tier 2 semantic catalog search."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        rag_facade: Optional[ArchitectRAGProtocol] = None,
        catalog_client: Optional[Any] = None,
        lineage_client: Optional[Any] = None,
        embedder: Optional[Any] = None,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.rag_facade = rag_facade
        self.catalog_client = catalog_client
        self.lineage_client = lineage_client
        self.embedder = embedder

    def _build_state_update(
        self,
        proposal: ArchitectureProposal,
        projection: ArchitectProjection,
        downstream_impact: list[dict[str, Any]],
    ) -> dict[str, Any]:
        update: dict[str, Any] = {
            "current_phase": "design",
            "architecture_revisions": [proposal],
            "architecture_revision_count": proposal.revision_number,
            "architecture_approved": False,
            "architecture_feedback": None,
            "build_logs": [
                BuildLogEntry(
                    phase="design",
                    actor="architect",
                    action="propose_architecture",
                    status="completed",
                    details={
                        "revision": proposal.revision_number,
                        "pattern": proposal.pattern,
                        "runtime": proposal.runtime,
                        "reused_components": [
                            c.get("component_id") or c.get("component_name")
                            for c in proposal.reused_components
                        ],
                    },
                )
            ],
        }
        if projection.mode == "change" and projection.change_request is not None:
            impacted_ids = [
                str(node.get("downstream_agent_id"))
                for node in downstream_impact
                if node.get("downstream_agent_id")
            ]
            if impacted_ids:
                update["change_request"] = projection.change_request.model_copy(
                    update={"downstream_impacted_agents": impacted_ids}
                )
        return update

    async def adesign(self, projection: ArchitectProjection) -> dict[str, Any]:
        """Async RAG + Tier 2 component-guided architecture proposal."""
        summary = projection.parsed_spec_summary
        query_topic = f"{summary.agent_name} {summary.description} {summary.autonomy_level}"

        patterns_xml = ""
        past_decisions_xml = ""
        org_standards_xml = ""
        component_docs_xml = ""

        if self.rag_facade is not None:
            patterns_xml = await self.rag_facade.query_patterns(query_topic)
            past_decisions_xml = await self.rag_facade.query_past_decisions(query_topic)
            org_standards_xml = await self.rag_facade.query_org_standards(
                f"{summary.data_classification or 'internal'} runtime naming governance"
            )
            comp_query = " ".join(summary.shared_component_requests) or summary.description
            component_docs_xml = await self.rag_facade.query_component_docs(
                comp_query, query=summary.agent_name
            )

        semantic_components: list[dict[str, Any]] = []
        if self.catalog_client is not None and self.embedder is not None:
            query_vec = await self.embedder.embed_query(query_topic)
            hits = self.catalog_client.search_components_semantic(query_vec, top_k=5)
            semantic_components = [
                h.model_dump(mode="json", exclude={"embedding"}) for h in hits
            ]

        downstream_impact: list[dict[str, Any]] = []
        if (
            projection.mode == "change"
            and projection.change_request is not None
            and self.lineage_client is not None
        ):
            nodes = self.lineage_client.get_downstream_impact(projection.change_request.agent_id)
            downstream_impact = [n.model_dump(mode="json") for n in nodes]

        messages = build_architecture_messages(
            projection,
            patterns_xml=patterns_xml,
            past_decisions_xml=past_decisions_xml,
            org_standards_xml=org_standards_xml,
            component_docs_xml=component_docs_xml,
            semantic_components=semantic_components,
            downstream_impact=downstream_impact,
        )
        draft = await self.llm_client.acomplete_structured(
            specialist_name="architect",
            response_model=ArchitectureDesignDraft,
            messages=messages,
        )
        proposal = _enforce_runtime_and_components(draft, projection)
        return self._build_state_update(proposal, projection, downstream_impact)

    def design(self, projection: ArchitectProjection) -> dict[str, Any]:
        """Synchronous architecture design entrypoint."""
        if self.rag_facade is None and self.embedder is None:
            downstream_impact: list[dict[str, Any]] = []
            if (
                projection.mode == "change"
                and projection.change_request is not None
                and self.lineage_client is not None
            ):
                nodes = self.lineage_client.get_downstream_impact(
                    projection.change_request.agent_id
                )
                downstream_impact = [n.model_dump(mode="json") for n in nodes]

            messages = build_architecture_messages(
                projection,
                patterns_xml="",
                past_decisions_xml="",
                org_standards_xml="",
                component_docs_xml="",
                semantic_components=[],
                downstream_impact=downstream_impact,
            )
            draft = self.llm_client.complete_structured(
                specialist_name="architect",
                response_model=ArchitectureDesignDraft,
                messages=messages,
            )
            proposal = _enforce_runtime_and_components(draft, projection)
            return self._build_state_update(proposal, projection, downstream_impact)
        return run_async_selector(self.adesign(projection))
