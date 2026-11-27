"""DCM Safety Gate — destructive operation detection and approval enforcement.

Detects potentially destructive DDL operations (DROP, ALTER TYPE, RENAME, TRUNCATE)
and gates their execution behind explicit user approval. Strips SQL comments,
single-quoted string literals, and dollar-quoted function bodies prior to scanning
to avoid false positives (e.g., error messages inside triggers).
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
            lines.append("\n[WARNING] Destructive operations detected:")
            for d in self.destructive_statements:
                lines.append(f"  - {d.reason}")
                preview = d.statement[:120].replace("\n", " ")
                lines.append(f"    SQL: {preview}...")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Literal / comment stripping for accurate DDL scanning
# ---------------------------------------------------------------------------

_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def strip_sql_literals_and_comments(sql: str) -> str:
    """Replace comments, single-quoted strings, and dollar-quoted bodies with blanks.

    Double-quoted identifiers remain intact so table/column DDL keywords inside
    identifiers don't mask statement keywords while string/function payloads cannot
    cause false positives.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # -- single line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            newline = sql.find("\n", i + 2)
            if newline == -1:
                break
            out.append("\n")
            i = newline + 1
            continue
        # /* block comment */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        # 'single-quoted string literal'
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        # $tag$ ... $tag$ dollar-quoted body
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end != -1:
                    out.append(" '' ")
                    i = end + len(tag)
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Destructive & Safe pattern matching
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
        re.compile(r"\bALTER\s+TABLE\b.*?\bRENAME\s+TO\b", re.IGNORECASE | re.DOTALL),
        "ALTER TABLE ... RENAME TO — may break dependent queries and contracts",
    ),
    (
        re.compile(r"\bALTER\s+TABLE\b.*?\bRENAME\s+COLUMN\b", re.IGNORECASE | re.DOTALL),
        "ALTER TABLE ... RENAME COLUMN — may break dependent queries",
    ),
    (
        re.compile(r"\bALTER\s+TABLE\b.*?\bRENAME\s+CONSTRAINT\b", re.IGNORECASE | re.DOTALL),
        "ALTER TABLE ... RENAME CONSTRAINT — renames constraint referenced by migrations/apps",
    ),
    (
        re.compile(r"\bALTER\s+SCHEMA\b.*?\bRENAME\s+TO\b", re.IGNORECASE | re.DOTALL),
        "ALTER SCHEMA ... RENAME TO — breaks cross-schema references",
    ),
    (
        re.compile(r"\bALTER\s+INDEX\b.*?\bRENAME\s+TO\b", re.IGNORECASE | re.DOTALL),
        "ALTER INDEX ... RENAME TO — schema/index drift",
    ),
    (
        re.compile(r"\bRENAME\s+(?:TABLE|COLUMN)\b", re.IGNORECASE),
        "RENAME — may break dependent queries",
    ),
    (
        re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE),
        "DROP INDEX — may impact query performance",
    ),
    (
        re.compile(r"\bDROP\s+CONSTRAINT\b", re.IGNORECASE),
        "DROP CONSTRAINT — may weaken or alter data integrity",
    ),
    (
        re.compile(r"\bDROP\s+(?:TRIGGER|FUNCTION)\b", re.IGNORECASE),
        "DROP TRIGGER/FUNCTION — removes active database enforcement",
    ),
    (
        re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
        "TRUNCATE — all table data will be permanently deleted",
    ),
]

_SAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^\s*CREATE\s+(SCHEMA|TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*CREATE\s+OR\s+REPLACE\s+FUNCTION\b", re.IGNORECASE),
    re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+TABLE\s+.*\bADD\s+CONSTRAINT\b", re.IGNORECASE),
    re.compile(r"^\s*ALTER\s+TABLE\s+.*\bADD\s+COLUMN\b", re.IGNORECASE),
    re.compile(
        r"^\s*ALTER\s+TABLE\s+.*\bALTER\s+COLUMN\s+.*?\b(?:SET\s+DEFAULT|DROP\s+DEFAULT|SET\s+NOT\s+NULL|DROP\s+NOT\s+NULL)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"^\s*COMMENT\s+ON\b", re.IGNORECASE),
]


def classify_statement(statement: str) -> DDLClassification:
    """Classify a single DDL statement by risk level."""
    normalized = statement.strip()
    scannable = strip_sql_literals_and_comments(normalized)

    for pattern, reason in _DESTRUCTIVE_PATTERNS:
        if pattern.search(scannable):
            return DDLClassification(
                statement=normalized,
                risk_level=RiskLevel.DESTRUCTIVE,
                reason=reason,
            )

    for pattern in _SAFE_PATTERNS:
        if pattern.search(scannable):
            return DDLClassification(
                statement=normalized,
                risk_level=RiskLevel.SAFE,
                reason="Non-destructive DDL operation",
            )

    return DDLClassification(
        statement=normalized,
        risk_level=RiskLevel.SAFE,
        reason="Unclassified DDL — treated as non-destructive",
    )


def analyze_migration(statements: list[str]) -> SafetyReport:
    """Analyze a list of DDL statements and produce a safety report."""
    report = SafetyReport()
    for stmt in statements:
        if stmt.strip():
            report.classifications.append(classify_statement(stmt))
    return report


def gate_check(
    report: SafetyReport,
    approve_destructive: bool = False,
) -> bool:
    """Check if a migration plan passes the safety gate."""
    if not report.has_destructive:
        return True
    return approve_destructive
