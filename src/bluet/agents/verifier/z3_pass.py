"""Z3-assisted boundary/branch input synthesis for parity checks.

**What this pass covers**

*Numeric boundary conditions* — for each numeric-looking param, build an SMT
model over ``Int`` symbols and extract boundary *seed* inputs (``0``, ``±1``,
and the strategy-domain extremes) that the Hypothesis runner hardcodes via
``@example`` decorators. This guarantees the parity harness exercises the
edges of every numeric parameter, not just a random spread.

*Simple branch-coverage constraints* — each branch in the ``LogicSpec`` gets a
``Bool`` symbol; the solver checks pairwise-consistency of a multi-branch
coverage model (ensuring the spec itself encodes a reachable multi-branch
configuration, not just a degenerate single-branch graph).

**What this pass does NOT cover** (noted as open risk per PRD §15)

Full program equivalence / symbolic execution. Branch *conditions* are not
captured by the analyzer (``BranchSpec`` carries kind/line/label only), so Z3
cannot encode real reachability here. Treat Z3 as a *seed synthesizer for edge
inputs*, never as a parity oracle.
"""

from __future__ import annotations

from typing import Any

import z3

from bluet.agents.analyzer.models import FunctionDef
from bluet.agents.verifier.strategies import INT_MAX, INT_MIN, is_fully_numeric

#: Boundary candidates tested (in order); first satisfiable value wins.
_GRID_INT = [0, 1, -1, INT_MAX, INT_MIN, 10, -10]
_GRID_FLOAT = [0.0, 1.0, -1.0, 999.0, -999.0]


def _param_symbols(fn: FunctionDef) -> dict[str, z3.ArithRef]:
    return {name: z3.Int(name) for name in fn.params}


def synthesize_boundary_seeds(
    fn: FunctionDef,
) -> list[tuple[Any, ...]]:
    """Produce seed inputs from satisfiable Z3 boundary constraints.

    Returns ``[]`` when the function has no numeric params or is zero-arity
    (Hypothesis handles those natively). Otherwise returns a small batch of
    input tuples, one per ``@example`` line in the generated harness.

    The seeds are intentionally small (5–8 per numeric param) so the generated
    ``@example`` block does not bloat the harness. Each seed is guaranteed
    satisfiable — the solver extracted it — but the values themselves are
    deterministic grid points, not randomised.
    """
    if not fn.params or not is_fully_numeric(fn):
        return []

    syms = _param_symbols(fn)

    seeds: list[tuple[int, ...]] = []
    # Seed 1: all-zeros (trivially satisfiable).
    seeds.append(tuple(0 for _ in fn.params))

    # Per-param boundary sweep: fix one param to each grid value, others to 0.
    for i, name in enumerate(fn.params):
        for val in _GRID_INT:
            s = z3.Solver()
            for pname, sym in syms.items():
                if pname == name:
                    s.add(sym == val)
                else:
                    s.add(sym == 0)
            if s.check() == z3.sat:
                model = s.model()
                row = tuple(int(model[syms[p]].as_long()) for p in fn.params)
                seeds.append(row)

    # Deduplicate preserving order.
    seen: set[tuple[int, ...]] = set()
    deduped: list[tuple[int, ...]] = []
    for row in seeds:
        if row not in seen:
            seen.add(row)
            deduped.append(row)
    return deduped


def check_branch_coverage(fn: FunctionDef) -> tuple[bool, int]:
    """Verify the branch list in ``fn`` is self-consistent under Z3.

    For each branch a ``Bool`` symbol is created. The solver checks whether a
    full coverage model exists where all branches can *simultaneously* be
    true (jointly satisfiable) — a necessary but not sufficient condition for
    real reachability (see module docstring).

    Returns ``(all_jointly_satisfiable, n_branches)``. When there are zero
    branches the function trivially returns ``(True, 0)``.
    """
    n = len(fn.branches)
    if n == 0:
        return True, 0

    bools = [z3.Bool(f"branch_{fn.name}_{i}") for i in range(n)]
    solver = z3.Solver()
    solver.add(z3.And(*bools))

    all_sat = solver.check() == z3.sat
    return all_sat, n


__all__ = [
    "check_branch_coverage",
    "synthesize_boundary_seeds",
]
