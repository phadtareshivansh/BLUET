"""Unit tests for the Refactor Agent — mocked LLM, fully hermetic.

The LLM and formatter are injected deterministic fakes: no network, no
subprocess, no GPU. These tests prove the full pipeline contract
``LogicSpec in -> formatted ProposedCode out`` and the self-heal wiring
(whole-file regeneration with counter-examples in the prompt).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from bluet.agents.analyzer import LogicSpec, PythonParser
from bluet.agents.refactor import (
    CounterExample,
    ProposedCode,
    RefactorAgent,
    RefactorError,
    normalize_language,
)
from bluet.agents.refactor.agent import _TEMPLATES
from bluet.agents.refactor.formatters import format_code
from bluet.context_store import ContextHit, ContextIndexError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "python-legacy"
CURRENT_FILE = "python-legacy/simple_function.py"

SAMPLE_CODE = (
    "def compute_total(quantity, unit_price):\n"
    "    result = quantity * unit_price\n"
    "    return result\n"
)


def _fixture_spec(name: str = "simple_function.py") -> LogicSpec:
    source = (FIXTURES / name).read_text(encoding="utf-8")
    return PythonParser().parse(source)


class FakeLLM:
    """Deterministic stand-in for :class:`~bluet.agents.llm_client.LLMClient`."""

    def __init__(self, proposed: ProposedCode) -> None:
        self.ready = True
        self.calls = 0
        self.messages: list[dict[str, str]] = []
        self.response_model: type[BaseModel] | None = None
        self._proposed = proposed

    async def complete(
        self, messages: list[dict[str, str]], response_model: type[BaseModel]
    ) -> BaseModel:
        self.calls += 1
        self.messages = messages
        self.response_model = response_model
        return self._proposed


class FakeContextStore:
    def __init__(
        self, hits: list[ContextHit] | None = None, *, raise_on_query: bool = False
    ) -> None:
        self.hits = hits or []
        self.raise_on_query = raise_on_query
        self.queries: list[tuple[str, int]] = []

    async def query_context(self, query: str, *, top_k: int = 3) -> list[ContextHit]:
        self.queries.append((query, top_k))
        if self.raise_on_query:
            raise ContextIndexError("engine down", cause="engine")
        return self.hits


class RecordingFormatter:
    def __init__(self, prefix: str = "FORMATTED\n") -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, str]] = []

    def __call__(self, code: str, language: str) -> str:
        self.calls.append((code, language))
        return self.prefix + code


def _pipeline() -> tuple[FakeLLM, RecordingFormatter, FakeContextStore]:
    llm = FakeLLM(
        ProposedCode(
            file_path="ignored.py",
            code=SAMPLE_CODE,
            imports_added=["import os"],
            notes="pure arithmetic",
        )
    )
    fmt = RecordingFormatter()
    store = FakeContextStore()
    return llm, fmt, store


@pytest.mark.asyncio
async def test_pipeline_produces_formatted_proposed_code() -> None:
    llm, fmt, store = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)

    result = await agent.refactor(
        _fixture_spec(), target_language="python", current_file=CURRENT_FILE
    )

    assert isinstance(result, ProposedCode)
    assert result.file_path == CURRENT_FILE, "agent must use the repo-relative path"
    assert result.code == fmt.prefix + SAMPLE_CODE
    assert result.imports_added == ["import os"]
    assert result.notes == "pure arithmetic"
    assert fmt.calls == [(SAMPLE_CODE, "python")], "formatter runs after generation"
    assert llm.calls == 1
    assert llm.response_model is ProposedCode


@pytest.mark.asyncio
async def test_prompt_embeds_function_summary() -> None:
    llm, fmt, store = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)
    spec = _fixture_spec()

    await agent.refactor(spec, target_language="python", current_file=CURRENT_FILE)

    user = json.loads(llm.messages[1]["content"])
    fn = user["functions"][0]
    assert fn["name"] == "compute_total"
    assert fn["params"] == ["quantity", "unit_price"]
    assert fn["returns"] == ["total"]
    assert "Refactor Agent" in llm.messages[0]["content"]
    assert "compute_total" in llm.messages[0]["content"], "scaffold lists function stubs"


@pytest.mark.asyncio
async def test_context_hits_embedded_in_prompt() -> None:
    store = FakeContextStore(
        [
            ContextHit(
                id="h1",
                text="rate_limit() sleeps to avoid hammering hosts",
                score=0.92,
                metadata={"file": "python-legacy/loop_branch.py", "function": "main"},
            )
        ]
    )
    llm, fmt, _ = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)

    await agent.refactor(_fixture_spec(), target_language="python", current_file=CURRENT_FILE)

    assert len(store.queries) == 1
    query, top_k = store.queries[0]
    assert top_k == 3
    assert "compute_total" in query
    user = json.loads(llm.messages[1]["content"])
    assert user["context"] == [
        {
            "id": "h1",
            "text": "rate_limit() sleeps to avoid hammering hosts",
            "score": 0.92,
            "metadata": {"file": "python-legacy/loop_branch.py", "function": "main"},
        }
    ]


@pytest.mark.asyncio
async def test_context_index_error_is_soft() -> None:
    store = FakeContextStore(raise_on_query=True)
    llm, fmt, _ = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)

    result = await agent.refactor(
        _fixture_spec(), target_language="python", current_file=CURRENT_FILE
    )

    assert isinstance(result, ProposedCode), "index failure must not abort the pass"
    user = json.loads(llm.messages[1]["content"])
    assert user["context"] == []
    assert len(store.queries) == 1


@pytest.mark.asyncio
async def test_counter_examples_drive_whole_file_regen() -> None:
    llm, fmt, store = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)
    counter = CounterExample(
        function_name="compute_total",
        inputs=[2, 3],
        expected_output=6,
        observed_output=9,
        message="expected product, got sum",
    )

    await agent.refactor(
        _fixture_spec(),
        target_language="python",
        current_file=CURRENT_FILE,
        counter_examples=[counter],
    )

    assert llm.calls == 1, "whole-file regeneration == exactly one complete() call"
    system = llm.messages[0]["content"]
    assert "Counter-examples" in system
    user = json.loads(llm.messages[1]["content"])
    cx = user["counter_examples"][0]
    assert cx["function_name"] == "compute_total"
    assert cx["message"] == "expected product, got sum"
    assert cx["observed_output"] == 9


@pytest.mark.asyncio
async def test_language_aliases_route_to_python_formatter() -> None:
    llm, fmt, store = _pipeline()
    agent = RefactorAgent(llm=llm, context_store=store, formatter=fmt)

    await agent.refactor(_fixture_spec(), target_language="python2", current_file=CURRENT_FILE)
    assert fmt.calls[0][1] == "python"

    await agent.refactor(_fixture_spec(), target_language=None, current_file="src/x.py")
    assert fmt.calls[1][1] == "python", "empty target falls back to the .py suffix"

    assert normalize_language("java8") == "java"
    assert normalize_language("", filename="x.py") == "python"
    with pytest.raises(RefactorError, match="cobol"):
        normalize_language("cobol")
    with pytest.raises(RefactorError, match="unsupported"):
        normalize_language("")
    with pytest.raises(RefactorError, match="unsupported"):
        await agent.refactor(_fixture_spec(), target_language="cobol", current_file="x.cob")


def test_template_renders_scaffold() -> None:
    rendered = _TEMPLATES["python"].render(
        module_docstring="Refactored target module",
        imports="# :: imports_added are placed below by the model ::",
        bodies="def compute_total(quantity, unit_price):\n    pass",
    )
    assert "Refactored target module" in rendered
    assert "from __future__ import annotations" in rendered
    assert "imports_added are placed below" in rendered
    assert "def compute_total(quantity, unit_price):" in rendered


def test_format_code_ruff_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import bluet.agents.refactor.formatters as fmts

    monkeypatch.setattr(
        fmts.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            ["ruff", "format", "-"], 0, stdout="formatted\n", stderr=""
        ),
    )
    assert format_code("print(1)", "python") == "formatted\n"


def test_format_code_ruff_missing_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    import bluet.agents.refactor.formatters as fmts

    def _no_binary(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(fmts.subprocess, "run", _no_binary)

    with pytest.raises(RefactorError) as exc:
        format_code("print(1)", "python")
    assert "ruff" in str(exc.value)
    assert "install" in str(exc.value).lower()


def test_format_code_unknown_language() -> None:
    with pytest.raises(RefactorError, match="java"):
        format_code("class X {}", "java")
