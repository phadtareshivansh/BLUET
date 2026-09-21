"""Thin local-inference client for bluet's agents.

Talks to an OpenAI-compatible chat endpoint exposed by either **Ollama**
(``http://localhost:11434``) or **vLLM** (``http://localhost:8000``). The
client is async-first like the rest of bluet (context store, event bus,
orchestrator) and is intentionally *thin*: health probing, hardware-aware
model selection, the "is the model installed?" pull flow, and one
:meth:`LLMClient.complete` call that uses Pydantic-AI to force structured
output matching a ``response_model``.

Backend selection is driven by ``BLUET_LLM_BACKEND`` (``ollama``/``vllm``/
``auto``); ``auto`` probes Ollama first and falls back to vLLM. ``BLUET_LLM_MODEL``
forces a specific model regardless of detected hardware, and ``BLUET_LLM_YES=1``
auto-accepts the "pull this model?" prompt for CI/non-interactive use.

When no backend is reachable the client raises :class:`~bluet.errors.LLMUnavailableError`
with the exact commands to start the backend, so a job never hangs waiting on a
model that simply isn't running.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from typing import Self

import httpx
import psutil
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich.console import Console
from rich.progress import Progress
from rich.prompt import Confirm

from bluet.errors import LLMSchemaOutputError, LLMUnavailableError

_console = Console()

OLLAMA_BASE = "http://localhost:11434"
VLLM_BASE = "http://localhost:8000"

BACKEND_OLLAMA = "ollama"
BACKEND_VLLM = "vllm"
BACKEND_AUTO = "auto"
_BACKENDS = (BACKEND_OLLAMA, BACKEND_VLLM)

#: Per-tier model names, keyed by backend. ``small`` is the default no-GPU
#: target; ``dev`` (deepseek) is the dev-mode alternate for the same tier.
MODEL_TIERS: dict[str, dict[str, str]] = {
    "large": {
        BACKEND_OLLAMA: "qwen2.5-coder:32b-instruct-q4_K_M",
        BACKEND_VLLM: "Qwen/Qwen2.5-Coder-32B-Instruct",
    },
    "medium": {
        BACKEND_OLLAMA: "qwen2.5-coder:14b-instruct-q4_K_M",
        BACKEND_VLLM: "Qwen/Qwen2.5-Coder-14B-Instruct",
    },
    "small": {
        BACKEND_OLLAMA: "qwen2.5-coder:7b",
        BACKEND_VLLM: "Qwen/Qwen2.5-Coder-7B-Instruct",
    },
    "dev": {
        BACKEND_OLLAMA: "deepseek-coder-v2:16b",
        BACKEND_VLLM: "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
    },
}

#: Tier preference order, lightest first (drives fallback selection).
_TIER_ORDER = ("small", "dev", "medium", "large")

#: VRAM thresholds for tier selection (GiB).
VRAM_LARGE_GB = 24.0
VRAM_MEDIUM_GB = 16.0

#: Unified-memory thresholds for Apple Silicon tier selection (GiB of RAM).
RAM_MEDIUM_GB = 32.0
RAM_SMALL_GB = 16.0

HEALTH_TIMEOUT_SECONDS = 2.0

#: Read timeout for the streaming pull (layer downloads legitimately take
#: minutes; only the connect timeout stays short like a health probe).
PULL_READ_TIMEOUT_SECONDS = 300.0

#: Ollama pulls stream NDJSON progress over this endpoint.
OLLAMA_PULL_PATH = "/api/pull"
#: Ollama model discovery/health endpoint (also proves the daemon is alive).
OLLAMA_TAGS_PATH = "/api/tags"
#: vLLM health endpoint.
VLLM_HEALTH_PATH = "/health"
#: vLLM model discovery endpoint.
VLLM_MODELS_PATH = "/v1/models"


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_backend() -> str:
    value = os.environ.get("BLUET_LLM_BACKEND", BACKEND_AUTO).strip().lower()
    return value if value in _BACKENDS else BACKEND_AUTO


def detect_vram_gb() -> float | None:
    """Return total VRAM in GiB, or ``None`` when no NVIDIA GPU is present.

    Uses ``nvidia-smi`` first (zero required deps); falls back to ``pynvml``
    only if it happens to be importable. Any probe failure returns ``None`` so
    callers treat this as "no GPU detected" (dev mode), never an error.
    """
    if shutil.which("nvidia-smi") is not None:
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                first = proc.stdout.decode("utf-8", errors="replace").splitlines()[0].strip()
                return float(first) / 1024.0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    try:
        import pynvml  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        return total / (1024**3)
    except (OSError, ValueError, RuntimeError, pynvml.NVMLError):
        return None


def detect_ram_gb() -> float:
    """Return total system RAM in GiB via psutil."""
    return psutil.virtual_memory().total / (1024**3)


def is_apple_silicon() -> bool:
    """True on M-series Macs (``arm64``/``aarch64`` on Darwin).

    These machines have **no discrete VRAM** — the GPU shares unified memory
    with the CPU — so the normal no-GPU path must not bucket them into the
    generic dev-mode tier. RAM *is* the relevant capacity here.
    """
    return platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}


def resolve_model_tier(
    backend: str,
    vram_gb: float | None,
    *,
    model_override: str | None = None,
    ram_gb: float | None = None,
    apple_silicon: bool | None = None,
) -> tuple[str, str]:
    """Map detected hardware to a ``(tier, model_name)``.

    ``model_override`` (the ``BLUET_LLM_MODEL`` force) wins outright.
    Otherwise, on Apple Silicon with no discrete VRAM the tier is sized off
    **system RAM** (``>=32GB`` -> mid 14b, ``16-24GB`` -> small 7b, ``<16GB``
    -> the smallest viable model) rather than falling into the generic no-GPU
    dev bucket. On everything else: ``>=24GB`` -> large (32b), ``16-24GB`` ->
    medium (14b), ``<16GB`` or no GPU -> small (7b). ``dev`` (deepseek) stays
    available as a fallback candidate.
    """
    if model_override:
        return "override", model_override

    if (apple_silicon if apple_silicon is not None else is_apple_silicon()) and vram_gb is None:
        ram = ram_gb if ram_gb is not None else detect_ram_gb()
        if ram >= RAM_MEDIUM_GB:
            tier = "medium"
        elif ram >= RAM_SMALL_GB:
            tier = "small"
        else:
            tier = "small"  # smallest viable model in the tier table
        return tier, MODEL_TIERS[tier][backend]

    if vram_gb is None:
        tier = "small"
    elif vram_gb >= VRAM_LARGE_GB:
        tier = "large"
    elif vram_gb >= VRAM_MEDIUM_GB:
        tier = "medium"
    else:
        tier = "small"
    return tier, MODEL_TIERS[tier][backend]


def pick_fallback_model(
    available: list[str],
    desired: str,
    backend: str,
) -> str | None:
    """Pick a lighter already-installed model, or ``None`` if none is usable.

    Prefers the lightest known tier (7b -> deepseek-16b -> 14b -> 32b) that is
    installed and not the desired model, then any other installed model name.
    """
    known = [MODEL_TIERS[tier][backend] for tier in _TIER_ORDER]
    for name in known:
        if name != desired and name in available:
            return name
    others = [name for name in available if name != desired]
    return others[0] if others else None


def _ollama_install_instructions() -> str:
    return (
        "Install it with: `curl -fsSL https://ollama.com/install.sh | sh`"
        " then start it with `ollama serve`."
    )


class LLMClient:
    """Thin, async, hardware-aware client over Ollama/vLLM.

    Parameters
    ----------
    backend:
        ``"ollama"``, ``"vllm"``, or ``"auto"`` (probe Ollama then vLLM).
        Defaults to ``BLUET_LLM_BACKEND`` (auto).
    assume_yes:
        Auto-accept the "pull the tier model?" prompt (CI/non-interactive).
        Defaults to ``BLUET_LLM_YES``.
    base_url:
        Override the backend base URL (also handy for tests).
    timeout:
        Per-request timeout for health probes (keeps startup non-blocking).
    transport:
        Optional ``httpx`` transport; injected by tests to stay hermetic.
    model_override:
        Force a specific model (defaults to ``BLUET_LLM_MODEL``).
    """

    def __init__(
        self,
        *,
        backend: str | None = None,
        assume_yes: bool | None = None,
        base_url: str | None = None,
        timeout: float = HEALTH_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        model_override: str | None = None,
    ) -> None:
        requested = (backend or _env_backend()).strip().lower()
        if requested not in _BACKENDS and requested != BACKEND_AUTO:
            raise ValueError(f"unknown BLUET_LLM_BACKEND: {requested!r}")
        self.backend = requested
        self.assume_yes = _env_bool("BLUET_LLM_YES") if assume_yes is None else assume_yes
        self._base_url_override = base_url
        self.timeout = timeout
        self.transport = transport
        self.model_override = model_override or os.environ.get("BLUET_LLM_MODEL") or None

        self.resolved_backend: str | None = None
        self.base_url: str | None = None
        self.model_name: str | None = None
        self.hardware: dict[str, float | None] = {}
        self.available_models: list[str] = []
        self.ready = False
        self._http_client: httpx.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Probe the backend, select a model tier, and ensure it is available.

        Raises :class:`LLMUnavailableError` when no backend is reachable — with
        the exact commands to start one — and never blocks longer than
        ``timeout`` per probe.
        """
        if self.ready:
            return
        self.hardware = {"vram_gb": detect_vram_gb(), "ram_gb": detect_ram_gb()}

        if self.backend == BACKEND_AUTO:
            for candidate in _BACKENDS:
                if await self._open_backend(candidate):
                    break
            else:
                self._raise_unreachable()
        elif not await self._open_backend(self.backend):
            self._raise_unreachable()

        await self._started_client()

    async def _started_client(self) -> None:
        assert self.resolved_backend is not None
        assert self.base_url is not None
        tier, model = resolve_model_tier(
            self.resolved_backend,
            self.hardware["vram_gb"],
            model_override=self.model_override,
            ram_gb=self.hardware["ram_gb"],
        )
        self.available_models = await self._available()
        chosen = await self._ensure_model(model)
        if chosen is None:
            _console.print(
                f"[yellow]bluet: no usable {tier!r} tier model on {self.resolved_backend} "
                f"at {self.base_url} (installed: {self.available_models!r}). "
                "Pull one or set BLUET_LLM_MODEL.[/yellow]"
            )
            raise LLMUnavailableError(
                f"no usable model for the {tier!r} tier on {self.resolved_backend} "
                f"at {self.base_url} (installed: {self.available_models!r})."
            )
        self.model_name = chosen
        self.ready = True

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- backend probing ---------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout, transport=self.transport)
        return self._http_client

    async def _open_backend(self, backend: str) -> bool:
        base = self._base_url_override or {BACKEND_OLLAMA: OLLAMA_BASE, BACKEND_VLLM: VLLM_BASE}[backend]
        client = self._http()
        try:
            if backend == BACKEND_OLLAMA:
                resp = await client.get(f"{base}{OLLAMA_TAGS_PATH}")
            else:
                resp = await client.get(f"{base}{VLLM_HEALTH_PATH}")
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException):
            return False
        self.resolved_backend = backend
        self.base_url = base
        return True

    async def _available(self) -> list[str]:
        assert self.resolved_backend is not None
        assert self.base_url is not None
        client = self._http()
        if self.resolved_backend == BACKEND_OLLAMA:
            resp = await client.get(f"{self.base_url}{OLLAMA_TAGS_PATH}")
            resp.raise_for_status()
            return [str(model.get("name")) for model in resp.json().get("models", [])]
        resp = await client.get(f"{self.base_url}{VLLM_MODELS_PATH}")
        resp.raise_for_status()
        return [str(model.get("id")) for model in resp.json().get("data", [])]

    async def _ensure_model(self, model: str) -> str | None:
        """Ensure ``model`` is installed; returns the model to use.

        When the hardware-appropriate model is missing this prompts to pull it
        (Ollama only, with a live streaming progress bar) or to fall back to a
        lighter installed model. ``assume_yes`` skips the prompt entirely.
        Returns ``None`` when nothing usable could be resolved.
        """
        assert self.resolved_backend is not None
        assert self.base_url is not None
        available = await self._available()
        self.available_models = available
        if model in available:
            return model

        wants_pull = self.assume_yes
        if not wants_pull and self.resolved_backend == BACKEND_OLLAMA:
            try:
                wants_pull = Confirm.ask(f"Model {model!r} is not installed. Pull it now?", default=True)
            except (EOFError, OSError):
                # No interactive stdin (non-interactive run, piped input, or a
                # captured/closed terminal): decline and fall through to the
                # lighter-installed-model fallback instead of crashing.
                wants_pull = False
        if wants_pull:
            await self._pull_model(model)
            available = await self._available()
            self.available_models = available
            if model in available:
                return model

        fallback = pick_fallback_model(available, model, self.resolved_backend)
        if fallback is not None:
            _console.print(f"  [dim]using lighter installed model: {fallback}[/dim]")
        return fallback

    async def _pull_model(self, model: str) -> None:
        client = self._http()
        with Progress() as progress:
            task = progress.add_task(f"Pulling {model}", total=None)
            try:
                pull_timeout = httpx.Timeout(
                    connect=self.timeout,
                    read=PULL_READ_TIMEOUT_SECONDS,
                    write=PULL_READ_TIMEOUT_SECONDS,
                    pool=self.timeout,
                )
                async with client.stream(
                    "POST",
                    f"{self.base_url}{OLLAMA_PULL_PATH}",
                    json={"model": model, "stream": True},
                    timeout=pull_timeout,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            update = json.loads(line)
                        except ValueError:
                            continue
                        status = str(update.get("status", ""))
                        completed = update.get("completed")
                        total = update.get("total")
                        progress.update(
                            task,
                            description=f"Pulling {model} - {status}",
                            completed=completed,
                            total=total,
                        )
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                raise LLMUnavailableError(
                    f"failed to pull {model!r} from Ollama at {self.base_url}: {exc}"
                ) from exc

    def _raise_unreachable(self) -> None:
        raise LLMUnavailableError(
            "No local inference backend is reachable. Start one and retry:\n"
            f"  - Ollama ({OLLAMA_BASE}): {_ollama_install_instructions()}\n"
            f"  - vLLM ({VLLM_BASE}): `vllm serve <model> --port 8000`\n"
            "    (or `python -m vllm.entrypoints.openai.api_server --model <model> --port 8000`)"
        )

    # -- completions -------------------------------------------------------

    def _build_model(self) -> OpenAIChatModel:
        assert self.base_url is not None
        return OpenAIChatModel(
            self.model_name,
            provider=OpenAIProvider(base_url=f"{self.base_url}/v1", api_key="ollama"),
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Run a chat completion returning structured data of ``response_model``.

        ``messages`` is a chat-message list of ``{"role", "content"}`` dicts;
        ``system`` entries become Pydantic-AI ``instructions``; the remaining
        content is sent as one user turn. Raises
        :class:`~bluet.errors.LLMUnavailableError` on transport-level failures
        (never hangs) and :class:`~bluet.errors.LLMSchemaOutputError` when the
        model cannot emit output matching ``response_model``.
        """
        if not self.ready or self.model_name is None:
            raise LLMUnavailableError(
                f"LLM client is not ready (backend={self.resolved_backend!r}). "
                "Call `start()` after starting a backend; see the start steps above."
            )
        instructions = (
            "\n".join(m["content"] for m in messages if m.get("role") == "system") or None
        )
        content = [m["content"] for m in messages if m.get("role") in ("user", "assistant")]
        agent = Agent(
            model=self._build_model(),
            output_type=response_model,
            instructions=instructions,
            defer_model_check=True,
        )
        try:
            result = await agent.run(content)
        except (httpx.HTTPError, ModelHTTPError) as exc:
            raise LLMUnavailableError(
                f"completion against {self.model_name!r} on {self.resolved_backend} failed: {exc}"
            ) from exc
        except UnexpectedModelBehavior as exc:
            raise LLMSchemaOutputError(
                f"completion against {self.model_name!r} on {self.resolved_backend} could not "
                f"produce structured output for {response_model.__name__} after validations: {exc}. "
                "Use a larger model or override with BLUET_LLM_MODEL."
            ) from exc
        return result.output