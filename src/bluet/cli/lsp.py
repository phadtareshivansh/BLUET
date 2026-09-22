"""`bluet lsp` - start the BLUET Language Server over stdio."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from bluet.lsp import serve_stdio

console = Console()

app = typer.Typer(
    name="lsp",
    help="Start the BLUET Language Server (for VS Code extension).",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def lsp(
    workspace: Annotated[
        Path | None,
        typer.Argument(
            help="Workspace root (defaults to current directory).",
            file_okay=False,
            dir_okay=True,
            exists=True,
        ),
    ] = None,
) -> None:
    """Start the BLUET Language Server over stdio.

    This command is intended to be launched by a VS Code extension as the
    language server process. It communicates via stdin/stdout using the
    Language Server Protocol.
    """
    if workspace:
        import os
        os.chdir(workspace)

    console.print("[dim]Starting BLUET Language Server...[/dim]", file=sys.stderr)

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        console.print("[dim]Language server stopped.[/dim]", file=sys.stderr)


async def _run_server() -> None:
    """Run the LSP server."""
    async with serve_stdio():
        # Keep the server running until stdin closes
        await asyncio.Event().wait()