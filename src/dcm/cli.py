"""DCM CLI — Typer-based command-line interface for the DCM Schema Engine."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.syntax import Syntax

from src.dcm.compiler import (
    compile_contracts,
    load_contract,
    load_contracts_from_dir,
    validate_contracts_integrity,
)
from src.dcm.migrator import (
    DestructiveMigrationError,
    apply_migrations,
    compute_migration_plan,
    compute_offline_plan,
    create_revision,
    get_current_revision,
    get_migration_status,
    introspect_database,
)
from src.dcm.models import DataContractSchema

app = typer.Typer(
    name="dcm",
    help="DCM Schema Engine — deterministic YAML -> PostgreSQL DDL via SQLGlot + Alembic.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_CONTRACTS_DIR = Path("contracts")


def _resolve_db_uri() -> str:
    """Resolve the admin/migration database URI from environment settings."""
    from src.core.settings import get_env_settings

    env = get_env_settings()
    if not env.dcm_migration_db_uri:
        console.print(
            "[red]Error:[/red] DCM_MIGRATION_DB_URI is not set. "
            "Configure it in your .env file.",
            style="bold red",
        )
        raise typer.Exit(code=1)
    return env.dcm_migration_db_uri


def _load_contracts(target: Path) -> list[DataContractSchema]:
    """Load one contract file or all *.yaml contracts in a directory."""
    if target.is_file():
        return [load_contract(target)]
    contracts = load_contracts_from_dir(target)
    if not contracts:
        console.print(
            f"[yellow]Warning:[/yellow] No .yaml contract files found in {target}"
        )
        raise typer.Exit(code=0)
    return contracts


@app.command()
def validate(
    target: Path = typer.Argument(
        DEFAULT_CONTRACTS_DIR,
        help="YAML contract file or directory containing YAML contract files.",
        exists=True,
    ),
) -> None:
    """Validate YAML contracts — syntax, SQLGlot types, constraints, and FK integrity."""
    console.print(f"\n[bold]Validating contracts in {target}...[/bold]\n")

    try:
        contracts = _load_contracts(target)
        refs: list[DataContractSchema] = []
        if target.is_file() and target.parent.is_dir():
            refs = [
                c
                for c in load_contracts_from_dir(target.parent)
                if c.schema_name != contracts[0].schema_name
            ]
        validate_contracts_integrity(
            contracts,
            reference_contracts=refs or None,
            strict_external=True if (target.is_dir() or refs) else False,
        )
        # Dry-run DDL compilation verifies SQLGlot AST parsing for all statements
        compile_contracts(contracts)
    except Exception as exc:
        console.print(f"[red]  x Validation failed: {exc}[/red]")
        raise typer.Exit(code=1)

    for contract in contracts:
        console.print(
            f"[green]  ok {contract.schema_name} ({len(contract.tables)} tables)[/green]"
        )
    console.print(f"\n[green]All {len(contracts)} contract(s) valid.[/green]")


@app.command()
def compile(
    contracts_dir: Path = typer.Argument(
        DEFAULT_CONTRACTS_DIR,
        help="Directory containing YAML contract files.",
        exists=True,
    ),
) -> None:
    """Compile YAML contracts to PostgreSQL DDL and print to stdout."""
    contracts = _load_contracts(contracts_dir)
    validate_contracts_integrity(contracts, strict_external=True)
    stmts = compile_contracts(contracts)

    console.print(
        f"\n[bold]Compiled {len(stmts)} DDL statements from "
        f"{len(contracts)} contract(s):[/bold]\n"
    )
    syntax = Syntax("\n\n".join(stmts), "sql", theme="monokai", line_numbers=True)
    console.print(syntax)


@app.command()
def plan(
    contracts_dir: Path = typer.Argument(
        DEFAULT_CONTRACTS_DIR,
        help="Directory containing YAML contract files.",
        exists=True,
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Plan against an empty catalog without connecting to PostgreSQL.",
    ),
) -> None:
    """Diff compiled YAML contracts against the live PostgreSQL catalog."""
    contracts = _load_contracts(contracts_dir)
    validate_contracts_integrity(contracts, strict_external=True)

    if offline:
        console.print(f"\n[bold]Offline baseline plan for {contracts_dir}...[/bold]\n")
        migration_plan = compute_offline_plan(contracts)
    else:
        db_uri = _resolve_db_uri()
        console.print(f"\n[bold]Comparing live database against {contracts_dir}...[/bold]\n")
        catalog = introspect_database(db_uri, [c.schema_name for c in contracts])
        migration_plan = compute_migration_plan(contracts, catalog)

    if migration_plan.is_empty:
        console.print("[green]All schemas are up to date. No changes required.[/green]")
        return

    for schema_name in migration_plan.schemas:
        schema_ops = [op for op in migration_plan.ops if op.schema == schema_name]
        if not schema_ops:
            console.print(f"[blue][INFO] Schema: {schema_name} (no changes)[/blue]")
            continue
        console.print(f"[blue][INFO] Schema: {schema_name}[/blue]")
        for op in schema_ops:
            color = "red" if op.kind.startswith("drop") or "alter_column_type" in op.kind else "green"
            console.print(f"  [{color}]{op.description}[/{color}]")

    report = migration_plan.safety_report()
    console.print("\n[bold]Safety Analysis:[/bold]")
    console.print(report.summary())

    if report.has_destructive:
        console.print(
            "\n[yellow]Destructive operations detected. "
            "Use --approve-destructive with 'dcm apply' to proceed.[/yellow]"
        )
    else:
        console.print("\n[green]No destructive operations detected.[/green]")


@app.command()
def revision(
    message: str = typer.Option(
        ...,
        "-m",
        "--message",
        help="Migration revision message.",
    ),
    contracts_dir: Path = typer.Option(
        DEFAULT_CONTRACTS_DIR,
        "--contracts-dir",
        help="Directory containing YAML contract files.",
    ),
    allow_empty: bool = typer.Option(
        False,
        "--allow-empty",
        help="Create a revision even if the database is already synced.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Generate baseline revision from full contracts without querying DB.",
    ),
) -> None:
    """Generate a versioned Alembic migration script from contract diff."""
    contracts = _load_contracts(contracts_dir)
    validate_contracts_integrity(contracts, strict_external=True)
    db_uri = _resolve_db_uri()

    rev_path = create_revision(
        db_uri=db_uri,
        message=message,
        contracts=contracts,
        allow_empty=allow_empty,
        offline=offline,
    )
    if rev_path is None:
        console.print(
            "\n[green]No schema changes detected — no revision generated.[/green] "
            "(Pass --allow-empty to force.)"
        )
        return

    console.print(f"\n[green]Created revision: {rev_path}[/green]")


@app.command()
def apply(
    target_revision: str = typer.Option(
        "head",
        "--revision",
        help="Target Alembic revision (default: head).",
    ),
    sql: bool = typer.Option(
        False,
        "--sql",
        help="Emit Alembic upgrade SQL to stdout without executing.",
    ),
    approve_destructive: bool = typer.Option(
        False,
        "--approve-destructive",
        help="Approve destructive operations (DROP, ALTER TYPE, RENAME).",
    ),
) -> None:
    """Apply pending Alembic migrations to PostgreSQL (with Human Safety Gate)."""
    db_uri = _resolve_db_uri()

    try:
        output = apply_migrations(
            db_uri=db_uri,
            revision=target_revision,
            sql_only=sql,
            approve_destructive=approve_destructive,
        )
    except DestructiveMigrationError as exc:
        console.print("\n[red]Execution halted by Human Gate.[/red]")
        console.print(exc.report.summary())
        console.print(
            "\nTo proceed, re-run with: [bold]dcm apply --approve-destructive[/bold]"
        )
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"\n[red]Migration failed: {exc}[/red]")
        raise typer.Exit(code=1)

    if sql:
        typer.echo(output.rstrip())
        return

    current = get_current_revision(db_uri)
    if not output.strip():
        console.print(
            f"\n[green]All schemas already up to date at revision: {current or '(none)'}.[/green]"
        )
    else:
        console.print(
            f"\n[green]All schemas up to date at revision: {current}.[/green]"
        )


@app.command()
def status(
    contracts_dir: Optional[Path] = typer.Option(
        DEFAULT_CONTRACTS_DIR,
        "--contracts-dir",
        help="Contracts directory for per-schema drift check.",
    ),
) -> None:
    """Show current Alembic revision and schema sync status."""
    db_uri = _resolve_db_uri()
    contracts: Optional[list[DataContractSchema]] = None
    if contracts_dir is not None and contracts_dir.is_dir():
        contracts = load_contracts_from_dir(contracts_dir)

    try:
        info = get_migration_status(db_uri, contracts=contracts)
    except Exception as exc:
        console.print(f"\n[yellow]Could not retrieve status: {exc}[/yellow]")
        raise typer.Exit(code=1)

    head_label = " (head)" if info["is_up_to_date"] and info["current_revision"] else ""
    console.print("\n[bold]Migration Status:[/bold]")
    console.print(f"  Current revision: {info['current_revision'] or '(none)'}{head_label}")
    console.print(f"  Head revision:    {info['head_revision'] or '(none)'}")

    if info["schemas"]:
        console.print("  Target schemas:")
        for schema_name, synced in info["schemas"].items():
            state = "[green]synced[/green]" if synced else "[yellow]drift / pending[/yellow]"
            console.print(f"    - {schema_name} ({state})")

    if info["is_up_to_date"] and all(info["schemas"].values()):
        console.print("\n[green]Database is up to date.[/green]")
    else:
        console.print("\n[yellow]Pending migrations or schema drift detected.[/yellow]")


if __name__ == "__main__":
    app()
