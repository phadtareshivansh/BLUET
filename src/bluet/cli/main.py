"""bluet CLI entry point."""

from __future__ import annotations

import typer

from bluet.cli.diff import diff
from bluet.cli.doctor import doctor
from bluet.cli.run import run
from bluet.cli.status import status

app = typer.Typer(
    name="bluet",
    help="Agentic incremental refactoring toolbox.",
    no_args_is_help=True,
)
app.command(name="doctor")(doctor)
app.command(name="run")(run)
app.command(name="status")(status)
app.command(name="diff")(diff)


if __name__ == "__main__":
    app()
