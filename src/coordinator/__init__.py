"""Deterministic LangGraph Coordinator orchestration package."""

from src.coordinator.delivery import (
    DeliveryError,
    GitDeliveryResult,
    anode_delivery,
    make_delivery_node,
    node_delivery,
    publish_agent_to_local_git,
    synthesize_adr_markdown,
)
from src.coordinator.dependencies import CoordinatorDeps, make_dual_node, run_coro_sync
from src.coordinator.edges import (
    route_after_architecture_review,
    route_after_build_escalation,
    route_after_clarification_gate,
    route_after_construct_phase,
    route_after_design_phase,
    route_after_document_phase,
    route_after_feasibility_gate,
    route_after_impl_doc_review,
    route_after_research,
    route_after_research_phase,
    route_after_setup,
    route_after_spec_analysis,
    route_after_understand_phase,
    route_after_validation,
)
from src.coordinator.graph import (
    build_coordinator_graph,
    compile_coordinator_graph,
    load_impl_docs_package_from_dir,
)
from src.coordinator.subgraphs import (
    build_construct_subgraph,
    build_design_subgraph,
    build_document_subgraph,
    build_research_subgraph,
    build_understand_subgraph,
)

__all__ = [
    # dependencies
    "CoordinatorDeps",
    "make_dual_node",
    "run_coro_sync",
    # delivery
    "DeliveryError",
    "GitDeliveryResult",
    "anode_delivery",
    "make_delivery_node",
    "node_delivery",
    "publish_agent_to_local_git",
    "synthesize_adr_markdown",
    # edges
    "route_after_architecture_review",
    "route_after_build_escalation",
    "route_after_clarification_gate",
    "route_after_construct_phase",
    "route_after_design_phase",
    "route_after_document_phase",
    "route_after_feasibility_gate",
    "route_after_impl_doc_review",
    "route_after_research",
    "route_after_research_phase",
    "route_after_setup",
    "route_after_spec_analysis",
    "route_after_understand_phase",
    "route_after_validation",
    # subgraphs & top-level graph
    "build_construct_subgraph",
    "build_coordinator_graph",
    "build_design_subgraph",
    "build_document_subgraph",
    "build_research_subgraph",
    "build_understand_subgraph",
    "compile_coordinator_graph",
    "load_impl_docs_package_from_dir",
]
