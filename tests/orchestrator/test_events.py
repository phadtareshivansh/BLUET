"""Integration tests for the async ZMQ event bus.

Runs against an in-memory async SQLite database (same wiring as the DAO tests)
so handler delivery and log_event persistence are verified end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bluet.orchestrator import TOPICS, EventBus
from bluet.orchestrator import events as events_mod
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


def test_topics_defined() -> None:
    assert TOPICS == frozenset(
        {
            "task.analysis",
            "task.context",
            "task.refactor",
            "task.verify",
            "feedback.regression",
            "feedback.warning",
        }
    )


@pytest.mark.asyncio
async def test_two_topics_both_handlers_fire_and_persist(
    session_factory: async_sessionmaker, job
) -> None:
    analysis: list[dict] = []
    refactor: list[dict] = []

    async def on_analysis(payload: dict) -> None:
        analysis.append(payload)

    async def on_refactor(payload: dict) -> None:
        refactor.append(payload)

    async with EventBus(session_factory) as bus:
        await bus.subscribe("task.analysis", on_analysis)
        await bus.subscribe("task.refactor", on_refactor)
        await bus.publish("task.analysis", {"job_id": job.id, "module": "a.py"})
        await bus.publish("task.refactor", {"job_id": job.id, "module": "b.py"})
        await bus.flush()

    assert len(analysis) == 1
    assert len(refactor) == 1
    a, r = analysis[0], refactor[0]
    assert a["topic"] == "task.analysis" and a["module"] == "a.py"
    assert r["topic"] == "task.refactor" and r["module"] == "b.py"
    assert a["job_id"] == job.id and r["job_id"] == job.id
    assert isinstance(datetime.fromisoformat(a["timestamp"]), datetime)
    assert isinstance(datetime.fromisoformat(r["timestamp"]), datetime)

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    assert {e.event_type for e in events} == {"task.analysis", "task.refactor"}
    assert sorted(json.loads(e.payload_json)["module"] for e in events) == ["a.py", "b.py"]
    assert all("job_id" in json.loads(e.payload_json) for e in events)
    assert bus.errors == []


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_other_topics(
    session_factory: async_sessionmaker, job
) -> None:
    slow_started = asyncio.Event()
    slow_done: list[float] = []
    fast_done: list[float] = []

    async def slow_handler(_payload: dict) -> None:
        slow_started.set()
        await asyncio.sleep(0.8)
        slow_done.append(time.perf_counter())

    async def fast_handler(_payload: dict) -> None:
        fast_done.append(time.perf_counter())

    async with EventBus(session_factory) as bus:
        await bus.subscribe("task.refactor", slow_handler)
        await bus.subscribe("task.verify", fast_handler)
        await bus.publish("task.refactor", {"job_id": job.id})
        await asyncio.wait_for(slow_started.wait(), timeout=5)
        t0 = time.perf_counter()
        await bus.publish("task.verify", {"job_id": job.id})
        await bus.flush()

    assert fast_done, "verify handler never fired"
    assert fast_done[0] - t0 < 0.4, "fast topic was blocked by the slow subscriber"
    assert slow_done, "slow handler never completed"


@pytest.mark.asyncio
async def test_publish_requires_job_id(session_factory: async_sessionmaker, job) -> None:
    async with EventBus(session_factory) as bus:
        await bus.subscribe("task.analysis", lambda payload: None)
        with pytest.raises(ValueError, match="job_id"):
            await bus.publish("task.analysis", {"module": "a.py"})

    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    assert events == []


@pytest.mark.asyncio
async def test_persist_failure_is_logged_and_unblocks_other_topics(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker,
    job,
    caplog: pytest.LogCaptureFixture,
) -> None:
    analysis: list[dict] = []
    verify: list[dict] = []

    async def on_analysis(payload: dict) -> None:
        analysis.append(payload)

    async def on_verify(payload: dict) -> None:
        verify.append(payload)

    async def boom(*_args, **_kwargs) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(events_mod, "log_event", boom)
    caplog.set_level(logging.ERROR, logger="bluet.orchestrator.events")

    async with EventBus(session_factory) as bus:
        await bus.subscribe("task.analysis", on_analysis)
        await bus.subscribe("task.verify", on_verify)
        await bus.publish("task.analysis", {"job_id": job.id})
        await bus.publish("task.verify", {"job_id": job.id})
        await bus.flush()

    assert analysis and verify, "delivery must not be blocked by the persist failure"
    assert bus.errors and isinstance(bus.errors[0], RuntimeError)
    assert any("db down" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_unknown_topic_rejected(session_factory: async_sessionmaker) -> None:
    async with EventBus(session_factory) as bus:
        await bus.subscribe("task.verify", lambda payload: None)
        with pytest.raises(ValueError, match="unknown topic"):
            await bus.publish("task.no_such_topic", {"job_id": 1})
