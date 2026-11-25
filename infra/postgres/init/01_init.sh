#!/usr/bin/env bash
set -euo pipefail

# Provision extensions, 3-tier + RAG schemas, and least-privilege runtime role.
# Runs automatically on first PostgreSQL container initialization.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db_name="$POSTGRES_DB" \
  -v admin_user="$POSTGRES_USER" \
  -v app_user="$POSTGRES_APP_USER" \
  -v app_password="$POSTGRES_APP_PASSWORD" <<'EOSQL'
-- 1. Required Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 2. Base Schemas (owned by admin/migrator role)
CREATE SCHEMA IF NOT EXISTS agent_metadata;
CREATE SCHEMA IF NOT EXISTS ddlc_shared_metadata;
CREATE SCHEMA IF NOT EXISTS rag_knowledge;

-- 3. Least-Privilege Runtime Role (no SUPERUSER, CREATEDB, CREATEROLE, TRUNCATE, or DROP)
SELECT format(
  'CREATE ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'app_user',
  :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

-- 4. Database & Schema Grants
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'app_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA agent_metadata, ddlc_shared_metadata, rag_knowledge, public TO %I', :'app_user') \gexec

-- 5. Default DML Privileges for Future DCM/RAG/Checkpointer Tables (excludes TRUNCATE/DROP)
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA agent_metadata, ddlc_shared_metadata, rag_knowledge, public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'admin_user',
  :'app_user'
) \gexec

SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA agent_metadata, ddlc_shared_metadata, rag_knowledge, public GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'admin_user',
  :'app_user'
) \gexec
EOSQL
