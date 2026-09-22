"""`bluet status` - Textual TUI over the persisted job configuration."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Log, Static

from bluet.state.job_config import CONFIG_PATH, JobConfig, do_now


class HeaderCard(Static):
    """Displays general job overview information."""

    def update_config(self, config: JobConfig | None) -> None:
        if not config:
            self.update("[bold red]No active job configuration found.[/bold red]")
            return

        text = (
            f"[bold cyan]Job ID:[/bold cyan] {config.job_id}  |  "
            f"[bold cyan]Backend:[/bold cyan] [yellow]{config.backend}[/yellow]  |  "
            f"[bold cyan]Status:[/bold cyan] [green]{config.status}[/green]"
        )
        self.update(text)


class ActionsSidebar(Static):
    """Sidebar containing quick action controls."""

    def compose(self) -> ComposeResult:
        yield Button("Run Job", id="btn-run", variant="success")
        yield Button("Cancel Job", id="btn-cancel", variant="error")
        yield Button("Switch Backend", id="btn-backend", variant="primary")


class BluetStatusApp(App):
    """Interactive status panel for the persisted job configuration."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    HeaderCard {
        height: 3;
        content-align: center middle;
        background: $panel;
        border: solid $primary;
        margin: 1 1 0 1;
    }

    #main-container {
        layout: horizontal;
        height: 1fr;
        margin: 1;
    }

    ActionsSidebar {
        width: 24;
        background: $panel;
        border: solid $secondary;
        padding: 1;
    }

    ActionsSidebar Button {
        width: 100%;
        margin-bottom: 1;
    }

    Log {
        width: 1fr;
        border: solid $accent;
        background: $surface-darken-1;
        margin-left: 1;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh Status"),
    ]

    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        super().__init__()
        self.config_path = config_path
        self.config = JobConfig.load(self.config_path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield HeaderCard(id="header-card")

        with Horizontal(id="main-container"):
            yield ActionsSidebar()
            yield Log(id="job-log")

        yield Footer()

    def on_mount(self) -> None:
        self.title = "bluet status"
        header = self.query_one(HeaderCard)
        log = self.query_one(Log)

        if not self.config:
            self.config = JobConfig(
                job_id="job_01h9x8y",
                backend="unknown",
                status="IDLE",
                created_at=do_now(),
            )
            self.config.save(self.config_path)
            log.write_line(
                f"[system] No job config found - created placeholder at {self.config_path}"
            )
            log.write_line("[system] Run `bluet doctor` to record the real sandbox backend.")

        header.update_config(self.config)
        log.write_line(f"Loaded job configuration: {self.config.job_id}")
        log.write_line(f"Active backend: {self.config.backend}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        log = self.query_one(Log)
        header = self.query_one(HeaderCard)

        if event.button.id == "btn-run":
            if self.config:
                self.config.status = "RUNNING"
                self.config.save(self.config_path)
                header.update_config(self.config)
                log.write_line("[bold green]Job execution started on backend.[/bold green]")

        elif event.button.id == "btn-cancel":
            if self.config:
                self.config.status = "CANCELLED"
                self.config.save(self.config_path)
                header.update_config(self.config)
                log.write_line("[bold red]Job execution cancelled.[/bold red]")

        elif event.button.id == "btn-backend" and self.config is not None:
            self.config.backend = (
                "hardened-docker" if self.config.backend != "hardened-docker" else "gvisor"
            )
            self.config.save(self.config_path)
            header.update_config(self.config)
            log.write_line(
                f"[bold yellow]Switch planned - backend toggled in config to "
                f"{self.config.backend} (recording only; re-run `bluet doctor` to "
                f"re-diagnose).[/bold yellow]"
            )

    def action_refresh(self) -> None:
        self.config = JobConfig.load(self.config_path)
        self.query_one(HeaderCard).update_config(self.config)
        self.query_one(Log).write_line("Refreshed state from disk.")


if __name__ == "__main__":
    BluetStatusApp().run()
