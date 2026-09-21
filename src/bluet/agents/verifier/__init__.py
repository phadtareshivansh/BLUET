"""Verification Agent: differential parity for legacy-vs-proposed code."""

from bluet.agents.refactor.models import CounterExample
from bluet.agents.verifier.harness import build_harness_files, parse_harness_output
from bluet.agents.verifier.models import ParityStatus, ParitySummary
from bluet.agents.verifier.strategies import (
    register_function_strategy,
    strategy_exprs,
    unregister_function_strategy,
)
from bluet.agents.verifier.verifier import ParityVerifier, canonical_target_language
from bluet.agents.verifier.z3_pass import check_branch_coverage, synthesize_boundary_seeds

__all__ = [
    "CounterExample",
    "ParityStatus",
    "ParitySummary",
    "ParityVerifier",
    "build_harness_files",
    "canonical_target_language",
    "check_branch_coverage",
    "parse_harness_output",
    "register_function_strategy",
    "strategy_exprs",
    "synthesize_boundary_seeds",
    "unregister_function_strategy",
]
