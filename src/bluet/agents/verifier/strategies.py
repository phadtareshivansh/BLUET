"""Hypothesis strategy synthesis for parity property tests.

The analyzer's :class:`FunctionDef` captures parameter *names*, not static
types, so the verifier derives one Hypothesis strategy source-expression per
parameter from naming conventions (with an explicit, documented override
registry for cases the heuristics miss — e.g. a list-of-dicts ``records``
param). This is honest input *synthesis*, not type inference: it is the
prototype's stated limitation, and Phase 3/4 can revisit it if the analyzer
gains a type model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from bluet.agents.analyzer.models import FunctionDef

INT_MIN = -1000
INT_MAX = 1000

_NUMERIC_TOKENS = {
    "quantity",
    "qty",
    "count",
    "amount",
    "price",
    "unitprice",
    "total",
    "value",
    "balance",
    "score",
    "level",
    "size",
    "index",
    "num",
    "iteration",
    "iterations",
    "n",
    "i",
    "j",
    "k",
    "x",
    "y",
    "z",
    "w",
    "h",
    "width",
    "height",
}
_MONEY_TOKENS = {
    "price",
    "amount",
    "unitprice",
    "total",
    "balance",
    "surcharge",
    "cost",
    "fee",
}
_BOOL_TOKENS = {"premium", "flag", "enabled", "active", "dryrun", "verbose"}
_STRING_TOKENS = {
    "path",
    "name",
    "label",
    "key",
    "id",
    "title",
    "text",
    "code",
    "line",
    "file",
    "url",
    "host",
    "user",
    "dir",
}
_COLLECTION_TOKENS = {
    "records",
    "items",
    "rows",
    "entries",
    "values",
    "collection",
    "list",
    "arr",
    "array",
    "recordslist",
}

_DEFAULT_COLLECTION_EXPR = f"st.lists(st.integers(min_value={INT_MIN}, max_value={INT_MAX}))"
_INT_EXPR = f"st.integers(min_value={INT_MIN}, max_value={INT_MAX})"
_FLOAT_EXPR = (
    "st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False)"
)
_BOOL_EXPR = "st.booleans()"
_TEXT_EXPR = "st.text()"

#: Explicit strategy registry: function name -> one expression per parameter.
#: Registered strategies win over the heuristics; use this for shapes the
#: conventions cannot express (nested records, enums, …).
_REGISTERED: dict[str, list[str]] = {}


def register_function_strategy(name: str, exprs: Sequence[str]) -> None:
    """Register exact strategy expressions for function ``name`` (override)."""
    _REGISTERED[name] = [str(expr) for expr in exprs]


def unregister_function_strategy(name: str) -> None:
    """Drop a registered strategy (used by tests to stay hermetic)."""
    _REGISTERED.pop(name, None)


def _tokens(param: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", param) if t]


def _strategy_expr(param: str) -> str:
    lower = param.lower()
    toks = {t.lower() for t in _tokens(param)}
    if toks & _MONEY_TOKENS or lower in _MONEY_TOKENS:
        return _FLOAT_EXPR
    if toks & _NUMERIC_TOKENS or lower in _NUMERIC_TOKENS:
        return _INT_EXPR
    if toks & _BOOL_TOKENS or lower in _BOOL_TOKENS:
        return _BOOL_EXPR
    if toks & _STRING_TOKENS or lower in _STRING_TOKENS:
        return _TEXT_EXPR
    if toks & _COLLECTION_TOKENS or lower in _COLLECTION_TOKENS:
        return _DEFAULT_COLLECTION_EXPR
    return _INT_EXPR


def strategy_exprs(fn: FunctionDef) -> list[str]:
    """Strategy source expressions for ``fn.params`` (registered or heuristic).

    Returns one expression string per parameter, in ``fn.params`` order. An
    empty list (no params) yields an empty ``given()`` — Hypothesis treats that
    as a single fixed call, which is exactly right for zero-arg functions.
    """
    if fn.name in _REGISTERED:
        registered = _REGISTERED[fn.name]
        if len(registered) != len(fn.params):
            raise ValueError(
                f"registered strategy for {fn.name!r} has {len(registered)} "
                f"expressions for {len(fn.params)} params"
            )
        return list(registered)
    return [_strategy_expr(p) for p in fn.params]


def is_fully_numeric(fn: FunctionDef) -> bool:
    """True when every param maps to a numeric/boolean strategy (Z3 seedable)."""
    return all(
        expr == _INT_EXPR or expr == _FLOAT_EXPR or expr == _BOOL_EXPR
        for expr in strategy_exprs(fn)
    )


__all__ = [
    "INT_MAX",
    "INT_MIN",
    "is_fully_numeric",
    "register_function_strategy",
    "strategy_exprs",
    "unregister_function_strategy",
]
