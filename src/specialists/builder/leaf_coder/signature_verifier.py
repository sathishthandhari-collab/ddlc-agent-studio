"""Deterministic AST, callable signature, banned-library, and Jinja2 prompt AST verifiers."""

from __future__ import annotations

import ast
import re
from typing import Optional

import jinja2
import jinja2.meta

ALLOWED_PROMPT_CONTEXT_VARS: frozenset[str] = frozenset(
    {"input", "diagnostics", "feedback", "config"}
)

BANNED_IMPORTS: frozenset[str] = frozenset(
    {"pyspark", "pandera", "langchain_community"}
)

_SECRET_LITERAL_RE = re.compile(
    r"(?:sk-[A-Za-z0-9\-_]{16,}|Bearer\s+[A-Za-z0-9\-._~+/]+=*|ghp_[A-Za-z0-9]{20,})"
)


def _parse_expected_signature(signature: str) -> Optional[ast.FunctionDef]:
    """Parse a Python callable signature string like '(sql: str, dialect: str) -> tuple[bool, str]'."""
    sig_clean = signature.strip()
    if not sig_clean.startswith("("):
        return None
    stub_src = f"def _expected_stub{sig_clean}:\n    pass\n"
    try:
        mod = ast.parse(stub_src)
    except SyntaxError:
        return None
    fn = mod.body[0]
    return fn if isinstance(fn, ast.FunctionDef) else None


def verify_tool_source(
    tool_source: str,
    *,
    expected_func_name: str,
    expected_signature: Optional[str] = None,
    filename: str = "<leaf_tool>",
) -> list[str]:
    """Verify Python syntax, callable signature match, and banned patterns on Leaf Coder output."""
    errors: list[str] = []
    try:
        tree = ast.parse(tool_source, filename=filename)
    except SyntaxError as exc:
        return [f"SyntaxError in '{filename}' at line {exc.lineno}: {exc.msg}"]

    # 1. Check banned imports & deprecated datetime.utcnow()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".", 1)[0]
                if root_pkg in BANNED_IMPORTS:
                    errors.append(
                        f"Disallowed library import '{alias.name}' in '{filename}'."
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root_pkg = node.module.split(".", 1)[0]
            if root_pkg in BANNED_IMPORTS:
                errors.append(
                    f"Disallowed library import from '{node.module}' in '{filename}'."
                )
        elif isinstance(node, ast.Attribute) and node.attr == "utcnow":
            errors.append(
                f"Deprecated datetime.utcnow() detected in '{filename}'; use datetime.now(timezone.utc)."
            )

    if _SECRET_LITERAL_RE.search(tool_source):
        errors.append(f"Potential hardcoded secret/token detected in '{filename}'.")

    # 2. Locate target callable at top level
    target_fn: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == expected_func_name:
            target_fn = stmt
            break

    if target_fn is None:
        errors.append(
            f"Expected callable '{expected_func_name}' not found at top level of '{filename}'."
        )
        return errors

    # 3. Verify signature alignment if declared
    if expected_signature:
        expected_fn = _parse_expected_signature(expected_signature)
        if expected_fn is None:
            errors.append(f"Invalid declared signature format: {expected_signature!r}")
        else:
            exp_pos = [a.arg for a in expected_fn.args.args]
            exp_kw = [a.arg for a in expected_fn.args.kwonlyargs]
            act_pos = [a.arg for a in target_fn.args.args]
            act_kw = [a.arg for a in target_fn.args.kwonlyargs]

            if act_pos[: len(exp_pos)] != exp_pos or len(act_pos) < len(exp_pos):
                errors.append(
                    f"Function '{expected_func_name}' positional parameters {act_pos} do not match "
                    f"declared signature parameters {exp_pos}."
                )
            if exp_kw and not set(exp_kw).issubset(set(act_kw)):
                errors.append(
                    f"Function '{expected_func_name}' missing keyword-only parameters {exp_kw}; found {act_kw}."
                )
            if expected_fn.returns is not None and target_fn.returns is None:
                errors.append(
                    f"Function '{expected_func_name}' must include return type annotation "
                    f"matching '{ast.unparse(expected_fn.returns)}'."
                )

    return errors


def verify_prompt_template_ast(template_path: str, template_source: str) -> list[str]:
    """Statically validate Jinja2 template AST against the Universal Prompt Contract (Decision 28)."""
    env = jinja2.Environment()
    try:
        parsed_ast = env.parse(template_source)
    except jinja2.TemplateSyntaxError as exc:
        return [f"Syntax error in '{template_path}' at line {exc.lineno}: {exc.message}"]

    undeclared_vars = jinja2.meta.find_undeclared_variables(parsed_ast)
    illegal_vars = undeclared_vars - ALLOWED_PROMPT_CONTEXT_VARS
    if illegal_vars:
        return [
            f"Prompt template '{template_path}' references undeclared variables: "
            f"{sorted(illegal_vars)}. Templates must strictly conform to the Universal Prompt "
            f"Contract: {sorted(ALLOWED_PROMPT_CONTEXT_VARS)}."
        ]
    return []
