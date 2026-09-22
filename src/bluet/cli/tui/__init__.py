"""``bluet tui`` — 1990s BIOS dashboard for the BLUET pipeline.

Launches a Textual TUI styled after a classic AMI/Award setup screen: a status
bar with the sandbox backend, BIOS-style nav tabs, a live event log fed by a
real in-process EventBus, an ASCII pipeline being materialized on the right,
two BANK status rows, a heartbeat box, and a flickering block-font wordmark.
A guardrail BLOCK halts the line with a blinking amber ``HALTED — OVERRIDE
REQUIRED`` band until the human presses Enter; a refused sandbox turns the
right panel into red static.
"""

from __future__ import annotations

from typing import Annotated

import typer


def tui(
    none: Annotated[
        bool,
        typer.Option(
            "--none",
            help="Force the SANDBOX refused (red static) demo mode.",
        ),
    ] = False,
) -> None:
    """Launch the BIOS-style TUI dashboard (Ctrl+C or [q] to quit)."""
    from bluet.cli.tui.app import BluetBiosApp, _patch_windows_loop

    _patch_windows_loop()

    if none:
        from bluet.cli.tui.probe import RuntimeState
        from bluet.sandbox.runtime import RuntimeLimits

        state = RuntimeState(
            backend="none",
            sandbox_label="NONE — REFUSED",
            host_backend="windows",
            docker_ok=False,
            docker_error="docker: command refused (forced --none)",
            gvisor_available=False,
            limits=RuntimeLimits(),
            llm_backend="auto",
            llm_health="unprobed",
            tier="dev",
            job_id="none",
        )
        BluetBiosApp(canned_state=state).run()
        return

    BluetBiosApp().run()


__all__ = ["tui"]