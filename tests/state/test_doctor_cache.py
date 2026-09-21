"""Tests for the doctor cache and backend resolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bluet.errors import BluetEnvironmentError
from bluet.sandbox.runtime import BACKEND_GVISOR, BACKEND_HARDENED, Diagnostics, RuntimeLimits
from bluet.state import doctor_cache as dc
from bluet.state.doctor_cache import resolve_backend


def _write_cache(path: Path, backend: str, age: timedelta = timedelta(seconds=1)) -> None:
    stamp = datetime.now(UTC) - age
    path.write_text(json.dumps({"backend": backend, "timestamp": stamp.isoformat()}))


def test_read_fresh_cache_returns_backend(tmp_path: Path) -> None:
    path = tmp_path / "doctor.json"
    _write_cache(path, "gvisor", age=timedelta(seconds=10))
    assert dc.read_doctor_backend(path) == "gvisor"


def test_read_stale_cache_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "doctor.json"
    _write_cache(path, "hardened-docker", age=timedelta(seconds=100_000))
    assert dc.read_doctor_backend(path) is None


def test_read_missing_cache_returns_none(tmp_path: Path) -> None:
    assert dc.read_doctor_backend(tmp_path / "nope.json") is None


def test_read_unknown_backend_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "doctor.json"
    _write_cache(path, "blocked")
    assert dc.read_doctor_backend(path) is None


def test_write_creates_parents(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "doctor.json"
    dc.write_doctor_backend("gvisor", path)
    assert path.exists()
    assert json.loads(path.read_text())["backend"] == "gvisor"


def test_resolve_uses_fresh_cache_without_diagnose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_cache(tmp_path / "doctor.json", BACKEND_GVISOR, age=timedelta(seconds=5))
    monkeypatch.setattr(dc, "DOCTOR_CACHE_PATH", tmp_path / "doctor.json")

    def boom(*_args, **_kwargs):
        raise AssertionError("diagnose should not run with a fresh cache")

    monkeypatch.setattr(dc, "diagnose", boom)
    assert resolve_backend() == "gvisor"


def test_resolve_runs_diagnose_and_rewrites_cache_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "DOCTOR_CACHE_PATH", tmp_path / "doctor.json")

    def fake_diagnose(limits=None):
        return Diagnostics(
            docker_ok=True,
            docker_error=None,
            gvisor_available=False,
            backend=BACKEND_HARDENED,
            limits=RuntimeLimits(),
            host_backend="native-gvisor",
        )

    monkeypatch.setattr(dc, "diagnose", fake_diagnose)
    backend = resolve_backend()
    assert backend == "hardened-docker"
    assert dc.read_doctor_backend(tmp_path / "doctor.json") == "hardened-docker"


def test_resolve_returns_unknown_when_docker_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_doctor_backend", lambda: None)

    def blocked(limits=None):
        return Diagnostics(
            docker_ok=False,
            docker_error="daemon down",
            gvisor_available=False,
            backend=None,
            limits=RuntimeLimits(),
            host_backend="native-gvisor",
        )

    monkeypatch.setattr(dc, "diagnose", blocked)
    assert resolve_backend() == "unknown"


def test_resolve_returns_unknown_when_diagnose_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dc, "read_doctor_backend", lambda: None)
    monkeypatch.setattr(
        dc, "diagnose", lambda limits=None: (_ for _ in ()).throw(BluetEnvironmentError("no wsl"))
    )
    assert resolve_backend() == "unknown"


def test_resolve_does_not_cache_when_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "doctor.json"
    monkeypatch.setattr(dc, "DOCTOR_CACHE_PATH", cache_path)
    monkeypatch.setattr(dc, "read_doctor_backend", lambda: None)

    def blocked(limits=None):
        return Diagnostics(
            docker_ok=False,
            docker_error="daemon down",
            gvisor_available=False,
            backend=None,
            limits=RuntimeLimits(),
            host_backend="native-gvisor",
        )

    monkeypatch.setattr(dc, "diagnose", blocked)
    resolve_backend()
    assert not cache_path.exists()