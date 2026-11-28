"""Top-level Deterministic Coordinator Graph for Day-0 Build & Day-2 Change Flows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from langgraph.graph import END, StateGraph

from src.coordinator.delivery import make_delivery_node
from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.coordinator.edges import (
    route_after_construct_phase,
    route_after_design_phase,
    route_after_document_phase,
    route_after_research_phase,
    route_after_setup,
    route_after_understand_phase,
)
from src.coordinator.subgraphs import (
    build_construct_subgraph,
    build_design_subgraph,
    build_document_subgraph,
    build_research_subgraph,
    build_understand_subgraph,
)
from src.state.blackboard import (
    BlackboardState,
    BuildLogEntry,
    CatalogContext,
    ChangeRequestContext,
    ImplDocPackage,
    ImplDocRevision,
    build_contract_pointer_from_directory,
    get_latest_impl_docs,
)
from src.state.projections import project_for_architect, project_for_spec_analyst


def load_impl_docs_package_from_dir(impl_docs_dir: Path) -> ImplDocPackage:
    """Deterministically load a 5-file impl_docs/ directory into an ImplDocPackage."""
    base_dir = Path(impl_docs_dir).resolve()
    if not (base_dir / "agent.yaml").is_file():
        raise FileNotFoundError(f"Active impl_docs/agent.yaml not found at {base_dir}.")

    def _read_yaml(path: Path) -> dict[str, Any]:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return dict(loaded) if isinstance(loaded, dict) else {}

    agent_yaml = _read_yaml(base_dir / "agent.yaml")
    config_yaml = _read_yaml(base_dir / "config.yaml") if (base_dir / "config.yaml").is_file() else {}
    tests_yaml = _read_yaml(base_dir / "tests.yaml") if (base_dir / "tests.yaml").is_file() else {}

    stages: dict[str, dict[str, Any]] = {}
    stages_dir = base_dir / "stages"
    if stages_dir.is_dir():
        for stg_file in sorted(stages_dir.glob("*.yaml")):
            data = _read_yaml(stg_file)
            sid = str((data.get("stage") or {}).get("id") or stg_file.stem.removeprefix("stage_"))
            stages[sid] = data

    contracts: dict[str, dict[str, Any]] = {}
    contracts_dir = base_dir / "contracts"
    if contracts_dir.is_dir():
        for ctr_file in sorted(contracts_dir.glob("*.yaml")):
            data = _read_yaml(ctr_file)
            cname = str((data.get("contract") or {}).get("name") or ctr_file.stem)
            contracts[cname] = data

    agent_name = str((agent_yaml.get("agent") or {}).get("name") or "agent")
    agent_version = str((agent_yaml.get("agent") or {}).get("version") or "1.0.0")

    t3_yaml: Optional[dict[str, Any]] = None
    t3_file = base_dir / f"tier_3_{agent_name}.yaml"
    if t3_file.is_file():
        t3_yaml = _read_yaml(t3_file)

    pointer = build_contract_pointer_from_directory(
        contract_name=f"{agent_name}_impl_docs",
        base_dir=base_dir,
        contract_version=agent_version,
    )
    return ImplDocPackage(
        agent_yaml=agent_yaml,
        stages=stages,
        contracts=contracts,
        config_yaml=config_yaml,
        tests_yaml=tests_yaml,
        tier3_contract_yaml=t3_yaml,
        pointer=pointer,
    )


def _load_catalog_context(deps: CoordinatorDeps) -> CatalogContext:
    catalog_client = deps.get_catalog_client()
    if catalog_client is None:
        return CatalogContext()

    existing_agents = [
        a.model_dump(mode="json")
        for a in catalog_client.list_agents(status="active")
    ]
    known_contracts = [
        c.model_dump(mode="json")
        for c in catalog_client.list_contracts(status="active")
    ]
    available_components: list[dict[str, Any]] = []
    db = deps.metadata_db if deps.catalog_client is not None else deps.get_metadata_db()
    if db is not None:
        from src.metadata.db import tier2_table

        comp_tbl = tier2_table("component_registry")
        with db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT component_id, component_name, component_version, description,
                       component_type, interface_spec, storage_path, status
                FROM {comp_tbl}
                WHERE status = 'active'
                ORDER BY component_name ASC
                """
            ).fetchall()
        available_components = [dict(r) for r in rows]

    return CatalogContext(
        existing_agents=existing_agents,
        available_components=available_components,
        known_contracts=known_contracts,
    )


def build_coordinator_graph(deps: Optional[CoordinatorDeps] = None) -> StateGraph:
    """Build the uncompiled top-level Coordinator StateGraph (Day-0 Build & Day-2 Change)."""
    active_deps = deps or CoordinatorDeps()

    def node_setup(state: BlackboardState) -> dict[str, Any]:
        updates: dict[str, Any] = {"current_phase": "setup"}
        if state.get("catalog_context") is None:
            catalog = _load_catalog_context(active_deps)
            updates["catalog_context"] = catalog
        else:
            catalog = state["catalog_context"]

        if state.get("mode") == "change":
            raw_chg = state.get("change_request")
            if raw_chg is None:
                msg = "Change mode requires change_request on BlackboardState."
                return {
                    "current_phase": "failed",
                    "escalation_required": True,
                    "escalation_reason": msg,
                    "last_error": msg,
                }
            chg = (
                raw_chg
                if isinstance(raw_chg, ChangeRequestContext)
                else ChangeRequestContext.model_validate(raw_chg)
            )
            agent_id = str(state.get("target_agent_name") or chg.agent_id)
            updates["target_agent_name"] = agent_id

            if get_latest_impl_docs(state) is None:
                impl_dir = (active_deps.resolve_agents_output_dir() / agent_id / "impl_docs").resolve()
                pkg = load_impl_docs_package_from_dir(impl_dir)
                rev = ImplDocRevision(
                    revision_number=1,
                    package=pkg,
                    summary=f"Loaded active impl_docs from {impl_dir}.",
                    approved=False,
                )
                updates["impl_doc_revisions"] = [rev]
                updates["impl_doc_revision_count"] = 1
                updates["impl_docs_pointer"] = pkg.pointer

        updates["build_logs"] = [
            BuildLogEntry(
                phase="setup",
                actor="coordinator",
                action="initialize_run",
                status="completed",
                details={
                    "mode": state.get("mode", "build"),
                    "known_agents": len(catalog.existing_agents) if catalog else 0,
                    "known_components": len(catalog.available_components) if catalog else 0,
                },
            )
        ]
        return updates

    def node_change_analysis(state: BlackboardState) -> dict[str, Any]:
        projection = project_for_spec_analyst(state)
        return active_deps.get_spec_analyst().analyze_change_request(projection)

    def _sync_impact_analysis(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_architect()
        projection = project_for_architect(state)
        if getattr(specialist, "rag_facade", None) is None and getattr(specialist, "embedder", None) is None:
            out = specialist.design(projection)
        else:
            out = run_coro_sync(specialist.adesign(projection))
        proposal = out["architecture_revisions"][-1]
        out["current_phase"] = "impact_analysis"
        out["architecture_approved"] = True
        out["approved_architecture_version"] = proposal.revision_number
        out["architecture_decision"] = "approved"
        return out

    async def _async_impact_analysis(state: BlackboardState) -> dict[str, Any]:
        specialist = active_deps.get_architect()
        projection = project_for_architect(state)
        out = await specialist.adesign(projection)
        proposal = out["architecture_revisions"][-1]
        out["current_phase"] = "impact_analysis"
        out["architecture_approved"] = True
        out["approved_architecture_version"] = proposal.revision_number
        out["architecture_decision"] = "approved"
        return out

    def node_finalize_halt(state: BlackboardState) -> dict[str, Any]:
        final_phase = "aborted" if state.get("current_phase") == "aborted" else "failed"
        reason = state.get("escalation_reason") or state.get("last_error") or "Workflow halted prior to delivery."
        return {
            "current_phase": final_phase,
            "last_error": reason,
            "build_logs": [
                BuildLogEntry(
                    phase=final_phase,
                    actor="coordinator",
                    action="workflow_halted",
                    status="failed",
                    details={"reason": reason},
                )
            ],
        }

    understand_graph = build_understand_subgraph(active_deps)
    research_graph = build_research_subgraph(active_deps)
    design_graph = build_design_subgraph(active_deps)
    document_graph = build_document_subgraph(active_deps)
    construct_graph = build_construct_subgraph(active_deps)
    delivery_node = make_delivery_node(active_deps)

    workflow = StateGraph(BlackboardState)
    workflow.add_node("setup", node_setup)
    workflow.add_node("understand_phase", understand_graph)
    workflow.add_node("research_phase", research_graph)
    workflow.add_node("design_phase", design_graph)
    workflow.add_node("change_analysis", node_change_analysis)
    workflow.add_node("impact_analysis", make_dual_node(_sync_impact_analysis, _async_impact_analysis))
    workflow.add_node("document_phase", document_graph)
    workflow.add_node("construct_phase", construct_graph)
    workflow.add_node("deliver", delivery_node)
    workflow.add_node("halt", node_finalize_halt)

    workflow.set_entry_point("setup")
    workflow.add_conditional_edges(
        "setup",
        route_after_setup,
        {
            "understand_phase": "understand_phase",
            "change_analysis": "change_analysis",
            "abort": "halt",
        },
    )

    # Day-0 Build Flow
    workflow.add_conditional_edges(
        "understand_phase",
        route_after_understand_phase,
        {"research_phase": "research_phase", "abort": "halt"},
    )
    workflow.add_conditional_edges(
        "research_phase",
        route_after_research_phase,
        {"design_phase": "design_phase", "abort": "halt"},
    )
    workflow.add_conditional_edges(
        "design_phase",
        route_after_design_phase,
        {"document_phase": "document_phase", "abort": "halt"},
    )

    # Day-2 Change Flow
    workflow.add_edge("change_analysis", "impact_analysis")
    workflow.add_edge("impact_analysis", "document_phase")

    # Shared Part B: Document -> Construct -> Deliver
    workflow.add_conditional_edges(
        "document_phase",
        route_after_document_phase,
        {"construct_phase": "construct_phase", "abort": "halt"},
    )
    workflow.add_conditional_edges(
        "construct_phase",
        route_after_construct_phase,
        {
            "deliver": "deliver",
            "document_phase": "document_phase",
            "abort": "halt",
        },
    )
    workflow.add_edge("deliver", END)
    workflow.add_edge("halt", END)
    return workflow


def compile_coordinator_graph(
    *,
    deps: Optional[CoordinatorDeps] = None,
    checkpointer: Optional[Any] = None,
) -> Any:
    """Compile the Coordinator graph with an explicit or in-memory checkpointer."""
    from src.state.checkpointer import apply_allowlist_to_checkpointer, create_memory_checkpointer

    graph = build_coordinator_graph(deps=deps)
    saver = checkpointer if checkpointer is not None else create_memory_checkpointer()
    saver = apply_allowlist_to_checkpointer(saver)
    return graph.compile(checkpointer=saver)
