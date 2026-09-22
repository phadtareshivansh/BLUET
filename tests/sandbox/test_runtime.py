"""Unit tests for `bluet.sandbox.runtime` (docker + gVisor probing)."""

from __future__ import annotations

import subprocess

import pytest

from bluet.errors import BluetEnvironmentError
from bluet.sandbox import runtime as rt


def _fake_run(info_rc: int = 0, gvisor_rc: int = 0, out: str = "", err: str = ""):
    def _fake(cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        if "info" in argv:
            return subprocess.CompletedProcess(argv, info_rc, stdout=out, stderr=err)
        return subprocess.CompletedProcess(argv, gvisor_rc)

    return _fake


def _patch_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "get_execution_backend", lambda: "native-gvisor")
    monkeypatch.setattr(rt, "host_os", lambda: "Linux")


class TestDockerCommand:
    def test_linux_uses_host_docker(self) -> None:
        assert rt.docker_command(["info"]) == ["docker", "info"]

    def test_darwin_uses_host_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt, "host_os", lambda: "Darwin")
        assert rt.docker_command(["run", "--rm"]) == ["docker", "run", "--rm"]

    def test_windows_routes_through_wsl2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt, "host_os", lambda: "Windows")
        monkeypatch.setattr(rt, "default_wsl_distro", lambda: "Ubuntu-22.04")
        assert rt.docker_command(["info"]) == [
            "wsl.exe",
            "-d",
            "Ubuntu-22.04",
            "docker",
            "info",
        ]

    def test_windows_without_running_distro_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt, "host_os", lambda: "Windows")
        monkeypatch.setattr(rt, "default_wsl_distro", lambda: None)
        with pytest.raises(BluetEnvironmentError):
            rt.docker_command(["info"])


class TestCheckDocker:
    def test_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _fake_run(info_rc=0))
        assert rt.check_docker() == (True, None)

    def test_daemon_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _fake_run(info_rc=1, err="cannot connect"))
        ok, reason = rt.check_docker()
        assert ok is False
        assert "cannot connect" in reason

    def test_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _missing_docker)
        ok, reason = rt.check_docker()
        assert ok is False
        assert rt.DOCKER_INSTALL_URL in reason

    def test_timeout_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)

        def _timeout(cmd, **_kwargs):
            raise subprocess.TimeoutExpired(list(cmd), 15)

        monkeypatch.setattr(rt.subprocess, "run", _timeout)
        ok, reason = rt.check_docker()
        assert ok is False
        assert "timed out" in reason


def _missing_docker(_cmd, **_kwargs) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError("docker")


class TestDiagnose:
    def test_docker_down_blocks_with_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _fake_run(info_rc=1))
        d = rt.diagnose()
        assert d.docker_ok is False
        assert d.backend is None
        assert d.gvisor_available is False
        with pytest.raises(BluetEnvironmentError) as excinfo:
            rt.require_sandbox_ready(d)
        assert "without Docker" in str(excinfo.value)

    def test_gvisor_available_selects_gvisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _fake_run(info_rc=0, gvisor_rc=0))
        d = rt.diagnose()
        assert d.docker_ok is True
        assert d.gvisor_available is True
        assert d.backend == rt.BACKEND_GVISOR
        assert d.warnings == ()

    def test_gvisor_missing_falls_back_to_hardened_docker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_base(monkeypatch)
        monkeypatch.setattr(rt.subprocess, "run", _fake_run(info_rc=0, gvisor_rc=1))
        d = rt.diagnose()
        assert d.docker_ok is True
        assert d.gvisor_available is False
        assert d.backend == rt.BACKEND_HARDENED
        assert any("weaker isolation boundary" in w for w in d.warnings)

    def test_limits_defaults_and_flags(self) -> None:
        limits = rt.RuntimeLimits()
        assert limits.as_text() == "memory=2g, network=none, read-only, cap-drop=ALL"
        assert limits.as_flags() == [
            "--memory=2g",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
        ]

    def test_limits_are_configurable(self) -> None:
        limits = rt.RuntimeLimits(memory="4g", read_only=False, cap_drop=("NET_ADMIN", "SYS_ADMIN"))
        assert "--memory=4g" in limits.as_flags()
        assert "--read-only" not in limits.as_flags()
        assert sum(flag.startswith("--cap-drop=") for flag in limits.as_flags()) == 2


class TestGvisorDryRunCommand:
    def test_dry_run_argv(self) -> None:
        assert rt.GVISOR_DRYRUN == ["run", "--rm", "--runtime=runsc", "hello-world"]

    def test_diagnose_uses_dry_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_base(monkeypatch)
        seen: list[list[str]] = []
        monkeypatch.setattr(
            rt.subprocess,
            "run",
            lambda cmd, **_kwargs: seen.append(list(cmd)) or _fake_run(info_rc=0, gvisor_rc=0)(cmd),
        )
        rt.diagnose()
        assert any(any("runsc" in part for part in argv) for argv in seen)
