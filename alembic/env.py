"""Alembic environment configuration for DDLC Agent Studio.

Supports multi-schema migrations across:
  - agent_metadata (Tier 1)
  - ddlc_shared_metadata (Tier 2)
  - ddlc_{agent}_metadata (Tier 3, dynamically)

The database URL is injected by the DCM CLI or directly from environment.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Alembic Config object
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata — None means we do NOT use autogenerate.
# DCM generates DDL from YAML contracts, not from SQLAlchemy models.
target_metadata = None

# Schemas managed by DCM
MANAGED_SCHEMAS = ["agent_metadata", "ddlc_shared_metadata"]


def include_name(name, type_, parent_names):
    """Filter which schemas to include in autogenerate (if ever used)."""
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def _sqlalchemy_url() -> str:
    """Ensure postgresql:// URIs use the installed psycopg v3 driver."""
    url = config.get_main_option("sqlalchemy.url") or ""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to script output."""
    url = _sqlalchemy_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema="public",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    connectable = create_engine(
        _sqlalchemy_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table_schema="public",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
