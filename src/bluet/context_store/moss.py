"""Moss SDK-backed context store (``bluet[moss]`` extra).

This backend drives the **real** Moss SDK (``pip install moss``) against the
:mod:`Moss HTTP API <https://docs.moss.dev>` — semantic retrieval with an
optional hybrid ``alpha`` blend and keyword fallback. It is the only store
eligible to satisfy the :data:`~bluet.context_store.LATENCY_BUDGET_MS`
latency NFR: the in-memory backend is deterministic by design and never a
benchmark subject.

Integration path (documented choice)
    We use the **first-party async Python SDK** (``moss`` on PyPI) directly —
    not a subprocess/RPC layer and not hand-rolled Rust FFI. Moss ships an
    official async Python SDK that wraps its Rust core (``inferedge-moss-core``
    wheels come in per platform on ``pip install moss``), so a Python-side
    bridge would only duplicate a maintained, versioned API we can ``await``
    uniformly. We index through a real-time **``SessionIndex``**
    (``await client.session(index_name=...)``), the documented path for
    indexing during a live interaction: ``add_docs`` embeds locally (~1-5 ms),
    which is exactly the hot-path the PRD (Section 13) measures, and cloud
    persistence is available via ``push_index``/``create_index`` when we want
    durability. This is the path the benchmark measures — the in-memory
    backend is never a subject.

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
    :func:`bluet.license.gate` first (see README "License gate").
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from bluet.agents.analyzer.models import FunctionDef
from bluet.context_store import LATENCY_BUDGET_MS
from bluet.context_store.base import (
    ContextHit,
    ContextIndexError,
    ContextIndexSummary,
    IndexedFile,
)
from bluet.context_store.memory import InMemoryContextStore


def _function_doc(fn: FunctionDef) -> str:
    """Render one ``FunctionDef`` as the text Moss embeds and searches.

    ``FunctionDef`` carries structured analysis rather than raw source text, so
    the embedding document is a stable serialization of that structure: a
    readable ``function ... at line ...`` line plus the canonical JSON. Sorted
    keys keep identical specs bit-identical across runs.
    """
    body = json.dumps(fn.model_dump(), sort_keys=True, ensure_ascii=True)
    return f"function {fn.name} at line {fn.line}: {body}"


class MossContextStore(InMemoryContextStore):
    """Index and query context via the real Moss SDK.

    Uses a lazily-created real-time ``SessionIndex`` (the documented live
    interaction path). ``index_logic_spec`` writes one document per function
    and reports the honest wall-clock around ``add_docs``; ``query_context``
    runs the query against the same session and returns ``ContextHit``s with
    the real scores. The in-memory store remains only as the *construction*
    base (safe, dependency-free defaults); it is **never** silently reported
    as a Moss answer.
    """

    engine = "moss"

    def __init__(self, *, project_id: str | None = None, project_key: str | None = None) -> None:
        super().__init__()
        self._project_id = project_id or os.environ.get("MOSS_PROJECT_ID")
        self._project_key = project_key or os.environ.get("MOSS_PROJECT_KEY")
        self._moss_client = None
        self._moss_session = None
        self._moss_module = None
        self._session_name = "bluet-context"

    # -- lazy SDK lifecycle -------------------------------------------------

    def _client(self):
        if not self._project_id or not self._project_key:
            raise ContextIndexError(
                "moss SDK credentials missing; set MOSS_PROJECT_ID and "
                "MOSS_PROJECT_KEY to run the Moss latency benchmark",
                cause="missing-creds",
            )
        try:
            import moss
        except ImportError as exc:
            raise ContextIndexError(
                'moss SDK not installed; run `pip install "bluet[moss]"` or '
                "`uv sync --extra moss` to run the Moss latency benchmark",
                cause="missing-package",
            ) from exc

        if self._moss_client is None:
            self._moss_module = moss
            self._moss_client = moss.MossClient(self._project_id, self._project_key)
        return self._moss_client

    async def _session(self):
        """Return the cached real-time ``SessionIndex``, creating it lazily."""
        if self._moss_session is None:
            client = self._client()
            try:
                self._moss_session = await client.session(self._session_name)
            except Exception as exc:
                raise ContextIndexError(
                    f"failed to open Moss session '{self._session_name}': {exc}",
                    cause="engine",
                ) from exc
        return self._moss_session

    # -- indexing path (real SDK, honest latency) ---------------------------

    async def index_logic_spec(
        self, spec: Any, *, file_path: str, language: str
    ) -> ContextIndexSummary:
        """Index ``spec`` into Moss and report the honest wall-clock latency.

        One document per function (id ``f"{language}:{file_path}:{name}"``);
        the document text is the function's structured record, which Moss
        embeds via its default model. ``latency_ms``/``within_budget`` are
        measured around the real ``add_docs`` call.
        """
        started = time.perf_counter()
        try:
            functions = list(getattr(spec, "functions", None) or [])
            if not functions:
                raise ContextIndexError(
                    f"no functions to index in {file_path}; refusing a no-op Moss write",
                    cause="empty-spec",
                )
            documents = [
                {
                    "id": f"{language}:{file_path}:{fn.name}",
                    "text": _function_doc(fn),
                    "metadata": {
                        "file_path": file_path,
                        "language": language,
                        "name": fn.name,
                        "line": str(fn.line),
                    },
                }
                for fn in functions
            ]
            session = await self._session()
            await session.add_docs(documents)
        except ContextIndexError:
            raise
        except Exception as exc:
            raise ContextIndexError(
                f"moss add_docs failed for {file_path}: {exc}", cause="engine"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        return ContextIndexSummary(
            engine=self.engine,
            n_files=1,
            n_functions=len(documents),
            n_chunks=len(documents),
            latency_ms=latency_ms,
            within_budget=latency_ms <= LATENCY_BUDGET_MS,
            indexed_files=[
                IndexedFile(
                    index_id=f"{language}:{file_path}",
                    file_path=file_path,
                    language=language,
                    n_functions=len(documents),
                    n_chunks=len(documents),
                    engine=self.engine,
                )
            ],
        )

    # -- query path (SDK truth) ----------------------------------------------

    async def query_context(self, query: str, *, top_k: int = 3) -> list[ContextHit]:
        """Query the real Moss session; never a silent in-memory fallback."""
        session = await self._session()
        opts = self._moss_module.QueryOptions(top_k=top_k)
        try:
            results = await session.query(query, opts)
        except Exception as exc:
            raise ContextIndexError(f"moss query failed: {exc}", cause="engine") from exc
        if hasattr(results, "time_taken_ms"):
            self._last_query_latency_ms = results.time_taken_ms
        return [
            ContextHit(
                id=str(doc.id),
                text=getattr(doc, "text", "") or "",
                score=float(getattr(doc, "score", 0.0)),
                metadata={
                    "engine": self.engine,
                    "file_path": (getattr(doc, "metadata", {}) or {}).get("file_path", ""),
                    "language": (getattr(doc, "metadata", {}) or {}).get("language", ""),
                    "name": (getattr(doc, "metadata", {}) or {}).get("name", ""),
                },
            )
            for doc in getattr(results, "docs", [])
        ]


__all__ = ["MossContextStore"]
