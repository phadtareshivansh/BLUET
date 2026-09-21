"""Unit tests for the shared LLM client (no real Ollama/vLLM required).

HTTP is fully mocked via ``httpx.MockTransport``; hardware detection is
monkeypatched; the Rich prompt/progress bar are stubbed. Nothing touches the
network or the host GPU.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Self

import httpx
import pytest
from pydantic import BaseModel

from bluet.agents import llm_client as llm_mod
from bluet.agents.llm_client import (
    MODEL_TIERS,
    OLLAMA_PULL_PATH,
    OLLAMA_TAGS_PATH,
    VLLM_HEALTH_PATH,
    VLLM_MODELS_PATH,
    LLMClient,
    detect_vram_gb,
    pick_fallback_model,
    resolve_model_tier,
)
from bluet.errors import (
    BluetEnvironmentError,
    BluetLLMError,
    LLMSchemaOutputError,
    LLMUnavailableError,
)

OLLAMA_7B = MODEL_TIERS["small"]["ollama"]
OLLAMA_14B = MODEL_TIERS["medium"]["ollama"]
OLLAMA_32B = MODEL_TIERS["large"]["ollama"]
VLLM_7B = MODEL_TIERS["small"]["vllm"]

pull_requests: list[httpx.Request] = []


class _FakeProgress:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def add_task(self, description: str, total: int | None = None) -> int:
        return 0

    def update(self, task: int, **kwargs: Any) -> None:
        self.updates.append(kwargs)


def _pull_body(model: str) -> bytes:
    lines = (
        b'{"status":"success","completed":1,"total":2}\n'
        b'{"status":"success","completed":2,"total":2}\n'
    )
    return lines


def _ollama_tag_payload(models: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"models": [{"name": name, "size": 1} for name in models]},
    )


def _vllm_models_payload(models: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": name, "object": "model"} for name in models]})


def _unreachable_all(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "detect_vram_gb", lambda: None)
    monkeypatch.setattr(llm_mod, "detect_ram_gb", lambda: 32.0)
    monkeypatch.setattr(llm_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(llm_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(llm_mod, "Progress", _FakeProgress)
    pull_requests.clear()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> LLMClient:
    transport = httpx.MockTransport(handler)
    return LLMClient(transport=transport, **kwargs)


async def _started(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> LLMClient:
    client = _client(monkeypatch, handler, **kwargs)
    await client.start()
    return client


class TestBackendUnreachable:
    @pytest.mark.asyncio
    async def test_auto_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch, _unreachable_all)

        with pytest.raises(LLMUnavailableError) as excinfo:
            await client.start()

        message = str(excinfo.value)
        assert "ollama serve" in message
        assert "vllm serve" in message
        assert not client.ready


class TestModelPresent:
    @pytest.mark.asyncio
    async def test_ollama_reachable_model_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B])
            raise AssertionError(f"unexpected {request.url}")

        client = await _started(monkeypatch, handler)
        assert client.resolved_backend == "ollama"
        assert client.model_name == OLLAMA_7B
        assert client.ready

    @pytest.mark.asyncio
    async def test_vllm_reachable_model_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == VLLM_HEALTH_PATH:
                return httpx.Response(200, text="OK")
            if request.url.path == VLLM_MODELS_PATH:
                return _vllm_models_payload([VLLM_7B])
            raise AssertionError(f"unexpected {request.url}")

        client = await _started(monkeypatch, handler, backend="vllm")
        assert client.resolved_backend == "vllm"
        assert client.model_name == VLLM_7B
        assert client.ready


class TestModelMissingAcceptPull:
    @pytest.mark.asyncio
    async def test_pulls_with_progress_when_user_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed: set[str] = set()
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload(sorted(installed))
            if request.url.path == OLLAMA_PULL_PATH:
                pull_requests.append(request)
                installed.add(OLLAMA_7B)
                return httpx.Response(200, content=_pull_body(OLLAMA_7B))
            raise AssertionError(f"unexpected {request.url}")

        def fake_confirm(prompt: str, *, default: bool) -> bool:
            asked.append(prompt)
            return True

        monkeypatch.setattr(llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(fake_confirm)}))

        client = await _started(monkeypatch, handler)
        assert asked, "the pull prompt must have been shown"
        assert len(pull_requests) == 1
        assert pull_requests[0].url.path == OLLAMA_PULL_PATH
        assert client.model_name == OLLAMA_7B
        assert client.ready


class TestModelMissingDeclineFallback:
    @pytest.mark.asyncio
    async def test_declines_and_uses_lighter_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_mod, "detect_vram_gb", lambda: 16.0)
        # desired tier = medium (14B), only the 7B is installed.

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B, "phi3:mini"])
            raise AssertionError(f"unexpected {request.url}")

        def fake_confirm(prompt: str, *, default: bool) -> bool:
            return False

        monkeypatch.setattr(llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(fake_confirm)}))

        client = await _started(monkeypatch, handler)
        assert not pull_requests
        assert client.model_name == OLLAMA_7B
        assert client.ready

    @pytest.mark.asyncio
    async def test_eof_on_pull_prompt_declines_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_mod, "detect_vram_gb", lambda: 16.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B])
            raise AssertionError(f"unexpected {request.url}")

        def eof_confirm(prompt: str, *, default: bool) -> bool:
            raise EOFError("no stdin; non-interactive run")

        monkeypatch.setattr(llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(eof_confirm)}))

        client = await _started(monkeypatch, handler)
        assert not pull_requests, "EOF must decline the pull, not pull"
        assert client.model_name == OLLAMA_7B, "EOF declines to the lighter installed model"
        assert client.ready

    @pytest.mark.asyncio
    async def test_no_usable_model_prints_visible_warning_then_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_mod, "detect_vram_gb", lambda: 16.0)
        printed: list[str] = []
        monkeypatch.setattr(
            llm_mod,
            "_console",
            type("_Console", (), {"print": staticmethod(lambda *a, **k: printed.append(" ".join(a)))}),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([])  # nothing installed, nothing to fall back to
            raise AssertionError(f"unexpected {request.url}")

        def eof_confirm(prompt: str, *, default: bool) -> bool:
            raise EOFError

        monkeypatch.setattr(llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(eof_confirm)}))

        client = _client(monkeypatch, handler)
        with pytest.raises(LLMUnavailableError) as excinfo:
            await client.start()
        assert "no usable model" in str(excinfo.value)
        assert printed, "a visible warning must explain the missing model"
        assert "no usable" in printed[-1]
        assert "BLUET_LLM_MODEL" in printed[-1]

    @pytest.mark.asyncio
    async def test_captured_stdin_oserror_declines_like_eof(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``rich``'s Confirm raises ``OSError`` when stdin is pytest-captured."""
        monkeypatch.setattr(llm_mod, "detect_vram_gb", lambda: 16.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B])
            raise AssertionError(f"unexpected {request.url}")

        def captured_confirm(prompt: str, *, default: bool) -> bool:
            raise OSError("pytest: reading from stdin while captured")

        monkeypatch.setattr(
            llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(captured_confirm)})
        )

        client = await _started(monkeypatch, handler)
        assert not pull_requests
        assert client.model_name == OLLAMA_7B
        assert client.ready


class TestAssumeYes:
    @pytest.mark.asyncio
    async def test_accepts_pull_without_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed: set[str] = set()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload(sorted(installed))
            if request.url.path == OLLAMA_PULL_PATH:
                pull_requests.append(request)
                installed.add(OLLAMA_7B)
                return httpx.Response(200, content=_pull_body(OLLAMA_7B))
            raise AssertionError(f"unexpected {request.url}")

        def fail(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("Confirm must not be shown with assume_yes=True")

        monkeypatch.setattr(llm_mod, "Confirm", type("_Confirm", (), {"ask": staticmethod(fail)}))

        client = await _started(monkeypatch, handler, assume_yes=True)
        assert len(pull_requests) == 1
        assert client.model_name == OLLAMA_7B
        assert client.ready


class TestAutoFallback:
    @pytest.mark.asyncio
    async def test_falls_back_from_ollama_to_vllm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.port == 11434:
                raise httpx.ConnectError("refused", request=request)
            if request.url.port == 8000 and request.url.path == VLLM_HEALTH_PATH:
                return httpx.Response(200, text="OK")
            if request.url.port == 8000 and request.url.path == VLLM_MODELS_PATH:
                return _vllm_models_payload([VLLM_7B])
            raise AssertionError(f"unexpected {request.url}")

        client = await _started(monkeypatch, handler)
        assert client.resolved_backend == "vllm"
        assert client.ready


class TestModelResolution:
    def test_tier_mapping_by_vram(self) -> None:
        assert resolve_model_tier("ollama", None)[0] == "small"
        assert resolve_model_tier("ollama", 8.0)[0] == "small"
        assert resolve_model_tier("ollama", 16.0)[0] == "medium"
        assert resolve_model_tier("ollama", 20.0)[0] == "medium"
        assert resolve_model_tier("ollama", 24.0)[0] == "large"
        assert resolve_model_tier("ollama", 40.0)[0] == "large"

    def test_tier_names_per_backend(self) -> None:
        assert resolve_model_tier("ollama", 24.0) == ("large", OLLAMA_32B)
        assert resolve_model_tier("vllm", 16.0) == ("medium", MODEL_TIERS["medium"]["vllm"])
        assert resolve_model_tier("vllm", None) == ("small", VLLM_7B)

    def test_model_override_wins(self) -> None:
        assert resolve_model_tier("ollama", 40.0, model_override="custom:latest") == (
            "override",
            "custom:latest",
        )

    def test_pick_fallback_prefers_lightest(self) -> None:
        assert pick_fallback_model([OLLAMA_32B, OLLAMA_7B], OLLAMA_14B, "ollama") == OLLAMA_7B
        assert pick_fallback_model([OLLAMA_32B], OLLAMA_7B, "ollama") == OLLAMA_32B
        assert pick_fallback_model([], OLLAMA_7B, "ollama") is None
        assert (
            pick_fallback_model(["some/other:model"], OLLAMA_7B, "ollama") == "some/other:model"
        )

    def test_detect_vram_gb_via_nvidia_smi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(llm_mod, "shutil", type("_Shutil", (), {"which": staticmethod(lambda n: "/usr/bin/nvidia-smi")}))
        # 24576 MiB -> 24.0 GiB
        completed = subprocess.CompletedProcess([], 0, stdout=b" 24576\n")
        monkeypatch.setattr(llm_mod.subprocess, "run", lambda *a, **k: completed)
        assert detect_vram_gb() == 24.0

    def test_detect_vram_gb_none_when_probe_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(llm_mod, "shutil", type("_Shutil", (), {"which": staticmethod(lambda n: "/usr/bin/nvidia-smi")}))
        completed = subprocess.CompletedProcess([], 1, stdout=b"")
        monkeypatch.setattr(llm_mod.subprocess, "run", lambda *a, **k: completed)
        assert detect_vram_gb() is None


class TestAppleSiliconTiering:
    """Apple Silicon has no discrete VRAM; tiers are sized off unified RAM."""

    def test_general_machine_no_gpu_stays_small(self) -> None:
        assert resolve_model_tier("ollama", None, apple_silicon=False)[0] == "small"

    def test_ram_sizes_tier_on_apple_silicon(self) -> None:
        assert resolve_model_tier("ollama", None, apple_silicon=True, ram_gb=40.0)[0] == "medium"
        assert resolve_model_tier("ollama", None, apple_silicon=True, ram_gb=32.0)[0] == "medium"
        assert resolve_model_tier("ollama", None, apple_silicon=True, ram_gb=24.0)[0] == "small"
        assert resolve_model_tier("ollama", None, apple_silicon=True, ram_gb=8.0)[0] == "small"
        assert resolve_model_tier("vllm", None, apple_silicon=True, ram_gb=40.0) == (
            "medium",
            MODEL_TIERS["medium"]["vllm"],
        )

    def test_discrete_vram_on_mac_wins_over_ram(self) -> None:
        # An eGPU equipped Mac reports VRAM; eGPU path beats RAM sizing.
        assert resolve_model_tier("ollama", 40.0, apple_silicon=True, ram_gb=8.0)[0] == "large"

    def test_is_apple_silicon_detects_arm64_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(llm_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(llm_mod.platform, "machine", lambda: "arm64")
        assert llm_mod.is_apple_silicon() is True
        monkeypatch.setattr(llm_mod.platform, "machine", lambda: "x86_64")
        assert llm_mod.is_apple_silicon() is False
        monkeypatch.setattr(llm_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(llm_mod.platform, "machine", lambda: "arm64")
        assert llm_mod.is_apple_silicon() is False

    def test_resolve_defaults_to_live_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(llm_mod, "is_apple_silicon", lambda: True)
        monkeypatch.setattr(llm_mod, "detect_ram_gb", lambda: 40.0)
        assert resolve_model_tier("ollama", None)[0] == "medium"


class _FakeAgent:
    instances: ClassVar[list[_FakeAgent]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.user_content: Any = None
        _FakeAgent.instances.append(self)

    async def run(self, content: Any) -> Any:
        self.user_content = content
        return _FakeRunResult()


class _FakeRunResult:
    output = None


class _Parity(BaseModel):
    parity: bool
    score: float | None = None


class TestComplete:
    @pytest.mark.asyncio
    async def test_uses_pydantic_ai_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B])
            raise AssertionError(f"unexpected {request.url}")

        monkeypatch.setattr(llm_mod, "Agent", _FakeAgent)
        _FakeAgent.instances.clear()

        client = await _started(monkeypatch, handler)
        messages = [
            {"role": "system", "content": "You are a strict refactoring verifier."},
            {"role": "user", "content": "Check parity for fib(10)."},
        ]
        result = await client.complete(messages, _Parity)
        assert result is _FakeRunResult.output
        agent = _FakeAgent.instances[-1]
        assert agent.kwargs["output_type"] is _Parity
        assert agent.kwargs["instructions"] == "You are a strict refactoring verifier."
        assert agent.kwargs["defer_model_check"] is True
        assert agent.user_content == ["Check parity for fib(10)."]

    @pytest.mark.asyncio
    async def test_not_ready_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(monkeypatch, _unreachable_all)

        with pytest.raises(LLMUnavailableError) as excinfo:
            await client.complete([{"role": "user", "content": "hi"}], _Parity)
        assert "not ready" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_schema_output_failure_raises_schema_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        class _SchemaFailAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def run(self, content: Any) -> Any:
                raise UnexpectedModelBehavior(
                    "Invalid JSON response: too many output retries (1)"
                )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload([OLLAMA_7B])
            raise AssertionError(f"unexpected {request.url}")

        monkeypatch.setattr(llm_mod, "Agent", _SchemaFailAgent)
        client = await _started(monkeypatch, handler)

        with pytest.raises(LLMSchemaOutputError) as excinfo:
            await client.complete([{"role": "user", "content": "hi"}], _Parity)

        message = str(excinfo.value)
        assert OLLAMA_7B in message
        assert "_Parity" in message
        assert "BLUET_LLM_MODEL" in message
        assert isinstance(excinfo.value, LLMUnavailableError) is False
        assert isinstance(excinfo.value, BluetLLMError)
        assert isinstance(excinfo.value, BluetEnvironmentError)


class TestModelOverrideEnv:
    @pytest.mark.asyncio
    async def test_bluet_llm_model_forces_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLUET_LLM_MODEL", "custom:latest")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == OLLAMA_TAGS_PATH:
                return _ollama_tag_payload(["custom:latest", OLLAMA_32B])
            raise AssertionError(f"unexpected {request.url}")

        client = await _started(monkeypatch, handler)
        assert client.model_name == "custom:latest"
        assert client.ready