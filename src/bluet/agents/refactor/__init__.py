"""Refactor Agent — ``LogicSpec`` to formatted ``ProposedCode``.

Public surface, mirroring the analyzer package: re-export the payload model
(:class:`ProposedCode`), the self-heal counter-example seed
(:class:`CounterExample`), the formatter entry point (:func:`format_code`), the
agent itself (:class:`RefactorAgent`), and the failure type (:class:`RefactorError`).
"""

from __future__ import annotations

from bluet.agents.refactor.agent import RefactorAgent, normalize_language
from bluet.agents.refactor.errors import RefactorError
from bluet.agents.refactor.formatters import FORMATTERS, format_code
from bluet.agents.refactor.models import CounterExample, ProposedCode

__all__ = [
    "FORMATTERS",
    "CounterExample",
    "ProposedCode",
    "RefactorAgent",
    "RefactorError",
    "format_code",
    "normalize_language",
]
