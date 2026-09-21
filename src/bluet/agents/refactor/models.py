"""Payload models for the Refactor Agent — checkpoint-safe, dependency-free.

Mirrors the repository convention (see :mod:`bluet.agents.analyzer.models`):
plain Pydantic models whose only job is to round-trip through
:meth:`~pydantic.BaseModel.model_dump` for checkpoints and event payloads.
``ProposedCode`` is the single structured output of a refactor pass; it is
stored in ``BluetState.proposed_code`` as a plain dict and reconstructed by
downstream nodes (:class:`~bluet.verifier` in Prompt 2.3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProposedCode(BaseModel):
    """Target-language code synthesized by the Refactor Agent.

    Fields
    ------
    file_path:
        Repo-relative path of the file being rewritten (mirrors the job's
        ``current_file``).
    code:
        The complete reformatted target file (whole-file regeneration; there
        is no incremental per-function patching in this prototype).
    imports_added:
        Import lines the model introduced, captured separately so a later
        merge/verify pass can audit them without reparsing the file.
    notes:
        Free-form justification/assumptions from the model, for humans.
    """

    file_path: str = Field(description="Repo-relative path of the rewritten file")
    code: str = Field(description="Complete, formatted target-language source")
    imports_added: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, description="Model notes on the refactor")


class CounterExample(BaseModel):
    """A regression the Verification Agent caught for one function.

    ``function_name`` is required, not optional: it is what lets self-heal
    (Prompt 2.2) tell the LLM precisely which function's logic is wrong, even
    though the current self-heal scope regenerates the whole file.
    ``diff_summary`` explains the mismatch (or the structural problem, e.g. the
    function is missing entirely) for humans and ``bluet diff``.
    """

    function_name: str = Field(description="Function that failed parity verification")
    inputs: list[Any] = Field(
        default_factory=list, description="Counter-example inputs (Hypothesis-generated)"
    )
    expected_output: Any | None = Field(
        default=None, description="Output the legacy implementation produced"
    )
    actual_output: Any | None = Field(
        default=None, description="Output the proposed implementation produced"
    )
    observed_output: Any | None = Field(
        default=None,
        description="Deprecated alias for ``actual_output``; kept for back-compat and synced by a validator",
    )
    diff_summary: str | None = Field(
        default=None, description="Human-readable summary of the mismatch"
    )
    message: str | None = Field(default=None, description="Verifier's failure message")

    @model_validator(mode="after")
    def _sync_output_alias(self) -> CounterExample:
        if self.observed_output is None and self.actual_output is not None:
            self.observed_output = self.actual_output
        if self.actual_output is None and self.observed_output is not None:
            self.actual_output = self.observed_output
        return self
