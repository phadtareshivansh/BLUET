"""LangGraph StateGraph that drives a refactor job.

Nodes are stubs for now: pass-through functions that only log their stage over
the :class:`~bluet.orchestrator.events.EventBus` and return a ``{}`` update,
with a single real piece of control flow — the self-heal retry loop. Retries
are capped by ``MAX_RETRIES``; checkpoints are persisted to the repo's
``.bluet/state.db`` via LangGraph's async SQLite checkpointer so a paused job
can resume with the same ``thread_id``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, NotRequired, TypedDict

# Restrict checkpoint deserialization to known-safe types (see PyPI advisory).
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from bluet.agents.analyzer import parser_for_source
from bluet.agents.analyzer.models import LogicSpec
from bluet.context_store import ContextIndexError, ContextStore, InMemoryContextStore
from bluet.orchestrator.events import EventBus

MAX_RETRIES = 3

TOPIC_ANALYZE = "task.analysis"
TOPIC_INDEX = "task.context"
TOPIC_REFACTOR = "task.refactor"
TOPIC_VERIFY = "task.verify"
TOPIC_REGRESSION = "feedback.regression"
TOPIC_WARNING = "feedback.warning"

StateUpdate = dict[str, Any]
Node = Callable[[EventBus, "BluetState"], Awaitable[StateUpdate] | StateUpdate]


class BluetState(TypedDict):
    """Per-job state threaded through the refactor pipeline."""

    job_id: int
    repo_path: str
    target_language: str
    current_file: str
    logic_spec: NotRequired[dict[str, Any] | None]
    proposed_code: NotRequired[str | None]
    parity_result: NotRequired[dict[str, Any] | None]
    retry_count: int


def _event_payload(state: BluetState, stage: str) -> dict[str, Any]:
    return {
        "job_id": state["job_id"],
        "stage": stage,
        "repo_path": state["repo_path"],
        "current_file": state["current_file"],
    }


async def analyze_node(bus: EventBus, state: BluetState) -> StateUpdate:
    """Read the job's target file and reduce it to a :class:`LogicSpec`.

    Tolerant by design: an unknown target language, an unresolvable parser,
    or a missing file all degrade to ``{}`` so the pipeline can still run (and
    resume via checkpoints — ``logic_spec`` is a plain dict for that reason).
    """
    await bus.publish(TOPIC_ANALYZE, _event_payload(state, "analyze"))
    parser = parser_for_source(state["target_language"], state["current_file"])
    if parser is None:
        return {}
    path = Path(state["repo_path"]) / state["current_file"]
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    spec = parser.parse(source)
    return {"logic_spec": spec.model_dump()}


async def context_index_node(
    bus: EventBus, state: BluetState, *, context_store: ContextStore
) -> StateUpdate:
    """Index the job's :class:`LogicSpec` into the context store.

    Fail-soft by design: an index error or a latency budget miss **never**
    aborts the pipeline. On success we publish ``task.context`` with the
    honest measured latency and the budget verdict; when the index falls
    outside :data:`LATENCY_BUDGET_MS` (or the store raises) we publish the
    generic ``feedback.warning`` channel with ``source="context_index"`` so
    orchestrator/guardrail consumers can distinguish it from other warnings.
    """
    raw = state.get("logic_spec")
    if raw is None:
        await bus.publish(
            TOPIC_WARNING,
            {**_event_payload(state, "context_index"), "source": "context_index", "reason": "no_logic_spec"},
        )
        return {}
    spec = LogicSpec.model_validate(raw)
    try:
        summary = context_store.index_logic_spec(
            spec, file_path=state["current_file"], language=state["target_language"]
        )
    except ContextIndexError as exc:
        await bus.publish(
            TOPIC_WARNING,
            {**_event_payload(state, "context_index"), "source": "context_index", "reason": str(exc)},
        )
        return {}
    if not summary.within_budget:
        await bus.publish(
            TOPIC_WARNING,
            {
                **_event_payload(state, "context_index"),
                "source": "context_index",
                "reason": "latency_budget",
                "latency_ms": summary.latency_ms,
            },
        )
    await bus.publish(
        TOPIC_INDEX,
        {
            **_event_payload(state, "context_index"),
            "n_functions": len(spec.functions),
            "engine": summary.engine,
            "latency_ms": summary.latency_ms,
            "within_budget": summary.within_budget,
        },
    )
    return {}


async def refactor_node(bus: EventBus, state: BluetState) -> StateUpdate:
    await bus.publish(TOPIC_REFACTOR, _event_payload(state, "refactor"))
    return {}


async def verify_node(bus: EventBus, state: BluetState) -> StateUpdate:
    await bus.publish(TOPIC_VERIFY, _event_payload(state, "verify"))
    return {"parity_result": {"status": "pass", "score": None}}


async def self_heal_node(bus: EventBus, state: BluetState) -> StateUpdate:
    await bus.publish(TOPIC_REGRESSION, _event_payload(state, "self_heal"))
    return {"retry_count": state["retry_count"] + 1}


def _route(state: BluetState) -> str:
    parity = state.get("parity_result") or {}
    if parity.get("status") == "pass":
        return "success"
    if state["retry_count"] < MAX_RETRIES:
        return "retry"
    return "failed"


def build_refactor_graph(
    bus: EventBus,
    *,
    checkpointer: AsyncSqliteSaver | None = None,
    verify_node: Node = verify_node,
    context_store: ContextStore = InMemoryContextStore(),
):
    """Assemble and compile the refactor graph.

    ``verify_node`` is replaceable so tests can simulate an always-failing
    verifier. ``checkpointer`` opts into pause/resume persistence.
    ``context_store`` backs the ``context_index`` node (in-memory by default
    so the offline suite stays hermetic; pass a real SDK-backed store to
    benchmark against Moss).
    """
    graph = StateGraph(BluetState)
    graph.add_node("analyze", partial(analyze_node, bus))
    graph.add_node("context_index", partial(context_index_node, bus, context_store=context_store))
    graph.add_node("refactor", partial(refactor_node, bus))
    graph.add_node("verify", partial(verify_node, bus))
    graph.add_node("self_heal", partial(self_heal_node, bus))

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "context_index")
    graph.add_edge("context_index", "refactor")
    graph.add_edge("refactor", "verify")
    graph.add_conditional_edges(
        "verify",
        _route,
        {"success": END, "retry": "self_heal", "failed": END},
    )
    graph.add_edge("self_heal", "refactor")

    return graph.compile(checkpointer=checkpointer)


@asynccontextmanager
async def open_sqlite_checkpointer(db_path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Yield an async SQLite checkpointer for ``db_path`` (e.g. ``.bluet/state.db``)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def run_config(job_id: int) -> dict[str, dict[str, str]]:
    """Per-job thread config so resume reuses the same checkpoint lineage."""
    return {"configurable": {"thread_id": str(job_id)}}