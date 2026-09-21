"""`bluet doctor` - standalone sandbox/runtime diagnostics."""

from __future__ import annotations

from typing import Annotated

import typer

from bluet.cli.diagnostics import (
    build_summary_table,
    record_job,
    render_docker_blocked,
    render_hardened_warning,
)
from bluet.errors import BluetEnvironmentError
from bluet.sandbox.runtime import BACKEND_GVISOR, BACKEND_HARDENED, RuntimeLimits, diagnose

_CAP_DROP_DEFAULT = ["ALL"]


def _make_limits(memory: str, network: str, read_only: bool, cap_drop: list[str]) -> RuntimeLimits:
    return RuntimeLimits(
        memory=memory,
        network=network,
        read_only=read_only,
        cap_drop=tuple(cap_drop) if cap_drop else ("ALL",),
    )


def doctor(
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
) -> None:
    """Probe Docker + gVisor and report the sandbox backend BLUET will use."""
    from rich.console import Console

    console = Console()
    limits = _make_limits(memory, network, read_only, cap_drop)
    try:
        d = diagnose(limits)
    except BluetEnvironmentError as exc:
        console.print(f"[bold red]Environment error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    console.print(build_summary_table(d))

    if not d.docker_ok:
        render_docker_blocked(console, d)
        record_job(d, status="BLOCKED")
        raise typer.Exit(1)

    if d.backend == BACKEND_HARDENED:
        render_hardened_warning(console)
    elif d.backend == BACKEND_GVISOR:
        console.print("[green]gVisor active - sandboxed verification will run under runsc.[/green]")

    record_job(d, status="READY")