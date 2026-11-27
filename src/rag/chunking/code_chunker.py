"""Python AST-aware code chunker preserving functions, decorators, docstrings, and classes."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

from src.core.settings import get_studio_config
from src.rag.chunking.base import (
    BaseChunker,
    build_chunk_draft,
    count_tokens,
    normalize_newlines,
)
from src.rag.store.models import DocumentChunkDraft


class CodeChunker(BaseChunker):
    """Split Python modules at AST ClassDef and FunctionDef boundaries."""

    def __init__(
        self,
        target_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        self._target_tokens = target_tokens
        self._max_tokens = max_tokens

    def _resolve_bounds(self) -> tuple[int, int]:
        if self._target_tokens is not None and self._max_tokens is not None:
            return self._target_tokens, self._max_tokens

        cfg = get_studio_config().rag.chunking.code
        target = cfg.target_tokens if self._target_tokens is None else self._target_tokens
        maximum = cfg.max_tokens if self._max_tokens is None else self._max_tokens
        return max(1, target), max(1, maximum)

    def chunk(
        self,
        text: str,
        *,
        title: str = "",
        source_path: str = "",
    ) -> list[DocumentChunkDraft]:
        """Slice Python source code into AST-aligned DocumentChunkDraft chunks."""
        normalized = normalize_newlines(text)
        if not normalized.strip():
            return []

        target_tokens, max_tokens = self._resolve_bounds()
        mod_label = title.strip() or (Path(source_path).stem if source_path else "module")

        try:
            tree = ast.parse(normalized)
        except SyntaxError:
            return self._fallback_line_chunks(
                normalized,
                mod_label=mod_label,
                source_path=source_path,
                max_tokens=max_tokens,
            )

        lines = normalized.splitlines()
        slices: list[tuple[str, str, str]] = []  # (breadcrumb, content, node_type)
        header_nodes: list[ast.stmt] = []
        seen_def = False

        def flush_module_stmts(stmts: list[ast.stmt], *, suffix: str) -> None:
            if not stmts:
                return
            for block_text in self._group_stmt_slices(lines, stmts, max_tokens=max_tokens):
                slices.append((f"{mod_label} > {suffix}", block_text, suffix))

        trailing_stmts: list[ast.stmt] = []

        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if not seen_def:
                    flush_module_stmts(header_nodes, suffix="module")
                    header_nodes = []
                    seen_def = True
                elif trailing_stmts:
                    flush_module_stmts(trailing_stmts, suffix="module_body")
                    trailing_stmts = []

                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_src = self._slice_node_lines(lines, node)
                    slices.append((f"{mod_label} > {node.name}", func_src, "function"))
                else:
                    slices.extend(
                        self._chunk_class_node(
                            lines,
                            node,
                            mod_label=mod_label,
                            target_tokens=target_tokens,
                            max_tokens=max_tokens,
                        )
                    )
            else:
                if not seen_def:
                    header_nodes.append(node)
                else:
                    trailing_stmts.append(node)

        if not seen_def:
            flush_module_stmts(header_nodes, suffix="module")
        elif trailing_stmts:
            flush_module_stmts(trailing_stmts, suffix="module_body")

        drafts: list[DocumentChunkDraft] = []
        for idx, (breadcrumb, content, node_type) in enumerate(slices):
            meta = {"chunker": "code", "node_type": node_type}
            if source_path:
                meta["source_path"] = source_path
            drafts.append(
                build_chunk_draft(
                    chunk_index=idx,
                    content=content,
                    breadcrumb=breadcrumb,
                    metadata=meta,
                )
            )
        return drafts

    @staticmethod
    def _node_start_line(node: ast.stmt) -> int:
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            return min(d.lineno for d in decorators)
        return node.lineno

    def _slice_node_lines(self, lines: list[str], node: ast.stmt) -> str:
        start = self._node_start_line(node)
        end = node.end_lineno or node.lineno
        return "\n".join(lines[start - 1 : end]).rstrip()

    def _group_stmt_slices(
        self,
        lines: list[str],
        stmts: list[ast.stmt],
        *,
        max_tokens: int,
    ) -> list[str]:
        """Group consecutive module-level statements without breaking individual statements."""
        if not stmts:
            return []
        first_start = self._node_start_line(stmts[0])
        last_end = stmts[-1].end_lineno or stmts[-1].lineno
        combined = "\n".join(lines[first_start - 1 : last_end]).strip()
        if combined and count_tokens(combined) <= max_tokens:
            return [combined]

        groups: list[str] = []
        acc: list[str] = []
        for stmt in stmts:
            snippet = self._slice_node_lines(lines, stmt)
            if not snippet:
                continue
            if acc and count_tokens("\n".join([*acc, snippet])) > max_tokens:
                groups.append("\n".join(acc))
                acc = [snippet]
            else:
                acc.append(snippet)
        if acc:
            groups.append("\n".join(acc))
        return groups

    def _chunk_class_node(
        self,
        lines: list[str],
        cls_node: ast.ClassDef,
        *,
        mod_label: str,
        target_tokens: int,
        max_tokens: int,
    ) -> list[tuple[str, str, str]]:
        """Keep class whole unless exceeding max_tokens, then split by methods with header context."""
        class_src = self._slice_node_lines(lines, cls_node)
        if count_tokens(class_src) <= max_tokens:
            return [(f"{mod_label} > {cls_node.name}", class_src, "class")]

        class_start = self._node_start_line(cls_node)
        body = cls_node.body
        if not body:
            return [(f"{mod_label} > {cls_node.name}", class_src, "class")]

        # Build class header: decorators + class signature + docstring (if first statement is str).
        header_end = body[0].lineno - 1
        body_start_idx = 0
        if (
            isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            header_end = body[0].end_lineno or body[0].lineno
            body_start_idx = 1

        class_header = "\n".join(lines[class_start - 1 : header_end]).rstrip()
        header_tokens = count_tokens(class_header)

        # Include small leading class attributes before first method if they fit comfortably.
        remaining_body = body[body_start_idx:]
        first_method_pos = next(
            (
                i
                for i, member in enumerate(remaining_body)
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            len(remaining_body),
        )
        leading_attrs = remaining_body[:first_method_pos]
        if leading_attrs and first_method_pos < len(remaining_body):
            attr_end = self._node_start_line(remaining_body[first_method_pos]) - 1
            extended_header = "\n".join(lines[class_start - 1 : attr_end]).rstrip()
            if count_tokens(extended_header) <= max(target_tokens // 2, header_tokens + 60):
                class_header = extended_header
                remaining_body = remaining_body[first_method_pos:]

        if not remaining_body:
            return [(f"{mod_label} > {cls_node.name}", class_src, "class")]

        chunks: list[tuple[str, str, str]] = []
        acc_members: list[str] = []
        acc_names: list[str] = []

        def flush_members() -> None:
            if not acc_members:
                return
            combined = f"{class_header}\n\n" + "\n\n".join(acc_members)
            if len(acc_names) == 1:
                crumb = f"{mod_label} > {cls_node.name}.{acc_names[0]}"
            else:
                joined_names = ", ".join(acc_names[:3])
                if len(acc_names) > 3:
                    joined_names += ", ..."
                crumb = f"{mod_label} > {cls_node.name} ({joined_names})"
            chunks.append((crumb, combined, "class_slice"))
            acc_members.clear()
            acc_names.clear()

        for member in remaining_body:
            member_src = self._slice_node_lines(lines, member)
            member_name = getattr(member, "name", "attrs")
            if not acc_members:
                acc_members.append(member_src)
                acc_names.append(member_name)
                continue

            candidate = f"{class_header}\n\n" + "\n\n".join([*acc_members, member_src])
            if count_tokens(candidate) <= target_tokens:
                acc_members.append(member_src)
                acc_names.append(member_name)
            else:
                flush_members()
                acc_members.append(member_src)
                acc_names.append(member_name)

        flush_members()
        return chunks

    def _fallback_line_chunks(
        self,
        text: str,
        *,
        mod_label: str,
        source_path: str,
        max_tokens: int,
    ) -> list[DocumentChunkDraft]:
        """Fallback line-window chunker when Python source raises SyntaxError."""
        lines = text.split("\n")
        chunks: list[str] = []
        buf: list[str] = []
        for line in lines:
            if buf and count_tokens("\n".join([*buf, line])) > max_tokens:
                chunks.append("\n".join(buf))
                buf = [line]
            else:
                buf.append(line)
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                chunks.append(joined)

        drafts: list[DocumentChunkDraft] = []
        for idx, content in enumerate(chunks):
            meta = {"chunker": "code", "fallback": True}
            if source_path:
                meta["source_path"] = source_path
            drafts.append(
                build_chunk_draft(
                    chunk_index=idx,
                    content=content,
                    breadcrumb=f"{mod_label} (raw)",
                    metadata=meta,
                )
            )
        return drafts
