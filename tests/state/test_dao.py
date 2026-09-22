"""DAO round-trip tests against an in-memory async SQLite database."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bluet.state.db import init_schema
from bluet.state.repository import (
    create_job,
    get_job_by_id,
    list_events_for_job,
    log_event,
    record_parity_score,
    update_job_status,
)


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
async def session(engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest.mark.asyncio
async def test_create_job_roundtrip(session: AsyncSession) -> None:
    job = await create_job(session, "/repo/a", backend="gvisor")
    assert job.id is not None
    assert job.repo_path == "/repo/a"
    assert job.status == "PENDING"
    assert job.backend == "gvisor"
    assert job.created_at is not None
    assert (job.updated_at - job.created_at).total_seconds() < 1.0

    loaded = await get_job_by_id(session, job.id)
    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.repo_path == "/repo/a"
    assert loaded.status == "PENDING"
    assert loaded.backend == "gvisor"


@pytest.mark.asyncio
async def test_create_job_default_backend_from_cache(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bluet.state.repository.resolve_backend", lambda: "hardened-docker")
    job = await create_job(session, "/repo/b")
    assert job.repo_path == "/repo/b"
    assert job.backend == "hardened-docker"


@pytest.mark.asyncio
async def test_create_job_explicit_backend_wins(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bluet.state.repository.resolve_backend", lambda: "gvisor")
    job = await create_job(session, "/repo/c", backend="hardened-docker")
    assert job.backend == "hardened-docker"


@pytest.mark.asyncio
async def test_create_job_illegal_backend_falls_back_to_unknown(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bluet.state.repository.resolve_backend", lambda: "gvisor")
    job = await create_job(session, "/repo/d", backend="nonsense")
    assert job.backend == "unknown"


@pytest.mark.asyncio
async def test_update_job_status_roundtrip(session: AsyncSession) -> None:
    job = await create_job(session, "/repo/e", backend="gvisor")
    created = job.created_at

    updated = await update_job_status(session, job.id, "RUNNING")
    assert updated is not None
    assert updated.status == "RUNNING"
    assert updated.backend == "gvisor"
    assert updated.created_at == created
    assert updated.updated_at >= created

    loaded = await get_job_by_id(session, job.id)
    assert loaded is not None and loaded.status == "RUNNING"


@pytest.mark.asyncio
async def test_update_job_status_missing_returns_none(session: AsyncSession) -> None:
    assert await update_job_status(session, 99999, "RUNNING") is None


@pytest.mark.asyncio
async def test_log_event_roundtrip(session: AsyncSession) -> None:
    job = await create_job(session, "/repo/f", backend="gvisor")
    event = await log_event(
        session,
        job.id,
        agent_name="analyzer",
        event_type="module_scanned",
        payload={"module": "src/foo.py", "warnings": 2},
    )
    assert event.id is not None
    assert event.job_id == job.id
    assert event.agent_name == "analyzer"
    assert event.event_type == "module_scanned"
    assert json.loads(event.payload_json) == {"module": "src/foo.py", "warnings": 2}
    assert event.timestamp is not None

    events = await list_events_for_job(session, job.id)
    assert [e.id for e in events] == [event.id]


@pytest.mark.asyncio
async def test_log_event_foreign_key_enforced(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await log_event(session, 424242, agent_name="x", event_type="y")


@pytest.mark.asyncio
async def test_record_parity_score_roundtrip(session: AsyncSession) -> None:
    job = await create_job(session, "/repo/g", backend="gvisor")
    verified = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    row = await record_parity_score(
        session, job.id, module_path="src/bar.py", score=0.98, verified_at=verified
    )
    assert row.id is not None
    assert row.job_id == job.id
    assert row.module_path == "src/bar.py"
    assert row.score == 0.98
    assert row.verified_at.replace(tzinfo=UTC) == verified

    raw = await session.get(type(row), row.id)
    assert raw is not None
    assert raw.score == 0.98
    assert raw.module_path == "src/bar.py"


@pytest.mark.asyncio
async def test_get_job_by_id_missing_returns_none(session: AsyncSession) -> None:
    assert await get_job_by_id(session, 99999) is None
