"""Deterministic Python source normalization and optional Ruff formatting."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def normalize_python_source(source: str) -> str:
    """Deterministically normalize line endings, blank line spacing, and trailing newline."""
    lines = [line.rstrip() for line in source.replace("\r\n", "\n").splitlines()]
    collapsed: list[str] = []
    blank_streak = 0
    for line in lines:
        if not line:
            blank_streak += 1
            if blank_streak <= 2:
                collapsed.append("")
        else:
            blank_streak = 0
            collapsed.append(line)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed) + "\n"


def format_directory_with_ruff(target_dir: Path) -> bool:
    """Best-effort ruff formatting when ruff executable is available; returns True if run."""
    ruff_candidates = [
        Path(sys.executable).with_name("ruff.exe"),
        Path(sys.executable).with_name("ruff"),
    ]
    ruff_bin = next((p for p in ruff_candidates if p.is_file()), None)
    if ruff_bin is None:
        return False
    proc = subprocess.run(
        [str(ruff_bin), "format", "--quiet", str(target_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0
