"""Unit tests for the real-Moss context store, hermetic via a fake SDK.

No ``moss`` package, no credentials, no network needed: ``sys.modules["moss"]``
is swapped for a fake async client/session, so the lazy-SDK gates, the
document-per-function indexing contract, the honest-latency summary, and the
result mapping are all exercised in CI. The real-SDK-only latency measurement
lives in ``tests/benchmarks/test_moss_latency.py``.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import sys
import types
from types import SimpleNamespace

import pytest

from bluet.agents.analyzer import FunctionDef, LogicSpec
from bluet.context_store.base import ContextIndexError, ContextStore
from bluet.context_store.memory import InMemoryContextStore
from bluet.context_store.moss import MossContextStore


def _function(name: str = "billing_apply_tax", line: int = 3) -> FunctionDef:
    return FunctionDef(
        name=name,
        line=line,
        params=["amount", "rate"],
        returns=["total"],
        inputs=["amount", "rate"],
        outputs=["total"],
        state_mutations=["total"],
        branches=[],
        nondeterministic_calls=[],
    )


def _logic_spec(*functions: FunctionDef) -> LogicSpec:
    return LogicSpec(functions=list(functions))


# -- fake moss SDK -----------------------------------------------------------


class _FakeDoc:
    def __init__(self, doc_id: str, text: str, score: float, metadata: dict):
        self.id = doc_id
        self.text = text
        self.score = score
        self.metadata = metadata


class _FakeResults:
    def __init__(self, docs: list[_FakeDoc], time_taken_ms: float = 0.0):
        self.docs = docs
        self.time_taken_ms = time_taken_ms


class _FakeSessionIndex:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.fail_add: Exception | None = None
        self.fail_query: Exception | None = None
        self.slow_add_ms: float = 0.0
        self.last_query: str | None = None
        self.last_options: SimpleNamespace | None = None
        self.response_docs: list[_FakeDoc] = []

    async def add_docs(self, documents: list[dict]) -> None:
        if self.slow_add_ms:
            await asyncio.sleep(self.slow_add_ms / 1000.0)
        if self.fail_add is not None:
            raise self.fail_add
        self.added.extend(documents)

    async def query(self, query: str, options: SimpleNamespace) -> _FakeResults:
        if self.fail_query is not None:
            raise self.fail_query
        self.last_query = query
        self.last_options = options
        return _FakeResults(docs=self.response_docs)


class _FakeClient:
    def __init__(self, session_index: _FakeSessionIndex) -> None:
        self.session_index = session_index
        self.last_index_name: str | None = None

    async def session(self, index_name: str | None = None) -> _FakeSessionIndex:
        self.last_index_name = index_name
        return self.session_index


class _FakeQueryOptions:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def _mount_fake_moss(monkeypatch: pytest.MonkeyPatch, session: _FakeSessionIndex) -> _FakeClient:
    client = _FakeClient(session)
    module = types.ModuleType("moss")
    module.MossClient = lambda *a, **k: client  # type: ignore[attr-defined]
    module.QueryOptions = _FakeQueryOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "moss", module)
    return client


# -- uniform async contract guard --------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [ContextStore, InMemoryContextStore, MossContextStore],
)
def test_context_store_contract_is_async_only(cls: type) -> None:
    assert inspect.iscoroutinefunction(cls.index_logic_spec)
    assert inspect.iscoroutinefunction(cls.query_context)


# -- lazy SDK / credentials gates --------------------------------------------


def test_constructor_is_lazy_and_dependency_free() -> None:
    store = MossContextStore()
    assert store.engine == "moss"
    assert store._moss_session is None


def test_client_raises_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "moss", raising=False)
    real_import = builtins.__import__

    def _block(name: str, *args, **kwargs) -> object:
        if name == "moss":
            raise ImportError("no moss here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block)
    store = MossContextStore(project_id="p", project_key="k")
    with pytest.raises(ContextIndexError, match="not installed") as excinfo:
        store._client()
    assert excinfo.value.cause == "missing-package"


def test_client_raises_when_creds_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _mount_fake_moss(monkeypatch, _FakeSessionIndex())
    monkeypatch.delenv("MOSS_PROJECT_ID", raising=False)
    monkeypatch.delenv("MOSS_PROJECT_KEY", raising=False)
    store = MossContextStore()
    with pytest.raises(ContextIndexError, match="credentials missing") as excinfo:
        store._client()
    assert excinfo.value.cause == "missing-creds"


# -- indexing path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_index_logic_spec_writes_one_doc_per_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSessionIndex()
    client = _mount_fake_moss(monkeypatch, session)
    store = MossContextStore(project_id="p", project_key="k")

    spec = _logic_spec(_function("apply_tax", line=3), _function("apply_discount", line=9))
    summary = await store.index_logic_spec(spec, file_path="src/billing.py", language="python")

    assert client.last_index_name == "bluet-context"
    assert len(session.added) == 2
    assert session.added[0]["id"] == "python:src/billing.py:apply_tax"
    assert session.added[1]["id"] == "python:src/billing.py:apply_discount"
    assert session.added[0]["text"].startswith("function apply_tax at line 3: {")
    assert session.added[0]["metadata"]["line"] == "3"

    assert summary.engine == "moss"
    assert summary.n_functions == 2
    assert summary.n_chunks == 2
    assert summary.indexed_files[0].engine == "moss"
    assert summary.latency_ms >= 0.0
    assert summary.within_budget == (summary.latency_ms <= 3.0)


@pytest.mark.asyncio
async def test_index_logic_spec_measures_honest_wallclock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSessionIndex()
    session.slow_add_ms = 5.0  # real wall-time > 3ms budget
    _mount_fake_moss(monkeypatch, session)
    store = MossContextStore(project_id="p", project_key="k")

    summary = await store.index_logic_spec(
        _logic_spec(_function()), file_path="src/slow.py", language="python"
    )

    assert summary.latency_ms >= 5.0, "latency must be the measured wall-clock"
    assert summary.within_budget is False, "a >3ms real index must report out-of-budget"


@pytest.mark.asyncio
async def test_index_empty_spec_refuses_noop_write(monkeypatch: pytest.MonkeyPatch) -> None:
    _mount_fake_moss(monkeypatch, _FakeSessionIndex())
    store = MossContextStore(project_id="p", project_key="k")
    with pytest.raises(ContextIndexError, match="no functions") as excinfo:
        await store.index_logic_spec(_logic_spec(), file_path="src/none.py", language="python")
    assert excinfo.value.cause == "empty-spec"


@pytest.mark.asyncio
async def test_index_sdk_error_becomes_context_index_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSessionIndex()
    session.fail_add = RuntimeError("cloud blew up")
    _mount_fake_moss(monkeypatch, session)
    store = MossContextStore(project_id="p", project_key="k")

    with pytest.raises(ContextIndexError, match="add_docs failed") as excinfo:
        await store.index_logic_spec(
            _logic_spec(_function()), file_path="src/err.py", language="python"
        )
    assert excinfo.value.cause == "engine"


# -- query path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_context_maps_results_and_forwards_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSessionIndex()
    session.response_docs = [
        _FakeDoc(
            doc_id="python:src/billing.py:apply_tax",
            text="function {...}",
            score=0.91,
            metadata={"file_path": "src/billing.py", "language": "python", "name": "apply_tax"},
        )
    ]
    _mount_fake_moss(monkeypatch, session)
    store = MossContextStore(project_id="p", project_key="k")

    hits = await store.query_context("refactor billing amount", top_k=5)

    assert session.last_query == "refactor billing amount"
    assert session.last_options.top_k == 5
    assert len(hits) == 1
    assert hits[0].id == "python:src/billing.py:apply_tax"
    assert hits[0].score == 0.91
    assert hits[0].metadata["engine"] == "moss"
    assert hits[0].metadata["name"] == "apply_tax"


@pytest.mark.asyncio
async def test_query_sdk_error_becomes_context_index_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSessionIndex()
    session.fail_query = RuntimeError("index not loaded")
    _mount_fake_moss(monkeypatch, session)
    store = MossContextStore(project_id="p", project_key="k")

    with pytest.raises(ContextIndexError, match="query failed") as excinfo:
        await store.query_context("anything", top_k=3)
    assert excinfo.value.cause == "engine"
