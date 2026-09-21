"""Repository/DAO layer over the async SQLite store.

Functions take an ``AsyncSession`` (dependency-injected) so unit tests can
drive an in-memory database while callers wire a repo-scoped engine from
:mod:`bluet.state.db`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bluet.state.db import utc_now
from bluet.state.doctor_cache import resolve_backend
from bluet.state.models import AgentEvent, Job, ParityScore

VALID_BACKENDS = {"gvisor", "hardened-docker", "unknown"}


async def create_job(
    session: AsyncSession,
    repo_path: str,
    status: str = "PENDING",
    *,
    backend: str | None = None,
) -> Job:
    """Create a job row; stamp ``backend`` from the doctor cache.

    When no explicit backend is supplied, :func:`resolve_backend` is run in a
    thread (its inline probe shells out to docker) and the resulting
    ``gvisor``/``hardened-docker`` value is stamped onto the row. A job is
    still created when docker is unavailable, stamped ``unknown``.
    """
    if backend is None:
        backend = await asyncio.to_thread(resolve_backend)
    if backend not in VALID_BACKENDS:
        backend = "unknown"

    job = Job(repo_path=repo_path, status=status, backend=backend)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def update_job_status(
    session: AsyncSession, job_id: int, status: str
) -> Job | None:
    """Set a job's status (bumping ``updated_at``); returns the row or ``None``."""
    job = await session.get(Job, job_id)
    if job is None:
        return None
    job.status = status
    job.updated_at = utc_now()
    await session.commit()
    await session.refresh(job)
    return job


async def log_event(
    session: AsyncSession,
    job_id: int,
    agent_name: str,
    event_type: str,
    payload: dict[str, object] | None = None,
) -> AgentEvent:
    """Record an agent event; ``payload`` is JSON-serialized for storage."""
    event = AgentEvent(
        job_id=job_id,
        agent_name=agent_name,
        event_type=event_type,
        payload_json=json.dumps(payload, sort_keys=True),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def record_parity_score(
    session: AsyncSession,
    job_id: int,
    module_path: str,
    score: float,
    verified_at: datetime | None = None,
) -> ParityScore:
    """Persist one module's parity measurement."""
    row = ParityScore(
        job_id=job_id,
        module_path=module_path,
        score=score,
        verified_at=verified_at or utc_now(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_job_by_id(session: AsyncSession, job_id: int) -> Job | None:
    """Fetch a job by primary key, or ``None``."""
    return await session.get(Job, job_id)


async def list_events_for_job(session: AsyncSession, job_id: int) -> list[AgentEvent]:
    """Return all events for a job, oldest first (used by status/audit)."""
    result = await session.execute(
        select(AgentEvent).where(AgentEvent.job_id == job_id).order_by(AgentEvent.timestamp)
    )
    return list(result.scalars().all())