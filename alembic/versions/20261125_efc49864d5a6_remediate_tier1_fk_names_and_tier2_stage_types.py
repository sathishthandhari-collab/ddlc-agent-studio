"""remediate_tier1_fk_names_and_tier2_stage_types

Revision ID: efc49864d5a6
Revises: 8a1346032e3a
Create Date: 2026-11-25 18:43:47.168854+00:00

"""
from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'efc49864d5a6'
down_revision: Union[str, None] = '8a1346032e3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" DROP CONSTRAINT IF EXISTS "chk_stage_execution_stage_type";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" ADD CONSTRAINT "chk_stage_execution_stage_type" CHECK (stage_type IN (\'discovery\', \'reasoning\', \'code_generation\', \'sandbox_testing\', \'human_review\', \'delivery\', \'custom\'));')
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" ADD CONSTRAINT "fk_run_parent" FOREIGN KEY ("parent_run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE SET NULL ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" ADD CONSTRAINT "fk_exec_run" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" ADD CONSTRAINT "fk_artifact_run" FOREIGN KEY ("produced_run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_manifest_run" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_manifest_artifact" FOREIGN KEY ("artifact_id") REFERENCES "agent_metadata"."artifact_registry" ("artifact_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" ADD CONSTRAINT "fk_audit_run" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE RESTRICT ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" ADD CONSTRAINT "fk_decision_run" FOREIGN KEY ("run_id") REFERENCES "agent_metadata"."agent_run_header" ("run_id") ON DELETE CASCADE ON UPDATE NO ACTION;')
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" DROP CONSTRAINT IF EXISTS "fk_agent_run_header_parent_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" DROP CONSTRAINT IF EXISTS "fk_execution_log_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" DROP CONSTRAINT IF EXISTS "fk_artifact_registry_produced_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_artifact_manifest_artifact_id";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_artifact_manifest_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" DROP CONSTRAINT IF EXISTS "fk_audit_log_run_id";')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" DROP CONSTRAINT IF EXISTS "fk_decision_log_run_id";')


def downgrade() -> None:
    op.execute('ALTER TABLE "agent_metadata"."decision_log" ADD CONSTRAINT "fk_decision_log_run_id" FOREIGN KEY (run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE CASCADE;')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" ADD CONSTRAINT "fk_audit_log_run_id" FOREIGN KEY (run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE RESTRICT;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_artifact_manifest_run_id" FOREIGN KEY (run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE CASCADE;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" ADD CONSTRAINT "fk_artifact_manifest_artifact_id" FOREIGN KEY (artifact_id) REFERENCES agent_metadata.artifact_registry(artifact_id) ON DELETE RESTRICT;')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" ADD CONSTRAINT "fk_artifact_registry_produced_run_id" FOREIGN KEY (produced_run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE RESTRICT;')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" ADD CONSTRAINT "fk_execution_log_run_id" FOREIGN KEY (run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE CASCADE;')
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" ADD CONSTRAINT "fk_agent_run_header_parent_run_id" FOREIGN KEY (parent_run_id) REFERENCES agent_metadata.agent_run_header(run_id) ON DELETE SET NULL;')
    op.execute('ALTER TABLE "agent_metadata"."decision_log" DROP CONSTRAINT IF EXISTS "fk_decision_run";')
    op.execute('ALTER TABLE "agent_metadata"."audit_log" DROP CONSTRAINT IF EXISTS "fk_audit_run";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_manifest_artifact";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_manifest" DROP CONSTRAINT IF EXISTS "fk_manifest_run";')
    op.execute('ALTER TABLE "agent_metadata"."artifact_registry" DROP CONSTRAINT IF EXISTS "fk_artifact_run";')
    op.execute('ALTER TABLE "agent_metadata"."execution_log" DROP CONSTRAINT IF EXISTS "fk_exec_run";')
    op.execute('ALTER TABLE "agent_metadata"."agent_run_header" DROP CONSTRAINT IF EXISTS "fk_run_parent";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" DROP CONSTRAINT IF EXISTS "chk_stage_execution_stage_type";')
    op.execute('ALTER TABLE "ddlc_shared_metadata"."stage_execution" ADD CONSTRAINT "chk_stage_execution_stage_type" CHECK (stage_type::text = ANY (ARRAY[\'ingestion\'::character varying, \'validation\'::character varying, \'governance\'::character varying, \'transformation\'::character varying, \'quality\'::character varying, \'publishing\'::character varying, \'custom\'::character varying]::text[]));')
