"""Alembic environment configuration for DDLC Agent Studio.

Supports multi-schema migrations across:
  - agent_metadata (Tier 1)
  - ddlc_shared_metadata (Tier 2)
  - ddlc_{agent}_metadata (Tier 3, dynamically matched)

Resolves sqlalchemy.url from DCM CLI injection or DCM_MIGRATION_DB_URI environment.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

MANAGED_SCHEMAS = {"agent_metadata", "ddlc_shared_metadata"}


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Include Tier 1, Tier 2, and any Tier 3 ddlc_*_metadata schemas."""
    if type_ == "schema" and name is not None:
        return name in MANAGED_SCHEMAS or (
            name.startswith("ddlc_") and name.endswith("_metadata")
        )
    return True


def _resolve_sqlalchemy_url() -> str:
    """Resolve DB URL from Alembic config or DCM_MIGRATION_DB_URI, using psycopg v3."""
    url = config.get_main_option("sqlalchemy.url") or ""
    if not url or url.startswith("driver://"):
        load_dotenv()
        url = os.getenv("DCM_MIGRATION_DB_URI", "")
    if not url:
        raise RuntimeError(
            "Database URL not set for Alembic. Configure DCM_MIGRATION_DB_URI or run via dcm CLI."
        )
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to script output."""
    context.configure(
        url=_resolve_sqlalchemy_url(),
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
        _resolve_sqlalchemy_url(),
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
