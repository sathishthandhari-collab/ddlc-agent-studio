"""DCM Schema Engine package (SQLGlot compiler + Alembic migration manager)."""

from src.dcm.compiler import (
    ContractValidationError,
    compile_contract,
    compile_contracts,
    compile_to_sql,
    load_contract,
    load_contracts_from_dir,
    resolve_pg_type,
    sort_contracts_by_dependencies,
    validate_contracts_integrity,
)
from src.dcm.engine import SchemaEngine
from src.dcm.migrator import (
    CatalogSnapshot,
    DestructiveMigrationError,
    DiffOp,
    MigrationPlan,
    apply_migrations,
    compute_migration_plan,
    compute_offline_plan,
    create_revision,
    get_current_revision,
    get_migration_status,
    introspect_database,
    types_equivalent,
)
from src.dcm.models import (
    CheckConstraintSpec,
    ColumnSpec,
    DataContractSchema,
    ForeignKeySpec,
    IndexSpec,
    TableSpec,
    TriggerSpec,
    UniqueConstraintSpec,
    parse_index_column,
    parse_reference,
)
from src.dcm.safety import (
    DDLClassification,
    RiskLevel,
    SafetyReport,
    analyze_migration,
    classify_statement,
    gate_check,
)

__all__ = [
    # Engine facade
    "SchemaEngine",
    # Models
    "DataContractSchema",
    "TableSpec",
    "ColumnSpec",
    "IndexSpec",
    "ForeignKeySpec",
    "CheckConstraintSpec",
    "UniqueConstraintSpec",
    "TriggerSpec",
    "parse_reference",
    "parse_index_column",
    # Compiler
    "ContractValidationError",
    "load_contract",
    "load_contracts_from_dir",
    "resolve_pg_type",
    "sort_contracts_by_dependencies",
    "validate_contracts_integrity",
    "compile_contract",
    "compile_contracts",
    "compile_to_sql",
    # Migrator
    "CatalogSnapshot",
    "DestructiveMigrationError",
    "DiffOp",
    "MigrationPlan",
    "introspect_database",
    "compute_migration_plan",
    "compute_offline_plan",
    "create_revision",
    "apply_migrations",
    "get_current_revision",
    "get_migration_status",
    "types_equivalent",
    # Safety
    "RiskLevel",
    "DDLClassification",
    "SafetyReport",
    "classify_statement",
    "analyze_migration",
    "gate_check",
]
