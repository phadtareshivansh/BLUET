"""BIOS-style dashboard: ``bluet tui`` — a Textual app in 1990s AMI/Award drag.

The layout mirrors the reference wireframe 1:1: status bar / nav tabs /
(event log | pipeline art) / two bank rows / footer wordmark. Color and font
follow the phosphor spec. The left log is fed by a real EventBus (see
:mod:`bluet.cli.tui.driver`), the right panel materializes its ASCII pipeline
from static noise, guardrail BLOCKs blink the status bar amber, and a sandbox
refusal turns the whole right panel into red static.
"""

from __future__ import annotations

import asyncio
import random
import sys
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import RichLog, Static

from bluet.cli.tui import dashboard
from bluet.cli.tui.dashboard import (
    AMBER,
    DIM,
    GREEN,
    MINT,
    RED,
)
from bluet.cli.tui.driver import MAX_RETRIES, DemoDriver, build_files
from bluet.cli.tui.probe import RuntimeState, probe_runtime, snapshot_from_cache

SCANLINE = "#0B1B2B"

NOISE_CHARS = "█▓▒░┃┆│╱╲"


def _patch_windows_loop() -> None:
    """zmq.asyncio needs a selector loop on Windows; patch before any loop exists."""
    if sys.platform == "win32" and asyncio.get_event_loop_policy().__class__.__name__ == (
        "WindowsProactorEventLoopPolicy"
    ):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class TabsChanged(Message):
    """Raised when the user moves the BIOS menu cursor."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


@dataclass
class ModuleView:
    """Per-module pipeline state tracked from EventBus messages."""

    name: str
    analyze: str = "—"
    synthesize: str = "—"
    verify: str = "—"
    parity: str = "—"
    status: str = "pending"  # pending/running/done/failed/halted
    n_functions: int = 0
    flagged: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    score: float | None = None
    checks: int = 0
    proposed_lines: int = 0
    imports: list[str] = field(default_factory=list)
    retries: int = 0
    counter_examples: list[dict[str, Any]] = field(default_factory=list)

    def cells(self) -> str:
        return dashboard.stage_markers(self.analyze, self.synthesize, self.verify, self.parity)


def _pct(state: str) -> str:
    return state if state != "done" else "✓"


def _auto_label(backend: str) -> str:
    port = "11434" if "ollama" in backend else "8000"
    return f"{backend}:{port}"


class TopBar(Static):
    """Docked status line: product, mode, sandbox, job, version."""


class TabLabel(Static):
    """A single BIOS nav tab; click + electric-blue cursor handled by parent."""

    def __init__(self, label: str, index: int) -> None:
        super().__init__(label, id=f"tab-{index}")
        self.tab_index = index

    def on_click(self, event: Any) -> None:
        self.post_message(TabsChanged(self.tab_index))


class TabBar(Vertical):
    """Row of tab labels with an inverted-mint active cursor."""

    def __init__(self, labels: list[str], index: int = 0) -> None:
        super().__init__(id="tabbar")
        self.labels = list(labels)
        self._index = index

    def compose(self) -> ComposeResult:
        with Horizontal():
            for i, label in enumerate(self.labels):
                yield TabLabel(label, i)

    def select(self, index: int) -> None:
        self._index = index % len(self.labels)
        for i in range(len(self.labels)):
            tab = self.query_one(f"#tab-{i}", TabLabel)
            tab.set_class(i == self._index, "active")


class EventLog(RichLog):
    """Left-hand scrolling boot log fed by the event bus."""

    def boot(self, header: str) -> None:
        self.write(Text(header, style=DIM))


class StagePanel(Static):
    """Right-hand panel: pipeline art / stage tablet / refusal static."""


class BankRow(Static):
    """One BANK row (stage checklist / runtime status)."""


class HeartbeatBox(Static):
    """Tiny bordered liveness box, bottom-right above the wordmark."""


class Wordmark(Static):
    """Glitch-outlined BLUET logotype + MODERNIZE BOLDLY tagline."""


class BluetBiosApp(App[None]):
    """The BIOS dashboard itself."""

    DEFAULT_CSS = """
    Screen {
        background: #04070D;
        color: #58C7F3;
        hatch: horizontal #0B1B2B;
    }

    #topbar, #tabbar, #event-log, #stage-content, #bank1, #bank2, #footer {
        hatch: horizontal #0B1B2B;
    }

    #topbar {
        height: 1;
        background: #04070D;
        color: #58C7F3;
        border-bottom: round #0B1B2B;
        content-align: left middle;
        padding: 0 1;
    }

    #topbar.halted { color: #F6C945; }
    #topbar.halted.blink { text-opacity: 0.15; }

    #tabbar {
        height: 1;
        width: 100%;
        overflow: hidden;
    }

    #tabbar > Horizontal {
        height: 1;
        width: auto;
    }

    TabLabel {
        height: 1;
        color: #29608F;
        padding: 0 1;
    }

    TabLabel.active {
        background: #58C7F3;
        color: #04070D;
        text-style: bold;
    }

    #body {
        height: 1fr;
        min-height: 8;
        overflow: hidden;
    }

    #event-log {
        width: 1fr;
        border: round #29608F;
        background: transparent;
        margin-right: 1;
        scrollbar-color: #29608F;
        scrollbar-background: transparent;
        padding: 0 0 0 1;
    }

    #stage-wrap {
        width: 42%;
        min-width: 30;
        height: 1fr;
    }

    #stage-content {
        height: 1fr;
        border: round #29608F;
        background: transparent;
        content-align: center middle;
        overflow: hidden;
        padding: 0 1;
    }

    #heartbeat {
        height: 3;
        width: 24;
        border: round #29608F;
        background: #06131C;
        color: #29608F;
        content-align: left middle;
        margin-top: 1;
    }

    #heartbeat.good { color: #4FD68A; }
    #heartbeat.warn { color: #F6C945; }
    #heartbeat.bad  { color: #D64545; }

    #bank1, #bank2 {
        height: 1;
        width: 100%;
        color: #58C7F3;
        border-bottom: round #0B1B2B;
        background: transparent;
        content-align: left middle;
        padding: 0 1;
    }

    #bank2 { border-bottom: none; }

    #footer {
        height: 6;
        width: 100%;
        background: transparent;
        overflow: hidden;
    }

    #legend {
        width: 38%;
        height: 100%;
        color: #29608F;
        content-align: left bottom;
        padding: 0 1;
    }

    #wordmark-col {
        width: 62%;
        height: 100%;
        align: right middle;
    }

    #wordmark {
        color: #58C7F3;
        text-style: bold;
        width: auto;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("tab", "next_tab", "Next tab"),
        ("shift+tab", "prev_tab", "Prev tab"),
        ("left", "prev_tab", "Prev tab"),
        ("right", "next_tab", "Next tab"),
        ("1", "tab1", "Overview"),
        ("2", "tab2", "Analyze"),
        ("3", "tab3", "Synthesize"),
        ("4", "tab4", "Verify"),
        ("5", "tab5", "Diff"),
        ("6", "tab6", "Doctor"),
        ("enter", "override", "Override halt"),
        ("o", "override", "Override halt"),
        ("r", "rescan", "Re-probe runtime"),
        ("q", "quit", "Quit"),
    ]

    TABS = ("OVERVIEW", "ANALYZE", "SYNTHESIZE", "VERIFY", "DIFF", "DOCTOR")

    def __init__(
        self,
        *,
        seed: int = 7,
        canned_state: RuntimeState | None = None,
        target_files: list[str] | None = None,
    ) -> None:
        _patch_windows_loop()
        super().__init__()
        self._seed = seed
        self._rng = random.Random(seed)
        self._canned = canned_state
        self.runtime: RuntimeState = snapshot_from_cache() if canned_state is None else canned_state
        self.modules: dict[str, ModuleView] = {}
        self.driver: DemoDriver | None = None
        self.job_display: str = "#----"
        self._halted = False
        self._release = asyncio.Event()
        self._halting_reason = ""
        self._tab = 0
        self._art_progress = 0.0
        self._art_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._boot_task: asyncio.Task[None] | None = None
        self._driver_started = False
        self._blink_job: Any = None
        self._tick = 0
        self._files = list(target_files or build_files())
        self.title = "bluet tui"

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        yield TabBar(self.TABS)
        with Horizontal(id="body"):
            yield EventLog(id="event-log", max_lines=240, auto_scroll=True)
            with Vertical(id="stage-wrap"):
                yield StagePanel(id="stage-content")
                yield HeartbeatBox("", id="heartbeat")
        yield BankRow("", id="bank1")
        yield BankRow("", id="bank2")
        with Horizontal(id="footer"):
            yield Static(
                "TAB 1-6 JUMP  ENTER OVERRIDE  R RESCAN  Q QUIT",
                id="legend",
            )
            with Vertical(id="wordmark-col"):
                yield Wordmark("", id="wordmark")

    def on_mount(self) -> None:
        self.query_one(TabBar).select(0)
        self._boot_task = asyncio.create_task(self._boot())
        self._heartbeat = self.set_interval(1.0, self._tick_heartbeat)
        self._wordmark_flick = self.set_interval(0.18, self._flick_wordmark)
        self._blink_job = self.set_interval(0.55, self._blink_toggle)

    async def on_unmount(self) -> None:
        for task in (self._boot_task, self._probe_task, self._art_task):
            if task and not task.done():
                task.cancel()
        if self.driver is not None:
            await self.driver.stop()

    # -- lifecycle ---------------------------------------------------------

    async def _boot(self) -> None:
        log = self.query_one(EventLog)
        log.boot("[@POST]         power-on self test ....... OK")
        backend = self.runtime.sandbox_label
        log.boot(f"[@doctor]       cache backend={backend}  job={self.runtime.job_id}")
        log.boot("[@sequence]     bluet.tui v0.1.0  1-6:tabs  enter:override")
        self._apply_runtime(self.runtime)
        self._refresh_stage()
        self._maybe_animate()

        self._probe_task = asyncio.create_task(self._probe_and_refresh())

    async def _probe_and_refresh(self) -> None:
        if self._canned is not None:
            await self._maybe_start_driver()
            return
        log = self.query_one(EventLog)
        log.boot("[@doctor]       probing docker/gVisor + LLM ...")
        state = await probe_runtime()
        self.runtime = state
        self._apply_runtime(state)
        log.write(
            Text(
                " doctor: "
                f"docker={'OK' if state.docker_ok else 'DOWN'} "
                f"gvisor={'YES' if state.gvisor_available else 'NO'} "
                f"backend={state.sandbox_label}  llm={state.llm_model or 'n/a'} {state.llm_health}",
                style=GREEN if state.docker_ok else AMBER,
            )
        )
        await self._maybe_start_driver()

    async def _maybe_start_driver(self) -> None:
        if self._driver_started:
            return
        if self.runtime.backend == "none":
            self._enter_refusal()
            return
        self._driver_started = True
        self._set_refusal_off()
        log = self.query_one(EventLog)
        log.write(
            Text(f" bus         inproc://bluet-events-{self.job_short()}  subscribed 6 topics", style=DIM)
        )
        driver = DemoDriver(
            self._files,
            self.runtime.backend,
            halt_hook=self._wait_for_override,
            handler=self._on_bus,
            max_retries=MAX_RETRIES,
        )
        self.driver = driver
        await driver.start()
        await driver.drive()

    def _enter_refusal(self) -> None:
        top = self.query_one("#topbar", TopBar)
        top.update(f" BLUET (TM)  LOCAL INFERENCE  SANDBOX: {self.runtime.sandbox_label}  JOB {self.job_display}  v0.1.0")
        log = self.query_one(EventLog)
        log.write(
            Text(" POST         FAILED — sandboxed verification impossible", style=RED)
        )
        if self.runtime.docker_error:
            log.write(Text(f"               {self.runtime.docker_error}", style=AMBER))
        log.write(Text("               install/start Docker, then [r]rescan", style=AMBER))
        self._refresh_stage()

    def _set_refusal_off(self) -> None:
        pass

    def job_short(self) -> str:
        return str(self.runtime.job_id).replace("job_", "")[:4]

    def _apply_runtime(self, state: RuntimeState) -> None:
        self.runtime = state
        if self._halted:
            return
        self._refresh_topbar()
        self._refresh_bank2()
        self._refresh_heartbeat()

    def _refresh_topbar(self) -> None:
        self.query_one("#topbar", TopBar).update(
            f" BLUET (TM)  LOCAL INFERENCE  SANDBOX: {self.runtime.sandbox_label}  "
            f"JOB {self.job_display}  v0.1.0"
        )

    # -- bus handler --------------------------------------------------------

    async def _on_bus(self, payload: dict[str, Any]) -> None:
        self._record_event(payload)
        self._refresh_bank1()
        self._refresh_bank2()
        self._refresh_stage()

    def _record_event(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic", ""))
        file = str(payload.get("current_file", ""))
        mod = self.modules.setdefault(file, ModuleView(name=file))
        for line in dashboard.format_event_line(payload):
            self.query_one(EventLog).write(line)

        job = payload.get("job_id")
        if job is not None and not self._halted:
            label = str(job).replace("job_", "")
            if label != self.job_display:
                self.job_display = "#" + label[-4:]
                self._refresh_topbar()

        if topic == "task.analysis":
            stage = payload.get("stage")
            if stage == "start":
                mod.analyze = "…"
                mod.status = "running"
            elif stage == "analyzed":
                mod.analyze = "✓"
                mod.n_functions = int(payload.get("n_functions") or 0)
                mod.flagged = [str(f) for f in (payload.get("nondeterministic") or [])]

        elif topic == "task.refactor":
            stage = payload.get("stage")
            if stage == "start":
                mod.synthesize = "…"
            elif stage == "refactored":
                mod.synthesize = "✓"
                mod.proposed_lines = int(payload.get("proposed_lines") or 0)
                mod.imports = [str(i) for i in (payload.get("imports_added") or [])]

        elif topic == "task.verify":
            stage = payload.get("stage")
            if stage == "start":
                mod.verify = "…"
            elif stage == "verified":
                status = payload.get("status")
                mod.checks = int(payload.get("n_checks") or 0)
                mod.score = payload.get("score")
                mod.counter_examples = list(payload.get("counter_examples") or [])
                if status == "fail":
                    mod.verify = "✗"
                    mod.parity = "✗"
                    reason = payload.get("reason")
                    if reason == "retries exhausted":
                        mod.status = "failed"
                        self.query_one(EventLog).write(
                            Text(f" [{file}] retries exhausted — JOB FAILED", style=RED)
                        )
                else:
                    mod.verify = "✓"
                    mod.parity = "✓"
                    mod.status = "done"

        elif topic == "feedback.regression":
            mod.retries = int(payload.get("retry_count") or 0) + mod.retries
            mod.verify = "…"
            mod.parity = "…"
            mod.status = "running"

        elif topic == "feedback.warning":
            if payload.get("source") == "guardrail" and payload.get("severity") == "block":
                mod.status = "halted"
                mod.synthesize = "✗"
                self._halting_reason = str(payload.get("reason", "guardrail"))
            elif payload.get("source") == "guardrail" and payload.get("severity") == "override":
                mod.status = "running"
                mod.synthesize = "…"

    # -- halt / override ----------------------------------------------------

    def _enter_halt(self) -> None:
        if self._halted:
            return
        self._halted = True
        top = self.query_one("#topbar", TopBar)
        top.update(f" ■ HALTED — OVERRIDE REQUIRED  {self._halting_reason}  (enter) ")
        top.add_class("halted")
        self.query_one("#stage-content", StagePanel).update(
            self._craft_refusal_text(" HALTED — OVERRIDE REQUIRED ")
        )
        self._refresh_bank1()

    def _exit_halt(self) -> None:
        self._halted = False
        top = self.query_one("#topbar", TopBar)
        top.remove_class("halted")
        self._blink_toggle(force_hide=True)
        self._apply_runtime(self.runtime)

    def _blink_toggle(self, force_hide: bool = False) -> None:
        top = self.query_one("#topbar", TopBar)
        if force_hide or not self._halted:
            top.remove_class("blink")
            return
        top.toggle_class("blink")

    async def _wait_for_override(self) -> None:
        self._enter_halt()
        self._release.clear()
        await self._release.wait()
        self._exit_halt()

    def action_override(self) -> None:
        if self._halted:
            self._release.set()

    # -- runtime row / hearts ------------------------------------------------

    def _tick_heartbeat(self) -> None:
        box = self.query_one("#heartbeat", HeartbeatBox)
        self._tick += 1
        dots = "." * (3 - self._tick % 3)
        llm = f" llm {_auto_label(self.runtime.llm_backend)} {dots} {self.runtime.llm_health}"
        sand = f" sandbox {self.runtime.sandbox_label} {dots} {'OK' if self.runtime.backend != 'none' else 'FAIL'}"
        line = llm if self._tick % 2 else sand
        box.update(line)
        box.set_class(self.runtime.llm_up, "good")
        box.set_class(not self.runtime.llm_up, "warn")
        box.set_class(self.runtime.backend == "none", "bad")

    def _refresh_heartbeat(self) -> None:
        self._tick_heartbeat()

    def _flick_wordmark(self) -> None:
        wm = self.query_one("#wordmark", Wordmark)
        rows = (
            dashboard.wordmark_glitched("BLUET", self._rng)
            if self._rng.random() < 0.3
            else dashboard.wordmark("BLUET")
        )
        text = Text()
        for i, row in enumerate(rows):
            text.append(row, style=MINT if i != 2 else GREEN)
            if i != len(rows) - 1:
                text.append("\n")
        text.append("\nMODERNIZE BOLDLY. VERIFY EVERYTHING.", style=DIM)
        wm.update(text)

    # -- bank rows -----------------------------------------------------------

    def _refresh_bank1(self) -> None:
        cells: list[Text] = [Text("BANK 1  ", style=DIM)]
        for name, mod in list(self.modules.items()):
            label = name.rsplit("/", 1)[-1]
            if len(label) > 12:
                label = label[:11] + "~"
            cell = Text()
            cell.append(f"{label} ", style=AMBER if mod.status == "halted" else MINT)
            cell.append(mod.cells(), style=self._stage_color(mod))
            cells.append(cell)
            cells.append(Text("   ", style=DIM))
        self._clamp_row(cells)

    def _stage_color(self, mod: ModuleView) -> str:
        if mod.status == "failed":
            return RED
        if mod.status == "halted":
            return AMBER
        if mod.status == "done":
            return GREEN
        return MINT

    def _refresh_bank2(self) -> None:
        bank = self.query_one("#bank2", BankRow)
        r = self.runtime
        retry_total = sum(m.retries for m in self.modules.values())
        text = Text()
        text.append("BANK 2  ", style=DIM)
        text.append(f"LLM: {r.llm_model or 'n/a'}  ", style=MINT)
        text.append(f"BACKEND: {_auto_label(r.llm_backend)}  ", style=MINT)
        text.append(f"{r.memory_text} TIER:{r.tier}  ", style=MINT)
        text.append(f"RETRY {retry_total}/{MAX_RETRIES}", style=AMBER if retry_total else GREEN)
        bank.update(text)

    def _clamp_row(self, cells: list[Text]) -> None:
        full = Text()
        for c in cells:
            full.append_text(c)
        if len(full.plain) > 300:
            full = full[:300]
            full.append("…", style=DIM)
        bank = self.query_one("#bank1", BankRow)
        bank.update(full)

    # -- stage panel ---------------------------------------------------------

    def _refresh_stage(self) -> None:
        panel = self.query_one("#stage-content", StagePanel)
        if self.runtime.backend == "none":
            panel.update(self._craft_refusal_text())
            return
        if self._tab == 0:
            self._render_overview(panel)
        elif self._tab == 1:
            panel.update(self._craft_analyze())
        elif self._tab == 2:
            panel.update(self._craft_synthesize())
        elif self._tab == 3:
            panel.update(self._craft_verify())
        elif self._tab == 4:
            panel.update(self._craft_diff())
        else:
            panel.update(self._craft_doctor())

    def _craft_refusal_text(self, banner: str = " POST FAILED — SANDBOX REFUSED ") -> Text:
        rows = [self._rng.choice(NOISE_CHARS) * 40 for _ in range(6)]
        mid = len(rows) // 2
        text = Text()
        for i, row in enumerate(rows):
            if i == mid:
                text.append(banner.center(40, " "), style=AMBER)
            else:
                text.append(row, style=RED)
            if i != len(rows) - 1:
                text.append("\n")
        return text

    def _render_overview(self, panel: StagePanel) -> None:
        height = max(panel.size.height, 1)
        art = self._current_art(compact=height < 14)
        text = Text(dashboard.join_rows(art), style=MINT)
        modules = list(self.modules.values())
        if not modules:
            text.append("\n\n no modules yet — pipeline incoming", style=DIM)
        else:
            for mod in modules:
                score = mod.score
                bar = dashboard.parity_bar(score)
                marker = "✓" if mod.status in {"done", "halted"} else ("✗" if mod.status == "failed" else "…")
                cell = Text()
                cell.append(f"{mod.name.rsplit('/',1)[-1]:<12} ", style=DIM)
                cell.append(f"{bar} {score if score is not None else '-':<6} ", style=self._stage_color(mod))
                cell.append(marker, style=self._stage_color(mod))
                text.append("\n")
                text.append_text(cell)
        panel.update(text)

    def _current_art(self, *, compact: bool) -> list[str]:
        art = dashboard.PIPELINE_COMPACT if compact else dashboard.PIPELINE_TALL
        total = sum(m.n_functions for m in self.modules.values())
        done = sum(1 for m in self.modules.values() if m.status == "done")
        n = max(len(self.modules), 1)
        rendered = [
            row.format(
                load=f"{total}f",
                synth="✓" if done == n else "…",
                verify=f"{done}/{n}",
                parity="1.0" if done == n and not any(m.parity == "✗" for m in self.modules.values()) else "…",
                max=str(MAX_RETRIES),
            )
            for row in art
        ]
        return dashboard.materialize(rendered, self._art_progress, self._rng)

    def _maybe_animate(self) -> None:
        if self._tab == 0 and (self._art_task is None or self._art_task.done()):
            self._art_task = asyncio.create_task(self._anim_art())

    async def _anim_art(self) -> None:
        self._art_progress = 0.0
        try:
            for _ in range(20):
                self._art_progress = min(1.0, self._art_progress + 0.07)
                self._render_overview(self.query_one("#stage-content", StagePanel))
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    # -- stage tablets -------------------------------------------------------

    def _craft_analyze(self) -> Text:
        text = Text("ANALYZE — LOGIC SPEC", style=MINT)
        if not self.modules:
            text.append("\n\n awaiting task.analysis ...", style=DIM)
            return text
        for mod in self.modules.values():
            text.append(f"\n{mod.name.rsplit('/',1)[-1]:<16} {mod.n_functions}f", style=MINT)
            if mod.flagged:
                text.append(f"  !{len(mod.flagged)} non-deterministic", style=AMBER)
        return text

    def _craft_synthesize(self) -> Text:
        text = Text("SYNTHESIZE — PROPOSED", style=MINT)
        if not self.modules:
            text.append("\n\n awaiting task.refactor ...", style=DIM)
            return text
        for mod in self.modules.values():
            line = Text(f"\n{mod.name.rsplit('/',1)[-1]:<16}")
            line.append(f"{mod.proposed_lines}L", style=GREEN)
            if mod.imports:
                line.append(f"  +{len(mod.imports)} imports", style=DIM)
            if mod.synthesize == "✓":
                line.append("  schema-valid", style=GREEN)
            text.append_text(line)
        return text

    def _craft_verify(self) -> Text:
        text = Text("VERIFY — PARITY", style=MINT)
        if not self.modules:
            text.append("\n\n awaiting task.verify ...", style=DIM)
            return text
        for mod in self.modules.values():
            line = Text(f"\n{mod.name.rsplit('/',1)[-1]:<16}")
            if mod.score is not None:
                line.append(f"{mod.checks} checks  score={mod.score}", style=GREEN if mod.parity == "✓" else RED)
            for cx in mod.counter_examples:
                line.append(f"\n   fx={cx.get('function_name')}  {cx.get('diff_summary', '')}", style=AMBER)
            text.append_text(line)
        return text

    def _craft_diff(self) -> Text:
        text = Text("DIFF — LEGACY → MODERN", style=MINT)
        if not self.modules:
            text.append("\n\n no diff material yet — run a pipeline", style=DIM)
            return text
        for mod in self.modules.values():
            if not mod.names:
                continue
            fn = mod.names[0]
            text.append(f"\n--- legacy/{mod.name}", style=RED)
            text.append(f"\n+++ modern/{mod.name}", style=GREEN)
            text.append(f"\n@@ {fn} @@", style=DIM)
            text.append(f"\n- def {fn}(input): ...", style=RED)
            text.append(f"\n+ def {fn}(input: TypedInput): ...", style=GREEN)
        return text

    def _craft_doctor(self) -> Text:
        r = self.runtime
        text = Text("DOCTOR — RUNTIME", style=MINT)
        rows = [
            ("HOST OS", r.host_backend or "detecting", MINT),
            ("DOCKER DAEMON", "running" if r.docker_ok else (r.docker_error or "down"), GREEN if r.docker_ok else RED),
            ("GVISOR (RUNSC)", "available" if r.gvisor_available else "unavailable", GREEN if r.gvisor_available else AMBER),
            ("BACKEND", r.sandbox_label, RED if r.backend == "none" else GREEN),
            ("LIMITS", r.limits.as_text(), MINT),
            ("LLM BACKEND", _auto_label(r.llm_backend), MINT),
            ("LLM MODEL", r.llm_model or "n/a", MINT),
            ("LLM HEALTH", r.llm_health, GREEN if r.llm_up else AMBER),
            ("MEMORY", r.memory_text + f" TIER:{r.tier}", MINT),
        ]
        for key, value, color in rows:
            text.append(f"\n{key:<14}", style=DIM)
            text.append(value, style=color)
        return text

    # -- tab actions ---------------------------------------------------------

    def _set_tab(self, index: int) -> None:
        self._tab = index % len(self.TABS)
        self.query_one(TabBar).select(index % len(self.TABS))
        self._refresh_stage()
        self._maybe_animate()

    def action_next_tab(self) -> None:
        self._set_tab(self._tab + 1)

    def action_prev_tab(self) -> None:
        self._set_tab(self._tab - 1)

    def action_tab1(self) -> None:
        self._set_tab(0)

    def action_tab2(self) -> None:
        self._set_tab(1)

    def action_tab3(self) -> None:
        self._set_tab(2)

    def action_tab4(self) -> None:
        self._set_tab(3)

    def action_tab5(self) -> None:
        self._set_tab(4)

    def action_tab6(self) -> None:
        self._set_tab(5)

    def action_rescan(self) -> None:
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_and_refresh())

    def on_tabs_changed(self, message: TabsChanged) -> None:
        self._set_tab(message.index)
        message.stop()


__all__ = ["BluetBiosApp"]