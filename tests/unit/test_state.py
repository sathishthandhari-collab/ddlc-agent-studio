"""Zero-infra unit tests for Step 6: Blackboard state, Claim Check pointers, projections, and checkpointer."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError
from src.core.settings import RuntimeEnvSettings, get_env_settings
from src.state import (
    BLACKBOARD_MSGPACK_ALLOWLIST,
    SPECIALIST_ALLOWED_FIELDS,
    ArchitectureProposal,
    BlackboardState,
    BuildLogEntry,
    CatalogContext,
    ChangeRequestContext,
    CheckpointerConfigError,
    CheckpointerSchemaStatus,
    CheckpointerSetupError,
    ClarificationAnswer,
    ClarificationQuestion,
    ClarificationRound,
    ContractPointer,
    GateDecisionRecord,
    ImplDocPackage,
    ImplDocRevision,
    MetaDeliveryManifest,
    ProjectionError,
    ResearchFindings,
    StageDesignSpec,
    StaleStateError,
    ToolEvaluation,
    ValidationIssue,
    ValidationReport,
    adelete_thread_checkpoints,
    aget_thread_history,
    append_only,
    arecover_thread_state,
    assert_contract_pointer_valid,
    build_contract_pointer_from_bytes,
    build_contract_pointer_from_directory,
    check_checkpointer_schema,
    create_blackboard_serializer,
    create_memory_checkpointer,
    delete_thread_checkpoints,
    ensure_checkpointer_setup,
    extract_design_impacting_requirements,
    freeze_contract_pointer,
    get_approved_architecture,
    get_approved_impl_docs,
    get_latest_architecture,
    get_latest_impl_docs,
    get_latest_validation_report,
    get_open_clarification_questions,
    get_thread_history,
    initial_blackboard_state,
    make_resume_command,
    make_thread_config,
    normalize_checkpointer_uri,
    project_for_architect,
    project_for_builder,
    project_for_documentarian,
    project_for_leaf_coder,
    project_for_researcher,
    project_for_spec_analyst,
    project_for_specialist,
    project_for_validator,
    recover_thread_state,
    resolve_checkpointer_db_uri,
    summarize_parsed_spec,
    verify_contract_pointer,
    verify_state_pointers,
)


def _sample_parsed_spec() -> dict[str, Any]:
    return {
        "agent": {
            "name": "dbt_pipeline_builder_agent",
            "version": "1.0.0",
            "description": "Builds modular dbt models from OLTP schemas.",
            "owner": "analytics-engineering-team",
            "domain": "analytics",
        },
        "architecture": {
            "autonomy_level": "semi_autonomous",
            "complexity": "complex",
            "runtime_preference": "langgraph",
        },
        "stages": [
            {
                "id": "discover",
                "type": "discovery",
                "autonomy": "autonomous",
                "input_contract": "source_database_reference",
                "output_contract": "source_schema_profile",
                "tools": ["schema_inspector_v1"],
                "error_strategy": "retry",
            },
            {
                "id": "generate",
                "type": "code_generation",
                "autonomy": "autonomous",
                "input_contract": "transformation_plan",
                "output_contract": "dbt_artifact_bundle",
                "tools": ["sqlglot"],
                "error_strategy": "reflect",
            },
        ],
        "contracts": [
            {"id": "source_database_reference", "direction": "input"},
            {"id": "source_schema_profile", "direction": "internal"},
            {"id": "transformation_plan", "direction": "internal"},
            {"id": "dbt_artifact_bundle", "direction": "internal", "storage_mode": "claim_check"},
        ],
        "governance": {
            "data_classification": "internal",
            "approval_gates": ["review"],
        },
        "cross_agent": {
            "upstream_dependencies": [{"agent": "oltp_catalog_sync", "contract": "source_database_reference"}],
            "downstream_consumers": [{"agent": "dbt_ci_runner", "contract": "delivery_manifest"}],
            "shared_components": [{"name": "schema_inspector_v1", "version": "1.1.0"}],
        },
        "constraints": {
            "required_libraries": ["litellm", "instructor", "sqlglot"],
            "avoided_libraries": ["pyspark"],
        },
    }


def _sample_impl_package() -> ImplDocPackage:
    return ImplDocPackage(
        agent_yaml={
            "agent": {"name": "dbt_pipeline_builder_agent", "version": "1.0.0"},
            "architecture": {"pattern": "stateful_pipeline_with_gates", "runtime": "langgraph", "autonomy_level": "semi_autonomous"},
        },
        stages={
            "generate": {
                "stage": {"id": "generate", "name": "Generate dbt Models", "type": "code_generation"},
                "input": {"contract": "transformation_plan"},
                "output": {"contract": "dbt_artifact_bundle"},
                "processing": {
                    "processing_type": "llm_reasoning",
                    "tools": [
                        {
                            "name": "validate_sql_ast",
                            "type": "custom_code",
                            "handler": "tools/sqlglot_verifier.py::validate_sql_ast",
                            "signature": "(sql_content: str, dialect: str) -> tuple[bool, str]",
                        }
                    ],
                    "logic_description": "Verify generated SQL syntax against target dialect.",
                },
            }
        },
        contracts={
            "transformation_plan": {"contract": {"name": "transformation_plan", "fields": [{"name": "project_name", "type": "string"}]}},
            "dbt_artifact_bundle": {"contract": {"name": "dbt_artifact_bundle", "storage_mode": "claim_check"}},
        },
        config_yaml={"config": {"defaults": {"dbt": {"target_dialect": "snowflake"}}}},
        tests_yaml={"tests": {"contract_tests": []}},
    )


def _sample_populated_blackboard(tmp_path: Path) -> BlackboardState:
    artifact_dir = tmp_path / "generated_agent"
    (artifact_dir / "src").mkdir(parents=True)
    (artifact_dir / "src" / "graph.py").write_text("print('ok')\n", encoding="utf-8")
    code_ptr = build_contract_pointer_from_directory(
        contract_name="generated_codebase",
        base_dir=artifact_dir,
    )

    state = initial_blackboard_state(
        run_id="run-123",
        raw_spec_yaml="agent:\n  name: dbt_pipeline_builder_agent\n",
        target_agent_name="dbt_pipeline_builder_agent",
    )
    parsed = _sample_parsed_spec()
    rnd1 = ClarificationRound(
        round_number=1,
        questions=[
            ClarificationQuestion(
                question_id="q1",
                category="governance",
                field_path="governance.approval_gates",
                question="Require human review before Git PR?",
                impact_area="architecture",
            ),
            ClarificationQuestion(
                question_id="q2",
                category="naming",
                field_path="infrastructure.sources[0].schema",
                question="What is the exact schema name?",
                impact_area="spec",
            ),
        ],
        answers=[
            ClarificationAnswer(
                question_id="q1",
                answer="Yes, halt at review gate before PR.",
                design_impacting=True,
                resolved_requirement="Enforce Gate review before Git delivery.",
            ),
            ClarificationAnswer(
                question_id="q2",
                answer="analytics_prod",
                design_impacting=False,
            ),
        ],
        resolved=True,
    )
    catalog = CatalogContext(
        existing_agents=[{"agent_id": "oltp_catalog_sync", "agent_name": "OLTP Catalog Sync"}],
        available_components=[
            {"component_id": "schema_inspector_v1", "component_name": "schema_inspector_v1", "component_type": "connector"},
            {"component_id": "unused_masker_v1", "component_name": "unused_masker_v1", "component_type": "masker"},
        ],
    )
    research = ResearchFindings(
        verdict="feasible_with_caveats",
        summary="Feasible using sqlglot and dbt-core in sandbox.",
        tool_evaluations=[ToolEvaluation(tool_name="sqlglot", supported=True)],
        policy_constraints=["Require human gate before Git PR"],
    )
    arch_v1 = ArchitectureProposal(
        revision_number=1,
        agent_name="dbt_pipeline_builder_agent",
        pattern="stateful_pipeline_with_gates",
        runtime="langgraph",
        summary="Initial rejected draft",
        approved=False,
    )
    arch_v2 = ArchitectureProposal(
        revision_number=2,
        agent_name="dbt_pipeline_builder_agent",
        pattern="stateful_pipeline_with_gates",
        runtime="langgraph",
        summary="Approved LangGraph pipeline with reflection + review gate",
        stages=[StageDesignSpec(stage_id="generate", name="Generate", stage_type="code_generation")],
        reused_components=[{"component_id": "schema_inspector_v1", "usage_type": "import"}],
        approved=True,
    )
    impl_v1 = ImplDocRevision(
        revision_number=1,
        package=_sample_impl_package(),
        summary="Draft 1",
        approved=False,
    )
    impl_v2 = ImplDocRevision(
        revision_number=2,
        package=_sample_impl_package(),
        summary="Approved impl docs",
        approved=True,
    )
    val_report = ValidationReport(
        attempt_number=1,
        passed=False,
        checks_run=["ast_syntax", "jinja2_prompt_ast", "contract_coverage"],
        issues=[
            ValidationIssue(
                check_name="pytest_unit",
                target_stage_id="generate",
                route_target="leaf_coder",
                message="sqlglot CTE check failed",
                fix_instruction="Handle CTE aliases before sqlglot.parse",
            ),
            ValidationIssue(
                check_name="contract_coverage",
                route_target="documentarian",
                message="Missing field description on transformation_plan",
            ),
        ],
    )

    state["catalog_context"] = catalog
    state["parsed_spec"] = parsed
    state["parsed_spec_summary"] = summarize_parsed_spec(parsed)
    state["clarification_rounds"] = [rnd1]
    state["clarification_round_count"] = 1
    state["enriched_requirements"] = extract_design_impacting_requirements([rnd1])
    state["research_findings"] = research
    state["feasibility_verdict"] = research.verdict
    state["architecture_revisions"] = [arch_v1, arch_v2]
    state["architecture_revision_count"] = 2
    state["architecture_approved"] = True
    state["approved_architecture_version"] = 2
    state["architecture_feedback"] = "Add reflection loop"
    state["impl_doc_revisions"] = [impl_v1, impl_v2]
    state["impl_doc_revision_count"] = 2
    state["impl_docs_approved"] = True
    state["approved_impl_doc_version"] = 2
    state["generated_code_pointer"] = code_ptr
    state["shared_components_source"] = {"schema_inspector_v1": "def inspect(): ..."}
    state["validation_reports"] = [val_report]
    state["active_fix_instructions"] = ["Handle CTE aliases before sqlglot.parse"]
    state["build_fix_attempt_count"] = 1
    state["build_logs"] = [BuildLogEntry(phase="construct", actor="builder", action="compile")]
    state["gate_decisions"] = [
        GateDecisionRecord(gate_name="architecture_review", round_or_revision=2, decision="approved")
    ]
    return state


# ---------------------------------------------------------------------------
# 1. Blackboard State & Append-Only Reducer Tests
# ---------------------------------------------------------------------------


class TestBlackboardStateAndReducers:
    def test_append_only_reducer_preserves_history_on_empty_or_none(self) -> None:
        base = ["r1"]
        step2 = append_only(base, ["r2"])
        step3 = append_only(step2, [])
        step4 = append_only(step3, None)
        step5 = append_only(step4, "r3")

        assert base == ["r1"]
        assert step2 == ["r1", "r2"]
        assert step3 == ["r1", "r2"]
        assert step4 == ["r1", "r2"]
        assert step5 == ["r1", "r2", "r3"]

    def test_langgraph_stategraph_accumulates_append_only_channels(self) -> None:
        def round_one(state: BlackboardState) -> dict[str, Any]:
            return {
                "current_phase": "understand",
                "clarification_rounds": [
                    ClarificationRound(
                        round_number=1,
                        questions=[ClarificationQuestion(question_id="q1", category="gap", question="Dialect?")],
                    )
                ],
                "build_logs": [BuildLogEntry(phase="understand", actor="spec_analyst", action="round1")],
            }

        def round_two(state: BlackboardState) -> dict[str, Any]:
            return {
                "clarification_rounds": [
                    ClarificationRound(
                        round_number=2,
                        questions=[ClarificationQuestion(question_id="q2", category="gap", question="Sandbox?")],
                        resolved=True,
                    )
                ],
                "build_logs": [BuildLogEntry(phase="understand", actor="spec_analyst", action="round2")],
            }

        graph = StateGraph(BlackboardState)
        graph.add_node("r1", round_one)
        graph.add_node("r2", round_two)
        graph.set_entry_point("r1")
        graph.add_edge("r1", "r2")
        graph.add_edge("r2", END)

        app = graph.compile(checkpointer=create_memory_checkpointer())
        result = app.invoke(
            initial_blackboard_state(run_id="run-append", raw_spec_yaml="agent: {}"),
            config=make_thread_config("run-append"),
        )
        assert len(result["clarification_rounds"]) == 2
        assert [r.round_number for r in result["clarification_rounds"]] == [1, 2]
        assert len(result["build_logs"]) == 2

    def test_blackboard_accessors_and_change_mode_baseline(self) -> None:
        state = initial_blackboard_state(
            mode="change",
            target_agent_name="dbt_pipeline_builder_agent",
            change_request=ChangeRequestContext(
                request_text="Add semantic metrics",
                agent_id="dbt_pipeline_builder_agent",
                affected_stages=["generate"],
            ),
            baseline_impl_docs=_sample_impl_package(),
        )
        assert state["mode"] == "change"
        assert state["impl_doc_revision_count"] == 1
        assert get_latest_impl_docs(state) is not None
        assert get_approved_impl_docs(state) is None
        assert get_latest_architecture(state) is None
        assert get_approved_architecture(state) is None
        assert get_latest_validation_report(state) is None

        state["clarification_rounds"] = [
            ClarificationRound(
                round_number=1,
                questions=[
                    ClarificationQuestion(question_id="q1", category="gap", question="First?"),
                    ClarificationQuestion(question_id="q2", category="gap", question="Second?"),
                ],
                answers=[ClarificationAnswer(question_id="q1", answer="Answered")],
                resolved=False,
            )
        ]
        open_qs = get_open_clarification_questions(state)
        assert [q.question_id for q in open_qs] == ["q2"]

    def test_approved_selectors_return_none_on_missing_or_unapproved_version(
        self, tmp_path: Path
    ) -> None:
        state = _sample_populated_blackboard(tmp_path)
        assert get_approved_architecture(state) is not None
        assert get_approved_impl_docs(state) is not None

        # Explicit approved version pointing to non-existent revision must NOT silently fall back
        state["approved_architecture_version"] = 999
        state["approved_impl_doc_version"] = 999
        assert get_approved_architecture(state) is None
        assert get_approved_impl_docs(state) is None

        # Gate flag False overrides even approved=True revisions in history
        state["approved_architecture_version"] = None
        state["approved_impl_doc_version"] = None
        state["architecture_approved"] = False
        state["impl_docs_approved"] = False
        assert get_approved_architecture(state) is None
        assert get_approved_impl_docs(state) is None


# ---------------------------------------------------------------------------
# 2. Claim Check ContractPointer & Path Traversal Guard Tests
# ---------------------------------------------------------------------------


class TestContractPointerClaimCheck:
    def test_directory_pointer_hash_verify_tamper_and_freeze(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        (bundle_dir / "models" / "staging").mkdir(parents=True)
        sql_file = bundle_dir / "models" / "staging" / "stg_customers.sql"
        yml_file = bundle_dir / "models" / "schema.yml"
        sql_file.write_text("SELECT id FROM raw.customers\n", encoding="utf-8")
        yml_file.write_text("version: 2\n", encoding="utf-8")

        ptr = build_contract_pointer_from_directory(
            contract_name="dbt_artifact_bundle",
            base_dir=bundle_dir,
        )
        assert ptr.item_count == 2
        assert ptr.file_manifest == ["models/schema.yml", "models/staging/stg_customers.sql"]
        assert ptr.frozen is False
        assert verify_contract_pointer(ptr) is True

        frozen = freeze_contract_pointer(ptr, approved_by="engineer@example.com")
        assert ptr.frozen is False  # original never mutated in place
        assert frozen.frozen is True
        assert frozen.frozen_by == "engineer@example.com"
        assert frozen.payload_sha256 == ptr.payload_sha256

        # Tampering after freeze triggers FM-06 failure
        sql_file.write_text("SELECT id, email FROM raw.customers\n", encoding="utf-8")
        assert verify_contract_pointer(frozen) is False
        with pytest.raises(StaleStateError, match="Claim Check verification failed"):
            assert_contract_pointer_valid(frozen)

    def test_bytes_pointer_and_file_uri_verification(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "spec.yaml"
        content = b"agent:\n  name: demo_agent\n"
        spec_path.write_bytes(content)

        ptr = build_contract_pointer_from_bytes(
            contract_name="spec_yaml",
            content=content,
            storage_uri=spec_path.as_uri(),
        )
        assert verify_contract_pointer(ptr) is True

        spec_path.write_bytes(b"tampered")
        assert verify_contract_pointer(ptr) is False

    def test_path_traversal_rejected_on_directory_pointer(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("top-secret", encoding="utf-8")

        with pytest.raises(ValueError, match="traversal"):
            build_contract_pointer_from_directory(
                contract_name="bad_bundle",
                base_dir=bundle_dir,
                files=["../secret.txt"],
            )

    def test_verify_state_pointers_checks_all_present_pointers(self, tmp_path: Path) -> None:
        state = _sample_populated_blackboard(tmp_path)
        checks = verify_state_pointers(state)
        assert checks == {"generated_code_pointer": True}

    def test_directory_pointer_rejects_empty_manifest_and_tolerates_untracked_build_outputs(
        self, tmp_path: Path
    ) -> None:
        from src.state.blackboard import compute_directory_sha256

        bundle_dir = tmp_path / "dbt_bundle"
        (bundle_dir / "models" / "staging").mkdir(parents=True)
        model_file = bundle_dir / "models" / "staging" / "stg_orders.sql"
        schema_file = bundle_dir / "schema.yml"
        model_file.write_text("SELECT order_id FROM raw.orders\n", encoding="utf-8")
        schema_file.write_text("version: 2\n", encoding="utf-8")

        with pytest.raises(ValueError, match="files cannot be empty"):
            compute_directory_sha256(bundle_dir, files=[])
        with pytest.raises(ValueError, match="Invalid empty or current-directory manifest entry"):
            compute_directory_sha256(bundle_dir, files=[""])
        with pytest.raises(ValueError, match="Invalid empty or current-directory manifest entry"):
            compute_directory_sha256(bundle_dir, files=["."])
        with pytest.raises(ValueError, match="files cannot be empty"):
            build_contract_pointer_from_directory(
                contract_name="empty_manifest",
                base_dir=bundle_dir,
                files=[],
            )

        ptr = build_contract_pointer_from_directory(
            contract_name="dbt_bundle",
            base_dir=bundle_dir,
            files=["models/staging/stg_orders.sql", "schema.yml"],
        )
        assert verify_contract_pointer(ptr) is True

        # Writing untracked build artifacts (e.g. target/compiled/) must not invalidate pinned manifest
        compiled = bundle_dir / "target" / "compiled" / "stg_orders.sql"
        compiled.parent.mkdir(parents=True)
        compiled.write_text("SELECT order_id FROM analytics.raw_orders\n", encoding="utf-8")
        assert verify_contract_pointer(ptr) is True

        # Modifying a manifest-pinned file must still fail verification
        model_file.write_text("SELECT * FROM raw.orders\n", encoding="utf-8")
        assert verify_contract_pointer(ptr) is False


# ---------------------------------------------------------------------------
# 3. Strict State Projection & Context Funnel Tests
# ---------------------------------------------------------------------------


class TestStateProjectionsFunnel:
    def test_each_specialist_sees_only_authorized_fields(self, tmp_path: Path) -> None:
        state = _sample_populated_blackboard(tmp_path)

        spec_slice = project_for_spec_analyst(state)
        assert spec_slice.raw_spec_yaml is not None
        assert len(spec_slice.clarification_rounds) == 1
        assert "oltp_catalog_sync" in spec_slice.known_agent_names
        assert not hasattr(spec_slice, "research_findings")
        assert not hasattr(spec_slice, "architecture_revisions")
        assert not hasattr(spec_slice, "generated_code_pointer")

        research_slice = project_for_researcher(state)
        assert research_slice.parsed_spec["agent"]["name"] == "dbt_pipeline_builder_agent"
        assert len(research_slice.enriched_requirements) == 1
        assert research_slice.enriched_requirements[0].requirement_id == "r1_q1"
        assert not hasattr(research_slice, "raw_spec_yaml")
        assert not hasattr(research_slice, "clarification_rounds")
        assert not hasattr(research_slice, "architecture_revisions")

        arch_slice = project_for_architect(state)
        assert arch_slice.parsed_spec_summary.agent_name == "dbt_pipeline_builder_agent"
        assert arch_slice.research_findings is not None
        assert arch_slice.current_architecture is not None
        assert arch_slice.current_architecture.revision_number == 2
        assert not hasattr(arch_slice, "raw_spec_yaml")
        assert not hasattr(arch_slice, "parsed_spec")
        assert not hasattr(arch_slice, "clarification_rounds")
        assert not hasattr(arch_slice, "impl_doc_revisions")

        doc_slice = project_for_documentarian(state)
        assert doc_slice.approved_architecture is not None
        assert doc_slice.approved_architecture.revision_number == 2
        assert [c["component_id"] for c in doc_slice.shared_component_specs] == ["schema_inspector_v1"]
        assert doc_slice.contract_validation_feedback == [
            "contract_coverage: Missing field description on transformation_plan"
        ]
        assert not hasattr(doc_slice, "raw_spec_yaml")
        assert not hasattr(doc_slice, "research_findings")
        assert not hasattr(doc_slice, "clarification_rounds")
        assert not hasattr(doc_slice, "architecture_revisions")

        builder_slice = project_for_builder(state)
        assert builder_slice.target_agent_name == "dbt_pipeline_builder_agent"
        assert "generate" in builder_slice.approved_impl_docs.stages
        assert builder_slice.fix_instructions == ["Handle CTE aliases before sqlglot.parse"]
        assert not hasattr(builder_slice, "raw_spec_yaml")
        assert not hasattr(builder_slice, "parsed_spec")
        assert not hasattr(builder_slice, "research_findings")
        assert not hasattr(builder_slice, "approved_architecture")
        assert not hasattr(builder_slice, "catalog_context")

        leaf_create = project_for_leaf_coder(state, stage_id="generate", mode="create")
        assert leaf_create.handler == "tools/sqlglot_verifier.py::validate_sql_ast"
        assert leaf_create.input_contract is not None
        assert leaf_create.output_contract is not None
        assert leaf_create.fix_diagnostics == [
            "pytest_unit (generate): Handle CTE aliases before sqlglot.parse"
        ]

        validator_slice = project_for_validator(state)
        assert validator_slice.generated_code_pointer.item_count == 1
        assert "generate" in validator_slice.approved_impl_docs.stages
        assert not hasattr(validator_slice, "raw_spec_yaml")
        assert not hasattr(validator_slice, "research_findings")
        assert not hasattr(validator_slice, "build_logs")

    def test_projections_are_frozen_deep_clones_and_forbid_extra_fields(self, tmp_path: Path) -> None:
        state = _sample_populated_blackboard(tmp_path)
        builder_slice = project_for_builder(state)

        with pytest.raises(ValidationError):
            builder_slice.target_agent_name = "mutated"  # type: ignore[misc]

        # Mutating nested dict inside projected slice must not mutate Blackboard
        builder_slice.shared_components_source["schema_inspector_v1"] = "mutated"
        assert state["shared_components_source"]["schema_inspector_v1"] == "def inspect(): ..."

        with pytest.raises(ValidationError):
            type(builder_slice).model_validate(
                {**builder_slice.model_dump(), "raw_spec_yaml": "leaked"}
            )

    def test_projection_gates_fail_fast_when_prerequisites_missing(self, tmp_path: Path) -> None:
        empty_state = initial_blackboard_state(run_id="empty")
        with pytest.raises(ProjectionError):
            project_for_spec_analyst(empty_state)
        with pytest.raises(ProjectionError):
            project_for_researcher(empty_state)
        with pytest.raises(ProjectionError):
            project_for_architect(empty_state)
        with pytest.raises(ProjectionError):
            project_for_documentarian(empty_state)
        with pytest.raises(ProjectionError):
            project_for_builder(empty_state)
        with pytest.raises(ProjectionError):
            project_for_validator(empty_state)

        state = _sample_populated_blackboard(tmp_path)
        with pytest.raises(ProjectionError, match="existing_code"):
            project_for_leaf_coder(state, stage_id="generate", mode="patch")

        with pytest.raises(ProjectionError, match="Unknown specialist"):
            project_for_specialist("unknown_role", state)

        assert set(SPECIALIST_ALLOWED_FIELDS.keys()) == {
            "spec_analyst",
            "researcher",
            "architect",
            "documentarian",
            "builder",
            "leaf_coder",
            "validator",
        }

    def test_documentarian_hides_unselected_catalog_when_reused_empty_and_projects_on_revise(
        self, tmp_path: Path
    ) -> None:
        state = _sample_populated_blackboard(tmp_path)
        approved_no_reuse = state["architecture_revisions"][1].model_copy(
            update={"reused_components": []}
        )
        state["architecture_revisions"] = [state["architecture_revisions"][0], approved_no_reuse]
        state["validation_reports"] = []
        state["active_fix_instructions"] = []
        state["impl_doc_feedback"] = None
        state["impl_doc_decision"] = "revise"

        doc_slice = project_for_documentarian(state)
        assert doc_slice.shared_component_specs == []
        assert doc_slice.current_impl_docs is not None

        # Without revise/feedback/change/fixes, current_impl_docs should be omitted on fresh draft
        state["impl_doc_decision"] = None
        fresh_doc_slice = project_for_documentarian(state)
        assert fresh_doc_slice.current_impl_docs is None

    def test_architect_projects_current_proposal_on_revise_without_feedback_text(
        self, tmp_path: Path
    ) -> None:
        state = _sample_populated_blackboard(tmp_path)
        state["mode"] = "build"
        state["architecture_feedback"] = None
        state["architecture_decision"] = "revise"

        revising = project_for_architect(state)
        assert revising.current_architecture is not None
        assert revising.current_architecture.revision_number == 2

        state["architecture_decision"] = None
        fresh = project_for_architect(state)
        assert fresh.current_architecture is None

    def test_leaf_coder_projects_llm_config_prompt_and_output_schema_when_tools_empty(
        self, tmp_path: Path
    ) -> None:
        import copy

        state = _sample_populated_blackboard(tmp_path)
        pkg = state["impl_doc_revisions"][1].package.model_copy(deep=True)
        gen_stage = copy.deepcopy(pkg.stages["generate"])
        gen_stage["processing"]["tools"] = []
        gen_stage["processing"]["llm_config"] = {
            "prompt_template_ref": "prompts/generate_sql.j2",
            "output_schema": "DbtSqlGenerationOutput",
        }
        pkg.stages["generate"] = gen_stage
        state["impl_doc_revisions"] = [
            state["impl_doc_revisions"][0],
            state["impl_doc_revisions"][1].model_copy(update={"package": pkg}),
        ]

        leaf = project_for_leaf_coder(state, stage_id="generate", mode="create")
        assert leaf.tools == []
        assert leaf.tool_name is None
        assert leaf.handler is None
        assert leaf.prompt_template_ref == "prompts/generate_sql.j2"
        assert leaf.output_schema == "DbtSqlGenerationOutput"

    def test_spec_analyst_hides_impl_docs_in_build_and_exposes_in_change_mode(
        self, tmp_path: Path
    ) -> None:
        state = _sample_populated_blackboard(tmp_path)
        state["mode"] = "build"
        build_slice = project_for_spec_analyst(state)
        assert build_slice.active_impl_docs is None
        assert build_slice.change_request is None

        state["mode"] = "change"
        state["change_request"] = ChangeRequestContext(
            request_text="Add PII masking stage for customer email",
            agent_id="dbt_pipeline_builder_agent",
            affected_stages=["generate"],
        )
        change_slice = project_for_spec_analyst(state)
        assert change_slice.active_impl_docs is not None
        assert "generate" in change_slice.active_impl_docs.stages
        assert change_slice.change_request is not None
        assert change_slice.change_request.agent_id == "dbt_pipeline_builder_agent"


# ---------------------------------------------------------------------------
# 4. Checkpointer Config, Strict Msgpack Serde & Recovery Tests
# ---------------------------------------------------------------------------


class TestCheckpointerUnitAndRecovery:
    def test_uri_normalization_and_missing_env_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            normalize_checkpointer_uri("postgresql+psycopg://u:p@localhost:5434/db")
            == "postgresql://u:p@localhost:5434/db"
        )
        assert (
            normalize_checkpointer_uri("postgres://u:p@localhost:5434/db")
            == "postgresql://u:p@localhost:5434/db"
        )

        monkeypatch.delenv("CHECKPOINTER_DB_URI", raising=False)
        monkeypatch.delenv("METADATA_DB_URI", raising=False)
        empty_env = RuntimeEnvSettings(_env_file=tmp_path / ".missing.env")
        monkeypatch.setattr("src.state.checkpointer.get_env_settings", lambda: empty_env)
        with pytest.raises(CheckpointerConfigError, match="CHECKPOINTER_DB_URI"):
            resolve_checkpointer_db_uri()
        get_env_settings.cache_clear()

    def test_strict_msgpack_roundtrip_without_unregistered_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
        serde = create_blackboard_serializer()
        state = _sample_populated_blackboard(tmp_path)
        state["delivery_manifest"] = MetaDeliveryManifest(
            agent_id="dbt_pipeline_builder_agent",
            agent_version="1.0.0",
            workspace_path=str(tmp_path / "generated_agent"),
            impl_docs_path=str(tmp_path / "generated_agent" / "impl_docs"),
            code_pointer=state["generated_code_pointer"],
            tier2_registered=True,
            rag_indexed=True,
        )

        assert ("src.state.blackboard", "ContractPointer") in BLACKBOARD_MSGPACK_ALLOWLIST
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            typed_payload = serde.dumps_typed(state)
            restored = serde.loads_typed(typed_payload)

        serde_warnings = [w for w in caught if "unregistered type" in str(w.message).lower() or "blocked" in str(w.message).lower()]
        assert serde_warnings == []
        assert isinstance(restored["generated_code_pointer"], ContractPointer)
        assert isinstance(restored["architecture_revisions"][0], ArchitectureProposal)
        assert isinstance(restored["impl_doc_revisions"][0], ImplDocRevision)
        assert isinstance(restored["validation_reports"][0], ValidationReport)
        assert isinstance(restored["delivery_manifest"], MetaDeliveryManifest)

    def test_schema_check_and_privilege_safe_setup_with_stubs(self) -> None:
        class _FakeCursor:
            def __init__(self, rows: Any, one: Any = None) -> None:
                self._rows = rows
                self._one = one

            def fetchall(self) -> Any:
                return self._rows

            def fetchone(self) -> Any:
                return self._one

        class _ReadyConn:
            def execute(self, sql: str, params: Any = None) -> _FakeCursor:
                if "information_schema.tables" in sql:
                    return _FakeCursor(
                        [
                            {"table_name": "checkpoint_migrations"},
                            {"table_name": "checkpoints"},
                            {"table_name": "checkpoint_blobs"},
                            {"table_name": "checkpoint_writes"},
                        ]
                    )
                return _FakeCursor([], {"max_v": 9})

        status = check_checkpointer_schema(conn=_ReadyConn())  # type: ignore[arg-type]
        assert status.ready is True
        assert status.current_version == 9
        assert status.missing_tables == []

        # ensure_checkpointer_setup skips DDL if schema is already ready
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("src.state.checkpointer.check_checkpointer_schema", lambda **_: status)
            called = False

            def _should_not_run(*args: Any, **kwargs: Any) -> Any:
                nonlocal called
                called = True
                raise AssertionError("setup() should not be invoked when schema is ready")

            mp.setattr("src.state.checkpointer.PostgresSaver.from_conn_string", _should_not_run)
            out = ensure_checkpointer_setup(db_uri="postgresql://ddlc_app:x@localhost:5434/db")
            assert out.ready is True
            assert called is False

        # Missing tables + InsufficientPrivilege raises CheckpointerSetupError
        not_ready = CheckpointerSchemaStatus(
            ready=False,
            current_version=-1,
            expected_version=9,
            existing_tables=[],
            missing_tables=["checkpoint_migrations", "checkpoints"],
        )
        with pytest.raises(CheckpointerSetupError, match="DCM_MIGRATION_DB_URI"):
            with pytest.MonkeyPatch.context() as mp:
                from psycopg.errors import InsufficientPrivilege

                mp.setattr("src.state.checkpointer.check_checkpointer_schema", lambda **_: not_ready)

                class _Ctx:
                    def __enter__(self) -> Any:
                        raise InsufficientPrivilege("permission denied for schema public")

                    def __exit__(self, *args: Any) -> None:
                        return None

                mp.setattr("src.state.checkpointer.PostgresSaver.from_conn_string", lambda *_: _Ctx())
                ensure_checkpointer_setup(
                    db_uri="postgresql://ddlc_app:x@localhost:5434/db",
                    admin_db_uri="postgresql://ddlc_app:x@localhost:5434/db",
                )

    def test_interrupt_checkpoint_recovery_time_travel_and_cleanup(self, tmp_path: Path) -> None:
        artifact_dir = tmp_path / "bundle"
        artifact_dir.mkdir()
        (artifact_dir / "agent.yaml").write_text("agent:\n  name: demo\n", encoding="utf-8")
        ptr = build_contract_pointer_from_directory(contract_name="impl_docs", base_dir=artifact_dir)

        def node_design(state: BlackboardState) -> dict[str, Any]:
            return {
                "current_phase": "design",
                "impl_docs_pointer": ptr,
                "architecture_revisions": [
                    ArchitectureProposal(
                        revision_number=1,
                        agent_name="demo",
                        pattern="stateful_pipeline_with_gates",
                        runtime="langgraph",
                        summary="Proposal v1",
                    )
                ],
                "architecture_revision_count": 1,
            }

        def node_gate2(state: BlackboardState) -> dict[str, Any]:
            decision = interrupt({"gate": "architecture_review", "revision": 1})
            approved = decision.get("decision") == "approved"
            return {
                "current_phase": "document" if approved else "design",
                "architecture_approved": approved,
                "architecture_decision": decision.get("decision", "abort"),
                "approved_architecture_version": 1 if approved else None,
                "gate_decisions": [
                    GateDecisionRecord(
                        gate_name="architecture_review",
                        round_or_revision=1,
                        decision=decision.get("decision", "abort"),
                        reviewer=decision.get("reviewer", "lead_architect"),
                    )
                ],
            }

        def node_finish(state: BlackboardState) -> dict[str, Any]:
            return {"current_phase": "completed"}

        workflow = StateGraph(BlackboardState)
        workflow.add_node("design", node_design)
        workflow.add_node("gate2", node_gate2)
        workflow.add_node("finish", node_finish)
        workflow.set_entry_point("design")
        workflow.add_edge("design", "gate2")
        workflow.add_edge("gate2", "finish")
        workflow.add_edge("finish", END)

        checkpointer = create_memory_checkpointer()
        app = workflow.compile(checkpointer=checkpointer)
        thread_id = f"thread-{uuid4().hex[:8]}"
        cfg = make_thread_config(thread_id)

        app.invoke(
            initial_blackboard_state(run_id=thread_id, raw_spec_yaml="agent: {name: demo}"),
            config=cfg,
        )

        # Recover interrupted snapshot and verify Claim Check pointer integrity
        paused = recover_thread_state(checkpointer, thread_id, verify_pointers=True)
        assert paused is not None
        assert paused.resumable is True
        assert paused.next_nodes == ["gate2"]
        assert paused.pointer_integrity == {"impl_docs_pointer": True}
        assert len(paused.pending_interrupts) == 1
        assert paused.pending_interrupts[0]["value"] == {"gate": "architecture_review", "revision": 1}
        assert paused.state["current_phase"] == "design"

        # Resume gate with Command(resume=...)
        final_state = app.invoke(
            make_resume_command({"decision": "approved", "reviewer": "alice"}),
            config=cfg,
        )
        assert final_state["current_phase"] == "completed"
        assert final_state["architecture_approved"] is True

        completed = recover_thread_state(checkpointer, thread_id)
        assert completed is not None
        assert completed.resumable is False
        assert completed.next_nodes == []
        assert completed.pending_interrupts == []

        history = get_thread_history(checkpointer, thread_id)
        assert len(history) >= 3
        assert history[0].current_phase == "completed"

        # Time-travel recovery to the paused checkpoint
        paused_again = recover_thread_state(
            checkpointer,
            thread_id,
            checkpoint_id=paused.checkpoint_id,
        )
        assert paused_again is not None
        assert paused_again.state["current_phase"] == "design"
        assert paused_again.state["architecture_approved"] is False

        delete_thread_checkpoints(checkpointer, thread_id)
        assert recover_thread_state(checkpointer, thread_id) is None

    async def test_async_recovery_helpers_with_memory_checkpointer(self) -> None:
        def node_step(state: BlackboardState) -> dict[str, Any]:
            return {"current_phase": "completed"}

        workflow = StateGraph(BlackboardState)
        workflow.add_node("step", node_step)
        workflow.set_entry_point("step")
        workflow.add_edge("step", END)

        checkpointer = create_memory_checkpointer()
        app = workflow.compile(checkpointer=checkpointer)
        thread_id = f"async-{uuid4().hex[:8]}"
        cfg = make_thread_config(thread_id)

        await app.ainvoke(initial_blackboard_state(run_id=thread_id), config=cfg)
        snap = await arecover_thread_state(checkpointer, thread_id)
        assert snap is not None
        assert snap.state["current_phase"] == "completed"
        assert snap.resumable is False

        hist = await aget_thread_history(checkpointer, thread_id)
        assert len(hist) >= 2

        await adelete_thread_checkpoints(checkpointer, thread_id)
        assert await arecover_thread_state(checkpointer, thread_id) is None

    def test_subgraph_interrupt_bubbles_and_history_namespace_filtering(self) -> None:
        def sub_review(state: BlackboardState) -> dict[str, Any]:
            decision = interrupt({"gate": "sub_clarification", "round": 1})
            return {
                "current_phase": "design",
                "architecture_approved": decision.get("decision") == "approved",
            }

        sub_builder = StateGraph(BlackboardState)
        sub_builder.add_node("sub_review", sub_review)
        sub_builder.set_entry_point("sub_review")
        sub_builder.add_edge("sub_review", END)
        sub_graph = sub_builder.compile()

        def finish_node(state: BlackboardState) -> dict[str, Any]:
            return {"current_phase": "completed"}

        parent_builder = StateGraph(BlackboardState)
        parent_builder.add_node("understand_sub", sub_graph)
        parent_builder.add_node("finish", finish_node)
        parent_builder.set_entry_point("understand_sub")
        parent_builder.add_edge("understand_sub", "finish")
        parent_builder.add_edge("finish", END)

        checkpointer = create_memory_checkpointer()
        app = parent_builder.compile(checkpointer=checkpointer)
        thread_id = f"sub-{uuid4().hex[:8]}"
        cfg = make_thread_config(thread_id)

        app.invoke(initial_blackboard_state(run_id=thread_id), config=cfg)

        paused = recover_thread_state(checkpointer, thread_id)
        assert paused is not None
        assert paused.resumable is True
        assert paused.next_nodes == ["understand_sub"]
        assert len(paused.pending_interrupts) == 1
        assert paused.pending_interrupts[0]["value"] == {
            "gate": "sub_clarification",
            "round": 1,
        }

        all_hist = get_thread_history(checkpointer, thread_id, checkpoint_ns=None)
        root_hist = get_thread_history(checkpointer, thread_id, checkpoint_ns="")
        assert any(item.checkpoint_ns.startswith("understand_sub:") for item in all_hist)
        assert all(item.checkpoint_ns == "" for item in root_hist)
        assert len(all_hist) > len(root_hist)

        resumed = app.invoke(
            make_resume_command({"decision": "approved"}),
            config=cfg,
        )
        assert resumed["current_phase"] == "completed"
        assert resumed["architecture_approved"] is True

    def test_midrun_node_exception_leaves_resumable_checkpoint(self) -> None:
        attempts = {"count": 0}

        def unstable_node(state: BlackboardState) -> dict[str, Any]:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient LLM timeout")
            return {"current_phase": "completed"}

        workflow = StateGraph(BlackboardState)
        workflow.add_node("unstable", unstable_node)
        workflow.set_entry_point("unstable")
        workflow.add_edge("unstable", END)

        checkpointer = create_memory_checkpointer()
        app = workflow.compile(checkpointer=checkpointer)
        thread_id = f"err-{uuid4().hex[:8]}"
        cfg = make_thread_config(thread_id)

        with pytest.raises(RuntimeError, match="transient LLM timeout"):
            app.invoke(initial_blackboard_state(run_id=thread_id), config=cfg)

        failed_snap = recover_thread_state(checkpointer, thread_id)
        assert failed_snap is not None
        assert failed_snap.resumable is True
        assert failed_snap.next_nodes == ["unstable"]

        # Resume from checkpoint by invoking with None input
        resumed = app.invoke(None, config=cfg)
        assert resumed["current_phase"] == "completed"
        assert attempts["count"] == 2

    def test_sync_pool_closes_cleanly_when_open_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.state.checkpointer import PostgresCheckpointerManager

        events: list[str] = []

        class _FailingPool:
            def __init__(self, **kwargs: Any) -> None:
                self.closed = False
                events.append("init")

            def open(self, *, wait: bool = True, timeout: float = 30.0) -> None:
                events.append("open")
                raise RuntimeError("connection refused")

            def close(self, timeout: float = 5.0) -> None:
                self.closed = True
                events.append("close")

        monkeypatch.setattr("src.state.checkpointer.ConnectionPool", _FailingPool)
        mgr = PostgresCheckpointerManager(db_uri="postgresql://ddlc_app:pw@localhost:5434/db")

        with pytest.raises(RuntimeError, match="connection refused"):
            mgr.get_sync_pool()

        assert events == ["init", "open", "close"]
        assert mgr._sync_pool is None

    def test_async_pool_rejects_proactor_event_loop(self) -> None:
        import asyncio
        import sys

        from src.state.checkpointer import PostgresCheckpointerManager

        if sys.platform != "win32" or not hasattr(asyncio, "ProactorEventLoop"):
            pytest.skip("Windows ProactorEventLoop check only applies on win32.")

        mgr = PostgresCheckpointerManager(db_uri="postgresql://ddlc_app:pw@localhost:5434/db")

        async def _probe() -> None:
            await mgr.get_async_pool()

        loop = asyncio.ProactorEventLoop()
        try:
            with pytest.raises(CheckpointerConfigError, match="SelectorEventLoop"):
                loop.run_until_complete(_probe())
        finally:
            loop.close()

    async def test_async_saver_offloads_verify_setup_to_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from src.state.checkpointer import PostgresCheckpointerManager

        calls: list[tuple[str, Any]] = []
        fake_pool = object()
        fake_saver = object()
        status = CheckpointerSchemaStatus(
            ready=True,
            current_version=9,
            expected_version=9,
            missing_tables=[],
            error=None,
        )

        async def _fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(("to_thread", fn.__name__))
            return fn(*args, **kwargs)

        def _fake_ensure(db_uri: Any = None, *, admin_db_uri: Any = None) -> CheckpointerSchemaStatus:
            calls.append(("ensure", db_uri, admin_db_uri))
            return status

        def _fake_saver_cls(*, conn: Any, serde: Any) -> Any:
            calls.append(("saver", conn is fake_pool))
            return fake_saver

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
        monkeypatch.setattr("src.state.checkpointer.ensure_checkpointer_setup", _fake_ensure)
        monkeypatch.setattr("src.state.checkpointer.AsyncPostgresSaver", _fake_saver_cls)

        mgr = PostgresCheckpointerManager(db_uri="postgresql://ddlc_app:pw@localhost:5434/db")
        mgr._async_pool = fake_pool  # type: ignore[assignment]
        saver = await mgr.get_async_saver(verify_setup=True, admin_db_uri="postgresql://admin:pw@localhost:5434/db")

        assert saver is fake_saver
        assert calls == [
            ("to_thread", "_fake_ensure"),
            ("ensure", "postgresql://ddlc_app:pw@localhost:5434/db", "postgresql://admin:pw@localhost:5434/db"),
            ("saver", True),
        ]
