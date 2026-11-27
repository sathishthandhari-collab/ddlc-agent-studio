"""Markdown header-hierarchy chunker with atomic code block and pipe table protection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from src.core.settings import get_studio_config
from src.rag.chunking.base import (
    BaseChunker,
    build_chunk_draft,
    count_tokens,
    normalize_newlines,
    split_text_by_token_budget,
)
from src.rag.store.models import DocumentChunkDraft

_HEADER_RE = re.compile(r"^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE_START_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$|^\s*\|\s*:?-{2,}:?\s*\|\s*$")


@dataclass(frozen=True)
class _MarkdownBlock:
    kind: Literal["prose", "code", "table"]
    text: str


@dataclass
class _MarkdownSection:
    breadcrumb: str
    heading_line: Optional[str]
    blocks: list[_MarkdownBlock]


class MarkdownChunker(BaseChunker):
    """Split Markdown by #, ##, ### headers while preserving breadcrumbs and atomic blocks."""

    def __init__(
        self,
        target_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        overlap_tokens: Optional[int] = None,
    ) -> None:
        self._target_tokens = target_tokens
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens

    def _resolve_bounds(self) -> tuple[int, int, int]:
        if (
            self._target_tokens is not None
            and self._max_tokens is not None
            and self._overlap_tokens is not None
        ):
            return self._target_tokens, self._max_tokens, self._overlap_tokens

        cfg = get_studio_config().rag.chunking.markdown
        target = cfg.target_tokens if self._target_tokens is None else self._target_tokens
        maximum = cfg.max_tokens if self._max_tokens is None else self._max_tokens
        overlap = cfg.overlap_tokens if self._overlap_tokens is None else self._overlap_tokens
        return max(1, target), max(1, maximum), max(0, overlap)

    def chunk(
        self,
        text: str,
        *,
        title: str = "",
        source_path: str = "",
    ) -> list[DocumentChunkDraft]:
        """Chunk Markdown text into breadcrumb-prefixed DocumentChunkDraft instances."""
        normalized = normalize_newlines(text)
        if not normalized.strip():
            return []

        target_tokens, max_tokens, overlap_tokens = self._resolve_bounds()
        root_label = title.strip() or (Path(source_path).stem if source_path else "Document")
        sections = self._parse_sections(normalized, root_label=root_label)

        drafts: list[DocumentChunkDraft] = []
        chunk_idx = 0
        for section in sections:
            section_texts = self._chunk_section(
                section,
                target_tokens=target_tokens,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
            for content in section_texts:
                meta = {"chunker": "markdown"}
                if source_path:
                    meta["source_path"] = source_path
                drafts.append(
                    build_chunk_draft(
                        chunk_index=chunk_idx,
                        content=content,
                        breadcrumb=section.breadcrumb,
                        metadata=meta,
                    )
                )
                chunk_idx += 1

        return drafts

    def _parse_sections(self, text: str, *, root_label: str) -> list[_MarkdownSection]:
        """Parse lines into hierarchical header sections with atomic code/table blocks."""
        lines = text.split("\n")
        total = len(lines)
        idx = 0

        # Keep optional YAML frontmatter intact at top of document.
        frontmatter_block: Optional[_MarkdownBlock] = None
        if lines and lines[0].strip() == "---":
            end_fm = self._find_frontmatter_end(lines)
            if end_fm is not None:
                frontmatter_block = _MarkdownBlock(
                    kind="prose",
                    text="\n".join(lines[: end_fm + 1]).strip(),
                )
                idx = end_fm + 1

        header_stack: dict[int, str] = {1: root_label}
        sections: list[_MarkdownSection] = []
        current_breadcrumb = root_label
        current_heading_line: Optional[str] = None
        current_blocks: list[_MarkdownBlock] = []
        if frontmatter_block and frontmatter_block.text:
            current_blocks.append(frontmatter_block)
        prose_buf: list[str] = []

        def flush_prose() -> None:
            nonlocal prose_buf
            if not prose_buf:
                return
            joined = "\n".join(prose_buf).strip()
            prose_buf = []
            if not joined:
                return
            for para in re.split(r"\n\s*\n+", joined):
                cleaned = para.strip()
                if cleaned:
                    current_blocks.append(_MarkdownBlock(kind="prose", text=cleaned))

        def flush_section() -> None:
            flush_prose()
            if current_blocks:
                sections.append(
                    _MarkdownSection(
                        breadcrumb=current_breadcrumb,
                        heading_line=current_heading_line,
                        blocks=list(current_blocks),
                    )
                )
                current_blocks.clear()

        while idx < total:
            line = lines[idx]

            # 1. Fenced code block (``` or ~~~) — atomic, headers inside are ignored.
            fence_match = _FENCE_START_RE.match(line)
            if fence_match:
                flush_prose()
                fence_marker = fence_match.group(1)
                fence_char = fence_marker[0]
                fence_len = len(fence_marker)
                fence_lines = [line]
                idx += 1
                close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$")
                while idx < total:
                    fence_lines.append(lines[idx])
                    if close_re.match(lines[idx]):
                        idx += 1
                        break
                    idx += 1
                current_blocks.append(_MarkdownBlock(kind="code", text="\n".join(fence_lines)))
                continue

            # 2. Markdown header (#, ##, ###)
            header_match = _HEADER_RE.match(line)
            if header_match:
                flush_section()
                level = len(header_match.group(1))
                heading_text = header_match.group(2).strip()
                for k in list(header_stack.keys()):
                    if k >= level:
                        del header_stack[k]
                if level > 1 and 1 not in header_stack:
                    header_stack[1] = root_label
                header_stack[level] = heading_text
                current_breadcrumb = " > ".join(
                    header_stack[lvl] for lvl in sorted(header_stack)
                )
                current_heading_line = line.strip()
                idx += 1
                continue

            # 3. Markdown pipe table — atomic, never split mid-table.
            if self._is_table_start(lines, idx):
                flush_prose()
                table_lines = [lines[idx], lines[idx + 1]]
                idx += 2
                while idx < total:
                    row = lines[idx]
                    if not row.strip() or "|" not in row:
                        break
                    if _HEADER_RE.match(row) or _FENCE_START_RE.match(row):
                        break
                    table_lines.append(row)
                    idx += 1
                current_blocks.append(_MarkdownBlock(kind="table", text="\n".join(table_lines)))
                continue

            prose_buf.append(line)
            idx += 1

        flush_section()

        # If document contained only headings with no body lines, emit the heading as content.
        if not sections and current_heading_line:
            sections.append(
                _MarkdownSection(
                    breadcrumb=current_breadcrumb,
                    heading_line=current_heading_line,
                    blocks=[_MarkdownBlock(kind="prose", text=current_heading_line)],
                )
            )
        return sections

    @staticmethod
    def _find_frontmatter_end(lines: list[str]) -> Optional[int]:
        for i in range(1, min(len(lines), 100)):
            if lines[i].strip() == "---":
                return i
        return None

    @staticmethod
    def _is_table_start(lines: list[str], idx: int) -> bool:
        if idx + 1 >= len(lines):
            return False
        header = lines[idx]
        separator = lines[idx + 1]
        if "|" not in header or "|" not in separator:
            return False
        if _FENCE_START_RE.match(header) or _HEADER_RE.match(header):
            return False
        return bool(_TABLE_SEP_RE.match(separator))

    def _chunk_section(
        self,
        section: _MarkdownSection,
        *,
        target_tokens: int,
        max_tokens: int,
        overlap_tokens: int,
    ) -> list[str]:
        """Pack blocks within a section under token bounds while keeping code/tables atomic."""
        prefix = f"[Context: {section.breadcrumb}]\n\n"
        prefix_tokens = count_tokens(prefix)
        eff_max = max(1, max_tokens - prefix_tokens)
        eff_target = max(1, min(target_tokens - prefix_tokens, eff_max))

        blocks = list(section.blocks)
        if section.heading_line:
            # Prepend the section heading line to the first block for inline keyword recall.
            first = blocks[0]
            blocks[0] = _MarkdownBlock(
                kind=first.kind if first.kind != "prose" else "prose",
                text=f"{section.heading_line}\n\n{first.text}"
                if first.kind == "prose"
                else first.text,
            )
            if first.kind != "prose":
                blocks.insert(0, _MarkdownBlock(kind="prose", text=section.heading_line))

        whole_body = "\n\n".join(b.text for b in blocks)
        if count_tokens(whole_body) <= eff_max:
            return [f"{prefix}{whole_body}"]

        emitted_bodies: list[str] = []
        acc_blocks: list[_MarkdownBlock] = []

        def flush_acc() -> None:
            if acc_blocks:
                emitted_bodies.append("\n\n".join(b.text for b in acc_blocks))
                acc_blocks.clear()

        for block in blocks:
            block_tokens = count_tokens(block.text)

            # Oversized atomic code block or table: never split mid-block.
            if block.kind in {"code", "table"} and block_tokens > eff_max:
                flush_acc()
                emitted_bodies.append(block.text)
                continue

            # Oversized single prose paragraph: split with overlap.
            if block.kind == "prose" and block_tokens > eff_max:
                flush_acc()
                prose_parts = split_text_by_token_budget(
                    block.text,
                    max_tokens=eff_max,
                    target_tokens=eff_target,
                    overlap_tokens=overlap_tokens,
                )
                emitted_bodies.extend(prose_parts)
                continue

            if not acc_blocks:
                acc_blocks.append(block)
                continue

            candidate = "\n\n".join([*(b.text for b in acc_blocks), block.text])
            candidate_tokens = count_tokens(candidate)
            if candidate_tokens <= eff_target or (
                candidate_tokens <= eff_max and block.kind in {"code", "table"}
            ):
                acc_blocks.append(block)
                continue

            # Start a new chunk; carry trailing prose overlap only if both current and next are prose.
            prev_last = acc_blocks[-1]
            flush_acc()
            if (
                overlap_tokens > 0
                and prev_last.kind == "prose"
                and block.kind == "prose"
            ):
                overlap_snippet = self._tail_prose_overlap(
                    prev_last.text,
                    overlap_tokens=overlap_tokens,
                    max_with_next=eff_max - block_tokens - 2,
                )
                if overlap_snippet:
                    acc_blocks.append(_MarkdownBlock(kind="prose", text=overlap_snippet))
            acc_blocks.append(block)

        flush_acc()
        return [f"{prefix}{body}" for body in emitted_bodies if body.strip()]

    @staticmethod
    def _tail_prose_overlap(
        prose_text: str,
        *,
        overlap_tokens: int,
        max_with_next: int,
    ) -> str:
        """Extract trailing sentence(s) from prose_text up to overlap_tokens."""
        budget = min(overlap_tokens, max_with_next)
        if budget <= 0:
            return ""
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prose_text) if s.strip()]
        if not sentences:
            return ""
        chosen: list[str] = []
        for sent in reversed(sentences):
            trial = " ".join([sent, *chosen])
            if count_tokens(trial) > budget:
                break
            chosen.insert(0, sent)
        return " ".join(chosen)
