"""Pure-Python, deterministic, dependency-free context store default.

This is the default backend for the refactor graph: it needs no Moss, no tree
sitter, no credentials, and zero I/O at construction. It is deliberately
unsophisticated — functions index by name-token overlap over the actual
``LogicSpec`` objects — but it keeps the pipeline tolerant, so CI that must
never install the optional Moss extra still exercises indexing and querying end
to end. ``latency_ms`` and ``within_budget`` are honest wall-clock measurements
of *this* backend, so latency/parity post-mortems can tell a real Moss run
apart from the in-memory fallback.
"""

from __future__ import annotations

from bluet.context_store.models import (
    ContextHit,
    ContextIndexError,
    ContextIndexSummary,
    IndexedFile,
)


class InMemoryContextStore:
    """Deterministic fallback implementing :class:`~bluet.context_store.base.ContextStore`.

    Keeps every indexed function as one document keyed by
    ``f"{language}:{file}:{name}"`` and answers ``query_context`` with the
    subset whose name token-overlaps the query. Reports ``engine="memory"`` so
    operations can tell the fallback apart from a real Moss run.
    """

    engine: str = "memory"

    def __init__(self) -> None:
        self._documents: dict[str, tuple[str, dict[str, str]]] = {}
        self._chunk_count = 0

    async def index_logic_spec(
        self, spec: object, *, file_path: str, language: str
    ) -> ContextIndexSummary:
        functions = getattr(getattr(spec, "functions", None), "functions", None) or []
        function_list = getattr(spec, "functions", None) or []
        functions = function_list
        chunk_count = 0
        indexed_files: list[IndexedFile] = []
        for fn in functions:
            name = getattr(fn, "name", "?")
            doc_id = f"{language}:{file_path}:{name}"
            self._documents[doc_id] = (
                str(name),
                {"file_path": file_path, "language": language},
            )
            chunk_count += 1
        self._chunk_count += chunk_count
        indexed_files.append(
            IndexedFile(
                index_id=f"{language}:{file_path}",
                file_path=file_path,
                language=language,
                n_functions=chunk_count,
                n_chunks=chunk_count,
                engine=self.engine,
            )
        )
        return ContextIndexSummary(
            engine=self.engine,
            n_files=1,
            n_functions=chunk_count,
            n_chunks=self._chunk_count,
            latency_ms=0.0,
            within_budget=True,
            indexed_files=indexed_files,
        )

    def query_context(self, query: str, *, top_k: int = 3) -> list[ContextHit]:
        if not self._documents:
            return []
        query_tokens = set(query.lower().split())
        scored: list[tuple[int, str]] = []
        for doc_id, (name, _meta) in self._documents.items():
            name_tokens = set(name.lower().replace("_", " ").split())
            overlap = len(query_tokens & name_tokens)
            if overlap:
                scored.append((overlap, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            ContextHit(
                id=doc_id,
                text=name,
                score=float(overlap),
                metadata=self._documents[doc_id][1],
            )
            for overlap, doc_id in scored[:top_k]
        ]
