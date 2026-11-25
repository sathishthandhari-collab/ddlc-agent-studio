"""initial_metadata_framework_baseline

Revision ID: 8a1346032e3a
Revises: 
Create Date: 2026-11-25 18:16:09.262518+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a1346032e3a'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."agent_run_header" (
    "run_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "parent_run_id" UUID DEFAULT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "agent_version" VARCHAR(50) NOT NULL,
    "operation_kind" VARCHAR(50) NOT NULL DEFAULT 'standard',
    "initiator_id" VARCHAR(100) NOT NULL,
    "initiator_type" VARCHAR(30) NOT NULL,
    "trigger_type" VARCHAR(30) NOT NULL,
    "source_revision" VARCHAR(100) DEFAULT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'running',
    "started_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "completed_at" TIMESTAMPTZ DEFAULT NULL,
    "duration_ms" BIGINT DEFAULT NULL,
    "total_input_tokens" INTEGER NOT NULL DEFAULT 0,
    "total_output_tokens" INTEGER NOT NULL DEFAULT 0,
    "total_cost_usd" NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    "run_parameters" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "agent_run_header_pkey" PRIMARY KEY ("run_id"),
    CONSTRAINT "chk_agent_run_header_initiator_type" CHECK (initiator_type IN ('human', 'agent', 'system', 'schedule')),
    CONSTRAINT "chk_agent_run_header_trigger_type" CHECK (trigger_type IN ('manual', 'api', 'schedule', 'event', 'parent_run')),
    CONSTRAINT "chk_agent_run_header_status" CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'paused', 'timed_out')),
    CONSTRAINT "chk_run_duration" CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT "chk_run_terminal_completed" CHECK (status NOT IN ('completed', 'failed', 'cancelled', 'timed_out') OR completed_at IS NOT NULL),
    CONSTRAINT "chk_run_running_completed" CHECK (status != 'running' OR completed_at IS NULL)
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."execution_log" (
    "log_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "parent_log_id" UUID DEFAULT NULL,
    "retry_of_log_id" UUID DEFAULT NULL,
    "attempt_number" INTEGER NOT NULL DEFAULT 1,
    "event_sequence" INTEGER NOT NULL,
    "trace_id" VARCHAR(64) DEFAULT NULL,
    "span_id" VARCHAR(32) DEFAULT NULL,
    "step_name" VARCHAR(100) NOT NULL,
    "step_type" VARCHAR(30) NOT NULL DEFAULT 'node',
    "status" VARCHAR(30) NOT NULL DEFAULT 'started',
    "started_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "completed_at" TIMESTAMPTZ DEFAULT NULL,
    "duration_ms" INTEGER DEFAULT NULL,
    "model_provider" VARCHAR(50) DEFAULT NULL,
    "model_used" VARCHAR(100) DEFAULT NULL,
    "input_token_count" INTEGER NOT NULL DEFAULT 0,
    "output_token_count" INTEGER NOT NULL DEFAULT 0,
    "cached_token_count" INTEGER NOT NULL DEFAULT 0,
    "reasoning_token_count" INTEGER NOT NULL DEFAULT 0,
    "error_type" VARCHAR(100) DEFAULT NULL,
    "error_message" TEXT DEFAULT NULL,
    "stack_trace" TEXT DEFAULT NULL,
    "details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "execution_log_pkey" PRIMARY KEY ("log_id"),
    CONSTRAINT "chk_execution_log_attempt_number" CHECK (attempt_number >= 1),
    CONSTRAINT "chk_execution_log_step_type" CHECK (step_type IN ('node', 'tool_call', 'llm_call', 'gate', 'system')),
    CONSTRAINT "chk_execution_log_status" CHECK (status IN ('started', 'completed', 'failed', 'skipped', 'timed_out')),
    CONSTRAINT "chk_exec_duration" CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT "chk_exec_terminal" CHECK (status NOT IN ('completed', 'failed', 'skipped', 'timed_out') OR completed_at IS NOT NULL),
    CONSTRAINT "uq_execution_run_log" UNIQUE ("run_id", "log_id")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."artifact_registry" (
    "artifact_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "produced_run_id" UUID DEFAULT NULL,
    "produced_log_id" UUID DEFAULT NULL,
    "artifact_name" VARCHAR(255) NOT NULL,
    "artifact_kind" VARCHAR(50) NOT NULL,
    "artifact_version" VARCHAR(50) NOT NULL DEFAULT '1.0.0',
    "storage_uri" TEXT NOT NULL,
    "checksum_sha256" VARCHAR(64) NOT NULL,
    "byte_size" BIGINT DEFAULT NULL,
    "mime_type" VARCHAR(100) DEFAULT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "metadata" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "artifact_registry_pkey" PRIMARY KEY ("artifact_id"),
    CONSTRAINT "chk_artifact_registry_byte_size" CHECK (byte_size IS NULL OR byte_size >= 0)
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."artifact_manifest" (
    "manifest_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "artifact_id" UUID NOT NULL,
    "associated_log_id" UUID DEFAULT NULL,
    "role" VARCHAR(30) NOT NULL,
    "associated_phase" VARCHAR(50) DEFAULT NULL,
    "associated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "artifact_manifest_pkey" PRIMARY KEY ("manifest_id"),
    CONSTRAINT "chk_artifact_manifest_role" CHECK (role IN ('input', 'output', 'intermediate')),
    CONSTRAINT "uq_run_artifact_role" UNIQUE ("run_id", "artifact_id", "role")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."audit_log" (
    "audit_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID DEFAULT NULL,
    "correlation_id" VARCHAR(100) DEFAULT NULL,
    "occurred_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "recorded_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "action" VARCHAR(100) NOT NULL,
    "actor_id" VARCHAR(100) NOT NULL,
    "actor_type" VARCHAR(30) NOT NULL,
    "target_entity_type" VARCHAR(50) NOT NULL,
    "target_entity_id" VARCHAR(100) DEFAULT NULL,
    "outcome" VARCHAR(30) NOT NULL DEFAULT 'success',
    "before_state" JSONB DEFAULT NULL,
    "after_state" JSONB DEFAULT NULL,
    "ip_address" INET DEFAULT NULL,
    "user_agent" TEXT DEFAULT NULL,
    "details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "audit_log_pkey" PRIMARY KEY ("audit_id"),
    CONSTRAINT "chk_audit_log_actor_type" CHECK (actor_type IN ('human', 'agent', 'service_account', 'system')),
    CONSTRAINT "chk_audit_log_outcome" CHECK (outcome IN ('success', 'denied', 'failed', 'error'))
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "agent_metadata"."decision_log" (
    "decision_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "log_id" UUID DEFAULT NULL,
    "decision_status" VARCHAR(30) NOT NULL DEFAULT 'decided',
    "decision_type" VARCHAR(50) NOT NULL,
    "decision_value" TEXT DEFAULT NULL,
    "decided_by" VARCHAR(100) DEFAULT NULL,
    "decided_by_type" VARCHAR(30) DEFAULT NULL,
    "rationale" TEXT DEFAULT NULL,
    "alternatives_considered" JSONB DEFAULT NULL,
    "confidence_score" NUMERIC(4, 3) DEFAULT NULL,
    "occurred_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "decision_log_pkey" PRIMARY KEY ("decision_id"),
    CONSTRAINT "chk_decision_log_decision_status" CHECK (decision_status IN ('pending', 'decided', 'rejected', 'cancelled')),
    CONSTRAINT "chk_decision_log_decided_by_type" CHECK (decided_by_type IS NULL OR decided_by_type IN ('human', 'agent', 'rule_engine')),
    CONSTRAINT "chk_decision_log_confidence_score" CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0.000 AND 1.000),
    CONSTRAINT "chk_decision_resolution" CHECK (decision_status != 'decided' OR (
    decision_value IS NOT NULL AND
    decided_by IS NOT NULL AND
    decided_by_type IS NOT NULL AND
    rationale IS NOT NULL
))
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."agent_registry" (
    "agent_id" VARCHAR(100) NOT NULL,
    "agent_name" VARCHAR(200) NOT NULL,
    "agent_version" VARCHAR(50) NOT NULL,
    "description" TEXT DEFAULT NULL,
    "architecture_pattern" VARCHAR(50) NOT NULL,
    "runtime" VARCHAR(50) NOT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'draft',
    "owner" VARCHAR(100) NOT NULL,
    "source_repo" TEXT DEFAULT NULL,
    "config_snapshot" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "agent_registry_pkey" PRIMARY KEY ("agent_id"),
    CONSTRAINT "chk_agent_registry_status" CHECK (status IN ('draft', 'building', 'active', 'deprecated', 'archived'))
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."component_registry" (
    "component_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "component_name" VARCHAR(200) NOT NULL,
    "component_version" VARCHAR(50) NOT NULL,
    "description" TEXT NOT NULL,
    "component_type" VARCHAR(50) NOT NULL,
    "interface_spec" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "storage_path" TEXT NOT NULL,
    "embedding" vector(1536) DEFAULT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'active',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "component_registry_pkey" PRIMARY KEY ("component_id"),
    CONSTRAINT "chk_component_registry_component_type" CHECK (component_type IN ('validator', 'transformer', 'connector', 'masker', 'quality_rule', 'utility', 'template')),
    CONSTRAINT "chk_component_registry_status" CHECK (status IN ('draft', 'active', 'deprecated', 'archived')),
    CONSTRAINT "uq_component_name_version" UNIQUE ("component_name", "component_version")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."contract_registry" (
    "contract_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "agent_id" VARCHAR(100) NOT NULL,
    "contract_name" VARCHAR(200) NOT NULL,
    "contract_version" VARCHAR(50) NOT NULL,
    "direction" VARCHAR(20) NOT NULL,
    "schema_definition" JSONB NOT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'active',
    "registered_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "contract_registry_pkey" PRIMARY KEY ("contract_id"),
    CONSTRAINT "chk_contract_registry_direction" CHECK (direction IN ('input', 'output', 'internal')),
    CONSTRAINT "chk_contract_registry_status" CHECK (status IN ('draft', 'active', 'deprecated', 'archived')),
    CONSTRAINT "uq_contract_agent_name_version" UNIQUE ("agent_id", "contract_name", "contract_version")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."contract_lineage" (
    "lineage_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "producer_agent_id" VARCHAR(100) NOT NULL,
    "producer_contract_id" UUID NOT NULL,
    "consumer_agent_id" VARCHAR(100) NOT NULL,
    "consumer_contract_id" UUID NOT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'active',
    "established_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "contract_lineage_pkey" PRIMARY KEY ("lineage_id"),
    CONSTRAINT "chk_contract_lineage_status" CHECK (status IN ('active', 'deprecated', 'proposed')),
    CONSTRAINT "uq_lineage_edge" UNIQUE ("producer_contract_id", "consumer_contract_id")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."agent_dependency" (
    "dependency_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "upstream_agent_id" VARCHAR(100) NOT NULL,
    "downstream_agent_id" VARCHAR(100) NOT NULL,
    "dependency_type" VARCHAR(30) NOT NULL,
    "contract_id" UUID DEFAULT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'active',
    "established_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "agent_dependency_pkey" PRIMARY KEY ("dependency_id"),
    CONSTRAINT "chk_agent_dependency_dependency_type" CHECK (dependency_type IN ('data', 'component', 'config')),
    CONSTRAINT "chk_agent_dependency_status" CHECK (status IN ('active', 'deprecated', 'proposed')),
    CONSTRAINT "uq_agent_dep_edge" UNIQUE ("upstream_agent_id", "downstream_agent_id", "dependency_type")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."component_usage" (
    "usage_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "agent_id" VARCHAR(100) NOT NULL,
    "component_id" UUID NOT NULL,
    "component_version" VARCHAR(50) NOT NULL,
    "usage_type" VARCHAR(30) NOT NULL,
    "first_used_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "last_used_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "component_usage_pkey" PRIMARY KEY ("usage_id"),
    CONSTRAINT "chk_component_usage_usage_type" CHECK (usage_type IN ('import', 'extend', 'wrap')),
    CONSTRAINT "uq_component_usage" UNIQUE ("agent_id", "component_id")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."build_summary" (
    "build_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "spec_version" VARCHAR(50) NOT NULL,
    "clarification_rounds" INTEGER NOT NULL DEFAULT 0,
    "architecture_revisions" INTEGER NOT NULL DEFAULT 0,
    "impl_doc_revisions" INTEGER NOT NULL DEFAULT 0,
    "build_fix_attempts" INTEGER NOT NULL DEFAULT 0,
    "total_duration_ms" BIGINT DEFAULT NULL,
    "outcome" VARCHAR(30) NOT NULL,
    "completed_at" TIMESTAMPTZ DEFAULT NULL,
    CONSTRAINT "build_summary_pkey" PRIMARY KEY ("build_id"),
    CONSTRAINT "chk_build_summary_outcome" CHECK (outcome IN ('success', 'partial', 'failed', 'cancelled')),
    CONSTRAINT "uq_build_run" UNIQUE ("run_id")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."stage_execution" (
    "stage_exec_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "stage_id" VARCHAR(100) NOT NULL,
    "stage_name" VARCHAR(200) NOT NULL,
    "stage_type" VARCHAR(50) NOT NULL,
    "started_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    "completed_at" TIMESTAMPTZ DEFAULT NULL,
    "status" VARCHAR(30) NOT NULL DEFAULT 'running',
    "input_artifact_ref" UUID DEFAULT NULL,
    "output_artifact_ref" UUID DEFAULT NULL,
    "tokens_used" INTEGER NOT NULL DEFAULT 0,
    "cost_usd" NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    "error_details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT "stage_execution_pkey" PRIMARY KEY ("stage_exec_id"),
    CONSTRAINT "chk_stage_execution_stage_type" CHECK (stage_type IN ('ingestion', 'validation', 'governance', 'transformation', 'quality', 'publishing', 'custom')),
    CONSTRAINT "chk_stage_execution_status" CHECK (status IN ('running', 'completed', 'failed', 'skipped'))
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."data_quality_summary" (
    "quality_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "stage_id" VARCHAR(100) NOT NULL,
    "contract_ref" UUID DEFAULT NULL,
    "assertions_evaluated" INTEGER NOT NULL DEFAULT 0,
    "assertions_passed" INTEGER NOT NULL DEFAULT 0,
    "assertions_failed" INTEGER NOT NULL DEFAULT 0,
    "quality_score" NUMERIC(5, 4) DEFAULT NULL,
    "failure_details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "evaluated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "data_quality_summary_pkey" PRIMARY KEY ("quality_id"),
    CONSTRAINT "chk_data_quality_summary_quality_score" CHECK (quality_score IS NULL OR quality_score BETWEEN 0.0000 AND 1.0000),
    CONSTRAINT "chk_quality_counts" CHECK (assertions_evaluated >= 0 AND assertions_passed >= 0 AND assertions_failed >= 0)
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."config_snapshot" (
    "snapshot_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "config_type" VARCHAR(50) NOT NULL,
    "config_body" JSONB NOT NULL,
    "captured_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "config_snapshot_pkey" PRIMARY KEY ("snapshot_id"),
    CONSTRAINT "chk_config_snapshot_config_type" CHECK (config_type IN ('model_tiers', 'safety_valves', 'pipeline_settings', 'prompt_templates', 'full')),
    CONSTRAINT "uq_config_run_type" UNIQUE ("run_id", "config_type")
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."governance_audit" (
    "gov_audit_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "stage_id" VARCHAR(100) NOT NULL,
    "rule_id" VARCHAR(100) NOT NULL,
    "rule_name" VARCHAR(200) NOT NULL,
    "rule_type" VARCHAR(50) NOT NULL,
    "evaluation_result" VARCHAR(30) NOT NULL,
    "payload_sha256" VARCHAR(64) DEFAULT NULL,
    "details" JSONB NOT NULL DEFAULT '{}'::jsonb,
    "evaluated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "governance_audit_pkey" PRIMARY KEY ("gov_audit_id"),
    CONSTRAINT "chk_governance_audit_rule_type" CHECK (rule_type IN ('classification_tagging', 'access_control', 'retention', 'contract_policy', 'custom')),
    CONSTRAINT "chk_governance_audit_evaluation_result" CHECK (evaluation_result IN ('pass', 'fail', 'warning', 'skip'))
);
    """)
    op.execute("""
CREATE TABLE IF NOT EXISTS "ddlc_shared_metadata"."stage_gate_record" (
    "gate_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "run_id" UUID NOT NULL,
    "agent_id" VARCHAR(100) NOT NULL,
    "stage_id" VARCHAR(100) NOT NULL,
    "gate_type" VARCHAR(30) NOT NULL,
    "gate_status" VARCHAR(30) NOT NULL,
    "checked_by" VARCHAR(100) DEFAULT NULL,
    "payload_sha256" VARCHAR(64) DEFAULT NULL,
    "notes" TEXT DEFAULT NULL,
    "evaluated_at" TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT "stage_gate_record_pkey" PRIMARY KEY ("gate_id"),
    CONSTRAINT "chk_stage_gate_record_gate_type" CHECK (gate_type IN ('human_approval', 'automated_check', 'quality_gate')),
    CONSTRAINT "chk_stage_gate_record_gate_status" CHECK (gate_status IN ('passed', 'failed', 'overridden', 'pending'))
);
    """)
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" ADD CONSTRAINT "fk_agent_run_header_parent_run_id" FOREIGN KEY ("parent_run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" ADD CONSTRAINT "fk_execution_log_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" ADD CONSTRAINT "fk_exec_parent" FOREIGN KEY ("run_id", "parent_log_id") REFERENCES "agent_metadata"."execution_log" ("run_id", "log_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" ADD CONSTRAINT "fk_exec_retry" FOREIGN KEY ("run_id", "retry_of_log_id") REFERENCES "agent_metadata"."execution_log" ("run_id", "log_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" ADD CONSTRAINT "fk_artifact_registry_produced_run_id" FOREIGN KEY ("produced_run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" ADD CONSTRAINT "fk_artifact_step" FOREIGN KEY ("produced_run_id", "produced_log_id") REFERENCES "agent_metadata"."execution_log" ("run_id", "log_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_artifact_manifest_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_artifact_manifest_artifact_id" FOREIGN KEY ("artifact_id") REFERENCES "agent_metadata"."artifact_registry" ("artifact_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_manifest_step" FOREIGN KEY ("run_id", "associated_log_id") REFERENCES "agent_metadata"."execution_log" ("run_id", "log_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" ADD CONSTRAINT "fk_audit_log_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" ADD CONSTRAINT "fk_decision_log_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" ADD CONSTRAINT "fk_decision_step" FOREIGN KEY ("run_id", "log_id") REFERENCES "agent_metadata"."execution_log" ("run_id", "log_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_registry" ADD CONSTRAINT "fk_contract_registry_agent_id" FOREIGN KEY ("agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" ADD CONSTRAINT "fk_contract_lineage_producer_agent_id" FOREIGN KEY ("producer_agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" ADD CONSTRAINT "fk_contract_lineage_producer_contract_id" FOREIGN KEY ("producer_contract_id") REFERENCES "ddlc_shared_metadata"."contract_registry" ("contract_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" ADD CONSTRAINT "fk_contract_lineage_consumer_agent_id" FOREIGN KEY ("consumer_agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" ADD CONSTRAINT "fk_contract_lineage_consumer_contract_id" FOREIGN KEY ("consumer_contract_id") REFERENCES "ddlc_shared_metadata"."contract_registry" ("contract_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" ADD CONSTRAINT "fk_agent_dependency_upstream_agent_id" FOREIGN KEY ("upstream_agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" ADD CONSTRAINT "fk_agent_dependency_downstream_agent_id" FOREIGN KEY ("downstream_agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" ADD CONSTRAINT "fk_agent_dependency_contract_id" FOREIGN KEY ("contract_id") REFERENCES "ddlc_shared_metadata"."contract_registry" ("contract_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."component_usage" ADD CONSTRAINT "fk_component_usage_agent_id" FOREIGN KEY ("agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."component_usage" ADD CONSTRAINT "fk_component_usage_component_id" FOREIGN KEY ("component_id") REFERENCES "ddlc_shared_metadata"."component_registry" ("component_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."build_summary" ADD CONSTRAINT "fk_build_summary_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."build_summary" ADD CONSTRAINT "fk_build_summary_agent_id" FOREIGN KEY ("agent_id") REFERENCES "ddlc_shared_metadata"."agent_registry" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" ADD CONSTRAINT "fk_stage_execution_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" ADD CONSTRAINT "fk_stage_execution_input_artifact_ref" FOREIGN KEY ("input_artifact_ref") REFERENCES "agent_metadata"."artifact_registry" ("artifact_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" ADD CONSTRAINT "fk_stage_execution_output_artifact_ref" FOREIGN KEY ("output_artifact_ref") REFERENCES "agent_metadata"."artifact_registry" ("artifact_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."data_quality_summary" ADD CONSTRAINT "fk_data_quality_summary_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."data_quality_summary" ADD CONSTRAINT "fk_data_quality_summary_contract_ref" FOREIGN KEY ("contract_ref") REFERENCES "ddlc_shared_metadata"."contract_registry" ("contract_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."config_snapshot" ADD CONSTRAINT "fk_config_snapshot_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."governance_audit" ADD CONSTRAINT "fk_governance_audit_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_gate_record" ADD CONSTRAINT "fk_stage_gate_record_run_id" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_run_agent_status" ON "agent_metadata"."agent_run_header" ("agent_id", "status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_run_started_at" ON "agent_metadata"."agent_run_header" ("started_at" DESC);')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_run_parent" ON "agent_metadata"."agent_run_header" ("parent_run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_run_revision" ON "agent_metadata"."agent_run_header" ("source_revision");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_exec_run_seq" ON "agent_metadata"."execution_log" ("run_id", "event_sequence");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_exec_time_range" ON "agent_metadata"."execution_log" ("started_at", "completed_at");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_exec_parent" ON "agent_metadata"."execution_log" ("run_id", "parent_log_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_exec_otel_trace" ON "agent_metadata"."execution_log" ("trace_id", "span_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_exec_failed" ON "agent_metadata"."execution_log" ("run_id", "error_type") WHERE status = \'failed\';')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_artifact_checksum" ON "agent_metadata"."artifact_registry" ("checksum_sha256", "byte_size");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_artifact_kind_name" ON "agent_metadata"."artifact_registry" ("artifact_kind", "artifact_name");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_artifact_producer" ON "agent_metadata"."artifact_registry" ("produced_run_id", "produced_log_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_manifest_artifact_role" ON "agent_metadata"."artifact_manifest" ("artifact_id", "role");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_manifest_run_role" ON "agent_metadata"."artifact_manifest" ("run_id", "role");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_audit_occurred_at" ON "agent_metadata"."audit_log" ("occurred_at" DESC);')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_audit_actor" ON "agent_metadata"."audit_log" ("actor_id", "occurred_at" DESC);')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_audit_target" ON "agent_metadata"."audit_log" ("target_entity_type", "target_entity_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_audit_correlation" ON "agent_metadata"."audit_log" ("correlation_id");')
    op.execute("""
CREATE OR REPLACE FUNCTION "agent_metadata"."enforce_audit_log_immutability"()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'UPDATE and DELETE operations are prohibited on table agent_metadata.audit_log (Compliance Rule SEC-01)';
END;
$$ LANGUAGE plpgsql;
    """)
    op.execute("""
CREATE OR REPLACE TRIGGER "trg_audit_log_immutable"
BEFORE UPDATE OR DELETE ON "agent_metadata"."audit_log"
FOR EACH ROW EXECUTE FUNCTION "agent_metadata"."enforce_audit_log_immutability"();
    """)
    op.execute('CREATE INDEX IF NOT EXISTS "idx_decision_run_time" ON "agent_metadata"."decision_log" ("run_id", "occurred_at");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_decision_status" ON "agent_metadata"."decision_log" ("decision_status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_decision_step" ON "agent_metadata"."decision_log" ("run_id", "log_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_agent_status" ON "ddlc_shared_metadata"."agent_registry" ("status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_agent_owner" ON "ddlc_shared_metadata"."agent_registry" ("owner");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_component_type" ON "ddlc_shared_metadata"."component_registry" ("component_type");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_component_status" ON "ddlc_shared_metadata"."component_registry" ("status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_component_embedding_hnsw" ON "ddlc_shared_metadata"."component_registry" USING hnsw ("embedding" vector_cosine_ops) WHERE embedding IS NOT NULL;')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_contract_agent" ON "ddlc_shared_metadata"."contract_registry" ("agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_contract_direction" ON "ddlc_shared_metadata"."contract_registry" ("direction");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_contract_status" ON "ddlc_shared_metadata"."contract_registry" ("status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_lineage_producer" ON "ddlc_shared_metadata"."contract_lineage" ("producer_agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_lineage_consumer" ON "ddlc_shared_metadata"."contract_lineage" ("consumer_agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_dep_upstream" ON "ddlc_shared_metadata"."agent_dependency" ("upstream_agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_dep_downstream" ON "ddlc_shared_metadata"."agent_dependency" ("downstream_agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_usage_agent" ON "ddlc_shared_metadata"."component_usage" ("agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_usage_component" ON "ddlc_shared_metadata"."component_usage" ("component_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_build_agent" ON "ddlc_shared_metadata"."build_summary" ("agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_build_outcome" ON "ddlc_shared_metadata"."build_summary" ("outcome");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_stage_run" ON "ddlc_shared_metadata"."stage_execution" ("run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_stage_agent" ON "ddlc_shared_metadata"."stage_execution" ("agent_id", "stage_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_stage_status" ON "ddlc_shared_metadata"."stage_execution" ("status");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_quality_run" ON "ddlc_shared_metadata"."data_quality_summary" ("run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_quality_agent_stage" ON "ddlc_shared_metadata"."data_quality_summary" ("agent_id", "stage_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_config_run" ON "ddlc_shared_metadata"."config_snapshot" ("run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_config_agent" ON "ddlc_shared_metadata"."config_snapshot" ("agent_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gov_run" ON "ddlc_shared_metadata"."governance_audit" ("run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gov_agent_stage" ON "ddlc_shared_metadata"."governance_audit" ("agent_id", "stage_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gov_result" ON "ddlc_shared_metadata"."governance_audit" ("evaluation_result");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gate_run" ON "ddlc_shared_metadata"."stage_gate_record" ("run_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gate_agent_stage" ON "ddlc_shared_metadata"."stage_gate_record" ("agent_id", "stage_id");')
    op.execute('CREATE INDEX IF NOT EXISTS "idx_gate_status" ON "ddlc_shared_metadata"."stage_gate_record" ("gate_status");')


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gate_status";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gate_agent_stage";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gate_run";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gov_result";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gov_agent_stage";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_gov_run";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_config_agent";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_config_run";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_quality_agent_stage";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_quality_run";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_stage_status";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_stage_agent";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_stage_run";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_build_outcome";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_build_agent";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_usage_component";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_usage_agent";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_dep_downstream";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_dep_upstream";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_lineage_consumer";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_lineage_producer";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_contract_status";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_contract_direction";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_contract_agent";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_component_embedding_hnsw";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_component_status";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_component_type";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_agent_owner";')
    op.execute('DROP INDEX IF EXISTS "ddlc_shared_metadata"."idx_agent_status";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_decision_step";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_decision_status";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_decision_run_time";')
    op.execute('DROP TRIGGER IF EXISTS "trg_audit_log_immutable" ON "agent_metadata"."audit_log";')
    op.execute('DROP FUNCTION IF EXISTS "agent_metadata"."enforce_audit_log_immutability"();')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_audit_correlation";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_audit_target";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_audit_actor";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_audit_occurred_at";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_manifest_run_role";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_manifest_artifact_role";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_artifact_producer";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_artifact_kind_name";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_artifact_checksum";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_exec_failed";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_exec_otel_trace";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_exec_parent";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_exec_time_range";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_exec_run_seq";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_run_revision";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_run_parent";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_run_started_at";')
    op.execute('DROP INDEX IF EXISTS "agent_metadata"."idx_run_agent_status";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_gate_record" DROP CONSTRAINT IF EXISTS "fk_stage_gate_record_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."governance_audit" DROP CONSTRAINT IF EXISTS "fk_governance_audit_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."config_snapshot" DROP CONSTRAINT IF EXISTS "fk_config_snapshot_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."data_quality_summary" DROP CONSTRAINT IF EXISTS "fk_data_quality_summary_contract_ref";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."data_quality_summary" DROP CONSTRAINT IF EXISTS "fk_data_quality_summary_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" DROP CONSTRAINT IF EXISTS "fk_stage_execution_output_artifact_ref";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" DROP CONSTRAINT IF EXISTS "fk_stage_execution_input_artifact_ref";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" DROP CONSTRAINT IF EXISTS "fk_stage_execution_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."build_summary" DROP CONSTRAINT IF EXISTS "fk_build_summary_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."build_summary" DROP CONSTRAINT IF EXISTS "fk_build_summary_run_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."component_usage" DROP CONSTRAINT IF EXISTS "fk_component_usage_component_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."component_usage" DROP CONSTRAINT IF EXISTS "fk_component_usage_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" DROP CONSTRAINT IF EXISTS "fk_agent_dependency_contract_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" DROP CONSTRAINT IF EXISTS "fk_agent_dependency_downstream_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."agent_dependency" DROP CONSTRAINT IF EXISTS "fk_agent_dependency_upstream_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" DROP CONSTRAINT IF EXISTS "fk_contract_lineage_consumer_contract_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" DROP CONSTRAINT IF EXISTS "fk_contract_lineage_consumer_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" DROP CONSTRAINT IF EXISTS "fk_contract_lineage_producer_contract_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_lineage" DROP CONSTRAINT IF EXISTS "fk_contract_lineage_producer_agent_id";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."contract_registry" DROP CONSTRAINT IF EXISTS "fk_contract_registry_agent_id";')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" DROP CONSTRAINT IF EXISTS "fk_decision_step";')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" DROP CONSTRAINT IF EXISTS "fk_decision_log_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" DROP CONSTRAINT IF EXISTS "fk_audit_log_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_manifest_step";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_artifact_manifest_artifact_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_artifact_manifest_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" DROP CONSTRAINT IF EXISTS "fk_artifact_step";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" DROP CONSTRAINT IF EXISTS "fk_artifact_registry_produced_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" DROP CONSTRAINT IF EXISTS "fk_exec_retry";')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" DROP CONSTRAINT IF EXISTS "fk_exec_parent";')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" DROP CONSTRAINT IF EXISTS "fk_execution_log_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" DROP CONSTRAINT IF EXISTS "fk_agent_run_header_parent_run_id";')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."stage_gate_record" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."governance_audit" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."config_snapshot" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."data_quality_summary" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."stage_execution" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."build_summary" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."component_usage" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."agent_dependency" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."contract_lineage" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."contract_registry" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."component_registry" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "ddlc_shared_metadata"."agent_registry" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."decision_log" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."audit_log" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."artifact_manifest" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."artifact_registry" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."execution_log" CASCADE;')
    op.execute('DROP TABLE IF EXISTS "agent_metadata"."agent_run_header" CASCADE;')
