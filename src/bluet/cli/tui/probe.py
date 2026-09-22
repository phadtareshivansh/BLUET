"""Runtime probing for the BIOS dashboard: sandbox + LLM state.

The dashboard paints itself immediately from fast on-disk caches (doctor
cache, job config), then refines with a background probe that shells out to
the real hardware/daemon surface (the App runs this in a worker thread so the
TUI never stalls on ``docker info``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from bluet.agents.llm_client import (
    BACKEND_AUTO,
    BACKEND_OLLAMA,
    BACKEND_VLLM,
    MODEL_TIERS,
    OLLAMA_BASE,
    VLLM_BASE,
    detect_ram_gb,
    detect_vram_gb,
    is_apple_silicon,
    resolve_model_tier,
)
from bluet.errors import BluetEnvironmentError
from bluet.sandbox.runtime import (
    BACKEND_GVISOR,
    BACKEND_HARDENED,
    RuntimeLimits,
    diagnose,
)
from bluet.state.doctor_cache import read_doctor_backend
from bluet.state.job_config import CONFIG_PATH, JobConfig

#: Short health-probe timeout so a dead daemon never stalls the dashboard.
LLM_PROBE_TIMEOUT = 1.5


@dataclass
class RuntimeState:
    """What the top bar / bank-2 row / doctor tab need to render."""

    backend: str  # "gvisor" | "hardened-docker" | "none"
    sandbox_label: str
    host_backend: str
    docker_ok: bool
    docker_error: str | None
    gvisor_available: bool
    limits: RuntimeLimits
    job_id: str = "unknown"
    job_status: str = "IDLE"
    llm_backend: str = "auto"
    llm_base: str = OLLAMA_BASE
    llm_model: str = ""
    llm_up: bool = False
    llm_health: str = "unprobed"
    tier: str = "dev"
    vram_gb: float | None = None
    ram_gb: float = 0.0
    refreshed_at: float = field(default=0.0)

    @property
    def memory_text(self) -> str:
        if self.vram_gb:
            return f"VRAM {self.vram_gb:.1f}GB"
        if self.ram_gb:
            return f"RAM {self.ram_gb:.1f}GB"
        return "RAM n/a"


def _llm_backend_env() -> str:
    value = os.environ.get("BLUET_LLM_BACKEND", BACKEND_AUTO).strip().lower()
    return value if value in {BACKEND_OLLAMA, BACKEND_VLLM} else BACKEND_AUTO


def _resolve_llm_backend() -> str:
    """Concrete backend for tier resolution; ``auto`` maps to the default."""
    value = _llm_backend_env()
    return value if value in {BACKEND_OLLAMA, BACKEND_VLLM} else BACKEND_OLLAMA


def _fetch_json(url: str, timeout: float) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout, verify=False, follow_redirects=False) as client:
            response = client.get(url)
    except (httpx.HTTPError, OSError):
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def probe_llm() -> tuple[str, str, str, bool]:
    """Return ``(backend, base_url, model, healthy)`` for the active LLM.

    ``auto`` probes Ollama first, then vLLM (mirroring :data:`BACKEND_AUTO`);
    a 1.5s timeout per endpoint keeps the probe bounded.
    """
    requested = _llm_backend_env()
    targets: list[tuple[str, str, str, str, str]]
    if requested == BACKEND_OLLAMA:
        targets = [(BACKEND_OLLAMA, OLLAMA_BASE, "/api/tags", "models", "name")]
    elif requested == BACKEND_VLLM:
        targets = [(BACKEND_VLLM, VLLM_BASE, "/v1/models", "data", "id")]
    else:
        targets = [
            (BACKEND_OLLAMA, OLLAMA_BASE, "/api/tags", "models", "name"),
            (BACKEND_VLLM, VLLM_BASE, "/v1/models", "data", "id"),
        ]

    forced = os.environ.get("BLUET_LLM_MODEL")
    for backend, base, path, container, key in targets:
        body = _fetch_json(base + path, LLM_PROBE_TIMEOUT)
        if body is None:
            continue
        names = [str(e.get(key, "")) for e in body.get(container, []) if isinstance(e, dict)]
        model = forced or next(
            (n for n in names if "coder" in n or "qwen2.5" in n),
            names[0] if names else "",
        )
        return backend, base, model, True

    fallback_model = {
        BACKEND_OLLAMA: "qwen2.5-coder:14b",
        BACKEND_VLLM: "Qwen/Qwen2.5-Coder-14B-Instruct",
    }[requested if requested in {BACKEND_OLLAMA, BACKEND_VLLM} else BACKEND_OLLAMA]
    return requested, OLLAMA_BASE, forced or fallback_model, False


def _tier_for_model(model: str) -> str:
    if not model:
        return "dev"
    for tier, table in MODEL_TIERS.items():
        if model in table.values():
            return tier
    return "override"


def snapshot_from_cache() -> RuntimeState:
    """Cheap synchronous state from disk/env; the heavy probe refines later."""
    cached = read_doctor_backend()
    backend = cached if cached in {BACKEND_GVISOR, BACKEND_HARDENED} else "none"
    job = JobConfig.load(CONFIG_PATH)
    ram = detect_ram_gb() or 0.0
    try:
        vram = detect_vram_gb()
    except OSError:
        vram = None
    _, model = resolve_model_tier(
        _resolve_llm_backend(),
        vram,
        ram_gb=ram,
        apple_silicon=is_apple_silicon(),
        model_override=os.environ.get("BLUET_LLM_MODEL"),
    )
    llm_backend = _resolve_llm_backend()
    return RuntimeState(
        backend=backend,
        sandbox_label=_sandbox_label(backend),
        host_backend="",
        docker_ok=bool(cached),
        docker_error=None,
        gvisor_available=cached == BACKEND_GVISOR,
        limits=RuntimeLimits(),
        job_id=job.job_id if job else "unknown",
        job_status=job.status if job else "IDLE",
        llm_backend=llm_backend,
        llm_base=OLLAMA_BASE,
        llm_model=model,
        llm_up=False,
        llm_health="unprobed",
        tier=_tier_for_model(model),
        vram_gb=vram,
        ram_gb=ram,
        refreshed_at=time.time(),
    )


def _sandbox_label(backend: str) -> str:
    if backend == BACKEND_GVISOR:
        return "GVISOR"
    if backend == BACKEND_HARDENED:
        return "HARDENED-DOCKER"
    return "NONE — REFUSED"


async def probe_runtime() -> RuntimeState:
    """Full probe: ``diagnose()`` in a worker thread + live LLM health check."""
    state = snapshot_from_cache()

    backend_name, base, model, up = await asyncio.to_thread(probe_llm)
    state.llm_backend = backend_name
    state.llm_base = base
    state.llm_model = model
    state.llm_up = up
    state.llm_health = "OK" if up else "TIMEOUT"
    state.tier = _tier_for_model(model)

    try:
        d = await asyncio.to_thread(diagnose)
    except BluetEnvironmentError as exc:
        state.backend = "none"
        state.sandbox_label = _sandbox_label("none")
        state.docker_ok = False
        state.docker_error = str(exc)
        state.gvisor_available = False
        return state

    state.host_backend = d.host_backend
    state.docker_ok = d.docker_ok
    state.docker_error = d.docker_error
    state.gvisor_available = d.gvisor_available
    state.backend = d.backend or "none"
    state.sandbox_label = _sandbox_label(state.backend)
    state.limits = d.limits
    state.refreshed_at = time.time()
    return state


__all__ = ["RuntimeState", "probe_llm", "probe_runtime", "snapshot_from_cache"]