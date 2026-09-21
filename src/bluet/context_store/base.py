"""Context-store protocol and soft-failure semantics — the import hub.

Single responsibility: (1) define :class:`ContextStore`, the abstract contract
every backend implements, and :class:`ContextIndexError`, the one error type the
whole graph treats as a soft failure; (2) re-export the payload models from
:mod:`bluet.context_store.models` so callers and tests can import the exact
:class:`ContextHit` / :class:`IndexedFile` / :class:`ContextIndexSummary` / error
from one stable home. No Moss, no tree-sitter, no credentials — importable from
tests that must never require the optional extra.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bluet.context_store.models import (
    ContextHit,
    ContextIndexError,
    ContextIndexSummary,
    IndexedFile,
)


class ContextStore(ABC):
    """Abstract queryable context index every backend implements.

    Backends are safe to construct with zero I/O (construction never raises),
    expose ``engine``, and implement :meth:`index_logic_spec` and
    :meth:`query_context`. A failing backend raises :class:`ContextIndexError`;
    callers decide whether that is fatal. The concrete backends are
    :class:`~bluet.context_store.memory.InMemoryContextStore` (the default,
    dependency-free) and, behind the optional ``bluet[moss]`` extra, the real
    Moss-backed store.
    """

    engine: str

    @abstractmethod
    async def index_logic_spec(
        self, spec: object, *, file_path: str, language: str
    ) -> ContextIndexSummary:
        """Index one analyzed ``LogicSpec`` for ``file_path`` under ``language``.

        Async-first by contract: the real Moss SDK performs genuine I/O and
        must be awaited uniformly at every call site. There is **no** sync
        variant and **no** sync/async dispatch — every backend, including the
        hermetic in-memory one, is ``async def`` and awaited the same way.
        """

    @abstractmethod
    async def query_context(self, query: str, *, top_k: int = 3) -> list[ContextHit]:
        """Return the top-``top_k`` snippets most relevant to ``query`` (async)."""


__all__ = [
    "ContextHit",
    "ContextIndexError",
    "ContextIndexSummary",
    "ContextStore",
    "IndexedFile",
]
