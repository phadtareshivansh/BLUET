"""The doctor cache: salts the last ``bluet doctor`` outcome to disk.

``bluet doctor``/``bluet run`` write ``~/.bluet/doctor.json`` (via
:func:`bluet.cli.diagnostics.record_job`) containing the selected sandbox
backend and its timestamp. When a :class:`bluet.state.models.Job` is created,
:func:`resolve_backend` reads this cache and stamps the backend onto the row -
re-running the doctor check inline only when the cache is missing or stale.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from bluet.errors import BluetEnvironmentError
from bluet.sandbox.runtime import (
    BACKEND_GVISOR,
    BACKEND_HARDENED,
    RuntimeLimits,
    diagnose,
)

UNKNOWN = "unknown"

#: How long a cached doctor result is considered fresh.
DOCTOR_CACHE_TTL_SECONDS = 300


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("BLUET_DOCTOR_TTL_SECONDS", DOCTOR_CACHE_TTL_SECONDS))
    except ValueError:
        return DOCTOR_CACHE_TTL_SECONDS


def default_doctor_cache_path() -> Path:
    env = os.environ.get("BLUET_DOCTOR_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".bluet" / "doctor.json"


DOCTOR_CACHE_PATH = default_doctor_cache_path()


def _resolve_path(path: Path | None) -> Path:
    return path or DOCTOR_CACHE_PATH


def write_doctor_backend(backend: str, path: Path | None = None) -> None:
    """Persist the backend decision with a UTC timestamp."""
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"backend": backend, "timestamp": datetime.now(UTC).isoformat()}
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def read_doctor_backend(path: Path | None = None) -> str | None:
    """Return the cached backend if present and fresh, else ``None``."""
    target = _resolve_path(path)
    if not target.exists():
        return None
    try:
        with open(target, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None

    backend = payload.get("backend")
    if backend not in {BACKEND_GVISOR, BACKEND_HARDENED}:
        return None

    raw_stamp = payload.get("timestamp")
    if not isinstance(raw_stamp, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw_stamp)
    except ValueError:
        return None

    age = datetime.now(UTC).replace(tzinfo=None) - stamp.replace(tzinfo=None)
    if age.total_seconds() > _ttl_seconds():
        return None
    return backend


def resolve_backend(limits: RuntimeLimits | None = None) -> str:
    """Return the backend to stamp on a new job.

    Prefers a fresh doctor cache; falls back to running the inline
    ``bluet doctor`` probe (which refreshes the cache). Returns
    "gvisor", "hardened-docker", or "unknown" (docker unavailable /
    WSL2 absent / unsupported host).
    """
    cached = read_doctor_backend()
    if cached is not None:
        return cached

    try:
        d = diagnose(limits)
    except BluetEnvironmentError:
        return UNKNOWN

    backend = d.backend if d.backend in {BACKEND_GVISOR, BACKEND_HARDENED} else UNKNOWN
    if d.docker_ok:
        write_doctor_backend(backend)
    return backend
