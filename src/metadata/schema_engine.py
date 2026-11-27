"""Studio Tier 3 DCM Schema Engine wrapper enforcing ddlc_<agent_name>_metadata conventions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from src.core.settings import DEFAULT_PROJECT_ROOT, get_env_settings, get_studio_config
from src.dcm.compiler import (
    ContractValidationError,
    load_contract,
    load_contracts_from_dir,
    validate_contracts_integrity,
)
from src.dcm.engine import SchemaEngine
from src.dcm.migrator import MigrationPlan, compute_offline_plan
from src.dcm.models import DataContractSchema

TIER3_SCHEMA_PATTERN = re.compile(r"^ddlc_([a-z][a-z0-9_]*)_metadata$")
_AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class Tier3ContractError(ContractValidationError):
    """Raised when a Tier 3 data contract violates naming or Tier 1/2 reference rules."""


def _reserved_platform_schemas() -> set[str]:
    schemas = get_studio_config().database.schemas
    return {"public", schemas.tier1, schemas.tier2, schemas.rag}


class Tier3SchemaClient:
    """Studio facade over DCM SchemaEngine for Tier 3 agent-specific metadata schemas."""

    def __init__(
        self,
        *,
        db_uri: Optional[str] = None,
        project_root: Optional[Path] = None,
        contracts_dir: Optional[Path] = None,
        engine: Optional[SchemaEngine] = None,
    ) -> None:
        self.project_root = project_root or DEFAULT_PROJECT_ROOT
        if contracts_dir is not None:
            self.contracts_dir = Path(contracts_dir)
        else:
            rel_contracts = get_studio_config().app.paths.contracts_dir
            self.contracts_dir = self.project_root / rel_contracts
        self._db_uri = db_uri
        self._engine = engine

    @property
    def engine(self) -> SchemaEngine:
        """Lazily initialize the underlying DCM SchemaEngine using DCM_MIGRATION_DB_URI."""
        if self._engine is None:
            resolved_uri = self._db_uri or get_env_settings().dcm_migration_db_uri
            if not resolved_uri:
                raise ValueError(
                    "Migration database URI is required (pass db_uri or set DCM_MIGRATION_DB_URI)."
                )
            self._engine = SchemaEngine(
                db_url=resolved_uri,
                project_root=self.project_root,
            )
        return self._engine

    @staticmethod
    def expected_schema_name(agent_name: str) -> str:
        """Compute and validate the canonical Tier 3 schema name `ddlc_<agent_name>_metadata`."""
        normalized = agent_name.strip().lower().replace("-", "_")
        if not normalized or not _AGENT_NAME_PATTERN.match(normalized):
            raise Tier3ContractError(
                f"Invalid agent_name {agent_name!r} for Tier 3 schema derivation; "
                "expected lowercase alphanumeric/underscore identifier starting with a letter."
            )
        candidate = f"ddlc_{normalized}_metadata"
        if candidate in _reserved_platform_schemas():
            raise Tier3ContractError(
                f"Agent name {agent_name!r} collides with reserved platform schema {candidate!r}."
            )
        return candidate

    @classmethod
    def validate_schema_name(
        cls,
        schema_name: str,
        *,
        agent_name: Optional[str] = None,
    ) -> str:
        """Verify that a Tier 3 schema name conforms to `ddlc_<agent_name>_metadata`."""
        reserved = _reserved_platform_schemas()
        if schema_name in reserved:
            raise Tier3ContractError(
                f"Schema {schema_name!r} is a reserved platform schema and cannot be used as Tier 3."
            )
        if not TIER3_SCHEMA_PATTERN.match(schema_name):
            raise Tier3ContractError(
                f"Tier 3 schema name {schema_name!r} must match 'ddlc_<agent_name>_metadata'."
            )
        if agent_name is not None:
            expected = cls.expected_schema_name(agent_name)
            if schema_name != expected:
                raise Tier3ContractError(
                    f"Tier 3 schema {schema_name!r} does not match expected schema {expected!r} "
                    f"for agent {agent_name!r}."
                )
        return schema_name

    def _load_reference_contracts(
        self,
        *,
        exclude_schema: str,
        reference_dir: Optional[str | Path] = None,
    ) -> list[DataContractSchema]:
        ref_root = Path(reference_dir) if reference_dir is not None else self.contracts_dir
        if not ref_root.is_dir():
            raise FileNotFoundError(
                f"Reference contracts directory not found: {ref_root}"
            )
        return [
            contract
            for contract in load_contracts_from_dir(ref_root)
            if contract.schema_name != exclude_schema
        ]

    def validate_contract(
        self,
        contract_path: str | Path,
        *,
        agent_name: Optional[str] = None,
        reference_dir: Optional[str | Path] = None,
    ) -> DataContractSchema:
        """Validate a Tier 3 YAML contract against naming rules and Tier 1/2 contracts."""
        contract = load_contract(Path(contract_path))
        self.validate_schema_name(contract.schema_name, agent_name=agent_name)
        refs = self._load_reference_contracts(
            exclude_schema=contract.schema_name,
            reference_dir=reference_dir,
        )
        validate_contracts_integrity(
            [contract],
            reference_contracts=refs,
            strict_external=True,
        )
        return contract

    def plan(
        self,
        contract_path: str | Path,
        *,
        agent_name: Optional[str] = None,
        offline: bool = False,
        reference_dir: Optional[str | Path] = None,
    ) -> MigrationPlan:
        """Validate a Tier 3 contract and compute an offline or live MigrationPlan."""
        contract = self.validate_contract(
            contract_path,
            agent_name=agent_name,
            reference_dir=reference_dir,
        )
        if offline:
            return compute_offline_plan([contract])
        return self.engine.plan(contract_path, offline=False)

    def apply_migration(
        self,
        contract_path: str | Path,
        message: str,
        *,
        agent_name: Optional[str] = None,
        approve_destructive: bool = False,
        reference_dir: Optional[str | Path] = None,
    ) -> Optional[str]:
        """Validate, generate an Alembic revision, and apply a Tier 3 migration cleanly."""
        self.validate_contract(
            contract_path,
            agent_name=agent_name,
            reference_dir=reference_dir,
        )
        return self.engine.migrate(
            contract_path,
            message=message,
            approve_destructive=approve_destructive,
        )

    def migrate(
        self,
        contract_path: str | Path,
        message: str,
        *,
        agent_name: Optional[str] = None,
        approve_destructive: bool = False,
        reference_dir: Optional[str | Path] = None,
    ) -> Optional[str]:
        """Alias for apply_migration matching SchemaEngine.migrate()."""
        return self.apply_migration(
            contract_path,
            message=message,
            agent_name=agent_name,
            approve_destructive=approve_destructive,
            reference_dir=reference_dir,
        )

    def status(self, contracts_dir: Optional[str | Path] = None) -> dict[str, Any]:
        """Return Alembic revision and per-schema sync status."""
        return self.engine.status(contracts_dir=contracts_dir)
