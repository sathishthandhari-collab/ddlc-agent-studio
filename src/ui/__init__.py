"""Chainlit user interface, human gate handlers, and streaming Coordinator adapters."""

from src.ui.gates import (
    DEFAULT_GATE_TIMEOUT_SECONDS,
    handle_gate_interrupt,
    load_impl_doc_previews,
    parse_clarification_answers,
    render_gate_prompt,
    uri_to_local_path,
)
from src.ui.handlers import (
    NODE_TITLES,
    PHASE_LABELS,
    NodeDisplayEvent,
    WorkflowTurnResult,
    build_telemetry_handler,
    extract_interrupt_from_snapshot,
    extract_interrupt_payload,
    format_build_log_entry,
    format_delivery_summary,
    format_node_update_markdown,
    run_coordinator_turn,
    unwrap_recovered_interrupt,
)

__all__ = [
    "DEFAULT_GATE_TIMEOUT_SECONDS",
    "NODE_TITLES",
    "NodeDisplayEvent",
    "PHASE_LABELS",
    "WorkflowTurnResult",
    "build_telemetry_handler",
    "extract_interrupt_from_snapshot",
    "extract_interrupt_payload",
    "format_build_log_entry",
    "format_delivery_summary",
    "format_node_update_markdown",
    "handle_gate_interrupt",
    "load_impl_doc_previews",
    "parse_clarification_answers",
    "render_gate_prompt",
    "run_coordinator_turn",
    "unwrap_recovered_interrupt",
    "uri_to_local_path",
]
