"""CLI integration tests for `bluet run <path>` and `bluet status <job_id>`."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

from bluet.cli import diagnostics as cli_diag_mod
from bluet.cli.main import app
from bluet.sandbox import runtime as rt_mod
from bluet.state.db import engine_for_repo
from bluet.state.repository import get_job_by_id, list_events_for_job

runner = CliRunner()

FIXTURE = Path("tests/fixtures/python-legacy/simple_function.py")


def _apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rt_mod, "get_execution_backend", lambda: "native-gvisor")
    monkeypatch.setattr(rt_mod, "host_os", lambda: "Linux")
    monkeypatch.setattr(cli_diag_mod, "host_os", lambda: "Linux")
    monkeypatch.setenv("BLUET_LIVE", "0")

    real_write = cli_diag_mod.write_doctor_backend

    def _fake(cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        if "info" in argv:
            return subprocess.CompletedProcess(argv, 0, stderr="")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(rt_mod.subprocess, "run", _fake)
    monkeypatch.setattr(
        cli_diag_mod,
        "write_doctor_backend",
        lambda backend, path=None: real_write(backend, tmp_path / "doctor.json"),
    )
    monkeypatch.setattr(cli_diag_mod, "CONFIG_PATH", tmp_path / "job.json")


def _job_id(output: str) -> int:
    match = re.search(r"Job\s+#?(\d+)", output)
    assert match is not None, f"no job id found in output:\n{output}"
    return int(match.group(1))


class TestRunPipeline:
    def test_run_file_sets_up_completed_job_and_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        shutil.copy(FIXTURE, repo / "simple_function.py")

        result = runner.invoke(app, ["run", str(repo / "simple_function.py")])
        assert result.exit_code == 0, result.output
        job_id = _job_id(result.output)
        assert "compute_total" in result.output

        status = runner.invoke(app, ["status", str(job_id), "--repo", str(repo)])
        assert status.exit_code == 0, status.output
        assert "COMPLETED" in status.output
        assert "simple_function.py" in status.output

    def test_run_directory_analyzes_every_source_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        shutil.copy(FIXTURE, repo / "simple_function.py")
        (repo / "other.py").write_text("def double(x):\n    return x + x\n")
        (repo / "notes.txt").write_text("not a source file")

        result = runner.invoke(app, ["run", str(repo)])
        assert result.exit_code == 0, result.output
        assert "2 file(s)" in result.output
        assert "compute_total" in result.output
        assert "double" in result.output

    def test_run_persists_job_and_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        shutil.copy(FIXTURE, repo / "simple_function.py")

        result = runner.invoke(app, ["run", str(repo / "simple_function.py")])
        assert result.exit_code == 0, result.output
        job_id = _job_id(result.output)

        async def _read() -> tuple[str, set[str]]:
            engine = engine_for_repo(repo)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session:
                    job = await get_job_by_id(session, job_id)
                    events = await list_events_for_job(session, job_id)
                    assert job is not None
                    return job.status, {e.event_type for e in events}
            finally:
                await engine.dispose()

        status, event_types = asyncio.run(_read())
        assert status == "COMPLETED"
        assert {"task.analysis", "task.context", "task.refactor", "task.verify"} <= event_types


class TestStatusTable:
    def test_status_missing_database_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["status", "1", "--repo", str(tmp_path / "absent")])
        assert result.exit_code == 1
        assert "No state database found" in result.output

    def test_status_unknown_job_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        shutil.copy(FIXTURE, repo / "simple_function.py")
        runner.invoke(app, ["run", str(repo / "simple_function.py")])

        result = runner.invoke(app, ["status", "999", "--repo", str(repo)])
        assert result.exit_code == 1
        assert "Job 999 not found" in result.output
