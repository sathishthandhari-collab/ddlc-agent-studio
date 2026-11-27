"""Base abstractions, token counting, hashing, and file routing for RAG chunking."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from src.rag.store.models import DocumentChunkDraft

if TYPE_CHECKING:
    pass

_FALLBACK_TOKEN_RE = re.compile(r"\w+|[^\w\s]")

_MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
_YAML_EXTENSIONS = frozenset({".yaml", ".yml"})
_CODE_EXTENSIONS = frozenset({".py"})


def compute_sha256(text: str) -> str:
    """Compute the lowercase hex SHA-256 digest for UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _get_tiktoken_encoding() -> Optional[Any]:
    """Load and cache cl100k_base encoding once; return None if offline/unavailable."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    """Count tokens using cached cl100k_base, falling back deterministically offline."""
    if not text:
        return 0
    encoding = _get_tiktoken_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    return len(_FALLBACK_TOKEN_RE.findall(text))


def normalize_newlines(text: str) -> str:
    """Normalize CRLF/CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_chunk_draft(
    *,
    chunk_index: int,
    content: str,
    breadcrumb: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> DocumentChunkDraft:
    """Construct a DocumentChunkDraft with computed SHA-256 and token count."""
    return DocumentChunkDraft(
        chunk_index=chunk_index,
        chunk_sha256=compute_sha256(content),
        breadcrumb=breadcrumb,
        content=content,
        token_count=count_tokens(content),
        metadata=dict(metadata or {}),
    )


def split_text_by_token_budget(
    text: str,
    *,
    max_tokens: int,
    target_tokens: Optional[int] = None,
    overlap_tokens: int = 0,
) -> list[str]:
    """Split prose into chunks bounded by max_tokens with optional overlap."""
    cleaned = text.strip()
    if not cleaned:
        return []

    eff_max = max(1, max_tokens)
    eff_target = max(1, min(target_tokens or eff_max, eff_max))
    eff_overlap = max(0, min(overlap_tokens, eff_max // 2))

    if count_tokens(cleaned) <= eff_max:
        return [cleaned]

    units = _split_into_bounded_units(cleaned, eff_max)
    chunks: list[str] = []
    current: list[str] = []

    for unit in units:
        if not current:
            current.append(unit)
            continue

        candidate = " ".join([*current, unit])
        candidate_tokens = count_tokens(candidate)
        if candidate_tokens <= eff_target or (
            candidate_tokens <= eff_max and len(current) == 1 and count_tokens(current[0]) < eff_target // 2
        ):
            current.append(unit)
            continue

        chunks.append(" ".join(current))
        overlap_units = _select_overlap_units(current, unit, eff_overlap, eff_max)
        current = [*overlap_units, unit]

    if current:
        chunks.append(" ".join(current))

    return chunks


def _split_into_bounded_units(text: str, max_tokens: int) -> list[str]:
    """Split text into sentences, falling back to words or slices if any unit exceeds max_tokens."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not sentences:
        sentences = [text]

    bounded: list[str] = []
    for sentence in sentences:
        if count_tokens(sentence) <= max_tokens:
            bounded.append(sentence)
            continue
        words = [w for w in sentence.split() if w]
        if not words:
            bounded.extend(_slice_oversized_token(sentence, max_tokens))
            continue
        for word in words:
            if count_tokens(word) <= max_tokens:
                bounded.append(word)
            else:
                bounded.extend(_slice_oversized_token(word, max_tokens))
    return bounded


def _slice_oversized_token(token: str, max_tokens: int) -> list[str]:
    """Character-slice a single unbroken token so every piece fits within max_tokens."""
    pieces: list[str] = []
    start = 0
    length = len(token)
    while start < length:
        end = length
        while end > start + 1 and count_tokens(token[start:end]) > max_tokens:
            end = start + max(1, (end - start) // 2)
        while end < length and count_tokens(token[start : end + 1]) <= max_tokens:
            end += 1
        pieces.append(token[start:end])
        start = end
    return pieces


def _select_overlap_units(
    previous_units: list[str],
    next_unit: str,
    overlap_tokens: int,
    max_tokens: int,
) -> list[str]:
    """Select trailing units from previous_units that fit both overlap_tokens and max_tokens."""
    if overlap_tokens <= 0:
        return []

    selected: list[str] = []
    for unit in reversed(previous_units):
        trial = [unit, *selected]
        trial_text = " ".join(trial)
        if count_tokens(trial_text) > overlap_tokens:
            break
        if count_tokens(f"{trial_text} {next_unit}") > max_tokens:
            break
        selected = trial
    return selected


class BaseChunker(ABC):
    """Abstract base class for structure-aware document chunkers."""

    @abstractmethod
    def chunk(
        self,
        text: str,
        *,
        title: str = "",
        source_path: str = "",
    ) -> list[DocumentChunkDraft]:
        """Split source text into ordered DocumentChunkDraft objects."""


def chunk_file(
    path: Path | str,
    text: str,
    *,
    title: str = "",
) -> list[DocumentChunkDraft]:
    """Route a source file to the appropriate structure-aware chunker by extension."""
    from src.rag.chunking.code_chunker import CodeChunker
    from src.rag.chunking.markdown_chunker import MarkdownChunker
    from src.rag.chunking.yaml_chunker import YamlChunker

    file_path = Path(path)
    ext = file_path.suffix.lower()
    source_str = file_path.as_posix()

    if ext in _YAML_EXTENSIONS:
        chunker: BaseChunker = YamlChunker()
    elif ext in _CODE_EXTENSIONS:
        chunker = CodeChunker()
    elif ext in _MARKDOWN_EXTENSIONS:
        chunker = MarkdownChunker()
    else:
        chunker = MarkdownChunker()

    return chunker.chunk(text, title=title, source_path=source_str)
