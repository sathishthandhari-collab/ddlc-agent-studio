"""provision_rag_knowledge_schema

Revision ID: b1f4c8d9e2a3
Revises: efc49864d5a6
Create Date: 2026-11-27 00:00:00.000000+00:00

"""
from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1f4c8d9e2a3'
down_revision: Union[str, None] = 'efc49864d5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALLOWED_DOMAINS_SQL = (
    "'library_docs', "
    "'org_standards', "
    "'pattern_catalog', "
    "'past_decisions', "
    "'component_docs', "
    "'code_conventions'"
)


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector";')
    op.execute("CREATE SCHEMA IF NOT EXISTS rag_knowledge;")

    op.execute(f"""
CREATE TABLE IF NOT EXISTS rag_knowledge.knowledge_documents (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(64) NOT NULL,
    source_path VARCHAR(512) NOT NULL,
    title VARCHAR(256) NOT NULL,
    file_sha256 CHAR(64) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimension INT NOT NULL DEFAULT 1536,
    chunk_count INT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_rag_source UNIQUE (domain, source_path),
    CONSTRAINT chk_rag_domain CHECK (domain IN ({_ALLOWED_DOMAINS_SQL}))
);
    """)

    op.execute(f"""
CREATE TABLE IF NOT EXISTS rag_knowledge.knowledge_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES rag_knowledge.knowledge_documents(document_id) ON DELETE CASCADE,
    domain VARCHAR(64) NOT NULL,
    chunk_index INT NOT NULL,
    chunk_sha256 CHAR(64) NOT NULL,
    breadcrumb VARCHAR(512),
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    tsv_content_simple tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    tsv_content_english tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    token_count INT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_rag_chunk_order UNIQUE (document_id, chunk_index),
    CONSTRAINT chk_rag_chunk_domain CHECK (domain IN ({_ALLOWED_DOMAINS_SQL}))
);
    """)

    # B-tree indexes for filtering and foreign-key lookups
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_docs_domain "
        "ON rag_knowledge.knowledge_documents(domain);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_domain "
        "ON rag_knowledge.knowledge_chunks(domain);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc_id "
        "ON rag_knowledge.knowledge_chunks(document_id);"
    )

    # 6 domain-partitioned partial HNSW indexes (cosine distance metric: <=>)
    for domain in (
        "library_docs",
        "org_standards",
        "pattern_catalog",
        "past_decisions",
        "component_docs",
        "code_conventions",
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_rag_hnsw_{domain} "
            f"ON rag_knowledge.knowledge_chunks "
            f"USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) "
            f"WHERE domain = '{domain}';"
        )

    # Dual inverted GIN indexes for full-text search
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv_simple "
        "ON rag_knowledge.knowledge_chunks USING gin(tsv_content_simple);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_chunks_tsv_english "
        "ON rag_knowledge.knowledge_chunks USING gin(tsv_content_english);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS rag_knowledge.idx_rag_chunks_tsv_english;")
    op.execute("DROP INDEX IF EXISTS rag_knowledge.idx_rag_chunks_tsv_simple;")

    for domain in (
        "code_conventions",
        "component_docs",
        "past_decisions",
        "pattern_catalog",
        "org_standards",
        "library_docs",
    ):
        op.execute(f"DROP INDEX IF EXISTS rag_knowledge.idx_rag_hnsw_{domain};")

    op.execute("DROP INDEX IF EXISTS rag_knowledge.idx_rag_chunks_doc_id;")
    op.execute("DROP INDEX IF EXISTS rag_knowledge.idx_rag_chunks_domain;")
    op.execute("DROP INDEX IF EXISTS rag_knowledge.idx_rag_docs_domain;")

    op.execute("DROP TABLE IF EXISTS rag_knowledge.knowledge_chunks CASCADE;")
    op.execute("DROP TABLE IF EXISTS rag_knowledge.knowledge_documents CASCADE;")
