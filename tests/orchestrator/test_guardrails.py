"""Tests for Enkrypt guardrails (Prompt 2.4)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bluet.agents.refactor import ProposedCode
from bluet.guardrails import EnkryptGuardrail
from bluet.guardrails.models import Category, GuardrailReport, Severity
from bluet.orchestrator import (
    EventBus,
    build_refactor_graph,
    open_sqlite_checkpointer,
    run_config,
)
from bluet.state.db import init_schema
from bluet.state.repository import create_job, list_events_for_job


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    await init_schema(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def job(session_factory: async_sessionmaker):
    async with session_factory() as sess:
        return await create_job(sess, "/repo/test", backend="gvisor")


def _fixture_state(job_id: int) -> dict:
    return {
        "job_id": job_id,
        "repo_path": "/repo/test",
        "target_language": "python",
        "current_file": "test.py",
        "retry_count": 0,
    }


class _StubRefactorAgent:
    """Deterministic stand-in for RefactorAgent."""

    def __init__(
        self, proposed: ProposedCode | None = None, *, exc: Exception | None = None
    ) -> None:
        self.proposed = proposed
        self.exc = exc

    async def refactor(self, spec, *, target_language, current_file, counter_examples=None):
        if self.exc is not None:
            raise self.exc
        return self.proposed


class _PermissiveGuardrail(EnkryptGuardrail):
    """Guardrail that downgrades BLOCK to WARN (simulates --guardrail-ok)."""

    def scan_legacy_input(self, source: str) -> GuardrailReport:
        report = super().scan_legacy_input(source)
        if report.blocked:
            return GuardrailReport(
                tuple(v for v in report.violations if v.severity == Severity.WARN)
            )
        return report

    def scan_proposed_output(self, code: str) -> GuardrailReport:
        report = super().scan_proposed_output(code)
        if report.blocked:
            return GuardrailReport(
                tuple(v for v in report.violations if v.severity == Severity.WARN)
            )
        return report


class _BlockingGuardrail(EnkryptGuardrail):
    """Guardrail that always blocks on violations."""


def test_guardrail_scanner_secrets_in_legacy_input():
    """Secrets in legacy source should be flagged and block."""
    guardrail = EnkryptGuardrail()
    source = 'api_key = "sk-1234567890abcdef"\npassword = "secret123"\n'
    report = guardrail.scan_legacy_input(source)
    assert report.blocked
    assert len(report.violations) >= 2
    cats = {v.category for v in report.violations}
    assert Category.SECRET in cats


def test_guardrail_scanner_no_secrets_passes():
    """Clean legacy source should pass."""
    guardrail = EnkryptGuardrail()
    source = "def foo():\n    return 42\n"
    report = guardrail.scan_legacy_input(source)
    assert not report.blocked
    assert report.violations == ()


def test_guardrail_scanner_owasp_patterns_in_proposed():
    """OWASP patterns in proposed code should be flagged and block."""
    guardrail = EnkryptGuardrail()
    code = 'eval("__import__(\\"os\\").system(\\"rm -rf /\\")")\n'
    report = guardrail.scan_proposed_output(code)
    assert report.blocked
    cats = {v.category for v in report.violations}
    assert Category.INJECTION in cats


def test_guardrail_scanner_xss_patterns_in_proposed():
    """XSS patterns in proposed code should be flagged."""
    guardrail = EnkryptGuardrail()
    code = 'innerHTML = "<script>alert(1)</script>"\n'
    report = guardrail.scan_proposed_output(code)
    assert report.blocked
    cats = {v.category for v in report.violations}
    assert Category.XSS in cats


def test_guardrail_scanner_aws_key():
    """AWS access key pattern should be flagged."""
    guardrail = EnkryptGuardrail()
    source = "AKIAIOSFODNN7EXAMPLE\n"
    report = guardrail.scan_legacy_input(source)
    assert report.blocked
    assert any(v.pattern == "secret" for v in report.violations)


@pytest.mark.asyncio
async def test_guardrail_halt_on_secret_in_legacy(
    session_factory: async_sessionmaker,
    job,
    tmp_path,
):
    """Guardrail should halt the job when secret found in legacy source."""
    # Create a temp file with a secret
    repo = tmp_path / "repo"
    repo.mkdir()
    source_file = repo / "test.py"
    source_file.write_text('api_key = "sk-live-1234567890"\ndef foo():\n    return 1\n')

    fake = _StubRefactorAgent(ProposedCode(file_path="test.py", code="def foo():\n    return 2\n"))
    state = _fixture_state(job.id)
    state.update(repo_path=str(repo), current_file="test.py")

    async with (
        EventBus(session_factory) as bus,
        open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver,
    ):
        graph = build_refactor_graph(
            bus,
            checkpointer=saver,
            verify_node=None,
            refactor_agent=fake,
            guardrail=_BlockingGuardrail(),
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is None
    assert final["guardrail_halt"] is True
    async with session_factory() as sess:
        events = await list_events_for_job(sess, job.id)
    warns = [e for e in events if e.event_type == "feedback.warning"]
    assert warns
    assert any("guardrail" in json.loads(w.payload_json).get("source", "") for w in warns)


@pytest.mark.asyncio
async def test_guardrail_halt_on_owasp_in_proposed(
    session_factory: async_sessionmaker,
    job,
    tmp_path,
):
    """Guardrail should halt when proposed code has OWASP pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source_file = repo / "test.py"
    source_file.write_text("def foo():\n    return 1\n")

    # Proposed code with eval injection
    fake = _StubRefactorAgent(
        ProposedCode(file_path="test.py", code='eval("__import__(\\"os\\").system(\\"id\\")")\n')
    )
    state = _fixture_state(job.id)
    state.update(repo_path=str(repo), current_file="test.py")

    async with (
        EventBus(session_factory) as bus,
        open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver,
    ):
        graph = build_refactor_graph(
            bus,
            checkpointer=saver,
            verify_node=None,
            refactor_agent=fake,
            guardrail=_BlockingGuardrail(),
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is None
    assert final["guardrail_halt"] is True


@pytest.mark.asyncio
async def test_guardrail_ok_flag_bypasses_halt(
    session_factory: async_sessionmaker,
    job,
    tmp_path,
):
    """With guardrail_ok=True (permissive guardrail), job should proceed despite violations."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source_file = repo / "test.py"
    source_file.write_text('api_key = "sk-live-1234567890"\ndef foo():\n    return 1\n')

    fake = _StubRefactorAgent(ProposedCode(file_path="test.py", code="def foo():\n    return 2\n"))
    state = _fixture_state(job.id)
    state.update(repo_path=str(repo), current_file="test.py")

    async with (
        EventBus(session_factory) as bus,
        open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver,
    ):
        # Using permissive guardrail simulates --guardrail-ok
        graph = build_refactor_graph(
            bus,
            checkpointer=saver,
            verify_node=None,
            refactor_agent=fake,
            guardrail=_PermissiveGuardrail(),
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    # With permissive guardrail, the job proceeds
    assert final["proposed_code"] is not None
    assert final.get("guardrail_halt") is not True


@pytest.mark.asyncio
async def test_guardrail_ok_flag_on_proposed_owasp(
    session_factory: async_sessionmaker,
    job,
    tmp_path,
):
    """With permissive guardrail, OWASP pattern in proposed should not halt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source_file = repo / "test.py"
    source_file.write_text("def foo():\n    return 1\n")

    fake = _StubRefactorAgent(
        ProposedCode(file_path="test.py", code='eval("os.system(\\"id\\")")\n')
    )
    state = _fixture_state(job.id)
    state.update(repo_path=str(repo), current_file="test.py")

    async with (
        EventBus(session_factory) as bus,
        open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver,
    ):
        graph = build_refactor_graph(
            bus,
            checkpointer=saver,
            verify_node=None,
            refactor_agent=fake,
            guardrail=_PermissiveGuardrail(),
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is not None
    assert final.get("guardrail_halt") is not True


@pytest.mark.asyncio
async def test_clean_code_passes_guardrails(
    session_factory: async_sessionmaker,
    job,
    tmp_path,
):
    """Clean legacy and proposed code should pass through without halt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source_file = repo / "test.py"
    source_file.write_text("def foo():\n    return 1\n")

    fake = _StubRefactorAgent(ProposedCode(file_path="test.py", code="def foo():\n    return 42\n"))
    state = _fixture_state(job.id)
    state.update(repo_path=str(repo), current_file="test.py")

    async with (
        EventBus(session_factory) as bus,
        open_sqlite_checkpointer(tmp_path / ".bluet" / "state.db") as saver,
    ):
        graph = build_refactor_graph(
            bus,
            checkpointer=saver,
            verify_node=None,
            refactor_agent=fake,
            guardrail=_BlockingGuardrail(),
        )
        final = await graph.ainvoke(state, config=run_config(job.id))

    assert final["proposed_code"] is not None
    assert final.get("guardrail_halt") is not True


import json
