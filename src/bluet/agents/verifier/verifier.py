"""Verification Agent: differential parity between legacy and proposed code.

Owns the side-by-side execution contract the way the Refactor Agent owns
generation (Prompt 2.2): it runs legacy and proposed implementations against
the SAME Hypothesis-generated inputs inside the sandbox execution backend from
Phase 0 (:mod:`bluet.sandbox.execution`), compares outputs for exact parity,
and on any mismatch produces a structured :class:`CounterExample` whose
``function_name`` tells the self-heal loop exactly which function's logic is
wrong.

Per Prompt 2.3, scope is Python-only (Java 8 lands in Prompt 2.6). Functions
the analyzer flagged as non-deterministic (file/network/JDBC I/O) are excluded
from automated parity and surfaced on ``skipped_functions`` rather than being
fed synthetic inputs, which would be meaningless.

The sandbox runner is constructor-injectable so tests stay hermetic (local
subprocess) and Phase 4 slots the Docker/gVisor backend behind the same
interface with zero changes here.
"""

from __future__ import annotations

from collections.abc import Mapping

from bluet.agents.analyzer import get_logic_spec  # noqa: F401  (re-export for callers)
from bluet.agents.analyzer.models import FunctionDef, LogicSpec
from bluet.agents.refactor.models import ProposedCode
from bluet.agents.verifier.harness import build_harness_files, parse_harness_output
from bluet.agents.verifier.models import ParitySummary
from bluet.sandbox.execution import LocalRunner, SandboxRunner

_PYTHON_ALIASES = {"py", "python", "python2", "python3", "py2", "py3"}
_JAVA_ALIASES = {"java", "java8", "jdk8"}


def canonical_target_language(target_language: str, filename: str) -> str:
    """Resolve the target language ('' + suffix fallback) to ``python``/``java``."""
    raw = (target_language or "").strip().lower()
    if raw not in (_PYTHON_ALIASES | _JAVA_ALIASES):
        if filename.lower().endswith(".py"):
            return "python"
        if filename.lower().endswith(".java"):
            return "java"
    if raw in _JAVA_ALIASES:
        return "java"
    return "python"


class ParityVerifier:
    """Run differential parity for a file and return a :class:`ParitySummary`."""

    def __init__(
        self,
        *,
        runner: SandboxRunner | None = None,
        max_examples: int = 200,
        strategies: Mapping[str, list[str]] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._runner = runner if runner is not None else LocalRunner()
        self.max_examples = max_examples
        self.timeout = timeout
        if strategies:
            from bluet.agents.verifier.strategies import register_function_strategy

            for name, exprs in strategies.items():
                register_function_strategy(name, exprs)

    def _checkable(self, spec: LogicSpec) -> tuple[list[FunctionDef], list[str]]:
        checkable = [fn for fn in spec.functions if not fn.nondeterministic_calls]
        skipped = [fn.name for fn in spec.functions if fn.nondeterministic_calls]
        return checkable, skipped

    async def verify(
        self,
        spec: LogicSpec,
        legacy_source: str,
        proposed: ProposedCode,
        *,
        target_language: str,
        filename: str,
    ) -> ParitySummary:
        """Differentially verify ``proposed`` against ``legacy_source``.

        Raises nothing on parity failure — a failed check is a structured
        :class:`ParitySummary` with counter-examples, not an exception. A
        sandbox crash also resolves to a summary (``status="fail"`` with
        ``harness_error`` set) so the graph always has a verdict.
        """
        language = canonical_target_language(target_language, filename)
        if language != "python":
            return ParitySummary(
                status="skip",
                skipped_functions=[fn.name for fn in spec.functions],
            )

        checkable, skipped = self._checkable(spec)
        if not checkable:
            return ParitySummary(status="skip", skipped_functions=skipped)

        files = build_harness_files(
            legacy_source,
            proposed.code,
            checkable,
            max_examples=self.max_examples,
        )
        result = await self._runner.run(
            files,
            ["::pytest::", "test_parity.py"],
            timeout=self.timeout,
        )

        names = [fn.name for fn in checkable]
        passes, counter_examples = parse_harness_output(result.combined, names)
        planned = len(names)
        n_pass = len(passes)

        if result.timed_out:
            return ParitySummary(
                status="fail",
                score=0.0,
                n_checks=planned,
                skipped_functions=skipped,
                harness_error=f"verification sandbox timed out after {self.timeout}s",
            )

        if n_pass == 0 and not counter_examples and result.returncode != 0:
            # The harness never reached a verdict (e.g. the proposed file has a
            # syntax error and fails at import). Surface it honestly: a real
            # regression the pipeline must not paper over.
            mesg = (result.stderr or result.stdout or "unknown").strip()
            return ParitySummary(
                status="fail",
                score=0.0,
                n_checks=planned,
                skipped_functions=skipped,
                harness_error=f"parity harness crashed: {mesg[:2000]}",
            )

        n_fail = len(counter_examples)
        score = (planned - n_fail) / planned if planned else 0.0
        return ParitySummary(
            status="fail" if n_fail else "pass",
            score=round(score, 4),
            n_checks=planned,
            skipped_functions=skipped,
            counter_examples=counter_examples,
        )


__all__ = ["ParityVerifier", "canonical_target_language"]
