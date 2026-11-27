"""YAML tree-aware section chunker preserving parent root/header context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.settings import get_studio_config
from src.rag.chunking.base import (
    BaseChunker,
    build_chunk_draft,
    count_tokens,
    normalize_newlines,
)
from src.rag.store.models import DocumentChunkDraft

_TOP_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-\"'][^:\n]*:\s*(?:#.*)?$")


class YamlChunker(BaseChunker):
    """Slice YAML trees along semantic key boundaries while preserving parent context."""

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

        cfg = get_studio_config().rag.chunking.yaml
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
        """Parse YAML documents and split into structure-preserving slices."""
        normalized = normalize_newlines(text)
        if not normalized.strip():
            return []

        target_tokens, max_tokens = self._resolve_bounds()
        fallback_root = title.strip() or (Path(source_path).stem if source_path else "yaml")

        try:
            docs = [d for d in yaml.safe_load_all(normalized) if d is not None]
        except yaml.YAMLError:
            return self._fallback_malformed_yaml(
                normalized,
                root_label=fallback_root,
                source_path=source_path,
                target_tokens=target_tokens,
                max_tokens=max_tokens,
            )

        if not docs:
            return []

        slices: list[tuple[str, str]] = []
        for doc_idx, doc in enumerate(docs):
            root_label = self._derive_root_label(doc, fallback=fallback_root, doc_idx=doc_idx, multi=len(docs) > 1)
            slices.extend(
                self._slice_root(
                    doc,
                    root_label=root_label,
                    target_tokens=target_tokens,
                    max_tokens=max_tokens,
                )
            )

        drafts: list[DocumentChunkDraft] = []
        for idx, (breadcrumb, content) in enumerate(slices):
            meta = {"chunker": "yaml"}
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
    def _derive_root_label(doc: Any, *, fallback: str, doc_idx: int, multi: bool) -> str:
        label = fallback
        if isinstance(doc, dict):
            if isinstance(doc.get("agent"), dict) and doc["agent"].get("name"):
                label = str(doc["agent"]["name"])
            elif doc.get("schema_name"):
                label = str(doc["schema_name"])
            elif doc.get("name"):
                label = str(doc["name"])
            elif doc.get("id"):
                label = str(doc["id"])
        return f"{label}#{doc_idx + 1}" if multi else label

    @staticmethod
    def _dump_yaml(data: Any) -> str:
        return yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()

    @staticmethod
    def _wrap_path(key_path: list[str], value: Any) -> Any:
        wrapped = value
        for key in reversed(key_path):
            wrapped = {key: wrapped}
        return wrapped

    def _slice_root(
        self,
        doc: Any,
        *,
        root_label: str,
        target_tokens: int,
        max_tokens: int,
    ) -> list[tuple[str, str]]:
        dumped_full = self._dump_yaml(doc)
        if count_tokens(dumped_full) <= max_tokens:
            return [(root_label, dumped_full)]

        if isinstance(doc, dict):
            return self._slice_mapping(
                doc,
                key_path=[],
                breadcrumb_prefix=[root_label],
                target_tokens=target_tokens,
                max_tokens=max_tokens,
            )
        if isinstance(doc, list):
            return self._slice_list(
                doc,
                key_path=[],
                breadcrumb_prefix=[root_label],
                target_tokens=target_tokens,
                max_tokens=max_tokens,
            )
        return [(root_label, dumped_full)]

    def _slice_mapping(
        self,
        mapping: dict[Any, Any],
        *,
        key_path: list[str],
        breadcrumb_prefix: list[str],
        target_tokens: int,
        max_tokens: int,
    ) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        acc: dict[Any, Any] = {}
        acc_keys: list[str] = []

        def flush_acc() -> None:
            if not acc:
                return
            dumped = self._dump_yaml(self._wrap_path(key_path, acc))
            crumb_tail = ", ".join(acc_keys) if len(acc_keys) <= 3 else f"{acc_keys[0]}..{acc_keys[-1]}"
            results.append((" > ".join([*breadcrumb_prefix, crumb_tail]), dumped))
            acc.clear()
            acc_keys.clear()

        for key, value in mapping.items():
            key_str = str(key)
            single_wrapped = self._dump_yaml(self._wrap_path(key_path, {key: value}))
            single_tokens = count_tokens(single_wrapped)

            if single_tokens > max_tokens:
                flush_acc()
                if isinstance(value, dict) and value:
                    results.extend(
                        self._slice_mapping(
                            value,
                            key_path=[*key_path, key_str],
                            breadcrumb_prefix=[*breadcrumb_prefix, key_str],
                            target_tokens=target_tokens,
                            max_tokens=max_tokens,
                        )
                    )
                elif isinstance(value, list) and value:
                    results.extend(
                        self._slice_list(
                            value,
                            key_path=[*key_path, key_str],
                            breadcrumb_prefix=[*breadcrumb_prefix, key_str],
                            target_tokens=target_tokens,
                            max_tokens=max_tokens,
                        )
                    )
                else:
                    # Keep scalar KV pair intact rather than tearing key from value.
                    results.append((" > ".join([*breadcrumb_prefix, key_str]), single_wrapped))
                continue

            if not acc:
                acc[key] = value
                acc_keys.append(key_str)
                continue

            candidate = dict(acc)
            candidate[key] = value
            candidate_yaml = self._dump_yaml(self._wrap_path(key_path, candidate))
            if count_tokens(candidate_yaml) <= target_tokens or (
                count_tokens(candidate_yaml) <= max_tokens and len(acc) == 1
            ):
                acc[key] = value
                acc_keys.append(key_str)
            else:
                flush_acc()
                acc[key] = value
                acc_keys.append(key_str)

        flush_acc()
        return results

    def _slice_list(
        self,
        items: list[Any],
        *,
        key_path: list[str],
        breadcrumb_prefix: list[str],
        target_tokens: int,
        max_tokens: int,
    ) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        acc: list[Any] = []
        start_idx = 0

        def flush_acc(end_idx: int) -> None:
            if not acc:
                return
            dumped = self._dump_yaml(self._wrap_path(key_path, acc))
            label = f"[{start_idx}]" if start_idx == end_idx else f"[{start_idx}..{end_idx}]"
            results.append((" > ".join([*breadcrumb_prefix, label]), dumped))
            acc.clear()

        for idx, item in enumerate(items):
            single_wrapped = self._dump_yaml(self._wrap_path(key_path, [item]))
            if count_tokens(single_wrapped) > max_tokens:
                flush_acc(idx - 1)
                if isinstance(item, dict) and item:
                    item_label = str(item.get("name") or item.get("id") or f"[{idx}]")
                    sub_slices = self._slice_mapping(
                        item,
                        key_path=[],
                        breadcrumb_prefix=[*breadcrumb_prefix, item_label],
                        target_tokens=target_tokens,
                        max_tokens=max_tokens,
                    )
                    for sub_crumb, sub_yaml in sub_slices:
                        parsed_sub = yaml.safe_load(sub_yaml)
                        wrapped_item = self._dump_yaml(self._wrap_path(key_path, [parsed_sub]))
                        results.append((sub_crumb, wrapped_item))
                else:
                    results.append((" > ".join([*breadcrumb_prefix, f"[{idx}]"]), single_wrapped))
                start_idx = idx + 1
                continue

            if not acc:
                acc.append(item)
                start_idx = idx
                continue

            candidate = [*acc, item]
            candidate_yaml = self._dump_yaml(self._wrap_path(key_path, candidate))
            if count_tokens(candidate_yaml) <= target_tokens or (
                count_tokens(candidate_yaml) <= max_tokens and len(acc) == 1
            ):
                acc.append(item)
            else:
                flush_acc(idx - 1)
                acc.append(item)
                start_idx = idx

        flush_acc(len(items) - 1)
        return results

    def _fallback_malformed_yaml(
        self,
        text: str,
        *,
        root_label: str,
        source_path: str,
        target_tokens: int,
        max_tokens: int,
    ) -> list[DocumentChunkDraft]:
        """Gracefully chunk malformed YAML along unindented top-level lines."""
        sections: list[str] = []
        current: list[str] = []

        for line in text.split("\n"):
            if current and _TOP_KEY_RE.match(line):
                block = "\n".join(current).strip()
                if block:
                    sections.append(block)
                current = [line]
            else:
                current.append(line)
        if current:
            block = "\n".join(current).strip()
            if block:
                sections.append(block)

        chunks_text: list[str] = []
        acc_lines: list[str] = []
        for sec in sections:
            if count_tokens(sec) <= max_tokens:
                if acc_lines and count_tokens("\n".join([*acc_lines, sec])) > target_tokens:
                    chunks_text.append("\n".join(acc_lines))
                    acc_lines = [sec]
                else:
                    acc_lines.append(sec)
                continue

            if acc_lines:
                chunks_text.append("\n".join(acc_lines))
                acc_lines = []
            # Split oversized raw section on line boundaries.
            line_buf: list[str] = []
            for ln in sec.split("\n"):
                if line_buf and count_tokens("\n".join([*line_buf, ln])) > max_tokens:
                    chunks_text.append("\n".join(line_buf))
                    line_buf = [ln]
                else:
                    line_buf.append(ln)
            if line_buf:
                chunks_text.append("\n".join(line_buf))

        if acc_lines:
            chunks_text.append("\n".join(acc_lines))

        drafts: list[DocumentChunkDraft] = []
        for idx, content in enumerate(chunks_text):
            meta = {"chunker": "yaml", "fallback": True}
            if source_path:
                meta["source_path"] = source_path
            drafts.append(
                build_chunk_draft(
                    chunk_index=idx,
                    content=content,
                    breadcrumb=f"{root_label} (raw)",
                    metadata=meta,
                )
            )
        return drafts
