"""Deterministic build verification checks for Phase 7 Validator (builder.md §8)."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src.core.settings import DEFAULT_PROJECT_ROOT
from src.specialists.builder.compiler.ast_validator import has_skeleton_header
from src.specialists.builder.compiler.generators.contract_gen import to_pascal_case
from src.specialists.builder.compiler.generators.graph_gen import parse_tool_handler
from src.specialists.builder.leaf_coder.signature_verifier import (
    BANNED_IMPORTS,
    verify_prompt_template_ast,
    verify_tool_source,
)
from src.specialists.documentarian.validator import validate_impl_doc_package
from src.state.blackboard import (
    ContractPointer,
    ImplDocPackage,
    ValidationIssue,
    verify_contract_pointer,
)

_HARDCODED_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9\-_]{16,}|Bearer\s+[A-Za-z0-9\-._~+/]+=*|ghp_[A-Za-z0-9]{20,})"
)
_HARDCODED_DB_PASS_RE = re.compile(
    r"postgres(?:ql)?://[^:\s/\"']+:[^@\s<\"']{3,}@[A-Za-z0-9_.\-]+",
    re.IGNORECASE,
)


def check_pointer_and_skeleton(
    code_dir: Path,
    *,
    runtime: str,
    code_pointer: ContractPointer,
) -> list[ValidationIssue]:
    """Verify Claim Check integrity, # DO NOT EDIT headers, and Python AST syntax."""
    issues: list[ValidationIssue] = []

    if not verify_contract_pointer(code_pointer, base_dir=code_dir):
        issues.append(
            ValidationIssue(
                check_name="claim_check_integrity",
                severity="critical",
                route_target="compiler",
                message="Generated codebase failed Claim Check SHA-256 verification.",
                fix_instruction="Re-run Builder to emit clean verified files.",
            )
        )

    expected_skeleton = [
        "src/models/contracts_pydantic.py",
        "src/core/config.py",
        "src/core/telemetry.py",
        "tests/contracts/test_contracts.py",
        "src/graph.py" if runtime == "langgraph" else "src/pipeline.py",
    ]
    for rel_path in expected_skeleton:
        file_path = code_dir / rel_path
        if not file_path.is_file():
            issues.append(
                ValidationIssue(
                    check_name="skeleton_integrity",
                    severity="error",
                    file_path=rel_path,
                    route_target="compiler",
                    message=f"Required Skeleton file missing: {rel_path}",
                    fix_instruction=f"Re-compile Skeleton to emit {rel_path}.",
                )
            )
            continue
        source = file_path.read_text(encoding="utf-8")
        if not has_skeleton_header(source):
            issues.append(
                ValidationIssue(
                    check_name="skeleton_integrity",
                    severity="error",
                    file_path=rel_path,
                    route_target="compiler",
                    message=f"Skeleton file '{rel_path}' missing immutable '# DO NOT EDIT' header.",
                    fix_instruction=f"Restore compiler banner on {rel_path}.",
                )
            )

    for py_file in sorted(code_dir.rglob("*.py")):
        rel = py_file.relative_to(code_dir).as_posix()
        try:
            ast.parse(py_file.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            target_route = "leaf_coder" if rel.startswith(("src/tools/", "tests/unit/")) else "compiler"
            issues.append(
                ValidationIssue(
                    check_name="ast_syntax",
                    severity="error",
                    file_path=rel,
                    route_target=target_route,
                    message=f"Python syntax error in '{rel}' line {exc.lineno}: {exc.msg}",
                    fix_instruction=f"Fix Python syntax error at line {exc.lineno} in {rel}: {exc.msg}",
                )
            )

    return issues


def check_prompt_templates(code_dir: Path, impl_docs: ImplDocPackage) -> list[ValidationIssue]:
    """Verify prompt template existence and Universal Prompt Contract AST compliance (Decision 28)."""
    issues: list[ValidationIssue] = []
    checked_paths: set[str] = set()

    for stage_id, stg in sorted(impl_docs.stages.items()):
        proc = stg.get("processing") or {}
        if proc.get("processing_type") != "llm_reasoning":
            continue
        llm_cfg = proc.get("llm_config") or {}
        prompt_ref = llm_cfg.get("prompt_template_ref")
        if not prompt_ref:
            continue
        rel_prompt = prompt_ref if str(prompt_ref).startswith("src/") else f"src/{prompt_ref}"
        checked_paths.add(rel_prompt)
        target = code_dir / rel_prompt
        if not target.is_file():
            issues.append(
                ValidationIssue(
                    check_name="jinja2_prompt_ast",
                    severity="error",
                    file_path=rel_prompt,
                    target_stage_id=stage_id,
                    route_target="leaf_coder",
                    message=f"Missing Jinja2 prompt template '{rel_prompt}' for stage '{stage_id}'.",
                    fix_instruction=f"Synthesize prompt template {rel_prompt} for stage '{stage_id}'.",
                )
            )
            continue

        ast_errs = verify_prompt_template_ast(rel_prompt, target.read_text(encoding="utf-8"))
        for err in ast_errs:
            issues.append(
                ValidationIssue(
                    check_name="jinja2_prompt_ast",
                    severity="error",
                    file_path=rel_prompt,
                    target_stage_id=stage_id,
                    route_target="leaf_coder",
                    message=err,
                    fix_instruction=err,
                )
            )

    prompts_dir = code_dir / "src" / "prompts"
    if prompts_dir.is_dir():
        for tmpl_file in sorted(prompts_dir.rglob("*.jinja2")):
            rel = tmpl_file.relative_to(code_dir).as_posix()
            if rel in checked_paths:
                continue
            for err in verify_prompt_template_ast(rel, tmpl_file.read_text(encoding="utf-8")):
                issues.append(
                    ValidationIssue(
                        check_name="jinja2_prompt_ast",
                        severity="error",
                        file_path=rel,
                        route_target="leaf_coder",
                        message=err,
                        fix_instruction=err,
                    )
                )

    return issues


def check_contract_coverage(code_dir: Path, impl_docs: ImplDocPackage) -> list[ValidationIssue]:
    """Verify every contract and field in impl_docs exists in compiled contracts_pydantic.py."""
    issues: list[ValidationIssue] = []

    doc_errors = validate_impl_doc_package(impl_docs)
    for derr in doc_errors:
        issues.append(
            ValidationIssue(
                check_name="impl_docs_integrity",
                severity="error",
                route_target="documentarian",
                message=derr,
                fix_instruction=derr,
            )
        )

    contracts_file = code_dir / "src" / "models" / "contracts_pydantic.py"
    if not contracts_file.is_file():
        return issues

    try:
        tree = ast.parse(contracts_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return issues

    class_fields: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fields: set[str] = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
            class_fields[node.name] = fields

    for contract_name, raw_contract in sorted(impl_docs.contracts.items()):
        body = (
            raw_contract.get("contract")
            if isinstance(raw_contract.get("contract"), dict)
            else raw_contract
        )
        expected_cls = to_pascal_case(str(body.get("name") or contract_name))
        if expected_cls not in class_fields:
            issues.append(
                ValidationIssue(
                    check_name="contract_coverage",
                    severity="error",
                    file_path="src/models/contracts_pydantic.py",
                    route_target="compiler",
                    message=f"Contract '{contract_name}' class '{expected_cls}' missing from contracts_pydantic.py.",
                    fix_instruction=f"Recompile contracts_pydantic.py to emit {expected_cls}.",
                )
            )
            continue

        actual_fields = class_fields[expected_cls]
        for fld in body.get("fields") or []:
            fname = str(fld.get("name") or "")
            if fname and fname not in actual_fields:
                issues.append(
                    ValidationIssue(
                        check_name="contract_coverage",
                        severity="error",
                        file_path="src/models/contracts_pydantic.py",
                        route_target="compiler",
                        message=f"Field '{fname}' of contract '{contract_name}' missing on {expected_cls}.",
                        fix_instruction=f"Recompile {expected_cls} to include field '{fname}'.",
                    )
                )

    return issues


def check_hooks_and_shared_components(
    code_dir: Path,
    impl_docs: ImplDocPackage,
) -> list[ValidationIssue]:
    """Verify custom_code hooks match declared signatures and shared components are not duplicated."""
    issues: list[ValidationIssue] = []
    deps = impl_docs.agent_yaml.get("dependencies") or {}
    declared_shared = {
        str(sc.get("component_id"))
        for sc in (deps.get("shared_components") or [])
        if isinstance(sc, dict) and sc.get("component_id")
    }

    for stage_id, stg in sorted(impl_docs.stages.items()):
        proc = stg.get("processing") or {}
        for tool in proc.get("tools") or []:
            ttype = tool.get("type")
            tname = str(tool.get("name") or "")
            if ttype == "custom_code" and tool.get("handler"):
                handler = str(tool["handler"]).strip()
                mod_rel = handler.split("::", 1)[0].strip()
                rel_tool_path = f"src/{mod_rel}"
                tool_path = code_dir / rel_tool_path
                _, func_name = parse_tool_handler(handler)

                if not tool_path.is_file():
                    issues.append(
                        ValidationIssue(
                            check_name="interface_hooks",
                            severity="error",
                            file_path=rel_tool_path,
                            target_stage_id=stage_id,
                            route_target="leaf_coder",
                            message=f"Custom tool handler '{rel_tool_path}::{func_name}' missing on disk.",
                            fix_instruction=f"Synthesize {rel_tool_path} defining {func_name}.",
                        )
                    )
                    continue

                sig_errors = verify_tool_source(
                    tool_path.read_text(encoding="utf-8"),
                    expected_func_name=func_name,
                    expected_signature=tool.get("signature"),
                    filename=rel_tool_path,
                )
                for err in sig_errors:
                    issues.append(
                        ValidationIssue(
                            check_name="interface_hooks",
                            severity="error",
                            file_path=rel_tool_path,
                            target_stage_id=stage_id,
                            route_target="leaf_coder",
                            message=err,
                            fix_instruction=err,
                        )
                    )

            elif ttype == "shared_component":
                if tname not in declared_shared:
                    issues.append(
                        ValidationIssue(
                            check_name="shared_component_usage",
                            severity="error",
                            target_stage_id=stage_id,
                            route_target="documentarian",
                            message=f"Stage '{stage_id}' uses shared_component '{tname}' not declared in agent.yaml.",
                            fix_instruction=f"Declare '{tname}' in agent.yaml dependencies.shared_components.",
                        )
                    )
                duplicate_candidate = code_dir / "src" / "tools" / f"{tname}.py"
                if duplicate_candidate.is_file():
                    issues.append(
                        ValidationIssue(
                            check_name="shared_component_usage",
                            severity="error",
                            file_path=f"src/tools/{tname}.py",
                            target_stage_id=stage_id,
                            route_target="compiler",
                            message=f"Shared component '{tname}' appears duplicated under src/tools/{tname}.py.",
                            fix_instruction="Import shared components from _shared instead of duplicating them.",
                        )
                    )

    return issues


def check_hardcoding_and_security(code_dir: Path) -> list[ValidationIssue]:
    """Scan generated Python code for hardcoded secrets, DB passwords, banned imports, and utcnow()."""
    issues: list[ValidationIssue] = []
    src_dir = code_dir / "src"
    if not src_dir.is_dir():
        return issues

    for py_file in sorted(src_dir.rglob("*.py")):
        rel = py_file.relative_to(code_dir).as_posix()
        content = py_file.read_text(encoding="utf-8")
        route_target = "leaf_coder" if rel.startswith("src/tools/") else "compiler"

        if _HARDCODED_SECRET_RE.search(content):
            issues.append(
                ValidationIssue(
                    check_name="zero_hardcoding",
                    severity="critical",
                    file_path=rel,
                    route_target=route_target,  # type: ignore[arg-type]
                    message=f"Hardcoded API secret/token pattern detected in '{rel}'.",
                    fix_instruction=f"Remove hardcoded secret from {rel} and read from settings/env.",
                )
            )
        if _HARDCODED_DB_PASS_RE.search(content):
            issues.append(
                ValidationIssue(
                    check_name="zero_hardcoding",
                    severity="critical",
                    file_path=rel,
                    route_target=route_target,  # type: ignore[arg-type]
                    message=f"Hardcoded database password detected in URI inside '{rel}'.",
                    fix_instruction=f"Externalize DB credentials in {rel} via AgentSettings.",
                )
            )

        try:
            tree = ast.parse(content, filename=rel)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in BANNED_IMPORTS:
                        issues.append(
                            ValidationIssue(
                                check_name="banned_imports",
                                severity="error",
                                file_path=rel,
                                route_target=route_target,  # type: ignore[arg-type]
                                message=f"Banned library '{alias.name}' imported in '{rel}'.",
                                fix_instruction=f"Remove banned import '{alias.name}' from {rel}.",
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".", 1)[0] in BANNED_IMPORTS:
                    issues.append(
                        ValidationIssue(
                            check_name="banned_imports",
                            severity="error",
                            file_path=rel,
                            route_target=route_target,  # type: ignore[arg-type]
                            message=f"Banned library '{node.module}' imported in '{rel}'.",
                            fix_instruction=f"Remove banned import '{node.module}' from {rel}.",
                        )
                    )
            elif isinstance(node, ast.Attribute) and node.attr == "utcnow":
                issues.append(
                    ValidationIssue(
                        check_name="python312_conventions",
                        severity="error",
                        file_path=rel,
                        route_target=route_target,  # type: ignore[arg-type]
                        message=f"Deprecated datetime.utcnow() used in '{rel}'.",
                        fix_instruction=f"Replace datetime.utcnow() with datetime.now(timezone.utc) in {rel}.",
                    )
                )

    return issues


def _map_test_path_to_stage(test_rel: str, impl_docs: ImplDocPackage) -> tuple[str, str | None]:
    if test_rel.startswith("tests/contracts/"):
        return "compiler", None
    if test_rel.startswith("tests/integration/"):
        return "compiler", None
    # tests/unit/test_<mod>.py -> find stage owning tools/<mod>.py
    stem = test_rel.rsplit("/", 1)[-1].removeprefix("test_").removesuffix(".py")
    for stage_id, stg in impl_docs.stages.items():
        for tool in (stg.get("processing") or {}).get("tools") or []:
            handler = str(tool.get("handler") or "")
            if f"/{stem}.py::" in handler or handler.startswith(f"tools/{stem}.py::"):
                return "leaf_coder", stage_id
    return "leaf_coder", None


def run_generated_tests(
    code_dir: Path,
    impl_docs: ImplDocPackage,
    *,
    timeout_seconds: int = 60,
) -> tuple[list[ValidationIssue], dict[str, Any]]:
    """Execute generated pytest suites offline and map any failures back to route targets."""
    test_dirs = [
        rel
        for rel in ("tests/contracts", "tests/unit", "tests/integration")
        if (code_dir / rel).is_dir() and any((code_dir / rel).rglob("test_*.py"))
    ]
    if not test_dirs:
        return [], {"executed": False, "reason": "no_test_files_found"}

    env = dict(os.environ)
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    existing_pp = env.get("PYTHONPATH", "")
    paths = [str(code_dir.resolve()), str(DEFAULT_PROJECT_ROOT.resolve())]
    if existing_pp:
        paths.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(paths)

    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_dirs, "--tb=short", "-q"],
        cwd=str(code_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    summary = {
        "executed": True,
        "returncode": proc.returncode,
        "duration_ms": elapsed_ms,
        "test_dirs": test_dirs,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
    }

    if proc.returncode == 0:
        return [], summary

    issues: list[ValidationIssue] = []
    failed_files: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED "):
            test_spec = line[len("FAILED ") :].split(" - ", 1)[0].strip()
            test_rel = test_spec.split("::", 1)[0].replace("\\", "/")
            failed_files.add(test_rel)

    if not failed_files:
        issues.append(
            ValidationIssue(
                check_name="pytest_suite",
                severity="error",
                route_target="leaf_coder",
                message=f"Generated pytest suite failed (exit code {proc.returncode}).",
                fix_instruction=(proc.stdout or proc.stderr)[-1200:].strip(),
            )
        )
    else:
        for test_rel in sorted(failed_files):
            route_target, stage_id = _map_test_path_to_stage(test_rel, impl_docs)
            issues.append(
                ValidationIssue(
                    check_name="pytest_suite",
                    severity="error",
                    file_path=test_rel,
                    target_stage_id=stage_id,
                    route_target=route_target,  # type: ignore[arg-type]
                    message=f"Pytest failure in '{test_rel}'.",
                    fix_instruction=(proc.stdout or proc.stderr)[-1200:].strip(),
                )
            )

    return issues, summary
