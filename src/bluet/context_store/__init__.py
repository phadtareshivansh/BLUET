"""Context store package: durable, queryable logic-spec indices.

The refactor pipeline produces a :class:`~bluet.agents.analyzer.models.LogicSpec`
per analyzed file. A context store keeps that derived model queryable for the
refactor and verify stages (and eventually the benchmark/parity harness) without
reparsing the source on every node visit.

Tolerant by design (matching the repository's fail-soft convention): the
default in-memory store is always available with zero dependencies or
credentials, and the real Moss-backed store degrades to warnings — never raised
exceptions — when the native SDK or its credentials are unavailable, so an
indexing problem can never halt a refactor job.
"""

from bluet.context_store.base import (
    ContextHit,
    ContextIndexError,
    ContextIndexSummary,
    ContextStore,
    IndexedFile,
)
from bluet.context_store.memory import InMemoryContextStore

LATENCY_BUDGET_MS = 3.0

__all__ = [
    "LATENCY_BUDGET_MS",
    "ContextHit",
    "ContextIndexError",
    "ContextIndexSummary",
    "ContextStore",
    "InMemoryContextStore",
    "IndexedFile",
]
