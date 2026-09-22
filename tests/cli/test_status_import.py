"""Import smoke tests for the status TUI (does not launch the app)."""

from __future__ import annotations

from textual.app import App

from bluet.cli.status_app import BluetStatusApp
from bluet.state.job_config import JobConfig


def test_status_app_class() -> None:
    assert issubclass(BluetStatusApp, App)


def test_status_app_reads_config(tmp_path) -> None:
    config_path = tmp_path / "job.json"
    JobConfig(
        job_id="job_x", backend="hardened-docker", status="READY", created_at="2026-01-01"
    ).save(config_path)

    app = BluetStatusApp(config_path=config_path)
    assert app.config is not None
    assert app.config.backend == "hardened-docker"


def test_status_app_falls_back_to_placeholder(tmp_path) -> None:
    app = BluetStatusApp(config_path=tmp_path / "missing.json")
    assert app.config is None
