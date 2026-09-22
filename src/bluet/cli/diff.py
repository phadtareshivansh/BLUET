"""bluet diff — colorized side-by-side diff between legacy and proposed code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.syntax import Syntax
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluet.state.db import engine_for_repo
from bluet.state.models import ParityScore
from bluet.state.repository import (
    get_job_by_id,
    list_events_for_job,
    list_proposed_code_for_job,
)

app = typer.Typer(help="Show side-by-side diff for a completed job.")


def _colorize_diff(legacy: str, proposed: str) -> str:
    """Generate a unified diff with ANSI colors (green=add, red=remove)."""
    import difflib

    legacy_lines = legacy.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    diff = list(difflib.unified_diff(legacy_lines, proposed_lines, lineterm=""))
    if not diff:
        return ""
    out: list[str] = []
    for line in diff:
        if line.startswith("+"):
            out.append(f"[green]{line}[/green]")
        elif line.startswith("-"):
            out.append(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            out.append(f"[cyan]{line}[/cyan]")
        else:
            out.append(line)
    return "".join(out)


def _render_side_by_side(legacy: str, proposed: str) -> str:
    """Render a simple side-by-side view using Rich's syntax highlighting."""
    legacy_syntax = Syntax(legacy, "python", theme="monokai", line_numbers=True)
    proposed_syntax = Syntax(proposed, "python", theme="monokai", line_numbers=True)
    # For side-by-side in terminal, we just stack them with headers
    return f"[bold]Legacy:[/bold]\n{legacy_syntax}\n\n[bold]Proposed:[/bold]\n{proposed_syntax}"


@app.command(name="diff")
def diff(
    job_id: Annotated[int, typer.Argument(help="Job ID to show diff for")],
    repo: Annotated[
        Path,
        typer.Option("--repo", "-r", help="Repository path (default: .bluet/ in cwd)"),
    ] = Path(".bluet"),
    export: Annotated[
        Path | None,
        typer.Option("--export", "-e", help="Write diff to file instead of stdout"),
    ] = None,
    side_by_side: Annotated[
        bool,
        typer.Option("--side-by-side/--unified", help="Side-by-side view (default: unified diff)"),
    ] = False,
) -> None:
    """Show colorized diff between legacy and proposed code for a completed job."""
    import asyncio
    asyncio.run(_diff_async(job_id, repo, export, side_by_side))


async def _diff_async(
    job_id: Annotated[int, typer.Argument(help="Job ID to show diff for")],
    repo: Annotated[
        Path,
        typer.Option("--repo", "-r", help="Repository path (default: .bluet/ in cwd)"),
    ] = Path(".bluet"),
    export: Annotated[
        Path | None,
        typer.Option("--export", "-e", help="Write diff to file instead of stdout"),
    ] = None,
    side_by_side: Annotated[
        bool,
        typer.Option("--side-by-side/--unified", help="Side-by-side view (default: unified diff)"),
    ] = False,
) -> None:
    """Show colorized diff between legacy and proposed code for a completed job."""
    console = Console()

    # Resolve repo path
    repo_path = repo.resolve()
    if not repo_path.exists():
        console.print(f"[bold red]Repository path not found:[/bold red] {repo_path}")
        raise typer.Exit(1)

    # Load job and proposed code
    engine = engine_for_repo(repo_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        job = await get_job_by_id(session, job_id)
        if job is None:
            console.print(f"[bold red]Job {job_id} not found.[/bold red]")
            raise typer.Exit(1)

        proposed_rows = await list_proposed_code_for_job(session, job_id)
        if not proposed_rows:
            console.print(
                f"[bold yellow]No proposed code found for job {job_id}.[/bold yellow]"
            )
            return

        # Get parity score
        parity_result = await session.execute(
            select(ParityScore).where(ParityScore.job_id == job_id)
        )
        parity_rows = list(parity_result.scalars().all())
        parity_by_module = {p.module_path: p.score for p in parity_rows}

        # Get counter-examples from events
        events = await list_events_for_job(session, job_id)
        counter_examples_by_module: dict[str, list[dict]] = {}
        for event in events:
            if event.event_type == "task.verify":
                payload = json.loads(event.payload_json)
                if "counter_examples" in payload:
                    module = payload.get("current_file")
                    if module:
                        counter_examples_by_module.setdefault(module, []).extend(
                            payload["counter_examples"]
                        )

        # Render each module's diff
        output_parts: list[str] = []
        for row in proposed_rows:
            module = row.module_path
            legacy_path = repo_path / module
            legacy_source = legacy_path.read_text(encoding="utf-8", errors="replace")
            proposed_source = row.code

            output_parts.append(f"\n{'=' * 60}")
            output_parts.append(f"Module: [bold cyan]{module}[/bold cyan]")
            if module in parity_by_module:
                output_parts.append(
                    f"Parity Score: [bold]{parity_by_module[module]:.2%}[/bold]"
                )
            output_parts.append(f"{'=' * 60}")

            if counter_examples_by_module.get(module):
                output_parts.append(
                    f"\n[bold yellow]Counter-examples ({len(counter_examples_by_module[module])}):[/bold yellow]"
                )
                for cx in counter_examples_by_module[module]:
                    output_parts.append(
                        f"  - {cx.get('function_name', '?')}: {cx.get('diff_summary', 'mismatch')}"
                    )

            if side_by_side:
                output_parts.append(_render_side_by_side(legacy_source, proposed_source))
            else:
                output_parts.append(_colorize_diff(legacy_source, proposed_source))

        full_output = "\n".join(output_parts)

        if export:
            export.write_text(full_output, encoding="utf-8")
            console.print(f"[green]Diff written to[/green] {export}")
        else:
            console.print(full_output)


__all__ = ["diff"]
