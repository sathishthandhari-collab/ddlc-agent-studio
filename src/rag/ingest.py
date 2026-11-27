"""Knowledge corpus ingestion pipeline and `ddlc rag` Typer CLI."""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

import typer
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.core.settings import get_studio_config
from src.rag.chunking.base import chunk_file, compute_sha256
from src.rag.store.models import ALLOWED_DOMAINS, validate_domain

if TYPE_CHECKING:
    from src.rag.embeddings.base import BaseEmbedder

SUPPORTED_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".yaml", ".yml", ".py"})
_MD_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$")

rag_app = typer.Typer(
    name="rag",
    help="Knowledge corpus schema management, ingestion, status, and hybrid search.",
    no_args_is_help=True,
)
cli_app = typer.Typer(
    name="ddlc",
    help="DDLC Agent Studio CLI.",
    no_args_is_help=True,
)
cli_app.add_typer(rag_app, name="rag")
console = Console()


class IngestFileResult(BaseModel):
    """Per-file outcome emitted by corpus ingestion."""

    domain: str
    source_path: str
    title: str
    file_sha256: str
    chunk_count: int = 0
    status: Literal["ingested", "skipped", "dry_run", "error"]
    reason: Optional[str] = None


class IngestSummary(BaseModel):
    """Aggregated summary for a knowledge corpus ingestion run."""

    knowledge_dir: str
    domains_processed: list[str] = Field(default_factory=list)
    files_scanned: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    total_chunks: int = 0
    dry_run: bool = False
    results: list[IngestFileResult] = Field(default_factory=list)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _extract_title(file_path: Path, text: str) -> str:
    if file_path.suffix.lower() in {".md", ".markdown"}:
        in_fence = False
        for line in text.splitlines()[:40]:
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            match = _MD_H1_RE.match(line)
            if match:
                return match.group(1).strip()
    return file_path.stem


def _resolve_target_domains(domains: Optional[Sequence[str]]) -> list[str]:
    if domains is None or not list(domains):
        return sorted(ALLOWED_DOMAINS)

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in domains:
        validated = validate_domain(raw)
        if validated not in seen:
            seen.add(validated)
            ordered.append(validated)
    return ordered


def _discover_domain_files(domain_dir: Path) -> list[Path]:
    if not domain_dir.is_dir():
        return []
    files: list[Path] = []
    for candidate in domain_dir.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        rel_parts = candidate.relative_to(domain_dir).parts
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue
        files.append(candidate)
    return sorted(files)


async def _call_upsert(store: Any, **payload: Any) -> Any:
    fn = store.upsert_document_and_chunks
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    kwargs = dict(payload)
    if sig is not None:
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if not accepts_var_kw:
            if "embedding_dimension" not in params and "dimensions" in params:
                kwargs["dimensions"] = kwargs.pop("embedding_dimension")
            kwargs = {k: v for k, v in kwargs.items() if k in params}

    return await _maybe_await(fn(**kwargs))


def _is_embedding_config_error(exc: BaseException) -> bool:
    mod = sys.modules.get("src.rag.embeddings.embedder")
    if mod is not None:
        err_cls = getattr(mod, "EmbeddingConfigError", None)
        if err_cls is not None and isinstance(exc, err_cls):
            return True
    return type(exc).__name__ == "EmbeddingConfigError"


async def ingest_knowledge_corpus(
    knowledge_dir: Optional[Path | str] = None,
    domains: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    force: bool = False,
    store: Optional[Any] = None,
    embedder: Optional["BaseEmbedder"] = None,
    batch_size: Optional[int] = None,
    db_uri: Optional[str] = None,
) -> IngestSummary:
    """Scan domain folders under knowledge_dir, chunk documents, embed, and upsert to pgvector."""
    cfg = get_studio_config()
    base_dir = Path(knowledge_dir) if knowledge_dir is not None else Path(cfg.app.paths.knowledge_dir)
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Knowledge directory not found: {base_dir}")

    target_domains = _resolve_target_domains(domains)
    eff_batch_size = max(1, batch_size or cfg.rag.embedding.batch_size)

    summary = IngestSummary(
        knowledge_dir=base_dir.as_posix(),
        domains_processed=target_domains,
        dry_run=dry_run,
    )

    owns_store = False
    active_store = store
    active_embedder = embedder

    try:
        if not dry_run and active_store is None:
            from src.rag.store.pgvector_store import PgVectorRAGStore

            active_store = PgVectorRAGStore(db_uri=db_uri)
            owns_store = True

        for domain in target_domains:
            domain_dir = base_dir / domain
            for file_path in _discover_domain_files(domain_dir):
                summary.files_scanned += 1
                source_path = file_path.relative_to(base_dir).as_posix()

                try:
                    text = file_path.read_text(encoding="utf-8")
                except Exception as exc:
                    summary.files_failed += 1
                    summary.results.append(
                        IngestFileResult(
                            domain=domain,
                            source_path=source_path,
                            title=file_path.stem,
                            file_sha256="",
                            status="error",
                            reason=f"Read error: {exc}",
                        )
                    )
                    continue

                file_sha256 = compute_sha256(text)
                title = _extract_title(file_path, text)

                if dry_run:
                    try:
                        chunks = chunk_file(file_path, text, title=title)
                    except Exception as exc:
                        summary.files_failed += 1
                        summary.results.append(
                            IngestFileResult(
                                domain=domain,
                                source_path=source_path,
                                title=title,
                                file_sha256=file_sha256,
                                status="error",
                                reason=f"Chunking error: {exc}",
                            )
                        )
                        continue
                    summary.total_chunks += len(chunks)
                    summary.results.append(
                        IngestFileResult(
                            domain=domain,
                            source_path=source_path,
                            title=title,
                            file_sha256=file_sha256,
                            chunk_count=len(chunks),
                            status="dry_run",
                        )
                    )
                    continue

                if not force:
                    existing_sha = await _maybe_await(
                        active_store.get_document_hash(domain, source_path)
                    )
                    if existing_sha is not None and str(existing_sha).strip() == file_sha256:
                        summary.files_skipped += 1
                        summary.results.append(
                            IngestFileResult(
                                domain=domain,
                                source_path=source_path,
                                title=title,
                                file_sha256=file_sha256,
                                status="skipped",
                                reason="unchanged_sha256",
                            )
                        )
                        continue

                chunks = chunk_file(file_path, text, title=title)
                if not chunks:
                    summary.files_skipped += 1
                    summary.results.append(
                        IngestFileResult(
                            domain=domain,
                            source_path=source_path,
                            title=title,
                            file_sha256=file_sha256,
                            status="skipped",
                            reason="empty_content",
                        )
                    )
                    continue

                if active_embedder is None:
                    from src.rag.embeddings.embedder import LiteLLMEmbedder

                    active_embedder = LiteLLMEmbedder()

                # Two-phase sequence: outbound embedding calls always happen outside DB transactions.
                chunk_texts = [c.content for c in chunks]
                embeddings: list[list[float]] = []
                for start in range(0, len(chunk_texts), eff_batch_size):
                    batch = chunk_texts[start : start + eff_batch_size]
                    embeddings.extend(await active_embedder.embed_texts(batch))

                if len(embeddings) != len(chunks):
                    raise RuntimeError(
                        f"Embedder returned {len(embeddings)} vectors for {len(chunks)} chunks "
                        f"in {source_path}."
                    )

                await _call_upsert(
                    active_store,
                    domain=domain,
                    source_path=source_path,
                    title=title,
                    file_sha256=file_sha256,
                    embedding_model=active_embedder.model_name,
                    embedding_dimension=active_embedder.dimensions,
                    chunks=chunks,
                    embeddings=embeddings,
                    metadata={
                        "extension": file_path.suffix.lower(),
                        "title": title,
                    },
                )
                summary.files_ingested += 1
                summary.total_chunks += len(chunks)
                summary.results.append(
                    IngestFileResult(
                        domain=domain,
                        source_path=source_path,
                        title=title,
                        file_sha256=file_sha256,
                        chunk_count=len(chunks),
                        status="ingested",
                    )
                )
    finally:
        if owns_store and active_store is not None:
            close_fn = getattr(active_store, "close", None)
            if callable(close_fn):
                await _maybe_await(close_fn())

    return summary


def _handle_cli_error(exc: BaseException) -> None:
    if _is_embedding_config_error(exc):
        console.print(
            Panel(
                f"[bold red]{exc}[/bold red]\n\n"
                "Configure the required embedding provider API key (for example "
                "[bold]OPENAI_API_KEY[/bold]) in your environment or .env file.",
                title="Embedding Configuration Error",
                border_style="red",
            )
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[red]Error:[/red] {exc}")
    raise typer.Exit(code=1) from exc


@rag_app.command("init-schema")
def init_schema_cmd(
    db_uri: Optional[str] = typer.Option(
        None,
        "--db-uri",
        help="PostgreSQL URI (defaults to RAG_DB_URI).",
    ),
) -> None:
    """Ensure the rag_knowledge schema, tables, and indexes exist."""
    async def _run() -> None:
        from src.rag.store.pgvector_store import ensure_rag_schema

        await _maybe_await(ensure_rag_schema(db_uri=db_uri))

    try:
        asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_cli_error(exc)

    console.print("[green]RAG schema initialized successfully.[/green]")


@rag_app.command("ingest")
def ingest_cmd(
    knowledge_dir: Optional[Path] = typer.Option(
        None,
        "--knowledge-dir",
        "-k",
        help="Root directory containing domain folders (defaults to config app.paths.knowledge_dir).",
    ),
    domain: Optional[list[str]] = typer.Option(
        None,
        "--domain",
        "-d",
        help="Knowledge domain to ingest (repeatable).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Scan and chunk files without calling the embedder or writing to PostgreSQL.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-embed and upsert even if the file SHA-256 hash matches.",
    ),
    db_uri: Optional[str] = typer.Option(
        None,
        "--db-uri",
        help="PostgreSQL URI (defaults to RAG_DB_URI).",
    ),
) -> None:
    """Ingest knowledge corpus files into rag_knowledge with SHA-256 deduplication."""
    try:
        summary = asyncio.run(
            ingest_knowledge_corpus(
                knowledge_dir=knowledge_dir,
                domains=domain,
                dry_run=dry_run,
                force=force,
                db_uri=db_uri,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_cli_error(exc)

    mode_label = "Dry Run" if summary.dry_run else "Ingestion"
    table = Table(title=f"RAG Corpus {mode_label} Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Knowledge Dir", summary.knowledge_dir)
    table.add_row("Domains", ", ".join(summary.domains_processed))
    table.add_row("Files Scanned", str(summary.files_scanned))
    table.add_row("Files Ingested", str(summary.files_ingested))
    table.add_row("Files Skipped", str(summary.files_skipped))
    table.add_row("Files Failed", str(summary.files_failed))
    table.add_row("Total Chunks", str(summary.total_chunks))
    console.print(table)

    if summary.files_failed > 0:
        for res in summary.results:
            if res.status == "error":
                console.print(f"[red]  x {res.source_path}: {res.reason}[/red]")
        raise typer.Exit(code=1)


def _attr_or_key(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


@rag_app.command("status")
def status_cmd(
    domain: Optional[str] = typer.Option(
        None,
        "--domain",
        "-d",
        help="Optional domain filter.",
    ),
    db_uri: Optional[str] = typer.Option(
        None,
        "--db-uri",
        help="PostgreSQL URI (defaults to RAG_DB_URI).",
    ),
) -> None:
    """Display indexed domain counts and registered knowledge documents."""
    async def _run() -> tuple[Any, list[Any]]:
        from src.rag.store.pgvector_store import PgVectorRAGStore

        validated = validate_domain(domain) if domain is not None else None
        store = PgVectorRAGStore(db_uri=db_uri)
        try:
            counts = await _maybe_await(store.get_domain_counts())
            docs = await _maybe_await(store.list_documents(domain=validated))
            return counts, list(docs or [])
        finally:
            await _maybe_await(store.close())

    try:
        counts, docs = asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_cli_error(exc)

    counts_table = Table(title="RAG Domain Counts")
    counts_table.add_column("Domain", style="cyan")
    counts_table.add_column("Documents", justify="right")
    counts_table.add_column("Chunks", justify="right")

    if isinstance(counts, dict):
        for dom_name, val in sorted(counts.items()):
            if isinstance(val, dict):
                doc_cnt = str(val.get("documents", val.get("document_count", "-")))
                chunk_cnt = str(val.get("chunks", val.get("chunk_count", "-")))
            else:
                doc_cnt = "-"
                chunk_cnt = str(val)
            counts_table.add_row(str(dom_name), doc_cnt, chunk_cnt)
    console.print(counts_table)

    docs_table = Table(title="Indexed Knowledge Documents")
    docs_table.add_column("Domain", style="cyan")
    docs_table.add_column("Source Path")
    docs_table.add_column("Title")
    docs_table.add_column("Chunks", justify="right")
    for doc in docs:
        docs_table.add_row(
            str(_attr_or_key(doc, "domain")),
            str(_attr_or_key(doc, "source_path")),
            str(_attr_or_key(doc, "title")),
            str(_attr_or_key(doc, "chunk_count", 0)),
        )
    console.print(docs_table)


@rag_app.command("search")
def search_cmd(
    domain: str = typer.Option(..., "--domain", "-d", help="Knowledge domain tag."),
    query: str = typer.Option(..., "--query", "-q", help="Search query."),
    top_k: int = typer.Option(3, "--top-k", "-k", help="Maximum chunks to return."),
    token_ceiling: int = typer.Option(1000, "--max-tokens", help="Hard token budget ceiling."),
    xml: bool = typer.Option(False, "--xml", help="Emit raw XML context slice."),
    db_uri: Optional[str] = typer.Option(
        None,
        "--db-uri",
        help="PostgreSQL URI (defaults to RAG_DB_URI).",
    ),
) -> None:
    """Execute a hybrid RAG search query against a single domain."""
    async def _run() -> Any:
        from src.rag.embeddings.embedder import LiteLLMEmbedder
        from src.rag.retrieval.hybrid_search import HybridRAGRetriever
        from src.rag.store.pgvector_store import PgVectorRAGStore

        validated = validate_domain(domain)
        store = PgVectorRAGStore(db_uri=db_uri)
        try:
            pool = await _maybe_await(store.get_pool())
            embedder = LiteLLMEmbedder()
            retriever = HybridRAGRetriever(db_pool=pool, embedder=embedder)
            return await retriever.search(
                domain=validated,
                query=query,
                top_k=top_k,
                token_ceiling=token_ceiling,
            )
        finally:
            await _maybe_await(store.close())

    try:
        slice_result = asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:
        _handle_cli_error(exc)

    if xml:
        typer.echo(slice_result.to_xml())
        return

    if not slice_result.chunks:
        console.print(
            f"[yellow]No matching chunks found in domain '{slice_result.domain}'.[/yellow]"
        )
        return

    table = Table(
        title=f"RAG Search Results ({slice_result.domain}, tokens={slice_result.total_tokens})"
    )
    table.add_column("#", justify="right")
    table.add_column("Source", style="cyan")
    table.add_column("Breadcrumb")
    table.add_column("RRF", justify="right")
    table.add_column("Rerank", justify="right")
    table.add_column("Tokens", justify="right")

    for idx, chunk in enumerate(slice_result.chunks, start=1):
        rerank_str = f"{chunk.rerank_score:.4f}" if chunk.rerank_score is not None else "-"
        table.add_row(
            str(idx),
            chunk.source_path or "unknown",
            chunk.breadcrumb or "General",
            f"{chunk.rrf_score:.4f}",
            rerank_str,
            str(chunk.token_count),
        )
    console.print(table)
    for idx, chunk in enumerate(slice_result.chunks, start=1):
        console.print(
            Panel(
                chunk.content,
                title=f"[{idx}] {chunk.source_path or 'unknown'} — {chunk.breadcrumb or 'General'}",
            )
        )


if __name__ == "__main__":
    cli_app()
