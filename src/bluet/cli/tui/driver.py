"""Demo driver for the BIOS dashboard: real EventBus traffic, real analyzer.

The dashboard is honest about its plumbing: the left-hand log is fed through a
real :class:`~bluet.orchestrator.events.EventBus` (actual ZMQ PUB/SUB over an
in-process InProc endpoint), with the app subscribed as a regular bus consumer.
The driver simulates the *pipeline*, not the bus — target files are really
parsed by the analyzer (real ``LogicSpec`` function counts / non-deterministic
flags) while the downstream LLM/sandbox stages are scripted but shaped exactly
like the orchestrator's own payloads. Everything published here flows through
the same ``task.*`` / ``feedback.*`` topics a ``bluet run`` job uses, so the
dashboard and a real job render identically.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import queue
import random
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bluet.orchestrator.events import TOPICS, EventBus
from bluet.state.db import init_schema
from bluet.state.repository import create_job

MAX_RETRIES = 3

#: Synthetic fallback files padded in so a sweep always exercises every state
#: (clean / self-heal / guardrail halt / retries-exhausted).
_SYNTHETIC = [
    "legacy/billing.py",
    "legacy/interest.py",
    "legacy/auth_handshake.py",
    "legacy/export_csv.py",
]

_TRAITS = ("clean", "self-heal", "guardrail", "exhaust")


def _package_sources(limit: int = 4) -> list[Path]:
    """Real ``.py`` modules from the installed bluet package (bluet's own code)."""
    spec = importlib.util.find_spec("bluet")
    if not spec or not spec.submodule_search_locations:
        return []
    root = Path(next(iter(spec.submodule_search_locations)))
    candidates = [p for p in root.rglob("*.py") if "__init__" not in p.name]
    candidates.sort(key=lambda p: (len(str(p)), str(p)))
    return candidates[:limit]


def _target_sources(limit: int = 4) -> list[Path]:
    """Real source files under ``BLUET_TUI_TARGET`` / cwd, else bluet itself."""
    ignored = {".venv", ".git", "__pycache__", ".pytesttmp", "node_modules", ".mypy_cache"}
    explicit = os.environ.get("BLUET_TUI_TARGET")
    target = Path(explicit).resolve() if explicit else Path.cwd()
    if target.is_file() and target.suffix == ".py":
        return [target]
    files = sorted(
        p
        for p in target.rglob("*.py")
        if p.is_file() and not any(seg in ignored for seg in p.parts)
    )
    if not files:
        return _package_sources(limit)
    return files[:limit]


def build_files() -> list[str]:
    """Up to 4 display names; real files are parsed, synthetic ones pad the set."""
    cwd = Path.cwd()
    names: list[str] = []
    for p in _target_sources():
        try:
            rel = p.relative_to(cwd)
        except ValueError:
            rel = Path("src") / p.name
        if rel not in names:
            names.append(str(rel))
    padded = [n for n in _SYNTHETIC if n not in names]
    return (names + padded)[:4]


def _seed_for(seed: Any) -> random.Random:
    return random.Random(str(seed) + str(abs(hash(str(seed)))))


def _find_source(name: str) -> Path | None:
    """Resolve ``name`` to a real file on disk (cwd first, then the package)."""
    cwd = Path.cwd()
    path = cwd / name
    if path.is_file():
        return path
    spec = importlib.util.find_spec("bluet")
    if spec and spec.submodule_search_locations:
        base = Path(next(iter(spec.submodule_search_locations)))
        candidate = base / (name.replace("legacy/", "").replace(".py", "") + ".py")
        if candidate.is_file():
            return candidate
    return None


_PARSE_TIMEOUT = 90.0


def _seeded_counts(name: str) -> dict[str, Any]:
    """Deterministic synthetic counts used until the real parse lands."""
    rng = _seed_for(name)
    n = rng.randint(6, 14)
    flagged = min(rng.randint(0, 1), n)
    names = [f"function_{i:02d}" for i in range(n)]
    return {
        "n_functions": n,
        "nondeterministic": [names[i] for i in range(1, flagged + 1)],
        "names": names,
    }


class DemoDriver:
    """Owns the in-process EventBus and the traffic flowing through it."""

    def __init__(
        self,
        files: Sequence[str],
        backend: str,
        *,
        halt_hook: Callable[[], Awaitable[None]],
        handler: Callable[[dict[str, Any]], Awaitable[None] | None],
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.files = list(files)
        self.backend = backend
        self.max_retries = max_retries
        self._halt_hook = halt_hook
        self._handler = handler
        self._engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self.bus: EventBus | None = None
        self.job_id: int | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        # Parser worker state (real analysis runs out-of-process so a native
        # parser crash can never take down the dashboard).
        self._known: dict[str, dict[str, Any]] = {}
        self._requested: set[str] = set()
        self._pending: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_proc: subprocess.Popen[Any] | None = None
        self._req_q: queue.Queue[tuple[str, str] | None] | None = None
        self._parser_ok = True

    @property
    def session_factory(self) -> Callable[[], AsyncSession]:
        return self._factory

    async def start(self) -> None:
        """Init the in-memory mirror schema and open the bus with every topic."""
        await init_schema(self._engine)
        self.bus = EventBus(self.session_factory)
        await self.bus.start()
        for topic in TOPICS:
            await self.bus.subscribe(topic, self._handler)
        self._spawn_worker()

    async def stop(self) -> None:
        for task in (self._run_task, self._idle_task):
            if task and not task.done():
                task.cancel()
        pending = [t for t in (self._run_task, self._idle_task) if t and not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._shutdown_worker()
        if self.bus is not None:
            await self.bus.stop()
        await self._engine.dispose()

    # -- real-analysis worker ----------------------------------------------

    def _spawn_worker(self) -> None:
        """Start the persistent parser subprocess, wedged in a daemon thread."""
        if self._worker_thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._req_q = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_io, name="bluet-tui-parser", daemon=True
        )
        self._worker_thread.start()

    def _settle_all_pending(self) -> None:
        self._parser_ok = False
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()

    def _settle_parse(self, name: str, result: dict[str, Any] | None) -> None:
        fut = self._pending.pop(name, None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _worker_io(self) -> None:
        """Drive the persistent worker: request line in, result line out."""
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "bluet.cli.tui._parse_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._settle_all_pending)
            return
        self._worker_proc = proc
        try:
            line = proc.stdout.readline()
        except OSError:
            line = ""
        if not line:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._settle_all_pending)
            return
        loop = self._loop
        req_q = self._req_q
        while req_q is not None and loop is not None:
            req = req_q.get()
            if req is None:
                break
            name, path = req
            try:
                proc.stdin.write(path + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    break
                data = json.loads(line.strip())
                result = data if data.get("ok") else None
                loop.call_soon_threadsafe(self._settle_parse, name, result)
            except (OSError, ValueError, json.JSONDecodeError):
                break
        try:
            proc.kill()
        except OSError:
            pass
        if loop is not None:
            loop.call_soon_threadsafe(self._settle_all_pending)

    def _shutdown_worker(self) -> None:
        if self._req_q is not None:
            self._req_q.put(None)
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        if self._worker_proc is not None and self._worker_proc.poll() is None:
            try:
                self._worker_proc.kill()
            except OSError:
                pass
        self._worker_thread = None
        self._req_q = None
        self._worker_proc = None

    async def _request_parse(self, name: str, path: str) -> dict[str, Any] | None:
        if not self._parser_ok or self._req_q is None:
            return None
        fut: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._pending[name] = fut
        self._req_q.put((name, path))
        try:
            return await asyncio.wait_for(fut, timeout=_PARSE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            self._pending.pop(name, None)
            return None

    async def _resolve(self, name: str, path: str) -> None:
        real = await self._request_parse(name, path)
        self._requested.discard(name)
        if not real:
            self._known.setdefault(name, {})["precise"] = False
            return
        self._known[name] = {**real, "precise": True}
        if self.job_id is not None:
            await self._publish(
                "task.analysis",
                {
                    "current_file": name,
                    "stage": "analyzed",
                    "precise": True,
                    "n_functions": real["n_functions"],
                    "nondeterministic": real["nondeterministic"],
                },
                delay=0.15,
            )

    async def _ensure_analysis(self, name: str) -> dict[str, Any]:
        """Seeded counts immediately; real ``LogicSpec`` data as a background refine."""
        cached = self._known.get(name)
        if cached is not None and cached.get("precise"):
            return cached
        if cached is None:
            cached = _seeded_counts(name)
            cached["precise"] = False
            self._known[name] = cached
        path = _find_source(name)
        if path is not None and name not in self._requested and self._parser_ok:
            self._requested.add(name)
            asyncio.create_task(self._resolve(name, str(path)))
        return dict(cached)

    async def _publish(
        self, topic: str, payload: dict[str, Any], delay: float = 0.45
    ) -> None:
        if self.bus is None:
            return
        await asyncio.sleep(delay)
        payload["job_id"] = payload.get("job_id", self.job_id)
        await self.bus.publish(topic, payload)

    async def _next_job(self) -> int:
        async with self.session_factory() as session:
            job = await create_job(session, ".", status="RUNNING", backend=self.backend)
        return job.id

    async def run(self) -> None:
        """Never-ending sweeps of the pipeline until cancelled."""
        try:
            sweep = 0
            while True:
                self.job_id = await self._next_job()
                for idx, name in enumerate(self.files):
                    trait = _TRAITS[(idx + sweep) % len(_TRAITS)]
                    await self._one_file(self.job_id, name, trait)
                sweep += 1
                await asyncio.sleep(7.0)
        except asyncio.CancelledError:
            pass

    async def _one_file(self, job_id: int, name: str, trait: str) -> None:
        base = {"job_id": job_id, "current_file": name}
        info = await self._ensure_analysis(name)
        flags = info["nondeterministic"]
        names = info["names"]
        rng = _seed_for(name)

        await self._publish(
            "task.analysis",
            {**base, "stage": "start", "target_language": "python"},
            delay=0.25,
        )
        await self._publish(
            "task.analysis",
            {
                **base,
                "stage": "analyzed",
                "n_functions": info["n_functions"],
                "nondeterministic": flags,
            },
            delay=0.35,
        )

        if trait == "guardrail":
            violation = {
                "severity": "block",
                "category": "injection",
                "pattern": "eval(",
                "line": rng.randint(3, 40),
                "snippet": "eval(user_input)",
            }
            await self._publish(
                "feedback.warning",
                {
                    **base,
                    "stage": "refactor",
                    "source": "guardrail",
                    "severity": "block",
                    "reason": "injection: eval( on line",
                    "guardrail_violation": violation,
                },
                delay=0.3,
            )
            await self._halt_hook()
            await self._publish(
                "feedback.warning",
                {
                    **base,
                    "stage": "refactor",
                    "source": "guardrail",
                    "severity": "override",
                    "reason": "override accepted, resume",
                },
                delay=0.2,
            )

        await self._publish(
            "task.refactor",
            {**base, "stage": "start", "target_language": "python", "target": "go"},
            delay=0.25,
        )
        await self._publish(
            "task.refactor",
            {
                **base,
                "stage": "refactored",
                "guardrail": "clean",
                "schema_valid": True,
                "imports_added": ["os"],
                "proposed_lines": rng.randint(80, 260),
            },
            delay=0.45,
        )

        await self._publish(
            "task.verify",
            {**base, "stage": "start", "sandbox": self.backend},
            delay=0.3,
        )

        if trait == "self-heal":
            fn = names[1] if len(names) > 1 else (names[0] if names else "calculate_interest")
            cx = {
                "function_name": fn,
                "inputs": {"principal": 0, "rate": -0.01},
                "expected_output": None,
                "actual_output": "ZeroDivisionError",
                "diff_summary": "rate <= 0 divides by zero in legacy only",
            }
            await self._publish(
                "task.verify",
                {
                    **base,
                    "stage": "verified",
                    "status": "fail",
                    "score": round(1 - 1 / max(info["n_functions"], 1), 3),
                    "n_checks": max(info["n_functions"], 1),
                    "n_failures": 1,
                    "counter_examples": [cx],
                    "sandbox": self.backend,
                },
                delay=0.5,
            )
            await self._publish(
                "feedback.regression",
                {
                    **base,
                    "stage": "self_heal",
                    "retry_count": 1,
                    "max_retries": self.max_retries,
                },
                delay=0.35,
            )

        if trait == "exhaust":
            planned = max(info["n_functions"] - len(flags), 1)
            for attempt in (1, 2):
                fn = names[0] if names else "function"
                cx = {
                    "function_name": fn,
                    "inputs": {"probe": [0, 1, 2]},
                    "expected_output": 3,
                    "actual_output": 1,
                    "diff_summary": "ordering bug in legacy",
                }
                await self._publish(
                    "task.verify",
                    {
                        **base,
                        "stage": "verified",
                        "status": "fail",
                        "score": round(1 - 1 / planned, 3),
                        "n_checks": planned,
                        "n_failures": 1,
                        "counter_examples": [cx],
                        "sandbox": self.backend,
                    },
                    delay=0.5,
                )
                await self._publish(
                    "feedback.regression",
                    {
                        **base,
                        "stage": "self_heal",
                        "retry_count": attempt,
                        "max_retries": self.max_retries,
                    },
                    delay=0.3,
                )

        planned = max(info["n_functions"] - len(flags), 1)
        score = 0.0 if trait == "exhaust" else 1.0
        status = "fail" if trait == "exhaust" else "pass"
        final = {
            **base,
            "stage": "verified",
            "status": status,
            "score": score,
            "n_checks": planned,
            "skipped_functions": list(flags),
            "sandbox": self.backend,
        }
        if trait == "exhaust":
            final["reason"] = "retries exhausted"
            final["n_failures"] = planned
        await self._publish("task.verify", final, delay=0.5)

    async def idle(self) -> None:
        """Periodic ambient traffic so the log keeps scrolling between sweeps."""
        try:
            while True:
                await asyncio.sleep(random.uniform(6.0, 10.0))
                if self.bus is not None and self.job_id is not None:
                    await self._publish(
                        "feedback.warning",
                        {
                            "job_id": self.job_id,
                            "current_file": "-",
                            "stage": "context_index",
                            "source": "context_index",
                            "reason": "latency_budget",
                            "latency_ms": random.randint(120, 260),
                        },
                        delay=0.0,
                    )
        except asyncio.CancelledError:
            pass

    async def drive(self) -> None:
        """Start the sweep + idle tasks; call once after :meth:`start`."""
        self._run_task = asyncio.create_task(self.run())
        self._idle_task = asyncio.create_task(self.idle())


__all__ = ["MAX_RETRIES", "DemoDriver", "build_files"]