"""Analyzer agent: parses one source file into a per-function LogicSpec."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bluet.agents.analyzer.base import (
    LanguageParser,
    get_parser,
    parser_for_source,
    register,
)
from bluet.agents.analyzer.java_parser import JavaParser
from bluet.agents.analyzer.models import (
    BranchSpec,
    FunctionDef,
    LogicSpec,
    NondeterministicCall,
)
from bluet.agents.analyzer.python_parser import PythonParser

__all__ = [
    "BranchSpec",
    "FunctionDef",
    "JavaParser",
    "LanguageParser",
    "LogicSpec",
    "NondeterministicCall",
    "PythonParser",
    "get_logic_spec",
    "get_parser",
    "parser_for_source",
    "register",
]


def get_logic_spec(state: Mapping[str, Any]) -> LogicSpec | None:
    """Reconstruct the :class:`LogicSpec` stored in a pipeline state dict.

    ``BluetState.logic_spec`` holds a plain dict (Pydantic objects are not
    checkpoint-safe as-is), so this is the single accessor that recovers the
    real object for downstream nodes (e.g. refactor_node, verify_node).
    """
    raw = state.get("logic_spec")
    if raw is None:
        return None
    return LogicSpec.model_validate(raw)