"""End-to-end tests for the LangGraph refactor pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bluet.agents.analyzer import LogicSpec
from bluet.agents.refactor import CounterExample, ProposedCode
from bluet.errors import LLMSchemaOutputError, LLMUnavailableError
from bluet.orchestrator import (
    MAX_RETRIES,
    EventBus,
    build_refactor_graph,
    open_sqlite_checkpointer,
    run_config,
)
from bluet.orchestrator.refactor_graph import verify_node
from bluet.state.db import init_schema
from bluet.state.repository import create_job, list_events_for_job


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    await init_schema(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def job(session_factory: async_sessionmaker):
    async with session_factory() as sess:
        return await create_job(sess, "/repo/bus", backend="gvisor")


def _initial_state(job_id: int) -> dict:
    return {
        "job_id": job_id,
        "repo_path": "/repo/bus",
        "target_language": "python",
        "current_file": "src/foo.py",
        "retry_count": 0,
    }


def _event_types(events: list) -> list[str]:
    return [e.event_type for e in events]


@pytest.mark.asyncio
async def test_retry_loop_terminates_after_max_retries(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    verify_calls: list[int] = []

    async def always_fail(bus: EventBus, state: dict) -> dict:
        await bus.publish(
            "task.verify",
            {
                "job_id": state["job_id"],
                "stage": "verify",
                "current_file": state["current_file"],
            },
        )
        verify_calls.append(state["job_id"])
        return {"parity_result": {"status": "fail", "reason": "regression"}}

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(bus, checkpointer=saver, verify_node=always_fail)

        final = await graph.ainvoke(_initial_state(job.id), config=run_config(job.id))

    assert final["retry_count"] == MAX_RETRIES
    assert final["parity_result"]["status"] == "fail"
    assert len(verify_calls) == 4, "expected 1 initial + 3 retry verify attempts"

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    types = _event_types(events)
    assert types.count("task.analysis") == 1
    assert types.count("task.refactor") == 4
    assert types.count("task.verify") == 4
    assert types.count("feedback.regression") == MAX_RETRIES

    async with open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver:
        saved = [c async for c in saver.alist(run_config(job.id))]
    assert saved, "expected at least one checkpoint to be persisted for resume"


@pytest.mark.asyncio
async def test_success_path_ends_after_first_verify(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(bus, checkpointer=saver, verify_node=verify_node)

        final = await graph.ainvoke(_initial_state(job.id), config=run_config(job.id))

    assert final["parity_result"]["status"] == "pass"
    assert final["retry_count"] == 0

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    types = _event_types(events)
    assert all(types.count(t) == 1 for t in ("task.analysis", "task.refactor", "task.verify"))
    assert types.count("feedback.regression") == 0

    for event in events:
        payload = json.loads(event.payload_json)
        assert payload["job_id"] == job.id
        assert "timestamp" in payload


@pytest.mark.asyncio
async def test_analyze_node_round_trips_real_logic_spec(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    state = _initial_state(job.id)
    state.update(
        repo_path=str(fixtures),
        target_language="python",
        current_file="python-legacy/simple_function.py",
    )

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, refactor_agent=_StubRefactorAgent(ProposedCode(file_path="dummy", code=""))
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    raw = final["logic_spec"]
    assert raw, "analyze_node should produce a logic_spec from the real fixture"
    assert LogicSpec.model_validate(raw).model_dump() == raw
    spec = LogicSpec.model_validate(raw)
    assert spec.functions, "parsed logic_spec should contain at least one function"
    for fn in spec.functions:
        assert fn.name
        assert fn.line >= 1


@pytest.mark.asyncio
async def test_analyze_event_persists_to_agent_event(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    state = _initial_state(job.id)
    state.update(
        repo_path=str(fixtures),
        target_language="python",
        current_file="python-legacy/simple_function.py",
    )

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, refactor_agent=_StubRefactorAgent(ProposedCode(file_path="dummy", code=""))
        )
        await graph.ainvoke(state, config=run_config(job.id))

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    types = _event_types(events)
    assert types.count("task.analysis") == 1
    analysis = next(e for e in events if e.event_type == "task.analysis")
    payload = json.loads(analysis.payload_json)
    assert payload["job_id"] == job.id
    assert payload["stage"] == "analyze"


class _StubRefactorAgent:
    """Deterministic stand-in for :class:`RefactorAgent` (no LLM, no formatter)."""

    def __init__(
        self,
        proposed: ProposedCode | None = None,
        *,
        exc: Exception | None = None,
    ) -> None:
        self.proposed = proposed
        self.exc = exc
        self.calls = 0
        self.last_args: tuple[str, str, list[CounterExample] | None] | None = None

    async def refactor(self, spec, *, target_language, current_file, counter_examples=None):
        self.calls += 1
        self.last_args = (target_language, current_file, counter_examples)
        if self.exc is not None:
            raise self.exc
        return self.proposed


def _fixture_state(job_id: int) -> dict:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    state = _initial_state(job_id)
    state.update(
        repo_path=str(fixtures),
        target_language="python",
        current_file="python-legacy/simple_function.py",
    )
    return state


@pytest.mark.asyncio
async def test_refactor_self_heal_loop_on_parity_failure(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    """Buggy proposal triggers self-heal: MAX_RETRIES retries, then fail."""
    buggy_code = "def compute_total(quantity, unit_price):\n    return quantity + unit_price\n"
    fake = _StubRefactorAgent(
        ProposedCode(
            file_path="python-legacy/simple_function.py",
            code=buggy_code,
            imports_added=[],
            notes="buggy implementation",
        )
    )
    state = _fixture_state(job.id)

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, verify_node=verify_node, refactor_agent=fake
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    # Self-heal loop runs MAX_RETRIES times (3), so refactor_agent called 4 times total
    assert fake.calls == MAX_RETRIES + 1
    # On retries 2-4, the agent receives counter-examples from the previous failure
    assert fake.last_args is not None
    lang, current_file, counter_examples = fake.last_args
    assert (lang, current_file) == ("python", "python-legacy/simple_function.py")
    assert counter_examples, "agent should receive counter-examples on retry"
    assert counter_examples[0].function_name == "compute_total"
    # Parity still fails after max retries
    assert final["retry_count"] == MAX_RETRIES
    assert final["parity_result"]["status"] == "fail"

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    types = [e.event_type for e in events]
    assert types.count("task.analysis") == 1
    assert types.count("task.refactor") == MAX_RETRIES + 1
    assert types.count("task.verify") == MAX_RETRIES + 1
    assert types.count("feedback.regression") == MAX_RETRIES


@pytest.mark.asyncio
async def test_refactor_correct_proposal_passes_on_first_verify(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    """Correct proposal passes parity on first try."""
    correct_code = "def compute_total(quantity, unit_price):\n    return quantity * unit_price\n"
    fake = _StubRefactorAgent(
        ProposedCode(
            file_path="python-legacy/simple_function.py",
            code=correct_code,
            imports_added=[],
            notes="correct implementation",
        )
    )
    state = _fixture_state(job.id)

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, verify_node=verify_node, refactor_agent=fake
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert fake.calls == 1
    lang, current_file, counter_examples = fake.last_args
    assert (lang, current_file) == ("python", "python-legacy/simple_function.py")
    assert counter_examples == [], "no counter-examples on first pass"
    assert final["retry_count"] == 0
    assert final["parity_result"]["status"] == "pass"

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    types = [e.event_type for e in events]
    assert types.count("task.analysis") == 1
    assert types.count("task.refactor") == 1
    assert types.count("task.verify") == 1
    assert types.count("feedback.regression") == 0


@pytest.mark.asyncio
async def test_refactor_node_fails_soft_on_llm_unavailable(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    fake = _StubRefactorAgent(exc=LLMUnavailableError("no backend on localhost"))
    state = _fixture_state(job.id)

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, verify_node=verify_node, refactor_agent=fake
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is None, "LLM failure must degrade, not abort"
    assert final["parity_result"]["status"] == "pass"
    assert final["retry_count"] == 0

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    warns = [json.loads(e.payload_json) for e in events if e.event_type == "feedback.warning"]
    assert warns, "expected a feedback.warning for the refactor failure"
    assert all(w["source"] == "refactor" for w in warns)
    assert "no backend on localhost" in warns[-1]["reason"]


@pytest.mark.asyncio
async def test_refactor_node_fails_soft_on_schema_output_error(
    session_factory: async_sessionmaker,
    job,
    tmp_path: Path,
) -> None:
    fake = _StubRefactorAgent(
        exc=LLMSchemaOutputError("model could not produce ProposedCode after retries")
    )
    state = _fixture_state(job.id)

    async with EventBus(session_factory) as bus, open_sqlite_checkpointer(
        tmp_path / ".bluet" / "state.db"
    ) as saver:
        graph = build_refactor_graph(
            bus, checkpointer=saver, verify_node=verify_node, refactor_agent=fake
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is None, "schema-output failure must degrade, not abort"
    assert final["retry_count"] == 0

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    warns = [json.loads(e.payload_json) for e in events if e.event_type == "feedback.warning"]
    assert warns, "expected a feedback.warning for the schema-output failure"
    assert warns[-1]["source"] == "refactor"
    assert "could not produce ProposedCode" in warns[-1]["reason"]