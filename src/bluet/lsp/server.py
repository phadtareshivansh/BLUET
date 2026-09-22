"""BLUET Language Server Protocol server using pygls.

Provides:
- textDocument/codeAction to trigger a refactor job on the open file
- Custom notifications (bluet/parityStatus) with live status as the LangGraph job progresses
- Inline diagnostics showing which functions have unresolved counter-examples
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lsprotocol.types import (
    CodeAction,
    CodeActionContext,
    CodeActionParams,
    Command,
    Diagnostic,
    DiagnosticSeverity,
    DidChangeTextDocumentParams,
    DidOpenTextDocumentParams,
    InitializeParams,
    InitializeResult,
    Location,
    MessageType,
    Position,
    Range,
    ServerCapabilities,
    TextDocumentIdentifier,
    TextDocumentSyncKind,
    WorkspaceFolder,
)
from pygls.lsp.server import LanguageServer
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluet.agents.analyzer import parser_for_source
from bluet.agents.analyzer.models import LogicSpec
from bluet.agents.refactor import CounterExample, RefactorAgent
from bluet.agents.verifier import ParityVerifier
from bluet.context_store import ContextStore, InMemoryContextStore
from bluet.orchestrator.events import EventBus, TOPICS
from bluet.orchestrator.refactor_graph import (
    MAX_RETRIES,
    BluetState,
    build_refactor_graph,
    open_sqlite_checkpointer,
    run_config,
)
from bluet.state.db import db_path_for_repo, engine_for_repo, init_schema
from bluet.state.repository import create_job, log_event, record_proposed_code, update_job_status
from bluet.sandbox.runtime import RuntimeLimits, diagnose

logger = logging.getLogger("bluet.lsp")


@dataclass
class JobSession:
    """Tracks an active refactor job for a specific file."""
    job_id: int
    file_path: str
    repo_path: Path
    task: asyncio.Task | None = None
    counter_examples: list[CounterExample] = field(default_factory=list)
    parity_status: str = "pending"  # pending, running, pass, fail
    current_stage: str = "idle"


class BluetLanguageServer(LanguageServer):
    """BLUET LSP server with code action support and live parity status."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._jobs: dict[str, JobSession] = {}
        self._event_bus: EventBus | None = None
        self._event_bus_task: asyncio.Task | None = None
        self._session_factory: Callable[[], Awaitable[Any]] | None = None
        self._checkpointer: Any = None
        self._repo_path: Path | None = None
        self._engine: Any = None
        self._context_store: ContextStore = InMemoryContextStore()
        self._refactor_agent: RefactorAgent | None = None
        self._verifier: ParityVerifier | None = None
        self._initialized: bool = False

        # Register features
        self._register_features()

    def _register_features(self) -> None:
        """Register LSP features."""

        @self.feature("textDocument/didOpen")
        async def did_open(params: DidOpenTextDocumentParams) -> None:
            await self._on_document_change(params.text_document.uri, params.text_document.text)

        @self.feature("textDocument/didChange")
        async def did_change(params: DidChangeTextDocumentParams) -> None:
            if params.content_changes:
                await self._on_document_change(params.text_document.uri, params.content_changes[0].text)

        @self.feature("textDocument/codeAction")
        async def code_action(params: CodeActionParams) -> list[CodeAction]:
            return await self._handle_code_action(params)

        @self.feature("initialize")
        async def initialize(params: InitializeParams) -> InitializeResult:
            return await self._initialize(params)

        @self.feature("shutdown")
        async def shutdown() -> None:
            await self._shutdown_server()

        @self.feature("exit")
        def exit_notification() -> None:
            pass

        @self.command("bluet.refactor")
        async def bluet_refactor(file_path: str) -> None:
            """Execute a refactor job on the given file."""
            await self._run_refactor_job(file_path)

    async def _initialize(self, params: InitializeParams) -> InitializeResult:
        """Initialize the LSP server and start the event bus."""
        if hasattr(self, '_initialized') and self._initialized:
            return InitializeResult(
                capabilities=ServerCapabilities(
                    text_document_sync=TextDocumentSyncKind.FULL,
                    code_action_provider=True,
                    diagnostic_provider=True,
                ),
                server_info={"name": "bluet", "version": "0.1.0"},
            )

        # Determine repo path from workspace folders
        if params.workspace_folders:
            self._repo_path = Path(params.workspace_folders[0].uri.replace("file://", ""))
        else:
            self._repo_path = Path.cwd()

        # Initialize database
        self._engine = engine_for_repo(self._repo_path)
        await init_schema(self._engine)

        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

        # Initialize event bus
        self._event_bus = EventBus(self._session_factory)
        await self._event_bus.start()

        # Initialize checkpointer
        db_path = db_path_for_repo(self._repo_path)
        # Use the checkpointer directly without context manager for server lifecycle
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        import aiosqlite
        conn = await aiosqlite.connect(str(db_path))
        self._checkpointer = AsyncSqliteSaver(conn)

        # Initialize agents
        self._refactor_agent = RefactorAgent()
        self._verifier = ParityVerifier()

        # Start event bus listener for parity status updates
        self._event_bus_task = asyncio.create_task(self._listen_for_events())

        self._initialized = True

        return InitializeResult(
            capabilities=ServerCapabilities(
                text_document_sync=TextDocumentSyncKind.Full,
                code_action_provider=True,
                diagnostic_provider=True,
            ),
            server_info={"name": "bluet", "version": "0.1.0"},
        )

    async def _on_document_change(self, uri: str, text: str) -> None:
        """Handle document open/change - update diagnostics."""
        file_path = uri.replace("file://", "")
        await self._update_diagnostics(file_path, text)

    async def _update_diagnostics(self, file_path: str, text: str) -> None:
        """Publish diagnostics for the given file."""
        diagnostics: list[Diagnostic] = []

        # Check if there's an active job with counter-examples for this file
        for session in self._jobs.values():
            if session.file_path == file_path and session.counter_examples:
                for cx in session.counter_examples:
                    if cx.function_name:
                        # Try to find the function line from the logic spec if available
                        start_line = 0
                        end_line = 0
                        # We don't have exact line numbers in counter-example, use a rough range
                        diagnostic = Diagnostic(
                            range=Range(
                                start=Position(line=start_line, character=0),
                                end=Position(line=end_line, character=0),
                            ),
                            message=f"Parity failure in '{cx.function_name}': {cx.diff_summary or cx.message or 'output mismatch'}",
                            severity=DiagnosticSeverity.Error,
                            source="bluet",
                            code="parity-failure",
                        )
                        diagnostics.append(diagnostic)

        self.publish_diagnostics(f"file://{file_path}", diagnostics)

    async def _handle_code_action(self, params: CodeActionParams) -> list[CodeAction]:
        """Handle code action request - offer to run refactor job."""
        file_path = params.text_document.uri.replace("file://", "")
        actions: list[CodeAction] = []

        # Only offer refactor action for supported languages
        parser = parser_for_source(None, os.path.basename(file_path))
        if parser is None:
            return actions

        # Create the "Run BLUET Refactor" code action
        action = CodeAction(
            title="Run BLUET Refactor",
            kind="refactor",
            command=Command(
                title="Run BLUET Refactor",
                command="bluet.refactor",
                arguments=[file_path],
            ),
        )
        actions.append(action)

        return actions

    async def _listen_for_events(self) -> None:
        """Listen to EventBus events and forward parity status to clients."""
        if not self._event_bus:
            return

        async def handle_event(payload: dict[str, Any]) -> None:
            job_id = payload.get("job_id")
            if job_id is None:
                return

            # Find the session for this job
            session = None
            for s in self._jobs.values():
                if s.job_id == job_id:
                    session = s
                    break

            if not session:
                return

            topic = payload.get("topic", "")
            status = payload.get("status", "")

            if topic == "task.analysis":
                session.current_stage = "analyzing"
                session.parity_status = "running"
            elif topic == "task.context":
                session.current_stage = "indexing"
            elif topic == "task.refactor":
                session.current_stage = "refactoring"
            elif topic == "task.verify":
                session.current_stage = "verifying"
                if status == "pass":
                    session.parity_status = "pass"
                    session.counter_examples = []
                elif status == "fail":
                    session.parity_status = "fail"
                    counter_examples = payload.get("counter_examples", [])
                    session.counter_examples = [
                        CounterExample.model_validate(cx) for cx in counter_examples
                    ]
                elif status == "skip":
                    session.parity_status = "skip"
            elif topic == "feedback.regression":
                session.current_stage = "self-healing"
            elif topic == "feedback.warning":
                session.current_stage = f"warning: {payload.get('source', 'unknown')}"

            # Send custom notification to client
            await self._send_parity_status(session)

            # Update diagnostics if verification completed
            if topic == "task.verify":
                await self._update_diagnostics(session.file_path, "")

        # Subscribe to all topics
        for topic in TOPICS:
            await self._event_bus.subscribe(topic, handle_event)

    async def _send_parity_status(self, session: JobSession) -> None:
        """Send custom bluet/parityStatus notification to client."""
        notification = {
            "jobId": session.job_id,
            "filePath": session.file_path,
            "status": session.parity_status,
            "stage": session.current_stage,
            "retryCount": getattr(session, "retry_count", 0),
            "counterExamples": [
                {
                    "function": cx.function_name,
                    "message": cx.message or cx.diff_summary or "Output mismatch",
                }
                for cx in session.counter_examples
            ],
        }
        self.protocol.notify("bluet/parityStatus", notification)

    async def _run_refactor_job(self, file_path: str) -> None:
        """Run the refactor pipeline for a single file."""
        if not self._session_factory or not self._checkpointer or not self._engine:
            return

        repo_path = self._repo_path or Path.cwd()
        rel_path = Path(file_path).relative_to(repo_path) if Path(file_path).is_absolute() else Path(file_path)

        # Detect target language from file extension
        suffix = Path(file_path).suffix.lower()
        target_language = "python" if suffix == ".py" else "java" if suffix == ".java" else ""

        # Run sandbox diagnostics
        limits = RuntimeLimits()
        diagnostics = diagnose(limits)
        if not diagnostics.docker_ok:
            self.show_message(
                MessageType.Error,
                f"BLUET cannot run refactor: {diagnostics.docker_error}",
            )
            return

        if diagnostics.backend == "hardened-docker":
            self.show_message(
                MessageType.Warning,
                "Docker is running but gVisor is unavailable; using hardened Docker fallback.",
            )

        # Create job
        async with self._session_factory() as session:
            job = await create_job(session, str(repo_path), status="RUNNING", backend=diagnostics.backend)
        job_id = job.id

        # Track this job
        job_session = JobSession(job_id=job_id, file_path=str(rel_path), repo_path=repo_path)
        self._jobs[str(job_id)] = job_session

        try:
            # Build and run the graph
            graph = build_refactor_graph(
                self._event_bus,
                checkpointer=self._checkpointer,
                context_store=self._context_store,
                refactor_agent=self._refactor_agent,
                session_factory=self._session_factory,
                verifier=self._verifier,
            )

            state: BluetState = {
                "job_id": job_id,
                "repo_path": str(repo_path),
                "target_language": target_language,
                "current_file": str(rel_path),
                "retry_count": 0,
            }

            final = await graph.ainvoke(state, config=run_config(job_id))

            # Record proposed code if generated
            proposed = final.get("proposed_code")
            if proposed:
                async with self._session_factory() as session:
                    await record_proposed_code(
                        session,
                        job_id,
                        proposed.get("file_path", str(rel_path)),
                        proposed.get("code", ""),
                        proposed.get("imports_added", []),
                        proposed.get("notes", ""),
                    )

            # Final status
            parity = final.get("parity_result", {})
            final_status = "COMPLETED" if parity.get("status") == "pass" else "FAILED"
            async with self._session_factory() as session:
                await update_job_status(session, job_id, final_status)

            job_session.parity_status = parity.get("status", "unknown")
            await self._send_parity_status(job_session)

            self.show_message(
                MessageType.Info,
                f"BLUET refactor {'completed' if final_status == 'COMPLETED' else 'failed'} for {rel_path}",
            )

        except Exception as exc:
            logger.exception("Refactor job failed")
            self.show_message(MessageType.Error, f"Refactor failed: {exc}")
            job_session.parity_status = "error"
            await self._send_parity_status(job_session)
        finally:
            # Cleanup - only remove job session, keep engine for server lifetime
            await self._event_bus.flush()
            if str(job_id) in self._jobs:
                del self._jobs[str(job_id)]

    async def _shutdown_server(self) -> None:
        """Shutdown the LSP server and cleanup resources."""
        if self._event_bus_task:
            self._event_bus_task.cancel()
            try:
                await self._event_bus_task
            except asyncio.CancelledError:
                pass

        if self._event_bus:
            await self._event_bus.stop()

        if self._checkpointer:
            await self._checkpointer.conn.close()

        if self._engine:
            await self._engine.dispose()


def create_server() -> BluetLanguageServer:
    """Factory function to create the BLUET LSP server."""
    return BluetLanguageServer("bluet", "0.1.0")


@asynccontextmanager
async def serve_stdio() -> Awaitable[BluetLanguageServer]:
    """Run the LSP server over stdio."""
    server = create_server()

    # Start the server in a background task
    server_task = asyncio.create_task(server.start_io())

    # Wait a bit for initialization
    await asyncio.sleep(0.1)

    try:
        yield server
    finally:
        await server._shutdown_server()
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass