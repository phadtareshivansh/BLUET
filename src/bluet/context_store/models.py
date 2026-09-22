"""Payload models for the context store — checkpoint-safe, dependency-free.

Mirrors the repository convention (see
:mod:`bluet.agents.analyzer.models`): plain Pydantic models whose only job is to
round-trip through :meth:`~pydantic.BaseModel.model_dump` for checkpoints and
event payloads)Skip. ``engine`` always names the concrete backend that produced
a result so latency/parity post-mortems can tell a real Moss run apart from the
in-memory fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ContextIndexError(Exception):
    """Any failure while indexing or querying a :class:`ContextStore`.

    Raised by backends when indexing fails for ``engine``; graph nodes treat it
    as a soft failure and keep the pipeline moving. ``cause`` records whether
    the failure was a missing package, missing credentials, or an engine error
    so operations can tell "not installed" apart from "db down".
    """

    def __init__(self, message: str, *, cause: str = "unknown") -> None:
        super().__init__(message)
        self.cause = cause


class IndexedFile(BaseModel):
    """One file successfully indexed into the store."""

    index_id: str = Field(description="Stable id for this file's index entry")
    file_path: str = Field(description="Repo-relative path of the indexed file")
    language: str = Field(description="Language the logic was parsed under")
    n_functions: int = Field(description="Number of functions indexed")
    n_chunks: int = Field(description="Number of embedding chunks written")
    indexed_at: datetime = Field(default_factory=_utc_now)
    engine: str = Field(description='Backend that produced the index: "moss" or "memory"')


class ContextHit(BaseModel):
    """One retrieved context result for a refactor query."""

    id: str = Field(description="Stable id of the matched function")
    text: str = Field(description="Human-readable snippet for the model")
    score: float = Field(description="Similarity/score from the engine")
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Source attribution: file, function name, line, language",
    )


class ContextIndexSummary(BaseModel):
    """Returned by :meth:`ContextStore.index_logic_spec` on success."""

    engine: str = Field(description="Backend that produced the index")
    n_files: int = Field(default=1)
    n_functions: int = Field(default=0)
    n_chunks: int = Field(default=0)
    latency_ms: float = Field(default=0.0, description="Honest wall-clock indexing time, measured")
    within_budget: bool = Field(default=True)
    indexed_files: list[IndexedFile] = Field(default_factory=list)
