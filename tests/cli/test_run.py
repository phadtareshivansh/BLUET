"""CLI tests for the `bluet run` auto-gate."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bluet.cli import diagnostics as cli_diag_mod
from bluet.cli.main import app
from bluet.sandbox import runtime as rt_mod
from bluet.state.job_config import JobConfig

runner = CliRunner()


def _norm(output: str) -> str:
    return " ".join(re.findall(r"[\w./:=-]+", output))


def _redirect_doctor_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_write = cli_diag_mod.write_doctor_backend
    monkeypatch.setattr(
        cli_diag_mod,
        "write_doctor_backend",
        lambda backend, path=None: real_write(backend, tmp_path / "doctor.json"),
    )


def _apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, info_rc: int, gvisor_rc: int) -> None:
    monkeypatch.setattr(rt_mod, "get_execution_backend", lambda: "native-gvisor")
    monkeypatch.setattr(rt_mod, "host_os", lambda: "Linux")
    monkeypatch.setattr(cli_diag_mod, "host_os", lambda: "Linux")

    def _fake(cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        if "info" in argv:
            return subprocess.CompletedProcess(
                argv, info_rc, stderr="daemon down" if info_rc else ""
            )
        return subprocess.CompletedProcess(argv, gvisor_rc)

    monkeypatch.setattr(rt_mod.subprocess, "run", _fake)
    monkeypatch.setattr(cli_diag_mod, "CONFIG_PATH", tmp_path / "job.json")
    _redirect_doctor_cache(monkeypatch, tmp_path)


class TestRunGate:
    def test_docker_down_blocks_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _apply(monkeypatch, tmp_path, info_rc=1, gvisor_rc=0)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 1
        assert "bluet run aborted" in _norm(result.output)

        cfg = JobConfig.load(tmp_path / "job.json")
        assert cfg is not None
        assert cfg.status == "BLOCKED"

    def test_docker_up_proceeds_on_gvisor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path, info_rc=0, gvisor_rc=0)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 0
        out = _norm(result.output)
        assert "Sandbox verified" in out
        assert "backend=gvisor" in out

        cfg = JobConfig.load(tmp_path / "job.json")
        assert cfg is not None
        assert cfg.backend == "gvisor"
        assert cfg.status == "RUNNING"

    def test_docker_up_without_gvisor_warns_but_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply(monkeypatch, tmp_path, info_rc=0, gvisor_rc=1)
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 0
        out = _norm(result.output)
        assert "weaker isolation boundary" in out
        assert "backend=hardened-docker" in out

        cfg = JobConfig.load(tmp_path / "job.json")
        assert cfg is not None
        assert cfg.backend == "hardened-docker"
        assert cfg.status == "RUNNING"
