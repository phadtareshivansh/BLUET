"""CLI tests for `bluet doctor` backend selection + exit behavior.

All combinations are mocked; no real docker/gVisor probing ever runs.
"""

from __future__ import annotations

import json
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


def _redirect_doctor_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_write = cli_diag_mod.write_doctor_backend
    monkeypatch.setattr(
        cli_diag_mod,
        "write_doctor_backend",
        lambda backend, path=None: real_write(backend, tmp_path / "doctor.json"),
    )


def _apply_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    info_rc: int = 0,
    gvisor_rc: int = 0,
) -> None:
    monkeypatch.setattr(rt_mod, "get_execution_backend", lambda: "native-gvisor")
    monkeypatch.setattr(rt_mod, "host_os", lambda: "Linux")
    monkeypatch.setattr(cli_diag_mod, "host_os", lambda: "Linux")

    def _fake(cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        if "info" in argv:
            return subprocess.CompletedProcess(argv, info_rc, stderr="daemon down" if info_rc else "")
        return subprocess.CompletedProcess(argv, gvisor_rc)

    monkeypatch.setattr(rt_mod.subprocess, "run", _fake)
    monkeypatch.setattr(cli_diag_mod, "CONFIG_PATH", tmp_path / "job.json")
    _redirect_doctor_cache(monkeypatch, tmp_path)


def _loaded(tmp_path: Path) -> JobConfig | None:
    assert JobConfig.load(tmp_path / "job.json") is not None
    return JobConfig.load(tmp_path / "job.json")


def _norm(output: str) -> str:
    return " ".join(re.findall(r"[\w./:=-]+", output))


class TestDockerDown:
    def test_exits_nonzero_and_blocks(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=1)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        out = _norm(result.output)
        assert "docker daemon not reachable" in out
        assert "https://docs.docker.com/get-docker/" in out

    def test_records_blocked_unknown_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=1)
        runner.invoke(app, ["doctor"])
        cfg = _loaded(tmp_path)
        assert cfg is not None
        assert cfg.backend == "unknown"
        assert cfg.status == "BLOCKED"
        assert cfg.docker_running is False


class TestDockerUpGvisorAvailable:
    def test_exits_zero_and_selects_gvisor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=0, gvisor_rc=0)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        out = _norm(result.output)
        assert "gVisor runsc" in out
        assert "gvisor" in out

    def test_records_gvisor_backend(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=0, gvisor_rc=0)
        runner.invoke(app, ["doctor"])
        cfg = _loaded(tmp_path)
        assert json.loads((tmp_path / "doctor.json").read_text())["backend"] == "gvisor"
        assert cfg is not None
        assert cfg.backend == "gvisor"
        assert cfg.status == "READY"
        assert cfg.gvisor_available is True
        assert cfg.docker_running is True
        assert cfg.limits == {"memory": "2g", "network": "none", "read_only": True, "cap_drop": ["ALL"]}


class TestDockerUpGvisorMissing:
    def test_exits_zero_with_warning_and_hardened_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=0, gvisor_rc=1)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        out = _norm(result.output)
        assert "weaker isolation boundary" in out
        assert "hardened-docker" in out

    def test_records_hardened_backend(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _apply_mocks(monkeypatch, tmp_path, info_rc=0, gvisor_rc=1)
        runner.invoke(app, ["doctor"])
        cfg = _loaded(tmp_path)
        assert cfg is not None
        assert cfg.backend == "hardened-docker"
        assert cfg.status == "READY"
        assert cfg.gvisor_available is False


class TestWindowsRouting:
    def test_windows_wsl2_selected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(rt_mod, "get_execution_backend", lambda: "wsl2-gvisor")
        monkeypatch.setattr(rt_mod, "host_os", lambda: "Windows")
        monkeypatch.setattr(rt_mod, "default_wsl_distro", lambda: "Ubuntu-22.04")
        monkeypatch.setattr(cli_diag_mod, "host_os", lambda: "Windows")

        def _fake(cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
            argv = list(cmd)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(rt_mod.subprocess, "run", _fake)
        monkeypatch.setattr(cli_diag_mod, "CONFIG_PATH", tmp_path / "job.json")
        _redirect_doctor_cache(monkeypatch, tmp_path)

        seen: list[list[str]] = []
        monkeypatch.setattr(
            rt_mod,
            "docker_command",
            lambda args: seen.append(["wsl.exe", "-d", "Ubuntu-22.04", "docker", *args])
            or ["wsl.exe", "-d", "Ubuntu-22.04", "docker", *args],
        )

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "wsl2-gvisor" in _norm(result.output)
        assert any(argv[0] == "wsl.exe" for argv in seen)