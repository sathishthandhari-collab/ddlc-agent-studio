"""Tests for DCM safety gate — destructive operation detection."""

from src.dcm.safety import (
    DDLClassification,
    RiskLevel,
    SafetyReport,
    analyze_migration,
    classify_statement,
    gate_check,
)


class TestClassifyStatement:
    """Test individual DDL statement classification."""

    def test_create_schema_is_safe(self):
        result = classify_statement('CREATE SCHEMA IF NOT EXISTS "agent_metadata";')
        assert result.risk_level == RiskLevel.SAFE

    def test_create_table_is_safe(self):
        result = classify_statement(
            'CREATE TABLE IF NOT EXISTS "agent_metadata"."test" (id UUID);'
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_create_index_is_safe(self):
        result = classify_statement(
            'CREATE INDEX IF NOT EXISTS "idx_test" ON "s"."t" ("col");'
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_create_unique_index_is_safe(self):
        result = classify_statement(
            'CREATE UNIQUE INDEX IF NOT EXISTS "idx_test" ON "s"."t" ("col");'
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_add_constraint_is_safe(self):
        result = classify_statement(
            'ALTER TABLE "s"."t" ADD CONSTRAINT "fk_test" FOREIGN KEY ("col") '
            'REFERENCES "s"."t2" ("id");'
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_create_function_is_safe(self):
        result = classify_statement(
            "CREATE OR REPLACE FUNCTION myschema.my_func() RETURNS TRIGGER AS $$ "
            "BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql;"
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_create_trigger_is_safe(self):
        result = classify_statement(
            'CREATE OR REPLACE TRIGGER "trg_test" BEFORE UPDATE ON "s"."t" '
            "FOR EACH ROW EXECUTE FUNCTION myschema.my_func();"
        )
        assert result.risk_level == RiskLevel.SAFE

    def test_drop_table_is_destructive(self):
        result = classify_statement('DROP TABLE "agent_metadata"."test";')
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_drop_schema_is_destructive(self):
        result = classify_statement('DROP SCHEMA "agent_metadata" CASCADE;')
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_drop_column_is_destructive(self):
        result = classify_statement('ALTER TABLE "s"."t" DROP COLUMN "old_col";')
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_alter_column_type_is_destructive(self):
        result = classify_statement(
            'ALTER TABLE "s"."t" ALTER COLUMN "col" TYPE VARCHAR(200);'
        )
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_truncate_is_destructive(self):
        result = classify_statement('TRUNCATE "agent_metadata"."test";')
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_drop_index_is_destructive(self):
        result = classify_statement('DROP INDEX "idx_test";')
        assert result.risk_level == RiskLevel.DESTRUCTIVE

    def test_drop_constraint_is_destructive(self):
        result = classify_statement('ALTER TABLE "s"."t" DROP CONSTRAINT "fk_old";')
        assert result.risk_level == RiskLevel.DESTRUCTIVE


class TestAnalyzeMigration:
    """Test migration plan analysis."""

    def test_all_safe(self):
        stmts = [
            'CREATE SCHEMA IF NOT EXISTS "test";',
            'CREATE TABLE IF NOT EXISTS "test"."t" (id UUID);',
            'CREATE INDEX IF NOT EXISTS "idx_t" ON "test"."t" ("id");',
        ]
        report = analyze_migration(stmts)
        assert not report.has_destructive
        assert len(report.safe_statements) == 3
        assert len(report.destructive_statements) == 0

    def test_mixed_safe_and_destructive(self):
        stmts = [
            'CREATE TABLE IF NOT EXISTS "test"."t" (id UUID);',
            'ALTER TABLE "test"."t" DROP COLUMN "old_col";',
            'CREATE INDEX IF NOT EXISTS "idx_t" ON "test"."t" ("id");',
        ]
        report = analyze_migration(stmts)
        assert report.has_destructive
        assert len(report.safe_statements) == 2
        assert len(report.destructive_statements) == 1

    def test_empty_statements(self):
        report = analyze_migration([])
        assert not report.has_destructive

    def test_summary_includes_warnings(self):
        stmts = ['DROP TABLE "test"."t";']
        report = analyze_migration(stmts)
        summary = report.summary()
        assert "Destructive" in summary
        assert "DROP TABLE" in summary


class TestGateCheck:
    """Test safety gate enforcement."""

    def test_safe_plan_passes(self):
        report = SafetyReport(
            classifications=[
                DDLClassification(
                    statement="CREATE TABLE ...",
                    risk_level=RiskLevel.SAFE,
                    reason="ok",
                )
            ]
        )
        assert gate_check(report) is True

    def test_destructive_blocked_without_flag(self):
        report = SafetyReport(
            classifications=[
                DDLClassification(
                    statement="DROP TABLE ...",
                    risk_level=RiskLevel.DESTRUCTIVE,
                    reason="data loss",
                )
            ]
        )
        assert gate_check(report) is False

    def test_destructive_approved_with_flag(self):
        report = SafetyReport(
            classifications=[
                DDLClassification(
                    statement="DROP TABLE ...",
                    risk_level=RiskLevel.DESTRUCTIVE,
                    reason="data loss",
                )
            ]
        )
        assert gate_check(report, approve_destructive=True) is True
