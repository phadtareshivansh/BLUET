"""Sandboxed execution backend for side-by-side parity runs.

The Verification Agent (Prompt 2.3) defines the contract here and ships a
**local** implementation (:class:`LocalRunner`) that shells out to the host
Python via ``subprocess`` — good enough to prove the parity loop hermetically.
Prompt 4.1 replaces the runner with a real Docker/gVisor-backed implementation
(:mod:`bluet.sandbox.docker`) using the same interface, so the verifier never
changes when the isolation backend does.

The interface is intentionally small and I/O-agnostic:

- :meth:`SandboxRunner.run` takes a *mapping of filesystem paths to their
  contents* plus an argv, and returns an :class:`ExecutionResult`. The runner
  owns *how* those files appear in a sandbox (temp dir locally, a container
  image for Docker) and *where* the process runs.
- The caller (verifier harness) never touches host filesystem paths, so the
  same harness bytes run identically under either backend.

Phase 4.1 keeps this contract but runs legacy and proposed code in separate
containers (both bound against the same job workdir), which is an isolation
choice, not a different interface.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bluet.sandbox.runtime import RuntimeLimits


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one sandboxed run."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    runtime_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


class SandboxRunner(ABC):
    """Run an argv inside a sandbox containing ``files``."""

    @abstractmethod
    async def run(
        self,
        files: Mapping[str, str],
        argv: Sequence[str],
        *,
        limits: RuntimeLimits | None = None,
        timeout: float = 120.0,
    ) -> ExecutionResult:
        """Execute ``argv`` with ``files`` materialized into the sandbox.

        ``argv[0]`` is the program to run (e.g. ``sys.executable`` resolved by
        the runner, or a container entrypoint); remaining entries are args.
        ``timeout`` protects the whole run; on expiry the result is returned
        with ``timed_out=True`` rather than raised.
        """


class LocalRunner(SandboxRunner):
    """Hermetic local implementation: temp-dir checkout + host subprocess.

    Used by tests and local ``bluet run`` until Prompt 4.1 wires the real
    Docker backend. The isolation boundary here is a private temp dir, not a
    container — this is the *stub* the doc explicitly allows for 2.3.
    """

    async def run(
        self,
        files: Mapping[str, str],
        argv: Sequence[str],
        *,
        limits: RuntimeLimits | None = None,
        timeout: float = 120.0,
    ) -> ExecutionResult:
        loop = asyncio.get_running_loop()

        with tempfile.TemporaryDirectory(prefix="bluet-local-") as tmp:
            workdir = Path(tmp)
            for rel, content in files.items():
                target = workdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            # Resolve the interpreter used for the harness inside the sandbox.
            # sys.executable (the bluet venv) guarantees hypothesis/z3 are importable.
            argv = list(argv)
            if argv and argv[0] == "::python::":
                argv[0] = sys.executable
            elif argv and argv[0] == "::pytest::":
                # Run pytest with the generated plugin to emit BLUET_ markers.
                argv = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "bluet_pytest_plugin",
                    "-x",
                    "test_parity.py",
                ]
            elif argv and argv[0] == "::mvn::":
                # Run Maven with JUnit tests for Java parity.
                # Assumes a pom.xml is present in the workdir with jqwik dependency.
                argv = ["mvn", "test", "-Dtest=ParityTest", "-q"]

            proc = await asyncio.subprocess.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            start = loop.time()
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                try:
                    await proc.communicate()
                except (ProcessLookupError, OSError):
                    pass
                return ExecutionResult(
                    returncode=-1,
                    stdout="",
                    stderr=f"command timed out after {timeout}s",
                    timed_out=True,
                    runtime_seconds=loop.time() - start,
                )
            return ExecutionResult(
                returncode=proc.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                runtime_seconds=loop.time() - start,
            )


__all__ = [
    "ExecutionResult",
    "LocalRunner",
    "SandboxRunner",
]
