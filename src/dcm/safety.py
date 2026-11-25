"""DCM Safety Gate — destructive operation detection and approval enforcement.

Detects potentially destructive DDL operations (DROP, ALTER TYPE, RENAME) and
gates their execution behind explicit user approval. Non-destructive operations
(CREATE, ADD COLUMN) are auto-approved.

This module scans raw DDL statements and classifies them by risk level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class RiskLevel(str, Enum):
    """Risk classification for DDL operations."""

    SAFE = "safe"
    DESTRUCTIVE = "destructive"


@dataclass
class DDLClassification:
    """Classification result for a single DDL statement."""

    statement: str
    risk_level: RiskLevel
    reason: str


@dataclass
class SafetyReport:
    """Aggregated safety analysis of a DDL migration plan."""

    classifications: list[DDLClassification] = field(default_factory=list)

    @property
    def has_destructive(self) -> bool:
        return any(c.risk_level == RiskLevel.DESTRUCTIVE for c in self.classifications)

    @property
    def safe_statements(self) -> list[DDLClassification]:
        return [c for c in self.classifications if c.risk_level == RiskLevel.SAFE]

    @property
    def destructive_statements(self) -> list[DDLClassification]:
        return [c for c in self.classifications if c.risk_level == RiskLevel.DESTRUCTIVE]

    def summary(self) -> str:
        """Human-readable summary of the safety analysis."""
        lines = [
            f"Total statements: {len(self.classifications)}",
            f"  Safe: {len(self.safe_statements)}",
            f"  Destructive: {len(self.destructive_statements)}",
        ]
        if self.has_destructive:
            lines.append("\n⚠  Destructive operations detected:")
            for d in self.destructive_statements:
                lines.append(f"  • {d.reason}")
                # Show first 120 chars of the statement
                preview = d.statement[:120].replace("\n", " ")
                lines.append(f"    SQL: {preview}...")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Destructive pattern matching
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
        "DROP TABLE — potential irreversible data loss",
    ),
    (
        re.compile(r"\bDROP\s+SCHEMA\b", re.IGNORECASE),
        "DROP SCHEMA — potential irreversible data loss across all tables",
    ),
    (
        re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
        "DROP COLUMN — column data will be permanently deleted",
    ),
    (
        re.compile(r"\bALTER\s+COLUMN\b.*?\bTYPE\b", re.IGNORECASE | re.DOTALL),
        "ALTER COLUMN TYPE — potential data truncation or conversion failure",
    ),
    (
        re.compile(r"\bRENAME\s+TABLE\b", re.IGNORECASE),
        "RENAME TABLE — may break dependent queries and foreign keys",
    ),
    (
        re.compile(r"\bRENAME\s+COLUMN\b", re.IGNORECASE),
        "RENAME COLUMN — may break dependent queries",
    ),
    (
        re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE),
        "DROP INDEX — may impact query performance",
    ),
    (
        re.compile(r"\bDROP\s+CONSTRAINT\b", re.IGNORECASE),
        "DROP CONSTRAINT — may weaken data integrity",
    ),
    (
        re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
        "TRUNCATE — all table data will be permanently deleted",
    ),
]

_SAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*CREATE\s+(SCHEMA|TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS\b", re.IGNORECASE),
    re.compile(r"^\s*CREATE\s+OR\s+REPLACE\s+FUNCTION\b", re.IGNORECASE),
    re.compile(r"^\s*CREATE\s+TRIGGER\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+TABLE\s+.*\bADD\s+CONSTRAINT\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+TABLE\s+.*\bADD\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"^\s*COMMENT\s+ON\b", re.IGNORECASE),
]


def classify_statement(statement: str) -> DDLClassification:
    """Classify a single DDL statement by risk level.

    Args:
        statement: A single SQL DDL statement.

    Returns:
        DDLClassification with risk level and reason.
    """
    normalized = statement.strip()

    # Check destructive patterns first (they take priority)
    for pattern, reason in _DESTRUCTIVE_PATTERNS:
        if pattern.search(normalized):
            return DDLClassification(
                statement=normalized,
                risk_level=RiskLevel.DESTRUCTIVE,
                reason=reason,
            )

    # Check known-safe patterns
    for pattern in _SAFE_PATTERNS:
        if pattern.search(normalized):
            return DDLClassification(
                statement=normalized,
                risk_level=RiskLevel.SAFE,
                reason="Non-destructive DDL operation",
            )

    # Default: treat unknown patterns as safe (additive by default)
    return DDLClassification(
        statement=normalized,
        risk_level=RiskLevel.SAFE,
        reason="Unclassified DDL — treated as non-destructive",
    )


def analyze_migration(statements: list[str]) -> SafetyReport:
    """Analyze a list of DDL statements and produce a safety report.

    Args:
        statements: List of SQL DDL statements.

    Returns:
        SafetyReport with classifications for each statement.
    """
    report = SafetyReport()
    for stmt in statements:
        if stmt.strip():
            report.classifications.append(classify_statement(stmt))
    return report


def gate_check(
    report: SafetyReport,
    approve_destructive: bool = False,
) -> bool:
    """Check if a migration plan passes the safety gate.

    Args:
        report: Safety analysis report.
        approve_destructive: If True, destructive ops are pre-approved.

    Returns:
        True if the migration may proceed, False if blocked.
    """
    if not report.has_destructive:
        return True
    return approve_destructive
