"""Enkrypt inline guardrails proxy (Prompt 2.4).

Provides a rule-based scanner that inspects:

- Inputs to the Refactor Agent (legacy source): hardcoded secrets and
  sensitive data (API keys, passwords, tokens, private keys, AWS access
  keys, connection strings).
- Outputs from the Refactor Agent (proposed code): OWASP-flagged
  patterns (``eval``/``exec``, ``os.system``, shell-injection-prone
  ``subprocess`` calls, XSS via ``<script>`` or ``innerHTML``).

This is an inline, rule-based implementation of the Enkrypt guardrails
contract. A real Enkrypt SDK would plug in through :class:`EnkryptGuardrail`
(implementing the same ``scan_*`` interface) without changing the
orchestrator layer. The rule-based fallback is what the offline suite
and the prompt's "inline proxy" spec require.
"""

from __future__ import annotations

import re
from re import Pattern

from bluet.guardrails.models import Category, GuardrailReport, GuardrailViolation, Severity

_SECRET_PATTERNS: list[tuple[Pattern[str], str, Category]] = [
    (
        re.compile(r"""(?i)(api[_-]?key|apikey)\s*[:=]\s*["']?\s*[A-Za-z0-9_\-]{16,}"""),
        "secret",
        Category.SECRET,
    ),
    (
        re.compile(r"""(?i)(password|passwd|pwd)\s*[:=]\s*["']?\s*\S{4,}"""),
        "secret",
        Category.SECRET,
    ),
    (re.compile(r"""(?i)token\s*[:=]\s*["']?\s*[A-Za-z0-9_\-]{16,}"""), "secret", Category.SECRET),
    (re.compile(r"""AKIA[0-9A-Z]{16}"""), "secret", Category.SECRET),
    (
        re.compile(r"""-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"""),
        "secret",
        Category.SECRET,
    ),
    (re.compile(r"""(?i)mongodb(\+srv)?://\S+:\S+@"""), "secret", Category.SECRET),
    (re.compile(r"""(?i)postgres(ql)?://\S+:\S+@"""), "secret", Category.SECRET),
]

_INJECTION_PATTERNS: list[tuple[Pattern[str], str, Category]] = [
    (re.compile(r"""eval\s*\(""", re.IGNORECASE), "eval injection", Category.INJECTION),
    (re.compile(r"""exec\s*\(""", re.IGNORECASE), "exec injection", Category.INJECTION),
    (re.compile(r"""os\.system\s*\(""", re.IGNORECASE), "os.system", Category.INJECTION),
    (
        re.compile(r"""subprocess\.(call|run|Popen)\s*\(.*shell\s*=\s*True""", re.IGNORECASE),
        "shell=True",
        Category.INJECTION,
    ),
    (re.compile(r"""__import__\s*\(""", re.IGNORECASE), "dynamic import", Category.INJECTION),
    (
        re.compile(r"""pickle\.loads?\s*\(""", re.IGNORECASE),
        "pickle deserialization",
        Category.INJECTION,
    ),
    (re.compile(r"""yaml\.load\s*\(""", re.IGNORECASE), "yaml unsafe load", Category.INJECTION),
]

_XSS_PATTERNS: list[tuple[Pattern[str], str, Category]] = [
    (re.compile(r"""<script[^>]*>""", re.IGNORECASE), "script tag", Category.XSS),
    (re.compile(r"""innerHTML\s*=""", re.IGNORECASE), "innerHTML assignment", Category.XSS),
    (re.compile(r"""document\.write\s*\(""", re.IGNORECASE), "document.write", Category.XSS),
]

_SECRET_SEVERITY = Severity.BLOCK
_INJECTION_SEVERITY = Severity.BLOCK
_XSS_SEVERITY = Severity.BLOCK


def _scan_lines(
    source: str,
    rules: list[tuple[Pattern[str], str, Category]],
    severity: Severity,
) -> list[GuardrailViolation]:
    violations: list[GuardrailViolation] = []
    for i, line in enumerate(source.splitlines(), start=1):
        for pat, label, category in rules:
            m = pat.search(line)
            if m:
                snippet = m.group(0)[:120]
                violations.append(
                    GuardrailViolation(
                        severity=severity,
                        category=category,
                        pattern=label,
                        snippet=snippet,
                        line=i,
                    )
                )
    return violations


class EnkryptGuardrail:
    """Inline guardrails proxy: scan legacy inputs and proposed outputs.

    Methods are constructor-injectable so tests can stub a single
    ``scan_*`` call. The ``scan_legacy_input`` / ``scan_proposed_output``
    contracts are what the orchestrator's ``refactor_node`` calls.
    """

    def scan_legacy_input(self, source: str) -> GuardrailReport:
        """Scan legacy source for hardcoded secrets before it reaches the LLM."""
        violations = _scan_lines(source, _SECRET_PATTERNS, _SECRET_SEVERITY)
        return GuardrailReport(tuple(violations))

    def scan_proposed_output(self, code: str) -> GuardrailReport:
        """Scan generated code for OWASP-flagged patterns before it is written."""
        violations: list[GuardrailViolation] = []
        violations.extend(_scan_lines(code, _INJECTION_PATTERNS, _INJECTION_SEVERITY))
        violations.extend(_scan_lines(code, _XSS_PATTERNS, _XSS_SEVERITY))
        return GuardrailReport(tuple(violations))


__all__ = ["Category", "EnkryptGuardrail", "GuardrailReport", "GuardrailViolation", "Severity"]
