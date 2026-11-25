"""DCM Schema Engine package (SQLGlot compiler + Alembic migration manager)."""

from .compiler import (
    ContractValidationError,
    compile_contract,
    compile_contracts,
    compile_to_sql,
    load_contract,
    load_contracts_from_dir,
    normalize_pg_type,
    resolve_pg_type,
    sort_contracts_by_dependencies,
    types_equivalent,
    validate_contracts_integrity,
    validate_sql_ast,
)
from .engine import SchemaEngine
from .migrator import (
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
)
from .models import (
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
from .safety import (
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
    "normalize_pg_type",
    "types_equivalent",
    "validate_sql_ast",
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
    # Safety
    "RiskLevel",
    "DDLClassification",
    "SafetyReport",
    "classify_statement",
    "analyze_migration",
    "gate_check",
]
