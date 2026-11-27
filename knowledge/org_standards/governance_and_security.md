# DDLC Agent Studio — Governance & Security Standards

Mandatory security controls, data governance boundaries, role isolation, and auditability rules for DDLC Agent Studio and generated DDLC agents.

---

## 1. Data Classification & Enterprise Policy Consumer Model

Per **Decision 23**, DDLC Studio retires standalone `governance.yaml` / `governance.py` modules and treats every DDLC agent as an **enterprise policy consumer**, not a standalone governance authority:

- **Contract Classification Levels**: Every data contract in `contracts/*.yaml` declares `classification` from the closed enum:
  `public` | `internal` | `confidential` | `restricted`
- **Field-Level Sensitivity Tags**: Sensitive fields declare contract metadata (`pii: true`, `pii_category`, `tags: ["gdpr", "financial"]`) rather than bespoke Python masking scripts.
- **Warehouse-Native Dynamic Masking**: For internal analytical platforms (Snowflake Dynamic Data Masking `WITH TAG`, Databricks Unity Catalog, Collibra, Immuta), contract tags compile into warehouse column tags so central platform policies enforce masking at query time.
- **Edge Egress Rule**: `governance.pii_strategy` (`mask | hash | encrypt | redact | tokenize | none_warehouse_managed`) applies strictly when egressing data to untrusted external endpoints. **Never generate ad-hoc regex PII scrubbers inside leaf pipeline code.**

---

## 2. Human Approval Gates & Cryptographic Freeze Clauses

- **Side-Effecting Gate Requirement**: Any stage producing irreversible external side effects (pushing Git branches, opening Pull Requests, registering Tier 2 lineage, or running destructive DDL) must be preceded by a `human_review` stage (`autonomy: human_required`) using LangGraph `interrupt()`.
- **Cryptographic Freeze Clause**: The human review gate (`node_review_stage`) presents the Claim Check pointer (`bundle_id`, `storage_uri`, `file_manifest`, `artifact_sha256`). Only an explicit `"approved"` decision creates `ReviewedArtifactBundle`, freezing the `bundle_id` and `artifact_sha256`.
- **Downstream Enforcement**: `node_deliver_stage` must verify `state.get("human_approved", False)` and validate `ReviewedArtifactBundle` prior to Git or Tier 2 writes. Downstream stages must never modify a frozen artifact bundle.
- **Destructive Schema Gate**: The DCM Schema Engine (`src/dcm/safety.py`) automatically flags `ALTER COLUMN TYPE`, `DROP COLUMN`, `DROP TABLE`, and `DROP SCHEMA` as destructive and halts execution until `--approve-destructive` is explicitly supplied.

---

## 3. Immutable Compliance Audit Trail (`SEC-01`)

- **Database-Enforced Immutability**: Table `agent_metadata.audit_log` (Tier 1) is legally append-only. PostgreSQL trigger `trg_audit_log_immutable` executing `agent_metadata.enforce_audit_log_immutability()` physically rejects all `UPDATE` and `DELETE` statements with:
  `UPDATE and DELETE operations are prohibited on table agent_metadata.audit_log (Compliance Rule SEC-01)`
- **Audit Coverage**: Gate approvals, overrides, schema updates, and security policy events are recorded via `MetadataCallbackHandler.record_audit_event(...)` alongside `decision_log` and Tier 2 `stage_gate_record` / `governance_audit`.

---

## 4. Least-Privilege PostgreSQL Roles

PostgreSQL access is strictly split between schema migration administration and runtime application execution (`infra/postgres/init/01_init.sh`, `.env.example`):

| Role | Environment Variable | Privileges & Scope |
|---|---|---|
| **`ddlc_admin`** | `DCM_MIGRATION_DB_URI` | Schema owner used **exclusively** by the DCM Schema Engine / Alembic CLI (`dcm apply`) to create schemas, tables, triggers, and indexes. |
| **`ddlc_app`** | `CHECKPOINTER_DB_URI`, `METADATA_DB_URI`, `RAG_DB_URI` | Least-privilege runtime role (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`) granted only `CONNECT`, schema `USAGE`, sequence `USAGE/SELECT`, and table `SELECT, INSERT, UPDATE, DELETE`. **No `DROP TABLE`, `ALTER`, or `TRUNCATE` privileges.** |

All dynamic schema/table references in SQL builders must pass Python `identifier.isidentifier()` validation (`_validate_sql_identifier` in `src/metadata/db.py`) before identifier quoting.

---

## 5. Path Traversal & Sandbox Isolation

When an LLM node emits file paths (`GeneratedFileSet`), the Skeleton node **must** sanitize paths before writing to `DBT_SANDBOX_DIR` (Decision 31):

```python
base_bundle_dir = bundle_path.resolve()
for file_entry in file_set.files:
    target_path = (base_bundle_dir / file_entry.path).resolve()
    if not target_path.is_relative_to(base_bundle_dir):
        raise ValueError(f"Path traversal detected in LLM output: {file_entry.path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(file_entry.content.encode("utf-8"))
```

- Generated dbt compilation runs exclusively inside `DBT_SANDBOX_DIR` using isolated dev/sandbox profiles — never against production warehouse credentials.
- Read-only ReAct exploration tools (`access: read_only`) must validate arguments and reject SQL statements containing write/DDL keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`).

---

## 6. Secret Externalization & Telemetry Redaction

- **No Plaintext Credentials**: Never embed API keys, passwords, or authenticated DB URIs in `spec.yaml`, `impl_docs/`, `config/settings.yaml`, or Python source files. Always declare secret placeholders via `${secrets.<lowercase_name>}` in `config.yaml` and load runtime values from environment variables / `.env` / secrets managers.
- **Automatic Telemetry Sanitization (`src/metadata/db.py`)**:
  - `sanitize_text()` and `sanitize_payload()` redact secrets matching `telemetry.redact_patterns` (`sk-[A-Za-z0-9\-_]{16,}`, `Bearer\s+[A-Za-z0-9\-._~+/]+=*`) to `[REDACTED]` before writing to JSONB or logs.
  - `truncate_stack_trace()` caps tracebacks at `stack_trace_max_bytes` (`32768` bytes / 32 KB).
  - `guard_inline_jsonb()` caps inline JSONB payloads at `inline_payload_max_bytes` (`65536` bytes / 64 KB), replacing oversized payloads with a structured pointer directing callers to `agent_metadata.artifact_registry`.
