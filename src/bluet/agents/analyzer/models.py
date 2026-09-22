"""Pydantic models describing the Analyzer Agent's ``LogicSpec`` output.

The parsed output must be checkpoint-safe, so ``LogicSpec`` models live only
inside the analyzer agent: :func:`bluet.agents.analyzer.get_logic_spec`
reconstructs the object from the plain dict stored in ``BluetState.logic_spec``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, computed_field

NondeterministicCallKind = Literal["file", "network", "jdbc", "console"]


class NondeterministicCall(BaseModel):
    """A single call or statement the analyzer flags as non-deterministic I/O."""

    kind: NondeterministicCallKind
    name: str
    line: int
    target: str | None = None


class BranchSpec(BaseModel):
    """A single control-flow construct (branch or loop) in a function body."""

    kind: str
    line: int
    label: str | None = None


class FunctionDef(BaseModel):
    """Everything the analyzer extracted from one function or method.

    This is the unit of work downstream agents act on: the Verification Agent
    generates Hypothesis inputs from ``inputs``/``params`` and self-heal
    counter-examples target a specific function, not the whole file.
    """

    name: str
    line: int
    params: list[str]
    returns: list[str]
    inputs: list[str]
    outputs: list[str]
    state_mutations: list[str]
    branches: list[BranchSpec]
    nondeterministic_calls: list[NondeterministicCall]


def _dedupe(values: Iterable[str]) -> list[str]:
    """Return values in order, dropping earlier duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class LogicSpec(BaseModel):
    """Structured view of one analyzed source file.

    ``functions`` is the source of truth. The flat top-level fields are
    ``@computed_field`` properties derived by aggregating across ``functions``
    rather than stored state, so the per-function and file-level views cannot
    drift out of sync. They exist purely as a convenience for file-level
    summaries (e.g. ``bluet diff``'s quick non-deterministic-call report).
    """

    functions: list[FunctionDef]

    @computed_field
    @property
    def inputs(self) -> list[str]:
        return _dedupe(c for fn in self.functions for c in fn.inputs)

    @computed_field
    @property
    def outputs(self) -> list[str]:
        return _dedupe(c for fn in self.functions for c in fn.outputs)

    @computed_field
    @property
    def state_mutations(self) -> list[str]:
        return _dedupe(c for fn in self.functions for c in fn.state_mutations)

    @computed_field
    @property
    def branches(self) -> list[BranchSpec]:
        return [branch for fn in self.functions for branch in fn.branches]

    @computed_field
    @property
    def nondeterministic_calls(self) -> list[NondeterministicCall]:
        return [call for fn in self.functions for call in fn.nondeterministic_calls]
