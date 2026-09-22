"""Job configuration persistence (JSON).

``bluet doctor`` records the selected sandbox backend here so it stays visible
to ``bluet status`` instead of being buried in a log file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


def default_config_path() -> Path:
    """Job config location, overridable via ``BLUET_JOB_CONFIG``."""
    env = os.environ.get("BLUET_JOB_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".bluet" / "job.json"


CONFIG_PATH = default_config_path()


def do_now() -> str:
    """Current UTC time as an ISO-8601 timestamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class JobConfig:
    job_id: str
    backend: str
    status: str
    created_at: str
    gvisor_available: bool | None = None
    docker_running: bool | None = None
    limits: dict[str, object] | None = None

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> JobConfig | None:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return cls(**json.load(f))
