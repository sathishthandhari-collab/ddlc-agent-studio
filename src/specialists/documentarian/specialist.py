"""Documentarian specialist — translates approved architecture into validated 5-file impl_docs/ + Tier 3 contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from src.metadata.schema_engine import Tier3SchemaClient
from src.specialists.documentarian.models import (
    AgentManifestDoc,
    ConfigDocument,
    ContractDocument,
    StageDocument,
    TestsDocument,
)
from src.specialists.documentarian.prompts import build_documentarian_messages
from src.specialists.documentarian.validator import validate_impl_doc_package
from src.specialists.llm import LiteLLMInstructorClient, StructuredLLMClient
from src.state.blackboard import (
    BuildLogEntry,
    ContractPointer,
    ImplDocPackage,
    ImplDocRevision,
    build_contract_pointer_from_bytes,
    build_contract_pointer_from_directory,
)
from src.state.projections import DocumentarianProjection


class ImplDocSynthesisOutput(BaseModel):
    """Structured Instructor output for the 5-file implementation document package."""

    agent_manifest: AgentManifestDoc
    stages: list[StageDocument] = Field(..., min_length=1)
    contracts: list[ContractDocument] = Field(..., min_length=1)
    config_doc: ConfigDocument
    tests_doc: TestsDocument
    tier3_contract_yaml: Optional[dict[str, Any]] = None
    summary: str = Field(default="Synthesized runtime-agnostic implementation document suite.")


class ImplDocValidationError(ValueError):
    """Raised when synthesized impl_docs fail deterministic cross-document integrity checks."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Implementation document validation failed:\n- " + "\n- ".join(errors))


def _default_tier3_contract(agent_name: str, tables: list[str]) -> dict[str, Any]:
    """Deterministically build a valid DCM Tier 3 contract when required by approved_architecture."""
    schema_name = Tier3SchemaClient.expected_schema_name(agent_name)
    table_names = tables or [f"{agent_name}_run_artifacts"]
    table_specs = []
    for tbl_name in table_names:
        safe_tbl = tbl_name.strip().lower().replace("-", "_")
        table_specs.append(
            {
                "name": safe_tbl,
                "description": f"Operational Tier 3 metadata table '{safe_tbl}' for {agent_name}.",
                "columns": [
                    {
                        "name": "record_id",
                        "type": "UUID",
                        "primary_key": True,
                        "nullable": False,
                        "default": "gen_random_uuid()",
                        "description": "Primary key identifier.",
                    },
                    {
                        "name": "run_id",
                        "type": "UUID",
                        "nullable": False,
                        "references": "agent_metadata.agent_run_header(run_id)",
                        "on_delete": "CASCADE",
                        "description": "Tier 1 execution run identifier.",
                    },
                    {
                        "name": "payload_sha256",
                        "type": "VARCHAR(64)",
                        "nullable": True,
                        "description": "Claim Check artifact SHA-256 digest.",
                    },
                    {
                        "name": "details",
                        "type": "JSONB",
                        "nullable": False,
                        "default": "'{}'::jsonb",
                        "description": "Structured stage-specific operational metadata.",
                    },
                    {
                        "name": "recorded_at",
                        "type": "TIMESTAMPTZ",
                        "nullable": False,
                        "default": "clock_timestamp()",
                        "description": "UTC timestamp when row was recorded.",
                    },
                ],
            }
        )
    return {
        "version": "1.0",
        "schema": schema_name,
        "description": f"Tier 3 agent-specific metadata schema for {agent_name}.",
        "tables": table_specs,
    }


def synthesis_to_package(
    synthesis: ImplDocSynthesisOutput,
    *,
    tier_3_schema_required: bool = False,
    tier_3_tables: Optional[list[str]] = None,
) -> ImplDocPackage:
    """Convert typed Instructor synthesis output into a Blackboard ImplDocPackage."""
    agent_yaml = synthesis.agent_manifest.model_dump(mode="json")
    stages_map = {
        stg.stage.id: stg.model_dump(mode="json")
        for stg in synthesis.stages
    }
    contracts_map = {
        ctr.contract.name: ctr.model_dump(mode="json")
        for ctr in synthesis.contracts
    }
    config_yaml = synthesis.config_doc.model_dump(mode="json")
    tests_yaml = synthesis.tests_doc.model_dump(mode="json")

    t3_yaml = synthesis.tier3_contract_yaml
    if t3_yaml is None and tier_3_schema_required:
        t3_yaml = _default_tier3_contract(
            synthesis.agent_manifest.agent.name,
            list(tier_3_tables or (synthesis.agent_manifest.metadata.tier_3_tables if synthesis.agent_manifest.metadata else [])),
        )

    return ImplDocPackage(
        agent_yaml=agent_yaml,
        stages=stages_map,
        contracts=contracts_map,
        config_yaml=config_yaml,
        tests_yaml=tests_yaml,
        tier3_contract_yaml=t3_yaml,
    )


def write_impl_docs_package(
    package: ImplDocPackage,
    output_dir: Path,
    *,
    tier3_contract_path: Optional[Path] = None,
) -> tuple[ContractPointer, Optional[ContractPointer]]:
    """Persist the 5-file impl_docs/ suite to disk and return tamper-evident Claim Check pointers."""
    base_dir = Path(output_dir)
    stages_dir = base_dir / "stages"
    contracts_dir = base_dir / "contracts"
    stages_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    def _dump_yaml(target: Path, data: Any) -> None:
        target.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    _dump_yaml(base_dir / "agent.yaml", package.agent_yaml)
    _dump_yaml(base_dir / "config.yaml", package.config_yaml)
    _dump_yaml(base_dir / "tests.yaml", package.tests_yaml)

    for stage_id, stage_data in sorted(package.stages.items()):
        _dump_yaml(stages_dir / f"stage_{stage_id}.yaml", stage_data)

    for contract_name, contract_data in sorted(package.contracts.items()):
        _dump_yaml(contracts_dir / f"{contract_name}.yaml", contract_data)

    agent_name = str((package.agent_yaml.get("agent") or {}).get("name") or "agent")
    agent_version = str((package.agent_yaml.get("agent") or {}).get("version") or "1.0.0")
    impl_pointer = build_contract_pointer_from_directory(
        contract_name=f"{agent_name}_impl_docs",
        base_dir=base_dir,
        contract_version=agent_version,
    )

    tier3_pointer: Optional[ContractPointer] = None
    if package.tier3_contract_yaml is not None:
        t3_target = tier3_contract_path or (base_dir / f"tier_3_{agent_name}.yaml")
        t3_target.parent.mkdir(parents=True, exist_ok=True)
        t3_bytes = yaml.safe_dump(
            package.tier3_contract_yaml, sort_keys=False, allow_unicode=True
        ).encode("utf-8")
        t3_target.write_bytes(t3_bytes)
        tier3_pointer = build_contract_pointer_from_bytes(
            contract_name=f"{agent_name}_tier3_contract",
            content=t3_bytes,
            storage_uri=t3_target.resolve().as_uri(),
            contract_version=str(package.tier3_contract_yaml.get("version") or "1.0"),
            file_manifest=[t3_target.name],
        )

    return impl_pointer, tier3_pointer


class DocumentarianSpecialist:
    """Authors and cross-validates the 5-file impl_docs/ package from approved architecture."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        *,
        max_repair_attempts: int = 2,
    ) -> None:
        self.llm_client: StructuredLLMClient = llm_client or LiteLLMInstructorClient()
        self.max_repair_attempts = max(0, max_repair_attempts)

    def author_impl_docs(
        self,
        projection: DocumentarianProjection,
        *,
        output_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Generate, validate, and optionally persist impl_docs/ into Blackboard state."""
        arch = projection.approved_architecture
        t3_required = bool(arch.tier_3_schema_required) if arch is not None else False
        t3_tables = list(arch.tier_3_tables) if arch is not None else []

        repair_errors: list[str] = []
        package: Optional[ImplDocPackage] = None
        summary_text = ""

        for attempt in range(self.max_repair_attempts + 1):
            messages = build_documentarian_messages(projection, repair_errors=repair_errors)
            synthesis = self.llm_client.complete_structured(
                specialist_name="documentarian",
                response_model=ImplDocSynthesisOutput,
                messages=messages,
            )
            summary_text = synthesis.summary
            candidate_pkg = synthesis_to_package(
                synthesis,
                tier_3_schema_required=t3_required,
                tier_3_tables=t3_tables,
            )
            val_errors = validate_impl_doc_package(candidate_pkg)
            if not val_errors:
                package = candidate_pkg
                break
            repair_errors = val_errors
            if attempt >= self.max_repair_attempts:
                raise ImplDocValidationError(val_errors)

        assert package is not None
        impl_pointer: Optional[ContractPointer] = None
        tier3_pointer: Optional[ContractPointer] = None
        if output_dir is not None:
            impl_pointer, tier3_pointer = write_impl_docs_package(package, output_dir)
            package = package.model_copy(update={"pointer": impl_pointer})

        next_rev = projection.impl_doc_revision_count + 1
        revision = ImplDocRevision(
            revision_number=next_rev,
            package=package,
            summary=summary_text,
            revision_feedback_addressed=projection.impl_doc_feedback,
            approved=False,
        )

        return {
            "current_phase": "document",
            "impl_doc_revisions": [revision],
            "impl_doc_revision_count": next_rev,
            "impl_doc_approved": False,
            "impl_doc_feedback": None,
            "impl_docs_pointer": impl_pointer,
            "tier3_contract_pointer": tier3_pointer,
            "build_logs": [
                BuildLogEntry(
                    phase="document",
                    actor="documentarian",
                    action="author_impl_docs",
                    status="completed",
                    details={
                        "revision": next_rev,
                        "stages": sorted(package.stages.keys()),
                        "contracts": sorted(package.contracts.keys()),
                        "has_tier3_contract": package.tier3_contract_yaml is not None,
                    },
                )
            ],
        }
