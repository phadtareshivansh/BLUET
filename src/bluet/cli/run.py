"""`bluet run` - auto-gated sandbox check, then orchestrator execution.

``bluet run <path>`` creates a SQLite-backed job for the target repository and
drives the LangGraph refactor pipeline (real ``analyze_node``, stub refactor/
verify nodes) with a live Rich progress table fed by EventBus messages. Every
event topic is mirrored into the repo's ``.bluet/state.db`` so ``bluet status
<job_id>`` can summarize the run afterwards.

With no ``path`` argument the command keeps its Phase 0 gate-only behavior:
inspect the sandbox backend and persist the result to the legacy ``job.json``
without running any pipeline.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.json import JSON
from rich.live import Live
from rich.table import Table
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluet.agents.analyzer import parser_for_source
from bluet.cli.diagnostics import record_job, render_docker_blocked, render_hardened_warning
from bluet.errors import BluetEnvironmentError
from bluet.orchestrator.events import TOPICS, EventBus
from bluet.orchestrator.refactor_graph import (
    build_refactor_graph,
    open_sqlite_checkpointer,
    run_config,
)
from bluet.sandbox.runtime import BACKEND_HARDENED, RuntimeLimits, diagnose
from bluet.state.db import db_path_for_repo, engine_for_repo, init_schema
from bluet.state.repository import (
    create_job,
    list_events_for_job,
    update_job_status,
)

_CAP_DROP_DEFAULT = ["ALL"]

# EventBus topic -> Live-table stage column.
_TOPIC_STAGE: dict[str, str] = {
    "task.analysis": "Analyze",
    "task.context": "Context",
    "task.refactor": "Refactor",
    "task.verify": "Verify",
    "feedback.regression": "Retry",
    "feedback.warning": "Warning",
}


def _make_limits(memory: str, network: str, read_only: bool, cap_drop: list[str]) -> RuntimeLimits:
    return RuntimeLimits(
        memory=memory,
        network=network,
        read_only=read_only,
        cap_drop=tuple(cap_drop) if cap_drop else ("ALL",),
    )


class _RunProgress:
    """Mutable progress model behind the Rich Live table (single event loop)."""

    def __init__(self, files: list[str]) -> None:
        self._files = list(files)
        self._stages: dict[str, set[str]] = {f: set() for f in self._files}
        self._done: set[str] = set()
        self._errors: dict[str, str] = {}
        self.version = 0

    def mark(self, payload: dict[str, Any]) -> None:
        file = payload.get("current_file")
        stage = _TOPIC_STAGE.get(str(payload.get("topic", "")))
        if file is not None and stage and file in self._stages:
            self._stages[file].add(stage)
            self.version += 1

    def done(self, file: str) -> None:
        if file in self._files:
            self._done.add(file)
            self.version += 1

    def fail(self, file: str, error: str) -> None:
        self._errors[file] = error
        self.version += 1

    def render(self) -> Table:
        table = Table(title="bluet run - refactor pipeline", show_lines=False)
        table.add_column("File", no_wrap=True)
        for stage in ("Analyze", "Context", "Refactor", "Verify", "Warning"):
            table.add_column(stage, justify="center")
        table.add_column("Status", justify="center")

        for file in self._files:
            stages = self._stages[file]
            row = [file]
            for stage in ("Analyze", "Context", "Refactor", "Verify", "Warning"):
                if stage in stages:
                    row.append("[green]ok[/green]")
                else:
                    row.append("[dim]-[/dim]")
            if file in self._errors:
                row.append(f"[bold red]error[/bold red]: {self._errors[file]}")
            elif file in self._done:
                row.append("[green]done[/green]")
            else:
                row.append("[yellow]running[/yellow]")
            table.add_row(*row)
        return table


def _resolve_targets(path: Path) -> tuple[Path, list[str]]:
    """Resolve ``path`` into ``(repo_dir, source_files)``.

    A directory yields every parseable source file under it (by suffix, via
    :func:`parser_for_source`); a single file yields ``(file.parent, [name])``.
    """
    target = path.resolve()
    if not target.exists():
        raise typer.BadParameter(f"no such file or directory: {target}")
    if target.is_dir():
        return target, sorted(
            str(p.relative_to(target))
            for p in target.rglob("*")
            if p.is_file() and parser_for_source(None, p.name) is not None
        )
    if parser_for_source(None, target.name) is None:
        raise typer.BadParameter(f"unsupported source file (python/java only): {target.name}")
    return target.parent, [target.name]


async def _drive(
    console: Console,
    engine: Any,
    repo_dir: Path,
    files: list[str],
    backend: str | None,
) -> int:
    """Create a job, run the refactor graph per file, and print results.

    Returns the job ID. Raises nothing on per-file analysis failures; those are
    recorded on the Live table and surfaced as a non-zero exit by the caller.
    """
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        job = await create_job(session, str(repo_dir), status="RUNNING", backend=backend)
    job_id = job.id

    progress = _RunProgress(files)
    specs: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}

    async with (
        EventBus(factory) as bus,
        open_sqlite_checkpointer(db_path_for_repo(repo_dir)) as saver,
    ):

        async def _on_event(payload: dict[str, Any]) -> None:
            progress.mark(payload)

        for topic in TOPICS:
            await bus.subscribe(topic, _on_event)

        graph = build_refactor_graph(bus, checkpointer=saver)

        async def _run_all() -> None:
            for file in files:
                state = {
                    "job_id": job_id,
                    "repo_path": str(repo_dir),
                    "target_language": "",
                    "current_file": file,
                    "retry_count": 0,
                }
                try:
                    final = await graph.ainvoke(state, config=run_config(job_id))
                    spec = final.get("logic_spec")
                    if spec:
                        specs[file] = spec
                    progress.done(file)
                except Exception as exc:  # noqa: BLE001 - surface, then keep going
                    failures[file] = str(exc)
                    progress.fail(file, str(exc))

        if os.environ.get("BLUET_LIVE", "1") != "0":
            async def _pump() -> None:
                pending = -1
                while True:
                    if progress.version != pending:
                        pending = progress.version
                        live.update(progress.render())
                    await asyncio.sleep(0.05)

            with Live(progress.render(), console=console, refresh_per_second=20) as live:
                pump = asyncio.create_task(_pump())
                try:
                    await _run_all()
                    await bus.flush()
                finally:
                    pump.cancel()
                    try:
                        await pump
                    except asyncio.CancelledError:
                        pass
        else:
            await _run_all()
            await bus.flush()

    final_status = "COMPLETED" if not failures else "FAILED"
    async with factory() as session:
        await update_job_status(session, job_id, final_status)
        events = await list_events_for_job(session, job_id)

    console.print()
    for file, spec in specs.items():
        console.print(f"[bold cyan]{file}[/bold cyan]")
        console.print(JSON.from_data(spec, indent=2))
        console.print()

    if failures:
        console.print("[bold red]Job failed for some files.[/bold red]")
        for file, error in failures.items():
            console.print(f"  [red]{file}:[/red] {error}")
    console.print(
        f"Job [cyan]#{job_id}[/cyan] for [bold]{repo_dir}[/bold] "
        f"[{'red' if failures else 'green'}]{final_status}[/] - "
        f"{len(files)} file(s), {len(events)} event(s)."
    )
    await engine.dispose()
    return job_id


def run(
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
) -> None:
    """Auto-run sandbox diagnostics, then drive the orchestrator on <path>."""
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
    console.print(
        f"[green]Sandbox verified[/green] - backend=[cyan]{d.backend}[/cyan] "
        f"(host={d.host_backend})."
    )

    if path is None:
        console.print(
            "[dim]gate-only: pass a file or directory argument to run the orchestrator (e.g. "
            "[cyan]bluet run src/[/cyan]).[/dim]"
        )
        return

    try:
        repo_dir, files = _resolve_targets(path)
    except typer.BadParameter as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(2) from exc

    if not files:
        console.print(
            f"[bold red]No supported source files[/bold red] (python/java) found under {repo_dir}."
        )
        raise typer.Exit(2)

    engine = engine_for_repo(repo_dir)
    asyncio.run(_drive(console, engine, repo_dir, files, backend=d.backend))
    record_job(d, status="COMPLETED")