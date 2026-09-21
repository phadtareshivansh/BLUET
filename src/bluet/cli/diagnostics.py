"""Shared CLI rendering + job-config recording for sandbox diagnostics."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bluet.sandbox.platform import host_os
from bluet.sandbox.runtime import (
    BACKEND_GVISOR,
    BACKEND_HARDENED,
    DOCKER_INSTALL_URL,
    GVISOR_INSTALL_URL,
    Diagnostics,
)
from bluet.state.doctor_cache import write_doctor_backend
from bluet.state.job_config import CONFIG_PATH, JobConfig, do_now


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:8]}"


def build_summary_table(d: Diagnostics) -> Table:
    table = Table(title="bluet doctor", expand=False, title_justify="left")
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Result")

    docker_cell = "[green]running[/green]" if d.docker_ok else "[bold red]unavailable[/bold red]"
    if d.docker_error:
        docker_cell += f"\n[dim]{d.docker_error}[/dim]"

    if d.docker_ok and d.gvisor_available:
        gvisor_cell = "[green]available[/green]"
    elif d.docker_ok:
        gvisor_cell = "[yellow]unavailable[/yellow]"
    else:
        gvisor_cell = "[dim]not checked[/dim]"

    if d.backend == BACKEND_GVISOR:
        backend_cell = "[green]gvisor[/green]"
    elif d.backend == BACKEND_HARDENED:
        backend_cell = "[yellow]hardened-docker[/yellow]"
    else:
        backend_cell = "[bold red]n/a - blocked[/bold red]"

    table.add_row("Host OS", host_os())
    table.add_row("Host backend", d.host_backend)
    table.add_row("Docker daemon", docker_cell)
    table.add_row("gVisor (runsc)", gvisor_cell)
    table.add_row("Backend selected", backend_cell)
    table.add_row("Resource limits", d.limits.as_text())
    return table


def record_job(d: Diagnostics, status: str) -> JobConfig:
    cfg = JobConfig(
        job_id=new_job_id(),
        backend=d.backend or "unknown",
        status=status,
        created_at=do_now(),
        gvisor_available=d.gvisor_available,
        docker_running=d.docker_ok,
        limits=asdict(d.limits),
    )
    cfg.save(path=CONFIG_PATH)
    write_doctor_backend(cfg.backend)
    return cfg


def render_docker_blocked(console: Console, d: Diagnostics) -> None:
    console.print(
        Panel(
            "[bold red]BLUET cannot execute sandboxed verification without Docker.[/bold red]\n\n"
            f"[red]{d.docker_error or 'Docker is unavailable.'}[/red]\n\n"
            f"Install or start Docker, then re-run. [link={DOCKER_INSTALL_URL}]"
            f"{DOCKER_INSTALL_URL}[/link]",
            title="bluet doctor - blocked",
            border_style="red",
        )
    )


def render_hardened_warning(console: Console) -> None:
    console.print(
        Panel(
            "[yellow]gVisor (runsc) is not available; verification will run with a "
            "weaker isolation boundary than gVisor provides. Falling back to "
            "hardened standard Docker ([bold]--network none --read-only "
            "--cap-drop=ALL --memory=2g[/bold]).[/yellow]\n\n"
            f"To get the full gVisor boundary, install the runsc runtime: "
            f"[link={GVISOR_INSTALL_URL}]{GVISOR_INSTALL_URL}[/link]",
            title="backend: hardened-docker",
            border_style="yellow",
        )
    )