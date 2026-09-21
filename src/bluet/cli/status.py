"""`bluet status` - job summary table from SQLite, or the Textual TUI.

``bluet status <job_id>`` reads the job row plus its persisted EventBus events
from the repository's ``.bluet/state.db`` and prints a Rich summary table. With
no ``job_id`` argument the legacy Textual TUI (over the file-based
``job.json``) is launched instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluet.state.db import db_path_for_repo, engine_for_repo
from bluet.state.models import AgentEvent, Job
from bluet.state.repository import get_job_by_id, list_events_for_job


def _color_status(status: str) -> str:
    colors = {
        "COMPLETED": "green",
        "RUNNING": "yellow",
        "PENDING": "cyan",
        "BLOCKED": "red",
        "FAILED": "red",
    }
    return f"[{colors.get(status, 'white')}]{status}[/]"


async def _load(root: Path, job_id: int) -> tuple[Job | None, list[AgentEvent]]:
    engine = engine_for_repo(root)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            job = await get_job_by_id(session, job_id)
            events = await list_events_for_job(session, job_id) if job else []
            return job, events
    finally:
        await engine.dispose()


def _show_summary(console: Console, job_id: int, root: Path) -> None:
    db_path = db_path_for_repo(root)
    if not db_path.exists():
        console.print(f"[bold red]No state database found:[/bold red] {db_path}")
        console.print("Run [cyan]bluet run[/cyan] against this repository first.")
        raise typer.Exit(1)

    job, events = asyncio.run(_load(root, job_id))
    if job is None:
        console.print(f"[bold red]Job {job_id} not found[/bold red] in the database at {db_path}.")
        raise typer.Exit(1)

    job_table = Table(title=f"Job #{job_id}", show_header=True)
    job_table.add_column("Field", style="bold cyan", no_wrap=True)
    job_table.add_column("Value")
    job_table.add_row("repo_path", str(job.repo_path))
    job_table.add_row("backend", str(job.backend))
    job_table.add_row("status", _color_status(job.status))
    job_table.add_row("created_at", str(job.created_at))
    job_table.add_row("updated_at", str(job.updated_at))
    console.print(job_table)
    console.print()

    by_file: dict[str, dict[str, int]] = {}
    for event in events:
        payload = json.loads(event.payload_json or "{}")
        file = str(payload.get("current_file") or "-")
        counts = by_file.setdefault(file, {})
        counts[event.event_type] = counts.get(event.event_type, 0) + 1

    if events:
        events_table = Table(title="Events")
        events_table.add_column("File", no_wrap=True)
        topics = sorted({e.event_type for e in events})
        for topic in topics:
            events_table.add_column(topic, justify="right")
        events_table.add_column("Total", justify="right")
        for file, counts in sorted(by_file.items()):
            row = [file] + [str(counts.get(topic, "")) or "" for topic in topics]
            row.append(str(sum(counts.values())))
            events_table.add_row(*row)
        console.print(events_table)
        console.print()

        latest = Table(title="Latest events")
        latest.add_column("Timestamp", no_wrap=True)
        latest.add_column("Agent", no_wrap=True)
        latest.add_column("Event", no_wrap=True)
        latest.add_column("File")
        for event in events[-8:]:
            payload = json.loads(event.payload_json or "{}")
            latest.add_row(
                str(event.timestamp), event.agent_name, event.event_type,
                str(payload.get("current_file") or "-"),
            )
        console.print(latest)


def status(
    job_id: Annotated[int | None, typer.Argument(help="Job ID to summarize")] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository whose .bluet/state.db to read (default: current directory)",
        ),
    ] = None,
) -> None:
    """Show a job summary from SQLite, or the Textual TUI when no job ID is given."""
    if job_id is None:
        from bluet.cli.status_app import BluetStatusApp

        BluetStatusApp().run()
        return

    _show_summary(Console(), job_id, repo or Path.cwd())