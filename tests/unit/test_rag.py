"""Deterministic zero-infra unit tests for Step 5 RAG chunking, retrieval, facades, embeddings, and ingestion."""

from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

import pytest
import yaml
from src.core.settings import RuntimeEnvSettings, get_studio_config
from src.rag.chunking.base import chunk_file, compute_sha256
from src.rag.chunking.code_chunker import CodeChunker
from src.rag.chunking.markdown_chunker import MarkdownChunker
from src.rag.chunking.yaml_chunker import YamlChunker
from src.rag.embeddings.embedder import EmbeddingConfigError, LiteLLMEmbedder
from src.rag.ingest import cli_app, ingest_knowledge_corpus
from src.rag.interfaces import ArchitectRAGFacade, BuilderRAGFacade, ResearcherRAGFacade
from src.rag.retrieval.hybrid_search import HybridRAGRetriever
from src.rag.retrieval.reranker import FlashRankReranker
from src.rag.retrieval.rrf import compute_rrf_score, fuse_ranked_lists, fuse_search_results
from src.rag.store.models import (
    ALLOWED_DOMAINS,
    RAGContextSlice,
    RAGSearchResult,
    validate_domain,
)
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"


# ---------------------------------------------------------------------------
# Helpers & Deterministic Doubles
# ---------------------------------------------------------------------------


def _make_search_result(
    *,
    domain: str = "library_docs",
    content: str = "sample content",
    token_count: int = 20,
    rrf_score: float = 0.032,
    rerank_score: Optional[float] = None,
    breadcrumb: Optional[str] = "Doc > Section",
    source_path: Optional[str] = "library_docs/sample.md",
) -> RAGSearchResult:
    return RAGSearchResult(
        chunk_id=uuid4(),
        document_id=uuid4(),
        domain=domain,
        breadcrumb=breadcrumb,
        content=content,
        token_count=token_count,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
        source_path=source_path,
    )


class _DeterministicFakeEmbedder:
    """Zero-network deterministic BaseEmbedder test double."""

    def __init__(self, dimensions: int = 1536, model_name: str = "fake-embed-v1") -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.embed_texts_calls: list[list[str]] = []
        self.embed_query_calls: list[str] = []
        self.event_log: Optional[list[str]] = None

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [0.0] * self.dimensions
        idx = int.from_bytes(digest[:2], "big") % self.dimensions
        vec[idx] = 1.0
        return vec

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        batch = list(texts)
        self.embed_texts_calls.append(batch)
        if self.event_log is not None:
            self.event_log.append(f"embed:{len(batch)}")
        return [self._vector_for(t) for t in batch]

    async def embed_query(self, query: str) -> list[float]:
        self.embed_query_calls.append(query)
        return self._vector_for(query)


class _FakeAsyncpgTransaction:
    def __init__(self, conn: "_FakeAsyncpgConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeAsyncpgTransaction":
        self._conn.in_transaction = True
        self._conn.tx_depth += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._conn.in_transaction = False


class _FakeAsyncpgConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.in_transaction = False
        self.tx_depth = 0
        self.operations: list[tuple[str, bool, str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeAsyncpgTransaction:
        return _FakeAsyncpgTransaction(self)

    async def execute(self, sql: str, *args: Any) -> str:
        self.operations.append(("execute", self.in_transaction, sql, args))
        return "SET"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.operations.append(("fetch", self.in_transaction, sql, args))
        return list(self._rows)


class _FakePoolAcquireCtx:
    def __init__(self, conn: _FakeAsyncpgConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeAsyncpgConnection:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeAsyncpgPool:
    def __init__(self, conn: _FakeAsyncpgConnection) -> None:
        self.conn = conn

    def acquire(self) -> _FakePoolAcquireCtx:
        return _FakePoolAcquireCtx(self.conn)


class _InMemoryIngestStore:
    """In-memory mock store implementing get_document_hash & upsert_document_and_chunks."""

    def __init__(self, event_log: Optional[list[str]] = None) -> None:
        self.hashes: dict[tuple[str, str], str] = {}
        self.upserts: list[dict[str, Any]] = []
        self.event_log = event_log

    async def get_document_hash(self, domain: str, source_path: str) -> Optional[str]:
        return self.hashes.get((domain, source_path))

    async def upsert_document_and_chunks(self, **kwargs: Any) -> dict[str, Any]:
        if self.event_log is not None:
            self.event_log.append(f"upsert:{kwargs['source_path']}")
        self.hashes[(kwargs["domain"], kwargs["source_path"])] = kwargs["file_sha256"]
        self.upserts.append(kwargs)
        return kwargs


# ---------------------------------------------------------------------------
# 1. Chunking Tests (Markdown, YAML, Code, Routing, Real 21-file Corpus)
# ---------------------------------------------------------------------------


class TestMarkdownChunker:
    def test_hierarchical_breadcrumbs_and_context_prefix(self) -> None:
        md = (
            "# Root Guide\n\n"
            "Overview paragraph.\n\n"
            "## Section Alpha\n\n"
            "Alpha details.\n\n"
            "### Subsection One\n\n"
            "Deep subsection details.\n\n"
            "## Section Beta\n\n"
            "Beta details after popping subsection.\n"
        )
        chunker = MarkdownChunker(target_tokens=300, max_tokens=500, overlap_tokens=0)
        chunks = chunker.chunk(md, title="FallbackTitle", source_path="library_docs/guide.md")

        breadcrumbs = [c.breadcrumb for c in chunks]
        assert breadcrumbs == [
            "Root Guide",
            "Root Guide > Section Alpha",
            "Root Guide > Section Alpha > Subsection One",
            "Root Guide > Section Beta",
        ]
        for chunk in chunks:
            assert chunk.content.startswith(f"[Context: {chunk.breadcrumb}]\n\n")
            assert chunk.metadata["chunker"] == "markdown"
            assert chunk.metadata["source_path"] == "library_docs/guide.md"

    def test_root_title_used_when_no_h1_present(self) -> None:
        md = "## First Topic\n\nSome text.\n\n### Nested Sub\n\nMore text.\n"
        chunker = MarkdownChunker(target_tokens=300, max_tokens=500, overlap_tokens=0)
        chunks = chunker.chunk(md, title="CustomRoot", source_path="org_standards/std.md")

        assert [c.breadcrumb for c in chunks] == [
            "CustomRoot > First Topic",
            "CustomRoot > First Topic > Nested Sub",
        ]
        assert all(c.content.startswith(f"[Context: {c.breadcrumb}]\n\n") for c in chunks)

    def test_fenced_code_block_never_split_and_ignores_inner_headers(self) -> None:
        code_body = "\n".join(
            [
                "```python",
                "# Not a Markdown H1 header",
                "## Not a Markdown H2 header",
                *[f"value_{i} = compute_step(arg_{i})" for i in range(30)],
                "```",
            ]
        )
        md = f"# Code Safety\n\nBefore code.\n\n{code_body}\n\n~~~sql\nSELECT 1;\n~~~\n\nAfter code.\n"
        chunker = MarkdownChunker(target_tokens=30, max_tokens=45, overlap_tokens=0)
        chunks = chunker.chunk(md, title="Code Safety")

        code_chunks = [c for c in chunks if "```python" in c.content]
        assert len(code_chunks) == 1
        assert "# Not a Markdown H1 header" in code_chunks[0].content
        assert "value_29 = compute_step(arg_29)" in code_chunks[0].content
        assert code_chunks[0].content.strip().endswith("```")
        assert all("Not a Markdown" not in (c.breadcrumb or "") for c in chunks)
        assert any("~~~sql\nSELECT 1;\n~~~" in c.content for c in chunks)

    def test_markdown_table_never_split_mid_table(self) -> None:
        table_rows = [
            "| Specialist | Domain | Top K | Max Tokens |",
            "|---|---|---:|---:|",
            *[f"| role_{i} | domain_{i} | {i} | {100 * i} |" for i in range(1, 15)],
        ]
        table_md = "\n".join(table_rows)
        md = f"# Budgets\n\nIntro line.\n\n{table_md}\n\nClosing note.\n"
        chunker = MarkdownChunker(target_tokens=30, max_tokens=45, overlap_tokens=0)
        chunks = chunker.chunk(md, title="Budgets")

        table_chunks = [c for c in chunks if "| Specialist | Domain |" in c.content]
        assert len(table_chunks) == 1
        for expected_row in table_rows:
            assert expected_row in table_chunks[0].content

    def test_prose_splitting_respects_max_tokens_and_overlap(self) -> None:
        sentences = [
            f"Sentence number {i} documents deterministic behavior in the DDLC pipeline."
            for i in range(1, 16)
        ]
        md = "# Long Prose\n\n" + " ".join(sentences)
        chunker = MarkdownChunker(target_tokens=45, max_tokens=65, overlap_tokens=15)
        chunks = chunker.chunk(md, title="Long Prose")

        assert len(chunks) >= 3
        for chunk in chunks:
            assert chunk.token_count <= 65
            assert chunk.content.startswith("[Context: Long Prose]\n\n")

        # Verify consecutive chunks share overlapping sentence content
        overlap_found = any(
            any(sent in chunks[idx].content and sent in chunks[idx + 1].content for sent in sentences)
            for idx in range(len(chunks) - 1)
        )
        assert overlap_found

    def test_frontmatter_preserved_intact(self) -> None:
        md = (
            "---\n"
            "adr_id: ADR-999\n"
            "status: Approved\n"
            "---\n\n"
            "# Decision Record\n\n"
            "Rationale body.\n"
        )
        chunker = MarkdownChunker(target_tokens=300, max_tokens=500, overlap_tokens=0)
        chunks = chunker.chunk(md, title="ADR-999")

        assert len(chunks) == 2
        assert "adr_id: ADR-999\nstatus: Approved" in chunks[0].content
        assert chunks[0].content.count("---") == 2
        assert chunks[1].breadcrumb == "Decision Record"


class TestYamlChunker:
    def test_top_level_key_slicing_preserves_parent_context_wrapper(self) -> None:
        doc = {
            "agent": {"name": "orders_etl_agent"},
            "metadata": {
                "owner": "data-platform",
                "description": "Long description " + " ".join(["word"] * 25),
                "compliance": "SOC2 and PII masking required across staging layers.",
            },
            "runtime": {
                "engine": "langgraph",
                "checkpointer": "postgres",
                "timeout_seconds": 180,
            },
        }
        yaml_text = yaml.safe_dump(doc, sort_keys=False)
        chunker = YamlChunker(target_tokens=30, max_tokens=50)
        chunks = chunker.chunk(yaml_text, source_path="contracts/orders.yaml")

        assert len(chunks) >= 2
        for chunk in chunks:
            assert chunk.breadcrumb is not None
            assert chunk.breadcrumb.startswith("orders_etl_agent > ")
            parsed = yaml.safe_load(chunk.content)
            assert isinstance(parsed, dict)

        # Nested split of 'metadata' must retain 'metadata' parent key wrapper
        meta_chunks = [c for c in chunks if "metadata" in yaml.safe_load(c.content)]
        assert len(meta_chunks) >= 1
        assert all(isinstance(yaml.safe_load(c.content)["metadata"], dict) for c in meta_chunks)

    def test_large_list_splitting_preserves_parent_key(self) -> None:
        doc = {
            "schema_name": "ddlc_shared_metadata",
            "stages": [
                {
                    "id": f"stage_{i}",
                    "type": "deterministic_tool",
                    "description": f"Stage {i} executes deterministic validation step {i}.",
                }
                for i in range(1, 9)
            ],
        }
        yaml_text = yaml.safe_dump(doc, sort_keys=False)
        chunker = YamlChunker(target_tokens=35, max_tokens=55)
        chunks = chunker.chunk(yaml_text, source_path="contracts/stages.yaml")

        assert len(chunks) >= 3
        stage_chunks = [c for c in chunks if "stages" in (yaml.safe_load(c.content) or {})]
        assert len(stage_chunks) >= 2
        for chunk in stage_chunks:
            loaded = yaml.safe_load(chunk.content)
            assert isinstance(loaded["stages"], list)
            assert len(loaded["stages"]) >= 1

    def test_invalid_yaml_fallback_without_crash(self) -> None:
        bad_yaml = (
            "agent:\n"
            "  name: broken\n"
            "stages:\n"
            "  - [unclosed bracket\n"
            "metadata:\n"
            "  owner: platform\n"
        )
        chunker = YamlChunker(target_tokens=20, max_tokens=40)
        chunks = chunker.chunk(bad_yaml, title="broken_spec", source_path="broken.yaml")

        assert len(chunks) >= 1
        assert all(c.metadata.get("fallback") is True for c in chunks)
        assert all(c.breadcrumb == "broken_spec (raw)" for c in chunks)


class TestCodeChunker:
    def test_ast_slicing_at_def_async_def_and_class_boundaries(self) -> None:
        source = '''
from __future__ import annotations

CONST_LIMIT = 10

@decorator_one("arg")
def sync_helper(x: int) -> int:
    """Multiply x by two."""
    return x * 2

@async_retry(attempts=3)
async def fetch_async(key: str) -> str:
    """Fetch value asynchronously."""
    return f"val:{key}"

class SmallMapper:
    """Maps payloads."""

    def run(self, item: str) -> str:
        return item.upper()
'''
        chunker = CodeChunker(target_tokens=200, max_tokens=350)
        chunks = chunker.chunk(source, title="sample_mod", source_path="src/sample_mod.py")

        assert [c.breadcrumb for c in chunks] == [
            "sample_mod > module",
            "sample_mod > sync_helper",
            "sample_mod > fetch_async",
            "sample_mod > SmallMapper",
        ]
        assert '@decorator_one("arg")' in chunks[1].content
        assert '"""Multiply x by two."""' in chunks[1].content
        assert "@async_retry(attempts=3)" in chunks[2].content
        assert "async def fetch_async" in chunks[2].content
        assert "class SmallMapper:" in chunks[3].content

    def test_never_splits_inside_oversized_function(self) -> None:
        body_lines = "\n".join(f"    total += {i}" for i in range(25))
        source = (
            "def large_compute(seed: int) -> int:\n"
            '    """Oversized function that must remain intact."""\n'
            "    total = seed\n"
            f"{body_lines}\n"
            "    return total\n"
        )
        chunker = CodeChunker(target_tokens=20, max_tokens=35)
        chunks = chunker.chunk(source, title="big_fn")

        assert len(chunks) == 1
        assert chunks[0].breadcrumb == "big_fn > large_compute"
        assert "def large_compute(seed: int) -> int:" in chunks[0].content
        assert "total += 24" in chunks[0].content
        assert "return total" in chunks[0].content

    def test_large_class_splits_by_methods_preserving_class_header_and_docstring(self) -> None:
        source = '''
@dataclass_like
class PipelineOrchestrator:
    """Coordinates deterministic pipeline stages."""

    def stage_discover(self, schema: str) -> list[str]:
        """Discover tables in target schema."""
        items = [schema, "public", "staging"]
        return [item.strip().lower() for item in items]

    async def stage_validate(self, tables: list[str]) -> bool:
        """Validate discovered tables against contracts."""
        checked = [tbl for tbl in tables if tbl]
        return len(checked) > 0

    def stage_publish(self, branch: str) -> str:
        """Publish deterministic artifacts to git branch."""
        return f"refs/heads/{branch}"
'''
        chunker = CodeChunker(target_tokens=45, max_tokens=70)
        chunks = chunker.chunk(source, title="orchestrator")

        assert len(chunks) >= 2
        for chunk in chunks:
            assert chunk.metadata["node_type"] == "class_slice"
            assert "@dataclass_like" in chunk.content
            assert "class PipelineOrchestrator:" in chunk.content
            assert '"""Coordinates deterministic pipeline stages."""' in chunk.content

        joined = "\n".join(c.content for c in chunks)
        assert "def stage_discover" in joined
        assert "async def stage_validate" in joined
        assert "def stage_publish" in joined

    def test_syntax_error_falls_back_gracefully(self) -> None:
        broken_py = (
            "def broken_func(:\n"
            "    x = [1, 2, 3\n"
            "    return x\n"
        )
        chunker = CodeChunker(target_tokens=20, max_tokens=35)
        chunks = chunker.chunk(broken_py, title="broken_mod", source_path="broken.py")

        assert len(chunks) >= 1
        assert all(c.metadata.get("fallback") is True for c in chunks)
        assert all(c.breadcrumb == "broken_mod (raw)" for c in chunks)


class TestChunkFileRoutingAndCorpus:
    def test_chunk_file_routes_by_extension(self) -> None:
        md_chunks = chunk_file("docs/guide.md", "# Title\n\nBody text.", title="Guide")
        yaml_chunks = chunk_file("cfg/app.yaml", "name: studio\nversion: 1\n")
        py_chunks = chunk_file("mod/calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n")

        assert md_chunks[0].metadata["chunker"] == "markdown"
        assert yaml_chunks[0].metadata["chunker"] == "yaml"
        assert py_chunks[0].metadata["chunker"] == "code"

    def test_all_21_real_day0_knowledge_files_chunk_cleanly(self) -> None:
        files = sorted(
            p for p in KNOWLEDGE_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")
        )
        assert len(files) == 21

        for path in files:
            text = path.read_text(encoding="utf-8")
            chunks = chunk_file(path, text, title=path.stem)
            assert len(chunks) >= 1, f"{path} produced zero chunks"
            for idx, chunk in enumerate(chunks):
                assert chunk.chunk_index == idx
                assert chunk.content.strip() != ""
                assert chunk.token_count > 0
                assert chunk.breadcrumb
                assert len(chunk.chunk_sha256) == 64
                assert chunk.chunk_sha256 == compute_sha256(chunk.content)


# ---------------------------------------------------------------------------
# 2. Models & XML Projection Tests
# ---------------------------------------------------------------------------


class TestModelsAndXmlProjection:
    def test_validate_domain_accepts_authorized_and_rejects_invalid(self) -> None:
        for domain in ALLOWED_DOMAINS:
            assert validate_domain(domain) == domain

        with pytest.raises(ValueError, match="Invalid RAG domain"):
            validate_domain("unknown_domain")

        with pytest.raises(ValueError, match="Invalid RAG domain"):
            validate_domain("")

    def test_empty_slice_renders_count_zero(self) -> None:
        empty = RAGContextSlice(
            domain="past_decisions",
            query='What "ADRs" & <patterns> exist?',
            chunks=[],
            total_tokens=0,
        )
        xml_str = empty.to_xml()
        assert 'count="0"' in xml_str
        assert xml_str.startswith('<rag_context domain="past_decisions"')
        assert xml_str.endswith("/>")

        elem = ET.fromstring(xml_str)
        assert elem.tag == "rag_context"
        assert elem.attrib["domain"] == "past_decisions"
        assert elem.attrib["query"] == 'What "ADRs" & <patterns> exist?'
        assert elem.attrib["count"] == "0"

    def test_special_characters_xml_escaped_and_scores_formatted(self) -> None:
        c1 = _make_search_result(
            domain="library_docs",
            content="Query uses embedding <=> $2 & Jinja2 {% if a < b %} \"ok\" 'yes'",
            rrf_score=0.03124,
            rerank_score=0.9412,
            breadcrumb='Section <"A"> & \'B\'',
            source_path="library_docs/pgvector.md",
        )
        c2 = _make_search_result(
            domain="library_docs",
            content="Fallback RRF score formatting check.",
            rrf_score=0.01639,
            rerank_score=None,
            breadcrumb=None,
            source_path=None,
        )
        ctx = RAGContextSlice(
            domain="library_docs",
            query='search "pgvector" <=> & <tags>',
            chunks=[c1, c2],
            total_tokens=40,
        )
        xml_str = ctx.to_xml()

        assert "embedding &lt;=&gt; $2 &amp; Jinja2 {% if a &lt; b %}" in xml_str
        assert 'score="0.94"' in xml_str
        assert 'score="0.0164"' in xml_str
        assert 'source="library_docs/pgvector.md"' in xml_str
        assert 'source="unknown"' in xml_str
        assert 'section="General"' in xml_str

        root = ET.fromstring(xml_str)
        assert root.attrib["count"] == "2"
        assert root.attrib["query"] == 'search "pgvector" <=> & <tags>'
        chunk_elems = root.findall("chunk")
        assert len(chunk_elems) == 2
        assert chunk_elems[0].attrib["section"] == 'Section <"A"> & \'B\''
        assert chunk_elems[0].attrib["source"] == "library_docs/pgvector.md"
        assert chunk_elems[0].attrib["score"] == "0.94"
        assert chunk_elems[1].attrib["section"] == "General"
        assert chunk_elems[1].attrib["source"] == "unknown"
        assert chunk_elems[1].attrib["score"] == "0.0164"


# ---------------------------------------------------------------------------
# 3. RRF & Reranker Circuit-Breaker Tests
# ---------------------------------------------------------------------------


class TestRRFAndReranker:
    def test_compute_rrf_score_math_and_validation(self) -> None:
        assert compute_rrf_score(dense_rank=1, sparse_rank=1, k=60) == pytest.approx(2.0 / 61.0)
        assert compute_rrf_score(dense_rank=2, sparse_rank=None, k=60) == pytest.approx(1.0 / 62.0)
        assert compute_rrf_score(dense_rank=None, sparse_rank=3, k=60) == pytest.approx(1.0 / 63.0)
        assert compute_rrf_score(dense_rank=None, sparse_rank=None, k=60) == 0.0

        with pytest.raises(ValueError, match="non-negative"):
            compute_rrf_score(dense_rank=1, k=-1)
        with pytest.raises(ValueError, match="dense_rank"):
            compute_rrf_score(dense_rank=0)
        with pytest.raises(ValueError, match="sparse_rank"):
            compute_rrf_score(sparse_rank=0)

    def test_fuse_ranked_lists_and_duplicate_handling(self) -> None:
        a = _make_search_result(content="A")
        b = _make_search_result(content="B")
        c = _make_search_result(content="C")

        # Duplicate 'a' in dense list must retain rank 1 (not overwritten by rank 3)
        # Scores: a = 1/61 + 1/62, c = 1/61, b = 1/62 -> order is [a, c, b]
        dense = [a, b, a]
        sparse = [c, a]
        fused = fuse_ranked_lists(dense, sparse, k=60)

        assert [item.chunk_id for item, _ in fused] == [a.chunk_id, c.chunk_id, b.chunk_id]
        assert fused[0][1] == pytest.approx((1.0 / 61.0) + (1.0 / 62.0))
        assert fused[1][1] == pytest.approx(1.0 / 61.0)
        assert fused[2][1] == pytest.approx(1.0 / 62.0)

        assert fuse_ranked_lists(dense, sparse, k=60, top_k=0) == []
        with pytest.raises(ValueError, match="non-negative"):
            fuse_ranked_lists(dense, sparse, k=-5)

        updated = fuse_search_results(dense, sparse, k=60, top_k=2)
        assert len(updated) == 2
        assert updated[0].rrf_score == pytest.approx((1.0 / 61.0) + (1.0 / 62.0))

    async def test_flashrank_reranker_reorders_and_attaches_scores(self) -> None:
        c_low = _make_search_result(content="low", rrf_score=0.05)
        c_high = _make_search_result(content="high", rrf_score=0.01)

        class _MockFastRanker:
            def rerank(self, request: Any) -> list[dict[str, Any]]:
                assert request.query == "test query"
                return [
                    {"id": str(c_high.chunk_id), "score": 0.95},
                    {"id": str(c_low.chunk_id), "score": 0.12},
                ]

        reranker = FlashRankReranker(
            model_name="mock-model",
            timeout_ms=500,
            enabled=True,
            fallback_to_rrf=True,
            ranker=_MockFastRanker(),
        )
        out = await reranker.rerank("test query", [c_low, c_high])
        assert [c.chunk_id for c in out] == [c_high.chunk_id, c_low.chunk_id]
        assert out[0].rerank_score == pytest.approx(0.95)
        assert out[1].rerank_score == pytest.approx(0.12)

    async def test_flashrank_timeout_circuit_breaker_falls_back_to_rrf(self) -> None:
        c1 = _make_search_result(content="lower rrf", rrf_score=0.01)
        c2 = _make_search_result(content="higher rrf", rrf_score=0.04)

        class _SlowRanker:
            def rerank(self, request: Any) -> list[dict[str, Any]]:
                time.sleep(0.25)
                return [{"id": str(c1.chunk_id), "score": 0.99}]

        reranker = FlashRankReranker(
            model_name="mock-model",
            timeout_ms=15,
            enabled=True,
            fallback_to_rrf=True,
            ranker=_SlowRanker(),
        )
        out = await reranker.rerank("slow query", [c1, c2])
        assert [c.chunk_id for c in out] == [c2.chunk_id, c1.chunk_id]
        assert all(c.rerank_score is None for c in out)

    async def test_flashrank_disabled_and_error_fall_back_to_rrf(self) -> None:
        c1 = _make_search_result(content="first", rrf_score=0.01)
        c2 = _make_search_result(content="second", rrf_score=0.03)

        class _BrokenRanker:
            def __init__(self) -> None:
                self.called = False

            def rerank(self, request: Any) -> list[dict[str, Any]]:
                self.called = True
                raise RuntimeError("ONNX failure")

        disabled_ranker = _BrokenRanker()
        reranker_off = FlashRankReranker(
            model_name="mock-model",
            timeout_ms=150,
            enabled=False,
            fallback_to_rrf=True,
            ranker=disabled_ranker,
        )
        out_off = await reranker_off.rerank("q", [c1, c2])
        assert not disabled_ranker.called
        assert [c.chunk_id for c in out_off] == [c2.chunk_id, c1.chunk_id]

        failing_ranker = _BrokenRanker()
        reranker_err = FlashRankReranker(
            model_name="mock-model",
            timeout_ms=150,
            enabled=True,
            fallback_to_rrf=True,
            ranker=failing_ranker,
        )
        out_err = await reranker_err.rerank("q", [c1, c2])
        assert failing_ranker.called
        assert [c.chunk_id for c in out_err] == [c2.chunk_id, c1.chunk_id]

        reranker_strict = FlashRankReranker(
            model_name="mock-model",
            timeout_ms=150,
            enabled=True,
            fallback_to_rrf=False,
            ranker=failing_ranker,
        )
        with pytest.raises(RuntimeError, match="ONNX failure"):
            await reranker_strict.rerank("q", [c1, c2])


# ---------------------------------------------------------------------------
# 4. HybridRAGRetriever Unit Tests
# ---------------------------------------------------------------------------


class TestHybridRAGRetriever:
    async def test_rejects_invalid_domain_without_touching_db(self) -> None:
        conn = _FakeAsyncpgConnection(rows=[])
        pool = _FakeAsyncpgPool(conn)
        embedder = _DeterministicFakeEmbedder()
        retriever = HybridRAGRetriever(
            db_pool=pool,
            embedder=embedder,
            reranker=FlashRankReranker(enabled=False),
        )

        with pytest.raises(ValueError, match="Invalid RAG domain"):
            await retriever.search(domain="nonexistent_domain", query="anything")

        assert conn.operations == []
        assert embedder.embed_query_calls == []

    async def test_sets_iterative_scan_in_transaction_before_separate_cte_fetch(self) -> None:
        doc_id = uuid4()
        rows = [
            {
                "chunk_id": uuid4(),
                "document_id": doc_id,
                "domain": "library_docs",
                "source_path": "library_docs/sqlglot.md",
                "breadcrumb": "SQLGlot > AST",
                "content": "Use sqlglot.parse_one for dialect-safe validation.",
                "token_count": 12,
                "rrf_score": 0.032,
            }
        ]
        conn = _FakeAsyncpgConnection(rows=rows)
        pool = _FakeAsyncpgPool(conn)
        embedder = _DeterministicFakeEmbedder()
        retriever = HybridRAGRetriever(
            db_pool=pool,
            embedder=embedder,
            reranker=FlashRankReranker(enabled=False),
            rrf_k=60,
            initial_candidates=20,
            schema_name="rag_knowledge",
        )

        res = await retriever.search(domain="library_docs", query="parse_one", top_k=3, token_ceiling=500)
        assert len(res.chunks) == 1
        assert res.total_tokens == 12

        assert len(conn.operations) == 2
        op0_kind, op0_in_tx, op0_sql, _ = conn.operations[0]
        op1_kind, op1_in_tx, op1_sql, op1_args = conn.operations[1]

        assert op0_kind == "execute"
        assert op0_in_tx is True
        assert op0_sql.strip() == "SET LOCAL hnsw.iterative_scan = 'strict_order'"

        assert op1_kind == "fetch"
        assert op1_in_tx is True
        assert "SET LOCAL" not in op1_sql
        assert op1_sql.count("WHERE domain = $1") == 2
        assert op1_args[0] == "library_docs"
        assert op1_args[2] == "parse_one"
        assert op1_args[3] == 60
        assert op1_args[4] == 20

    async def test_empty_db_result_returns_count_zero_xml(self) -> None:
        conn = _FakeAsyncpgConnection(rows=[])
        retriever = HybridRAGRetriever(
            db_pool=_FakeAsyncpgPool(conn),
            embedder=_DeterministicFakeEmbedder(),
            reranker=FlashRankReranker(enabled=False),
        )
        res = await retriever.search(domain="past_decisions", query="missing decision")
        assert res.chunks == []
        assert res.total_tokens == 0
        assert 'count="0"' in res.to_xml()

    async def test_enforces_top_k_and_token_ceiling_budget_clamping(self) -> None:
        doc_id = uuid4()
        rows = [
            {
                "chunk_id": uuid4(),
                "document_id": doc_id,
                "domain": "org_standards",
                "source_path": f"org_standards/doc_{i}.md",
                "breadcrumb": f"Sec {i}",
                "content": f"Chunk body {i}",
                "token_count": 120,
                "rrf_score": 0.05 - i * 0.005,
            }
            for i in range(5)
        ]
        retriever = HybridRAGRetriever(
            db_pool=_FakeAsyncpgPool(_FakeAsyncpgConnection(rows=rows)),
            embedder=_DeterministicFakeEmbedder(),
            reranker=FlashRankReranker(enabled=False),
        )

        # Token ceiling clamping: 2 * 120 = 240 <= 250, 3rd would hit 360 > 250
        res_budget = await retriever.search(
            domain="org_standards",
            query="standards",
            top_k=4,
            token_ceiling=250,
        )
        assert len(res_budget.chunks) == 2
        assert res_budget.total_tokens == 240

        # top_k clamping even when token ceiling permits more
        res_topk = await retriever.search(
            domain="org_standards",
            query="standards",
            top_k=1,
            token_ceiling=1000,
        )
        assert len(res_topk.chunks) == 1
        assert res_topk.total_tokens == 120

    async def test_telemetry_emission_and_non_blocking_callback_failure(self) -> None:
        rows = [
            {
                "chunk_id": uuid4(),
                "document_id": uuid4(),
                "domain": "code_conventions",
                "source_path": "code_conventions/py.md",
                "breadcrumb": "Style",
                "content": "Use Python 3.12 typing.",
                "token_count": 15,
                "rrf_score": 0.03,
            }
        ]
        captured: list[dict[str, Any]] = []
        retriever_ok = HybridRAGRetriever(
            db_pool=_FakeAsyncpgPool(_FakeAsyncpgConnection(rows=rows)),
            embedder=_DeterministicFakeEmbedder(),
            reranker=FlashRankReranker(enabled=False),
            on_telemetry=captured.append,
        )
        res_ok = await retriever_ok.search(domain="code_conventions", query="typing")
        assert len(res_ok.chunks) == 1
        assert len(captured) == 1
        ev = captured[0]
        assert ev["event"] == "rag_retrieval"
        assert ev["domain"] == "code_conventions"
        assert ev["candidate_count"] == 1
        assert ev["total_tokens_injected"] == 15
        assert set(ev["latency_ms"].keys()) == {"embedding_ms", "sql_cte_ms", "rerank_ms", "total_ms"}

        async def _exploding_telemetry(event: dict[str, Any]) -> None:
            raise RuntimeError("Telemetry sink unavailable")

        retriever_fail = HybridRAGRetriever(
            db_pool=_FakeAsyncpgPool(_FakeAsyncpgConnection(rows=rows)),
            embedder=_DeterministicFakeEmbedder(),
            reranker=FlashRankReranker(enabled=False),
            on_telemetry=_exploding_telemetry,
        )
        res_fail = await retriever_fail.search(domain="code_conventions", query="typing")
        assert len(res_fail.chunks) == 1


# ---------------------------------------------------------------------------
# 5. Specialist Facades Tests
# ---------------------------------------------------------------------------


class _RecordingRetriever:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        domain: str,
        query: str,
        top_k: int = 3,
        token_ceiling: int = 1000,
    ) -> RAGContextSlice:
        self.calls.append(
            {
                "domain": domain,
                "query": query,
                "top_k": top_k,
                "token_ceiling": token_ceiling,
            }
        )
        return RAGContextSlice(domain=domain, query=query, chunks=[], total_tokens=0)


class TestSpecialistFacades:
    async def test_researcher_architect_and_builder_domain_scoping_and_budgets(self) -> None:
        cfg_budgets = get_studio_config().rag.budgets
        assert (cfg_budgets["researcher"].top_k, cfg_budgets["researcher"].max_tokens) == (3, 1000)
        assert (cfg_budgets["architect"].top_k, cfg_budgets["architect"].max_tokens) == (4, 1400)
        assert (cfg_budgets["builder"].top_k, cfg_budgets["builder"].max_tokens) == (3, 1000)

        rec = _RecordingRetriever()
        researcher = ResearcherRAGFacade(rec)  # type: ignore[arg-type]
        architect = ArchitectRAGFacade(rec)  # type: ignore[arg-type]
        builder = BuilderRAGFacade(rec)  # type: ignore[arg-type]

        xml_r1 = await researcher.query_tool_docs("dbt-core", "run command")
        xml_r2 = await researcher.query_org_standards("pii governance")
        assert 'domain="library_docs"' in xml_r1
        assert 'domain="org_standards"' in xml_r2

        xml_a1 = await architect.query_patterns("staged gated pipeline")
        xml_a2 = await architect.query_past_decisions("snowflake lineage")
        xml_a3 = await architect.query_org_standards("naming standards")
        xml_a4 = await architect.query_component_docs("schema_inspector", "introspect")
        assert 'domain="pattern_catalog"' in xml_a1
        assert 'domain="past_decisions"' in xml_a2
        assert 'domain="org_standards"' in xml_a3
        assert 'domain="component_docs"' in xml_a4

        xml_b1 = await builder.query_component_docs("git_deliverer", "create pr")
        xml_b2 = await builder.query_code_conventions("pytest async")
        assert 'domain="component_docs"' in xml_b1
        assert 'domain="code_conventions"' in xml_b2

        assert rec.calls == [
            {
                "domain": "library_docs",
                "query": "dbt-core: run command",
                "top_k": 3,
                "token_ceiling": 1000,
            },
            {
                "domain": "org_standards",
                "query": "pii governance",
                "top_k": 3,
                "token_ceiling": 1000,
            },
            {
                "domain": "pattern_catalog",
                "query": "staged gated pipeline",
                "top_k": 4,
                "token_ceiling": 1400,
            },
            {
                "domain": "past_decisions",
                "query": "snowflake lineage",
                "top_k": 4,
                "token_ceiling": 1400,
            },
            {
                "domain": "org_standards",
                "query": "naming standards",
                "top_k": 4,
                "token_ceiling": 1400,
            },
            {
                "domain": "component_docs",
                "query": "schema_inspector: introspect",
                "top_k": 4,
                "token_ceiling": 1400,
            },
            {
                "domain": "component_docs",
                "query": "git_deliverer: create pr",
                "top_k": 3,
                "token_ceiling": 1000,
            },
            {
                "domain": "code_conventions",
                "query": "pytest async",
                "top_k": 3,
                "token_ceiling": 1000,
            },
        ]


# ---------------------------------------------------------------------------
# 6. Embedder, Ingestion Pipeline & Typer CLI Tests
# ---------------------------------------------------------------------------


class TestLiteLLMEmbedder:
    async def test_fails_fast_without_openai_api_key_and_does_not_call_litellm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            "src.rag.embeddings.embedder.get_env_settings",
            lambda: RuntimeEnvSettings(_env_file=tmp_path / ".missing.env"),
        )

        called = False

        def _fail_if_imported() -> Any:
            nonlocal called
            called = True
            raise AssertionError("litellm should not be imported or called when API key is missing")

        monkeypatch.setattr("src.rag.embeddings.embedder._import_litellm", _fail_if_imported)

        embedder = LiteLLMEmbedder(
            model_name="text-embedding-3-small",
            dimensions=1536,
            batch_size=4,
            max_retries=1,
            provider="openai",
        )
        assert await embedder.embed_texts([]) == []

        with pytest.raises(EmbeddingConfigError, match="OPENAI_API_KEY"):
            await embedder.embed_texts(["hello world"])
        assert called is False

    async def test_batches_requests_and_sorts_embeddings_by_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-for-unit-test")

        calls: list[list[str]] = []

        class _FakeLiteLLM:
            AuthenticationError = RuntimeError

            async def aembedding(
                self,
                *,
                model: str,
                input: list[str],
                dimensions: int,
            ) -> Any:
                calls.append(list(input))
                # Return items in reverse order with explicit index to verify sorting
                data = [
                    {
                        "index": idx,
                        "embedding": [float(len(text)), float(idx)],
                    }
                    for idx, text in reversed(list(enumerate(input)))
                ]
                return SimpleNamespace(data=data)

        monkeypatch.setattr("src.rag.embeddings.embedder._import_litellm", lambda: _FakeLiteLLM())

        embedder = LiteLLMEmbedder(
            model_name="text-embedding-3-small",
            dimensions=2,
            batch_size=2,
            max_retries=2,
            provider="openai",
        )
        texts = ["a", "bb", "ccc", "dddd", "eeeee"]
        vectors = await embedder.embed_texts(texts)

        assert calls == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
        assert vectors == [
            [1.0, 0.0],
            [2.0, 1.0],
            [3.0, 0.0],
            [4.0, 1.0],
            [5.0, 0.0],
        ]

        q_vec = await embedder.embed_query("query!")
        assert q_vec == [6.0, 0.0]

    async def test_fails_fast_without_openrouter_api_key_and_loads_from_env_settings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OR_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_BASE", raising=False)
        monkeypatch.delenv("OR_APP_NAME", raising=False)
        monkeypatch.setattr(
            "src.rag.embeddings.embedder.get_env_settings",
            lambda: RuntimeEnvSettings(_env_file=tmp_path / ".missing.env"),
        )

        embedder = LiteLLMEmbedder(
            model_name="qwen/qwen3-embedding-8b",
            dimensions=1536,
            batch_size=4,
            max_retries=1,
            provider="openrouter",
        )
        assert embedder.model_name == "openrouter/qwen/qwen3-embedding-8b"

        with pytest.raises(EmbeddingConfigError, match="OPENROUTER_API_KEY"):
            await embedder.embed_texts(["hello openrouter"])

        # Now supply credentials via RuntimeEnvSettings (.env) and verify propagation
        env_file = tmp_path / ".env"
        env_file.write_text(
            "OPENROUTER_API_KEY=sk-or-v1-test-key\n"
            "OPENROUTER_API_BASE=https://openrouter.ai/api/v1\n"
            "OR_APP_NAME=ddlc-agent-studio\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "src.rag.embeddings.embedder.get_env_settings",
            lambda: RuntimeEnvSettings(_env_file=env_file),
        )

        recorded_calls: list[dict[str, Any]] = []

        class _FakeOpenRouterLiteLLM:
            AuthenticationError = RuntimeError

            async def aembedding(
                self,
                *,
                model: str,
                input: list[str],
                dimensions: int,
            ) -> Any:
                import os

                recorded_calls.append(
                    {
                        "model": model,
                        "input": list(input),
                        "dimensions": dimensions,
                        "api_key": os.environ.get("OPENROUTER_API_KEY"),
                        "api_base": os.environ.get("OPENROUTER_API_BASE"),
                        "app_name": os.environ.get("OR_APP_NAME"),
                    }
                )
                return SimpleNamespace(
                    data=[{"index": idx, "embedding": [0.25] * 3} for idx in range(len(input))]
                )

        monkeypatch.setattr(
            "src.rag.embeddings.embedder._import_litellm", lambda: _FakeOpenRouterLiteLLM()
        )

        vecs = await embedder.embed_texts(["open source llm test"])
        assert vecs == [[0.25, 0.25, 0.25]]
        assert recorded_calls == [
            {
                "model": "openrouter/qwen/qwen3-embedding-8b",
                "input": ["open source llm test"],
                "dimensions": 1536,
                "api_key": "sk-or-v1-test-key",
                "api_base": "https://openrouter.ai/api/v1",
                "app_name": "ddlc-agent-studio",
            }
        ]


class TestIngestionPipelineAndCLI:
    async def test_dry_run_scans_all_21_real_knowledge_files(self) -> None:
        summary = await ingest_knowledge_corpus(knowledge_dir=KNOWLEDGE_DIR, dry_run=True)
        assert summary.dry_run is True
        assert summary.files_scanned == 21
        assert summary.files_ingested == 0
        assert summary.files_failed == 0
        assert summary.total_chunks >= 21
        assert set(summary.domains_processed) == set(ALLOWED_DOMAINS)
        assert all(r.status == "dry_run" for r in summary.results)

    async def test_sha256_deduplication_incremental_reingest_and_two_phase_ordering(
        self,
        tmp_path: Path,
    ) -> None:
        lib_dir = tmp_path / "library_docs"
        std_dir = tmp_path / "org_standards"
        lib_dir.mkdir()
        std_dir.mkdir()

        doc1 = lib_dir / "tool_a.md"
        doc2 = std_dir / "rule_b.md"
        doc1.write_text("# Tool A\n\nInitial tool documentation.", encoding="utf-8")
        doc2.write_text("# Rule B\n\nInitial org standard.", encoding="utf-8")

        events: list[str] = []
        store = _InMemoryIngestStore(event_log=events)
        embedder = _DeterministicFakeEmbedder()
        embedder.event_log = events

        # Run 1: Both files ingested; embedding always precedes upsert (Two-Phase Commit ordering)
        s1 = await ingest_knowledge_corpus(
            knowledge_dir=tmp_path,
            domains=["library_docs", "org_standards"],
            store=store,
            embedder=embedder,
        )
        assert s1.files_scanned == 2
        assert s1.files_ingested == 2
        assert s1.files_skipped == 0
        assert len(embedder.embed_texts_calls) == 2
        assert [e.split(":", 1)[0] for e in events] == ["embed", "upsert", "embed", "upsert"]

        # Run 2: Unchanged SHA-256 -> zero embed calls, all skipped
        embedder.embed_texts_calls.clear()
        events.clear()
        s2 = await ingest_knowledge_corpus(
            knowledge_dir=tmp_path,
            domains=["library_docs", "org_standards"],
            store=store,
            embedder=embedder,
        )
        assert s2.files_scanned == 2
        assert s2.files_ingested == 0
        assert s2.files_skipped == 2
        assert embedder.embed_texts_calls == []
        assert events == []

        # Run 3: Modify only doc1 -> only doc1 re-embedded and upserted
        doc1.write_text("# Tool A\n\nUpdated tool documentation v2.", encoding="utf-8")
        s3 = await ingest_knowledge_corpus(
            knowledge_dir=tmp_path,
            domains=["library_docs", "org_standards"],
            store=store,
            embedder=embedder,
        )
        assert s3.files_scanned == 2
        assert s3.files_ingested == 1
        assert s3.files_skipped == 1
        assert len(embedder.embed_texts_calls) == 1
        assert [r.source_path for r in s3.results if r.status == "ingested"] == [
            "library_docs/tool_a.md"
        ]

    async def test_two_phase_abort_skips_upsert_when_embedder_fails(
        self,
        tmp_path: Path,
    ) -> None:
        lib_dir = tmp_path / "library_docs"
        lib_dir.mkdir()
        (lib_dir / "tool.md").write_text("# Tool\n\nSome content.", encoding="utf-8")

        store = _InMemoryIngestStore()

        class _FailingEmbedder(_DeterministicFakeEmbedder):
            async def embed_texts(self, texts: list[str]) -> list[list[float]]:
                raise EmbeddingConfigError("Simulated missing key")

        with pytest.raises(EmbeddingConfigError, match="Simulated missing key"):
            await ingest_knowledge_corpus(
                knowledge_dir=tmp_path,
                domains=["library_docs"],
                store=store,
                embedder=_FailingEmbedder(),
            )
        assert store.upserts == []

    def test_typer_cli_dry_run_and_missing_api_key_exit_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = CliRunner()
        monkeypatch.chdir(REPO_ROOT)

        res_dry = runner.invoke(cli_app, ["rag", "ingest", "--dry-run"])
        assert res_dry.exit_code == 0, res_dry.output
        assert "RAG Corpus Dry Run Summary" in res_dry.output
        assert "21" in res_dry.output

        # Create a 1-file temp knowledge folder and mock store so CLI hits LiteLLMEmbedder
        kb = tmp_path / "kb"
        (kb / "library_docs").mkdir(parents=True)
        (kb / "library_docs" / "sample.md").write_text("# Sample\n\nBody.", encoding="utf-8")

        class _DummyCliStore(_InMemoryIngestStore):
            def __init__(self, db_uri: Optional[str] = None) -> None:
                super().__init__()

            async def close(self) -> None:
                return None

        monkeypatch.setattr("src.rag.store.pgvector_store.PgVectorRAGStore", _DummyCliStore)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OR_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            "src.rag.embeddings.embedder.get_env_settings",
            lambda: RuntimeEnvSettings(_env_file=tmp_path / ".missing.env"),
        )

        res_err = runner.invoke(
            cli_app,
            ["rag", "ingest", "--knowledge-dir", str(kb), "--domain", "library_docs"],
        )
        assert res_err.exit_code == 1
        assert "Embedding Configuration Error" in res_err.output
        assert "OPENROUTER_API_KEY" in res_err.output
