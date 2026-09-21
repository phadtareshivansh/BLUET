"""Verification Agent output models — checkpoint-safe Pydantic.

``ParitySummary`` is what fills ``BluetState.parity_result`` (as a plain dict
via :meth:`ParitySummary.to_state`): one verdict per verify pass, plus the
per-function :class:`CounterExample` regressions that drive the self-heal loop.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from bluet.agents.refactor.models import CounterExample

ParityStatus = Literal["pass", "fail", "skip"]


class ParitySummary(BaseModel):
    """Result of one differential-parity pass over a file's checkable functions.

    ``status == "pass"`` only when every checkable function matched. ``"fail"``
    means at least one :class:`CounterExample` (or a harness-level crash, see
    ``harness_error``) was recorded. ``"skip"`` means nothing was checkable
    (no proposed code, non-Python target, or all functions flagged
    non-deterministic) — skips are graded as success by the orchestrator.
    """

    status: ParityStatus
    score: float | None = Field(
        default=None,
        description="Fraction of planned function checks that passed (0..1)",
    )
    n_checks: int = Field(default=0, description="Functions the harness planned to verify")
    skipped_functions: list[str] = Field(
        default_factory=list,
        description="Function names excluded (e.g. non-deterministic I/O)",
    )
    counter_examples: list[CounterExample] = Field(
        default_factory=list,
        description="Regressions found; empty on pass/skip",
    )
    harness_error: str | None = Field(
        default=None,
        description="Sandbox/harness crash message when the run never completed",
    )

    def to_state(self) -> dict[str, Any]:
        """Plain-dict form for ``BluetState.parity_result`` (checkpoint-safe)."""
        return {
            "status": self.status,
            "score": self.score,
            "n_checks": self.n_checks,
            "skipped_functions": self.skipped_functions,
            "counter_examples": [cx.model_dump() for cx in self.counter_examples],
            "harness_error": self.harness_error,
        }


__all__ = ["ParityStatus", "ParitySummary"]
