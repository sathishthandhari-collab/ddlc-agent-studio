"""Validator specialist — Phase 7 deterministic verification & test execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from pydantic import BaseModel, Field

from src.specialists.llm import StructuredLLMClient
from src.specialists.validator.checks import (
    check_contract_coverage,
    check_hardcoding_and_security,
    check_hooks_and_shared_components,
    check_pointer_and_skeleton,
    check_prompt_templates,
    run_generated_tests,
)
from src.state.blackboard import (
    BuildLogEntry,
    ValidationIssue,
    ValidationReport,
)
from src.state.projections import ValidatorProjection


class ValidationDiagnosticSummary(BaseModel):
    """Optional fast-tier LLM diagnostic summary for human escalation readability."""

    concise_summary: str = Field(..., min_length=1)
    prioritized_fixes: list[str] = Field(default_factory=list)


def _resolve_code_dir(storage_uri: str) -> Path:
    raw = storage_uri.strip()
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        return Path(url2pathname(unquote(parsed.path))).resolve()
    return Path(raw).resolve()


class ValidatorSpecialist:
    """Deterministic Phase 7 verifier checking Skeleton, Muscle, contracts, security, and pytest."""

    def __init__(
        self,
        llm_client: Optional[StructuredLLMClient] = None,
        *,
        run_tests: bool = True,
        summarize_failures_with_llm: bool = False,
        test_timeout_seconds: int = 60,
    ) -> None:
        self.llm_client = llm_client
        self.run_tests = run_tests
        self.summarize_failures_with_llm = summarize_failures_with_llm
        self.test_timeout_seconds = test_timeout_seconds

    def validate(self, projection: ValidatorProjection) -> dict[str, Any]:
        """Execute deterministic Phase 7 validation checks and emit ValidationReport."""
        code_dir = _resolve_code_dir(projection.generated_code_pointer.storage_uri)
        impl_docs = projection.approved_impl_docs
        runtime = str((impl_docs.agent_yaml.get("architecture") or {}).get("runtime") or "langgraph")

        checks_run: list[str] = [
            "claim_check_and_skeleton",
            "jinja2_prompt_ast",
            "contract_coverage",
            "hooks_and_shared_components",
            "hardcoding_and_security",
        ]
        issues: list[ValidationIssue] = []

        issues.extend(
            check_pointer_and_skeleton(
                code_dir,
                runtime=runtime,
                code_pointer=projection.generated_code_pointer,
            )
        )
        issues.extend(check_prompt_templates(code_dir, impl_docs))
        issues.extend(check_contract_coverage(code_dir, impl_docs))
        issues.extend(check_hooks_and_shared_components(code_dir, impl_docs))
        issues.extend(check_hardcoding_and_security(code_dir))

        test_summary: dict[str, Any] = {"executed": False}
        if self.run_tests:
            checks_run.append("pytest_suite")
            test_issues, test_summary = run_generated_tests(
                code_dir,
                impl_docs,
                timeout_seconds=self.test_timeout_seconds,
            )
            issues.extend(test_issues)

        passed = not any(iss.severity in {"error", "critical"} for iss in issues)
        attempt_number = projection.build_fix_attempt_count + 1

        report = ValidationReport(
            attempt_number=attempt_number,
            passed=passed,
            checks_run=checks_run,
            issues=issues,
            test_summary=test_summary,
        )

        fix_instructions: list[str] = []
        seen_instr: set[str] = set()
        for iss in issues:
            label = iss.target_stage_id or iss.file_path or iss.check_name
            msg = f"[{iss.route_target}] {label}: {iss.fix_instruction or iss.message}"
            if msg not in seen_instr:
                seen_instr.add(msg)
                fix_instructions.append(msg)

        if not passed and self.summarize_failures_with_llm and self.llm_client is not None:
            try:
                diag = self.llm_client.complete_structured(
                    specialist_name="validator",
                    response_model=ValidationDiagnosticSummary,
                    messages=[
                        {
                            "role": "system",
                            "content": "Summarize these deterministic build validation errors into concise fix instructions.",
                        },
                        {
                            "role": "user",
                            "content": "\n".join(fix_instructions),
                        },
                    ],
                )
                if diag.prioritized_fixes:
                    fix_instructions = diag.prioritized_fixes
            except Exception:
                # Deterministic diagnostics are sufficient; never fail validation on summarizer
                pass

        return {
            "current_phase": "validate",
            "validation_reports": [report],
            "validation_passed": passed,
            "active_fix_instructions": [] if passed else fix_instructions,
            "build_logs": [
                BuildLogEntry(
                    phase="validate",
                    actor="validator",
                    action="validate_build",
                    status="completed" if passed else "failed",
                    details={
                        "attempt_number": attempt_number,
                        "passed": passed,
                        "checks_run": checks_run,
                        "issue_count": len(issues),
                        "test_returncode": test_summary.get("returncode"),
                    },
                )
            ],
        }
