"""Unit + integration tests for the Verification Agent (Prompt 2.3).

Pure-Hypothesis differential parity is exercised through a real subprocess
(LocalRunner) for the end-to-end cases, plus an injected fake runner to pin
the sandbox contract (files/argv) without a process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluet.agents.analyzer import parser_for_source
from bluet.agents.analyzer.models import FunctionDef, LogicSpec, NondeterministicCall
from bluet.agents.refactor.models import ProposedCode
from bluet.agents.verifier import ParitySummary, ParityVerifier, canonical_target_language
from bluet.agents.verifier.strategies import (
    register_function_strategy,
    unregister_function_strategy,
)
from bluet.sandbox.execution import ExecutionResult, SandboxRunner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "python-legacy"

LEGACY_TOTAL = "def compute_total(quantity, unit_price):\n    return quantity * unit_price\n"


def _compute_total_spec() -> LogicSpec:
    return LogicSpec(
        functions=[
            FunctionDef(
                name="compute_total",
                line=5,
                params=["quantity", "unit_price"],
                returns=["total"],
                inputs=["quantity", "unit_price"],
                outputs=["total"],
                state_mutations=[],
                branches=[],
                nondeterministic_calls=[],
            )
        ]
    )


def _file_spec() -> LogicSpec:
    return LogicSpec(
        functions=[
            FunctionDef(
                name="load_prices",
                line=5,
                params=["path"],
                returns=["rows"],
                inputs=["path"],
                outputs=["rows"],
                state_mutations=[],
                branches=[],
                nondeterministic_calls=[NondeterministicCall(kind="file", name="open", line=6)],
            )
        ]
    )


def _loop_spec() -> LogicSpec:
    return LogicSpec(
        functions=[
            FunctionDef(
                name="apply_surcharge",
                line=5,
                params=["records", "premium"],
                returns=["running"],
                inputs=["records", "premium"],
                outputs=["running"],
                state_mutations=["running"],
                branches=[],
                nondeterministic_calls=[],
            )
        ]
    )


def _proposed(code: str) -> ProposedCode:
    return ProposedCode(file_path="dummy", code=code)


async def _verify(code: str, spec: LogicSpec | None = None, *, examples: int = 60) -> ParitySummary:
    if spec is None:
        spec = _compute_total_spec()
    verifier = ParityVerifier(max_examples=examples)
    return await verifier.verify(
        spec,
        LEGACY_TOTAL,
        _proposed(code),
        target_language="python",
        filename="simple_function.py",
    )


@pytest.mark.asyncio
async def test_canonical_target_language() -> None:
    assert canonical_target_language("", "legacy.py") == "python"
    assert canonical_target_language(None, "x.py") == "python"
    assert canonical_target_language("py3", "x.py") == "python"
    assert canonical_target_language("java", "Billing.java") == "java"
    assert canonical_target_language("java8", "Billing.java") == "java"
    assert canonical_target_language("", "Billing.java") == "java"
    assert canonical_target_language("bogus", "legacy.py") == "python"


@pytest.mark.asyncio
async def test_identical_proposed_passes() -> None:
    summary = await _verify(LEGACY_TOTAL)
    assert summary.status == "pass"
    assert summary.n_checks == 1
    assert summary.score == 1.0
    assert summary.counter_examples == []


@pytest.mark.asyncio
async def test_buggy_proposed_fails_with_counter_example() -> None:
    summary = await _verify(
        "def compute_total(quantity, unit_price):\n    return quantity + unit_price\n"
    )
    assert summary.status == "fail"
    assert summary.score == 0.0
    assert len(summary.counter_examples) == 1
    cx = summary.counter_examples[0]
    assert cx.function_name == "compute_total"
    assert len(cx.inputs) == 2
    assert cx.expected_output != cx.actual_output
    assert cx.actual_output is not None
    assert cx.observed_output == cx.actual_output
    assert "mismatch" in (cx.diff_summary or "")


@pytest.mark.asyncio
async def test_proposed_without_function_is_failure() -> None:
    summary = await _verify("# regenerated file with no functions yet\n")
    assert summary.status == "fail"
    assert summary.score == 0.0
    assert len(summary.counter_examples) == 1
    cx = summary.counter_examples[0]
    assert cx.function_name == "compute_total"
    assert "does not define function" in (cx.diff_summary or "")


@pytest.mark.asyncio
async def test_proposed_syntax_error_is_harness_crash() -> None:
    summary = await _verify("def compute_total(\n")
    assert summary.status == "fail"
    assert summary.score == 0.0
    assert summary.counter_examples == []
    assert summary.harness_error, "crash must be surfaced as a harness_error"


@pytest.mark.asyncio
async def test_nondeterministic_function_is_skipped() -> None:
    source = (FIXTURES / "file_io.py").read_text(encoding="utf-8")
    parser = parser_for_source("python", "file_io.py")
    assert parser is not None
    spec = parser.parse(source)
    assert spec.functions[0].name == "load_prices"
    assert spec.functions[0].nondeterministic_calls, "analyzer must flag open()"

    verifier = ParityVerifier(max_examples=20)
    summary = await verifier.verify(
        spec,
        source,
        _proposed(source),
        target_language="python",
        filename="file_io.py",
    )
    assert summary.status == "skip"
    assert summary.skipped_functions == ["load_prices"]
    assert summary.score is None


@pytest.mark.asyncio
async def test_nondeterministic_function_skipped_via_direct_spec() -> None:
    verifier = ParityVerifier(max_examples=20)
    summary = await verifier.verify(
        _file_spec(),
        "def load_prices(path):\n    return []\n",
        _proposed("def load_prices(path):\n    return []\n"),
        target_language="python",
        filename="file_io.py",
    )
    assert summary.status == "skip"
    assert summary.skipped_functions == ["load_prices"]


@pytest.mark.asyncio
async def test_loop_branch_function_passes_with_registered_strategies(
    tmp_path: Path,
) -> None:
    source = (FIXTURES / "loop_branch.py").read_text(encoding="utf-8")
    register_function_strategy(
        "apply_surcharge",
        [
            (
                "st.lists(st.fixed_dictionaries({"
                "'premium': st.booleans(), "
                "'surcharge': st.integers(min_value=0, max_value=200), "
                "'base': st.integers(min_value=0, max_value=200)}), max_size=10)"
            ),
            "st.booleans()",
        ],
    )
    try:
        verifier = ParityVerifier(max_examples=100)
        summary = await verifier.verify(
            _loop_spec(),
            source,
            _proposed(source),
            target_language="python",
            filename="loop_branch.py",
        )
    finally:
        unregister_function_strategy("apply_surcharge")
    assert summary.status == "pass"
    assert summary.n_checks == 1
    assert summary.score == 1.0


@pytest.mark.asyncio
async def test_loop_branch_regression_is_detected() -> None:
    source = (FIXTURES / "loop_branch.py").read_text(encoding="utf-8")
    buggy = source.replace("return running", "return running + 5")
    register_function_strategy(
        "apply_surcharge",
        [
            (
                "st.lists(st.fixed_dictionaries({"
                "'premium': st.booleans(), "
                "'surcharge': st.integers(min_value=0, max_value=200), "
                "'base': st.integers(min_value=0, max_value=200)}), max_size=10)"
            ),
            "st.booleans()",
        ],
    )
    try:
        verifier = ParityVerifier(max_examples=100)
        summary = await verifier.verify(
            _loop_spec(),
            source,
            _proposed(buggy),
            target_language="python",
            filename="loop_branch.py",
        )
    finally:
        unregister_function_strategy("apply_surcharge")
    assert summary.status == "fail"
    assert len(summary.counter_examples) == 1
    assert summary.counter_examples[0].function_name == "apply_surcharge"


@pytest.mark.asyncio
async def test_java_verification_generates_maven_harness() -> None:
    runner = _FakeRunner(
        ExecutionResult(returncode=0, stdout="BLUET_PASS testAddCharge\n", stderr="")
    )
    verifier = ParityVerifier(runner=runner, max_examples=20)
    summary = await verifier.verify(
        _compute_total_spec(),
        LEGACY_TOTAL,
        _proposed(LEGACY_TOTAL),
        target_language="java",
        filename="Billing.java",
    )
    assert summary.status == "pass"
    assert set(runner.files or {}) == {
        "Legacy.java",
        "Proposed.java",
        "ParityTest.java",
        "pom.xml",
    }
    assert runner.argv == ["::mvn::", "test", "-Dtest=ParityTest"]
    assert "public class Legacy" in (runner.files or {})["Legacy.java"]
    assert "public class Proposed" in (runner.files or {})["Proposed.java"]
    assert "ParityTest" in (runner.files or {})["ParityTest.java"]
    assert "jqwik" in (runner.files or {})["pom.xml"]


class _FakeRunner(SandboxRunner):
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.files: dict[str, str] | None = None
        self.argv: list[str] | None = None

    async def run(self, files, argv, *, limits=None, timeout: float = 120.0) -> ExecutionResult:
        self.files = dict(files)
        self.argv = list(argv)
        return self.result


@pytest.mark.asyncio
async def test_runner_contract_files_and_argv() -> None:
    runner = _FakeRunner(
        ExecutionResult(returncode=0, stdout="BLUET_PASS test_compute_total\n", stderr="")
    )
    verifier = ParityVerifier(runner=runner, max_examples=20)
    summary = await verifier.verify(
        _compute_total_spec(),
        LEGACY_TOTAL,
        _proposed(LEGACY_TOTAL),
        target_language="python",
        filename="simple_function.py",
    )
    assert summary.status == "pass"
    assert set(runner.files or {}) == {
        "legacy.py",
        "proposed.py",
        "test_parity.py",
        "bluet_pytest_plugin.py",
    }
    assert runner.argv == ["::pytest::", "test_parity.py"]
    assert (runner.files or {})["legacy.py"] == LEGACY_TOTAL
    assert (runner.files or {})["proposed.py"] == LEGACY_TOTAL


@pytest.mark.asyncio
async def test_runner_timeout_is_a_failure_not_pass() -> None:
    runner = _FakeRunner(
        ExecutionResult(returncode=-1, stdout="", stderr="timed out", timed_out=True)
    )
    verifier = ParityVerifier(runner=runner, max_examples=20)
    summary = await verifier.verify(
        _compute_total_spec(),
        LEGACY_TOTAL,
        _proposed(LEGACY_TOTAL),
        target_language="python",
        filename="simple_function.py",
    )
    assert summary.status == "fail"
    assert summary.score == 0.0
    assert "timed out" in (summary.harness_error or "")
