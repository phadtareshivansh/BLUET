"""Tests for job config JSON persistence."""

from __future__ import annotations

from pathlib import Path

from bluet.state.job_config import JobConfig, do_now


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "job.json"
    cfg = JobConfig(
        job_id="job_abc123",
        backend="hardened-docker",
        status="READY",
        created_at=do_now(),
        gvisor_available=False,
        docker_running=True,
        limits={"memory": "2g", "network": "none", "read_only": True, "cap_drop": ["ALL"]},
    )
    cfg.save(path)

    loaded = JobConfig.load(path)
    assert loaded is not None
    assert loaded == cfg


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert JobConfig.load(tmp_path / "nope.json") is None
    assert JobConfig.load(tmp_path / "does-not-exist" / "job.json") is None


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "job.json"
    JobConfig(job_id="j", backend="gvisor", status="READY", created_at=do_now()).save(path)
    assert path.exists()