"""`bluet run` - auto-gated sandbox check, then (Phase 0) placeholder execution."""

from __future__ import annotations

from typing import Annotated

import typer

from bluet.cli.diagnostics import record_job, render_docker_blocked, render_hardened_warning
from bluet.errors import BluetEnvironmentError
from bluet.sandbox.runtime import BACKEND_HARDENED, RuntimeLimits, diagnose

_CAP_DROP_DEFAULT = ["ALL"]


def _make_limits(memory: str, network: str, read_only: bool, cap_drop: list[str]) -> RuntimeLimits:
    return RuntimeLimits(
        memory=memory,
        network=network,
        read_only=read_only,
        cap_drop=tuple(cap_drop) if cap_drop else ("ALL",),
    )


def run(
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
    """Run a job on the sandboxed backend (auto-runs sandbox diagnostics first)."""
    from rich.console import Console

    console = Console()
    limits = _make_limits(memory, network, read_only, cap_drop)

    try:
        d = diagnose(limits)
    except BluetEnvironmentError as exc:
        console.print(f"[bold red]Environment error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not d.docker_ok:
        render_docker_blocked(console, d)
        record_job(d, status="BLOCKED")
        console.print("[bold red]bluet run aborted: sandboxed verification requires Docker.[/bold red]")
        raise typer.Exit(1)

    if d.backend == BACKEND_HARDENED:
        render_hardened_warning(console)

    record_job(d, status="RUNNING")
    console.print(f"[green]Sandbox verified[/green] - backend=[cyan]{d.backend}[/cyan] "
                  f"(host={d.host_backend}).")
    console.print(
        "[dim]bluet run: orchestration is not implemented yet (Phase 0). "
        "Sandboxed verification will execute here in a later phase.[/dim]"
    )