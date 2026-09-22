"""Tests for the BIOS-style ``bluet tui`` dashboard."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App

import bluet.cli.main
from bluet.cli.tui.app import BluetBiosApp, ModuleView, _patch_windows_loop
from bluet.cli.tui.dashboard import format_event_line, join_rows, parity_bar
from bluet.cli.tui.driver import _seeded_counts
from bluet.cli.tui.probe import RuntimeLimits, RuntimeState

_patch_windows_loop()


def _gvisor_state() -> RuntimeState:
    return RuntimeState(
        backend="gvisor",
        sandbox_label="GVisor Sandbox e1.9",
        host_backend="",
        docker_ok=True,
        docker_error=None,
        gvisor_available=True,
        limits=RuntimeLimits(),
        job_id="smoke",
        job_status="RUNNING",
        tier="small",
        llm_backend="ollama",
        llm_model="qwen2.5-coder:7b",
        llm_up=True,
        llm_health="ok",
    )


def test_tui_command_registered() -> None:
    assert any(info.name == "tui" for info in bluet.cli.main.app.registered_commands)


def test_app_class_is_textual() -> None:
    assert issubclass(BluetBiosApp, App)


def test_app_carries_canned_runtime() -> None:
    app = BluetBiosApp(canned_state=_gvisor_state())
    assert app.runtime.backend == "gvisor"


@pytest.mark.asyncio
async def test_headless_boot_and_halt_override() -> None:
    app = BluetBiosApp(canned_state=_gvisor_state())
    async with app.run_test() as pilot:
        await pilot.pause()

        topbar = app.query_one("#topbar")
        assert "GVisor Sandbox" in str(topbar.render())

        saw_halt = False
        for _ in range(30):
            if app._halted:
                saw_halt = True
                break
            await asyncio.sleep(0.3)
        assert saw_halt, "guardrail halt should fire within ~9s"

        event_log = app.query_one("#event-log")
        assert len(list(getattr(event_log, "lines", [])) or []) > 0

        await pilot.press("o")
        for _ in range(20):
            if not app._halted:
                break
            await asyncio.sleep(0.2)
        assert app._halted is False, "override should clear the halt"

        await pilot.press("q")
        await pilot.pause()


def test_module_view_cells_roundtrip() -> None:
    view = ModuleView(name="src/bluet/agents/_nodes.py")
    view.analyze = "…"
    assert "…" in view.cells()
    assert "ANALYZE" in view.cells()
    blank = ModuleView(name="")
    assert isinstance(blank.cells(), str)


def test_seeded_counts_are_deterministic() -> None:
    a = _seeded_counts("mod.py")
    b = _seeded_counts("mod.py")
    assert a == b
    assert "n_functions" in a and "nondeterministic" in a


def test_format_event_line_override_branch() -> None:
    lines = list(format_event_line({"topic": "feedback.warning", "severity": "override", "job_id": "job_7"}))
    joined = " | ".join(str(line) for line in lines)
    assert "OVERRIDE" in joined


def test_parity_bar_bounds() -> None:
    assert parity_bar(0).count("█") == 0
    assert parity_bar(1.0).count("█") == 10
    assert parity_bar(None) == parity_bar(0)


def test_join_rows_one_string() -> None:
    rows = join_rows(["alpha", "beta", "gamma"])
    assert rows == "alpha\nbeta\ngamma"