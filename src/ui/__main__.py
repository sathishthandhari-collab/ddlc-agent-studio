"""Module entrypoint launching Chainlit with WindowsSelectorEventLoopPolicy when on Windows."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    """Launch ``chainlit run src/ui/app.py`` after applying Windows SelectorEventLoopPolicy."""
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from chainlit.cli import cli

    app_path = str(Path(__file__).with_name("app.py"))
    extra_args = list(argv) if argv is not None else sys.argv[1:]
    cli(args=["run", app_path, *extra_args], standalone_mode=False)


if __name__ == "__main__":
    main()
