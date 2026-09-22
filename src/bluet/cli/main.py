"""bluet CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bluet.cli.diff import diff
from bluet.cli.doctor import doctor
from bluet.cli.status import status

_CAP_DROP_DEFAULT = ["ALL"]


def _tui() -> None:
    """Lazy ``bluet tui`` — avoids importing Textual for every CLI invocation."""
    from bluet.cli.tui import tui

    tui()


def _run(
    path: Annotated[
        Path | None,
        typer.Argument(help="File or directory to analyze (omit for a gate-only check)"),
    ] = None,
    memory: Annotated[str, typer.Option(help="Hardened-Docker memory limit")] = "2g",
    network: Annotated[str, typer.Option(help="Hardened-Docker network mode")] = "none",
    read_only: Annotated[
        bool,
        typer.Option("--read-only/--no-read-only", help="Mount container read-only"),
    ] = True,
    cap_drop: Annotated[
        list[str],
        typer.Option(help="Capability to drop on fallback (repeatable; default ALL)"),
    ] = _CAP_DROP_DEFAULT,
    guardrail_ok: Annotated[
        bool,
        typer.Option(
            "--guardrail-ok", help="Bypass Enkrypt guardrails if they flag (override halt)"
        ),
    ] = False,
) -> None:
    """Auto-run sandbox diagnostics, then drive the orchestrator on <path>."""
    from bluet.cli.run import run

    run(path, memory, network, read_only, cap_drop, guardrail_ok)


def _lsp(
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
    """Lazy ``bluet lsp`` — the LSP pulls in the native analyzer package."""
    from bluet.cli.lsp import lsp

    lsp(workspace)


app = typer.Typer(
    name="bluet",
    help="Agentic incremental refactoring toolbox.",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.command(name="doctor")(doctor)
app.command(name="run")(_run)
app.command(name="status")(status)
app.command(name="diff")(diff)
app.command(name="lsp")(_lsp)
app.command(name="tui")(_tui)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    skip_tui: Annotated[
        bool,
        typer.Option(
            "--skip-tui",
            help="Print usage instead of opening the BIOS dashboard.",
        ),
    ] = False,
) -> None:
    """Launch the BIOS dashboard when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        if skip_tui:
            typer.echo(ctx.get_help())
        else:
            _tui()


if __name__ == "__main__":
    app()
