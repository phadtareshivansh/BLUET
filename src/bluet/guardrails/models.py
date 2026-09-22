"""Guardrail violation model.

A :class:`GuardrailViolation` represents a single item flagged by the
inline guardrails scanner (Prompt 2.4). It is deliberately a plain
dataclass so the enforcement layer (guardrails vs. real Enkrypt SDK)
can produce it without pulling in the SDK at import time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"


class Category(str, Enum):
    SECRET = "secret"
    INJECTION = "injection"
    XSS = "xss"


@dataclass(frozen=True)
class GuardrailViolation:
    """One item flagged by the guardrails scanner.

    ``severity == Severity.BLOCK`` means the job must halt
    (unless the user explicitly overrode via CLI). ``snippet`` holds
    the offending source fragment for the diagnostic.
    """

    severity: Severity
    category: Category
    pattern: str
    snippet: str
    line: int = 0

    def model_dump(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GuardrailReport:
    """Result of scanning a source or output."""

    violations: tuple[GuardrailViolation, ...] = ()

    @property
    def flagged(self) -> bool:
        return bool(self.violations)

    @property
    def blocked(self) -> bool:
        return any(v.severity == Severity.BLOCK for v in self.violations)


__all__ = ["Category", "GuardrailReport", "GuardrailViolation", "Severity"]
