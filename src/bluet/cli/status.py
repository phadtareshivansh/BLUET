"""`bluet status` command - boots the Textual status TUI."""

from __future__ import annotations


def status() -> None:
    """Show the persisted job configuration (backend, status, limits) in a TUI."""
    from bluet.cli.status_app import BluetStatusApp

    BluetStatusApp().run()