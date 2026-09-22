"""Unit tests for sandbox platform/backend detection.

All OS detection is mocked via ``monkeypatch``; no real host detection occurs.
"""

from __future__ import annotations

import subprocess

import pytest

from bluet.errors import BluetEnvironmentError
from bluet.sandbox import get_execution_backend
from bluet.sandbox import platform as platform_mod

WSL_LIST_HEADER = "  NAME            STATE           VERSION\n"


def _wsl_list_output(*lines: str) -> bytes:
    return (WSL_LIST_HEADER + "\n".join(lines)).encode("utf-8")


def _mock_wsl_run(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    completed = subprocess.CompletedProcess([], 0, stdout=stdout)
    monkeypatch.setattr(platform_mod.subprocess, "run", lambda *a, **k: completed)


class TestLinux:
    def test_returns_native_gvisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Linux")
        assert get_execution_backend() == "native-gvisor"


class TestDarwin:
    def test_returns_docker_gvisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Darwin")
        assert get_execution_backend() == "docker-gvisor"


class TestWindows:
    def test_running_wsl2_distro_returns_wsl2_gvisor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_mod, "_wsl_executable", lambda: "/mnt/c/wsl.exe")
        _mock_wsl_run(
            monkeypatch,
            _wsl_list_output("  * Ubuntu-22.04    Running    2"),
        )
        assert get_execution_backend() == "wsl2-gvisor"

    def test_wsl_exe_missing_raises_with_actionable_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_mod, "_wsl_executable", lambda: None)

        with pytest.raises(BluetEnvironmentError) as excinfo:
            get_execution_backend()

        message = str(excinfo.value)
        assert "WSL2" in message
        assert "wsl --install" in message
        assert "unsandboxed" in message

    def test_wsl_present_but_no_running_distro_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_mod, "_wsl_executable", lambda: "/mnt/c/wsl.exe")
        _mock_wsl_run(
            monkeypatch,
            _wsl_list_output("  * Ubuntu-22.04    Stopped    2"),
        )

        with pytest.raises(BluetEnvironmentError):
            get_execution_backend()

    def test_running_wsl1_distro_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_mod, "_wsl_executable", lambda: "/mnt/c/wsl.exe")
        _mock_wsl_run(
            monkeypatch,
            _wsl_list_output("  * Ubuntu-18.04    Running    1"),
        )

        with pytest.raises(BluetEnvironmentError):
            get_execution_backend()

    def test_nonzero_wsl_exit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_mod, "_wsl_executable", lambda: "/mnt/c/wsl.exe")
        completed = subprocess.CompletedProcess([], 1, stdout=b"")
        monkeypatch.setattr(platform_mod.subprocess, "run", lambda *a, **k: completed)

        with pytest.raises(BluetEnvironmentError):
            get_execution_backend()


class TestUnsupported:
    def test_unknown_os_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_mod.platform, "system", lambda: "Solaris")

        with pytest.raises(BluetEnvironmentError) as excinfo:
            get_execution_backend()

        assert "Solaris" in str(excinfo.value)


class TestWslEncodingQuirk:
    def test_utf16_output_is_decoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        utf16 = (
            "  NAME            STATE           VERSION\n  * Ubuntu-22.04    Running    2\n"
        ).encode("utf-16-le")
        assert b"\x00" in utf16
        assert platform_mod._decode_wsl_output(utf16) == (
            "  NAME            STATE           VERSION\n  * Ubuntu-22.04    Running    2\n"
        )
