"""Programmatic SchemaEngine facade for the Meta-Agent Builder specialist.

Matches the interface documented in design-docs/dcm-agent/dcm_agent.md §7:
    from dcm.engine import SchemaEngine
    engine = SchemaEngine(db_url=...)
    engine.validate_contract("contracts/tier_3_customer.yaml")
    revision = engine.migrate("contracts/tier_3_customer.yaml", message="...")
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.dcm.compiler import (
    load_contract,
    load_contracts_from_dir,
    validate_contracts_integrity,
)
from src.dcm.migrator import (
    MigrationPlan,
    apply_migrations,
    compute_migration_plan,
    compute_offline_plan,
    create_revision,
    get_current_revision,
    get_migration_status,
    introspect_database,
)
from src.dcm.models import DataContractSchema


class SchemaEngine:
    """High-level DCM Schema Engine client for programmatic & Meta-Agent use."""

    def __init__(
        self,
        db_url: Optional[str] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        if db_url is None:
            from src.core.settings import get_env_settings

            db_url = get_env_settings().dcm_migration_db_uri
        if not db_url:
            raise ValueError(
                "Database URL is required (pass db_url or set DCM_MIGRATION_DB_URI)."
            )
        self.db_url = db_url
        self.project_root = project_root or Path(__file__).resolve().parents[2]

    def validate_contract(
        self,
        contract_path: str | Path,
        reference_dir: Optional[str | Path] = None,
    ) -> DataContractSchema:
        """Validate a single YAML contract file including cross-schema FK integrity."""
        path = Path(contract_path)
        contract = load_contract(path)

        ref_root = (
            Path(reference_dir)
            if reference_dir is not None
            else (self.project_root / "contracts")
        )
        refs: list[DataContractSchema] = []
        if ref_root.is_dir():
            refs = [
                c
                for c in load_contracts_from_dir(ref_root)
                if c.schema_name != contract.schema_name
            ]

        validate_contracts_integrity(
            [contract],
            reference_contracts=refs,
            strict_external=bool(refs),
        )
        return contract

    def validate_directory(
        self,
        contracts_dir: str | Path,
    ) -> list[DataContractSchema]:
        """Validate all YAML contracts in a directory together."""
        contracts = load_contracts_from_dir(Path(contracts_dir))
        validate_contracts_integrity(contracts, strict_external=True)
        return contracts

    def plan(
        self,
        contract_path: str | Path,
        offline: bool = False,
    ) -> MigrationPlan:
        """Compute a MigrationPlan for a single contract file or directory."""
        target = Path(contract_path)
        contracts = (
            load_contracts_from_dir(target)
            if target.is_dir()
            else [self.validate_contract(target)]
        )
        if offline:
            return compute_offline_plan(contracts)
        schemas = [c.schema_name for c in contracts]
        catalog = introspect_database(self.db_url, schemas)
        return compute_migration_plan(contracts, catalog)

    def migrate(
        self,
        contract_path: str | Path,
        message: str,
        approve_destructive: bool = False,
    ) -> Optional[str]:
        """Validate, generate Alembic revision, and apply migration atomically.

        Returns:
            Applied Alembic revision ID, or None if schema was already up to date.
        """
        target = Path(contract_path)
        contracts = (
            self.validate_directory(target)
            if target.is_dir()
            else [self.validate_contract(target)]
        )
        rev_path = create_revision(
            db_uri=self.db_url,
            message=message,
            contracts=contracts,
            project_root=self.project_root,
        )
        if rev_path is None:
            return None

        apply_migrations(
            db_uri=self.db_url,
            revision="head",
            project_root=self.project_root,
            approve_destructive=approve_destructive,
        )
        return get_current_revision(self.db_url, project_root=self.project_root)

    def status(self, contracts_dir: Optional[str | Path] = None) -> dict:
        """Return Alembic revision and per-schema sync status."""
        contracts: Optional[list[DataContractSchema]] = None
        if contracts_dir is not None:
            contracts = load_contracts_from_dir(Path(contracts_dir))
        return get_migration_status(
            db_uri=self.db_url,
            contracts=contracts,
            project_root=self.project_root,
        )
