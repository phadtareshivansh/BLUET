"""Async event bus for orchestrator traffic.

Backed by a ZMQ PUB/SUB pair over a per-instance ``inproc://`` endpoint, so
multiple bus instances never collide. The bus is job-agnostic infrastructure:
``publish`` requires callers to include ``job_id`` in the payload (no null FK
ever reaches the DB), and every published event is mirrored into the
persistence layer through :func:`bluet.state.repository.log_event` as a
fire-and-forget task so ``publish`` stays non-blocking during heavy runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

import zmq
import zmq.asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from bluet.state.db import utc_now
from bluet.state.repository import log_event

logger = logging.getLogger("bluet.orchestrator.events")

TOPICS: frozenset[str] = frozenset(
    {
        "task.analysis",
        "task.context",
        "task.refactor",
        "task.verify",
        "feedback.regression",
        "feedback.warning",
    }
)

TOPIC_AGENTS: dict[str, str] = {
    "task.analysis": "analyzer",
    "task.context": "indexer",
    "task.refactor": "refactor",
    "task.verify": "verifier",
    "feedback.regression": "verifier",
    # Warning is a single generic channel shared by every feature (Moss SDK
    # latency misses, Enkrypt guardrail flags, …). Producers differentiate
    # themselves with ``payload["source"]`` — never with one topic per feature —
    # so a post-mortem can tell "context index missed budget" apart from
    # "guardrail fired" without growing the topic set.
    "feedback.warning": "orchestrator",
}

DEFAULT_AGENT = "orchestrator"

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]


class EventBus:
    """Asyncio PUB/SUB bus with durable, fire-and-forget event logging.

    Parameters
    ----------
    session_factory:
        Zero-arg callable returning an ``AsyncSession``; used to persist each
        event via the ``log_event`` DAO.
    settle:
        Warm-up delay after each ``subscribe`` so ZMQ's subscription
        propagation reaches the publisher before the first publish (avoids the
        ZMQ "slow joiner" drop problem).
    topic_agents:
        Optional override for the topic -> ``agent_name`` mapping used when
        persisting events (defaults to :data:`TOPIC_AGENTS`).
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        settle: float = 0.05,
        topic_agents: Mapping[str, str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settle = settle
        self._topic_agents = dict(topic_agents or TOPIC_AGENTS)
        self._endpoint = f"inproc://bluet-events-{uuid.uuid4().hex}"
        self._context: zmq.asyncio.Context | None = None
        self._publisher: zmq.asyncio.Socket | None = None
        self._subscribers: dict[str, zmq.asyncio.Socket] = {}
        self._reader_tasks: dict[str, asyncio.Task[None]] = {}
        self._handlers: dict[str, list[Handler]] = {}
        self._outstanding = 0
        self._drained = asyncio.Event()
        self._drained.set()
        # SQLite tolerates only one writer at a time; serialize the DAO write
        # (the asyncio lock lets publish stay non-blocking while writes queue).
        self._persist_lock = asyncio.Lock()
        self._started = False
        self.errors: list[BaseException] = []

    async def start(self) -> Self:
        if self._started:
            return self
        self._context = zmq.asyncio.Context()
        self._publisher = self._context.socket(zmq.PUB)
        self._publisher.bind(self._endpoint)
        self._started = True
        return self

    async def stop(self, timeout: float = 10.0) -> None:
        if not self._started:
            return
        try:
            await self.flush(timeout=timeout)
        finally:
            for task in self._reader_tasks.values():
                task.cancel()
            if self._reader_tasks:
                await asyncio.gather(*self._reader_tasks.values(), return_exceptions=True)
            self._reader_tasks.clear()
            for sock in self._subscribers.values():
                sock.close(linger=0)
            self._subscribers.clear()
            if self._publisher is not None:
                self._publisher.close(linger=0)
                self._publisher = None
            if self._context is not None:
                self._context.destroy(linger=0)
                self._context = None
            self._started = False

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def subscribe(self, topic: str, handler: Handler) -> None:
        """Register ``handler`` for events on ``topic``; returns immediately.

        Multiple handlers on the same topic share one SUB socket and are each
        dispatched concurrently, so a slow handler never blocks its peers.
        """
        if not self._started:
            raise RuntimeError("event bus is not started")
        if topic not in TOPICS:
            raise ValueError(f"unknown topic: {topic!r}")

        if topic not in self._handlers:
            sock = self._context.socket(zmq.SUB)
            sock.connect(self._endpoint)
            sock.setsockopt(zmq.SUBSCRIBE, topic.encode())
            self._subscribers[topic] = sock
            self._reader_tasks[topic] = asyncio.create_task(self._reader(topic, sock))
            self._handlers[topic] = []
        self._handlers[topic].append(handler)
        await asyncio.sleep(self._settle)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish ``payload`` on ``topic`` and persist it in the background.

        Raises ``ValueError`` immediately when ``job_id`` is missing so a null
        foreign key never reaches the DAO layer. The publish itself is
        non-blocking: ZMQ buffers the send, and the DB write runs in a
        fire-and-forget task (failures are logged and surfaced on
        :attr:`errors`).
        """
        if not self._started:
            raise RuntimeError("event bus is not started")
        if topic not in TOPICS:
            raise ValueError(f"unknown topic: {topic!r}")
        if "job_id" not in payload or payload["job_id"] is None:
            raise ValueError("publish payload must include a 'job_id'")

        envelope = dict(payload)
        envelope["topic"] = topic
        envelope["timestamp"] = utc_now().isoformat()

        await self._publisher.send_multipart(
            [topic.encode(), json.dumps(envelope, sort_keys=True).encode()]
        )
        self._track(self._persist(envelope))

    async def flush(self, timeout: float = 10.0) -> None:
        """Block until all published events have been dispatched and persisted.

        Polls until no work is outstanding for a short stabilization window, so
        the tiny gap between a publish's ZMQ enqueue and the reader task's
        dispatch is covered without requiring bare sleeps from callers.
        """
        if not self._started:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stable_since: float | None = None
        while True:
            await asyncio.sleep(0.01)
            if self._outstanding == 0 and self._drained.is_set():
                now = loop.time()
                stable_since = stable_since or now
                if now - stable_since >= 0.05:
                    return
            else:
                stable_since = None
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"event bus flush timed out with {self._outstanding} tasks outstanding"
                )

    async def _reader(self, topic: str, sock: zmq.asyncio.Socket) -> None:
        while True:
            try:
                frames = await sock.recv_multipart()
            except zmq.ContextTerminated:
                return
            except Exception:
                logger.exception("event bus reader error on %s", topic)
                return
            try:
                frame_topic = frames[0].decode()
                payload = json.loads(frames[1])
            except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if frame_topic != topic:
                continue
            for handler in list(self._handlers.get(topic, ())):
                self._track(self._run_handler(handler, payload))

    @staticmethod
    async def _run_handler(handler: Handler, payload: dict[str, Any]) -> None:
        try:
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "event bus handler %r raised", getattr(handler, "__name__", handler)
            )

    async def _persist(self, envelope: dict[str, Any]) -> None:
        # Executed as a detached task: any DAO/DB error must not crash the
        # publisher or block other topics. Surface it and keep going.
        try:
            async with self._persist_lock, self._session_factory() as session:
                await log_event(
                    session,
                    job_id=envelope["job_id"],
                    agent_name=self._topic_agents.get(envelope["topic"], DEFAULT_AGENT),
                    event_type=envelope["topic"],
                    payload=envelope,
                )
        except Exception as exc:  # noqa: BLE001
            self.errors.append(exc)
            logger.error(
                "failed to persist event on topic %s: %s", envelope["topic"], exc
            )

    def _track(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        self._outstanding += 1
        self._drained.clear()
        task = asyncio.ensure_future(coro)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, _task: asyncio.Task[None]) -> None:
        self._outstanding -= 1
        if self._outstanding <= 0:
            self._outstanding = 0
            self._drained.set()