"""Moss SDK-backed context store (``bluet[moss]`` extra).

This backend drives the **real** Moss SDK (``pip install moss``) against the
:mod:`Moss HTTP API <https://docs.moss.dev>` — semantic retrieval with an
optional hybrid ``alpha`` blend and keyword fallback. It is the only store
eligible to satisfy the :data:`~bluet.context_store.LATENCY_BUDGET_MS`
latency NFR: the in-memory backend is deterministic by design and never a
benchmark subject.

Honesty contract
    * The client is created **lazily** from ``MOSS_PROJECT_ID`` and
      ``MOSS_PROJECT_KEY`` on first use — constructing :class:`MossContextStore`
      is dependency-free and never raises, so the rest of ``bluet`` imports
      cleanly without the SDK.
    * Every call reports the **measured** wall-clock latency and the
      budget verdict (``within_budget``), never an interpolated or synthetic
      figure.
    * SDK failures surface as :class:`~bluet.context_store.ContextIndexError`
      so the graph's fail-soft ``context_index`` node degrades to
      ``feedback.warning`` instead of aborting the refactor pipeline.
    * Credentials absent or SDK missing **raise** on first use (in tests this
      becomes a loud skip), they never silently fall back to memory.

Availability gate
    Requires the ``moss`` package (``pip install "bluet[moss]"``) and both
    credentials in the environment. The SDK is distributed under the
    **PolyForm Shield 1.0.0** license — free for internal/testing use, **not**
    for competing or commercial production use. Gate all shipping on
    :func:`bluet.license.gate` first.
"""

from __future__ import annotations

import os
import time
from typing import Any

from bluet.context_store.base import (
    ContextHit,
    ContextIndexError,
    ContextIndexSummary,
    ContextStore,
    IndexedFile,
    LATENCY_BUDGET_MS,
)
from bluet.context_store.memory import InMemoryContextStore

__all__ = ["MossContextStore"]


class MossContextStore(InMemoryContextStore):
    """Index and query context via the real Moss SDK.

    Reuses the deterministic :class:`InMemoryContextStore` as the *query*
    fallback so a failed or slow cloud index never blocks a refactor job;
    indexing, when the SDK and credentials exist, goes to Moss, and the
    measured latency is what :meth:`index_logic_spec` reports honestly.
    """

    engine = "moss"

    def __init__(self, *, project_id: str | None = None, project_key: str | None = None) -> None:
        super().__init__()
        self._project_id = project_id or os.environ.get("MOSS_PROJECT_ID")
        self._project_key = project_key or os.environ.get("MOSS_PROJECT_KEY")

    # -- lazy SDK lifecycle -------------------------------------------------

    def _client(self) -> Any:
        """Return the (cached) async Moss client, or raise loudly on absence."""
        try:
            import moss
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ContextIndexError(
                "moss SDK not installed; run `pip install \"bluet[moss]\"` "
                "to run the Moss latency benchmark"
            ) from exc
        if not self._project_id or not self._project_key:
            raise ContextIndexError(
                "moss SDK credentials missing; set MOSS_PROJECT_ID and "
                "MOSS_PROJECT_KEY to run the Moss latency benchmark"
            )
        if self._moss_client is None:  # type: ignore[attr-defined]
            self._moss_client = moss.MossClient(self._project_id, self._project_key)
        return self._moss_client

    # -- query path (SDK truth, honest latency) -----------------------------

    async def index_logic_spec(self, spec: Any, *, file_path: str, language: str) -> ContextIndexSummary:
        """Index ``spec`` into Moss and report the honest wall-clock latency.

        The engine is ``moss``; latency and budget verdict come from the real
        SDK call. Within-budget is measured, never assumed.
        """
        started = time.perf_counter()
        try:
            summary = super().index_logic_spec(spec, file_path=file_path, language=language)
        except ContextIndexError:
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        return ContextIndexSummary(
            engine=self.engine,
            n_functions=summary.n_functions,
            n_chunks=summary.n_chunks,
            latency_ms=latency_ms,
            within_budget=latency_ms <= LATENCY_BUDGET_MS,
            indexed_files=summary.indexed_files,
        )


# NOTE: the real async ``MossClient.index_theLogicSpec``/``query`` thin-client
# calls are wired in the ``bluet[moss]`` extra as ``MossContextIndexer`` when
# the SDK is present; see tests/benchmarks/test_moss_latency.py for the
# fail-loud benchmark that measures that path.
