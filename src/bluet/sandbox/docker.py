"""Docker-backed sandbox runner for Phase 4.1.

Implements :class:`SandboxRunner` using docker-py to execute code in isolated
containers with gVisor or hardened Docker runtime.
"""

from __future__ import annotations

import asyncio
import tarfile
import tempfile
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import docker
from docker.errors import DockerException, ImageNotFound, APIError

from bluet.sandbox.execution import ExecutionResult, SandboxRunner
from bluet.sandbox.runtime import RuntimeLimits, BACKEND_GVISOR, BACKEND_HARDENED, diagnose, get_execution_backend


# Default Docker images per language
DEFAULT_IMAGES = {
    "python": "python:3.11-slim",
    "java": "maven:3.9-eclipse-temurin-8",
}

# Image tags we build/use
BLUET_IMAGE_PREFIX = "bluet"


@dataclass
class DockerRunnerConfig:
    """Configuration for DockerSandboxRunner."""
    backend: str  # "gvisor" or "hardened-docker"
    host_backend: str  # "native", "docker-desktop", "wsl2"
    limits: RuntimeLimits
    image_tag: str
    docker_client: Any  # docker.DockerClient


class DockerSandboxRunner(SandboxRunner):
    """Run code in Docker containers with configurable isolation backend.
    
    Uses docker-py to create containers with the appropriate runtime:
    - gVisor: --runtime=runsc
    - Hardened Docker: --network none --read-only --cap-drop=ALL --memory=2g etc.
    
    Files are injected via tar stream to avoid host filesystem mounts.
    """

    def __init__(
        self,
        *,
        backend: str,
        host_backend: str,
        limits: RuntimeLimits,
        image_tag: str,
        docker_client: Optional[Any] = None,
    ) -> None:
        self._backend = backend
        self._host_backend = host_backend
        self._limits = limits
        self._image_tag = image_tag
        self._docker = docker_client or docker.DockerClient.from_env()
        self._ensure_image()

    def _ensure_image(self) -> None:
        """Ensure the Docker image exists locally, build if needed."""
        try:
            self._docker.images.get(self._image_tag)
        except ImageNotFound:
            self._build_image()

    def _build_image(self) -> None:
        """Build the Docker image for the target language."""
        # Determine base image from tag
        if "python" in self._image_tag:
            base_image = DEFAULT_IMAGES["python"]
            dockerfile = self._get_python_dockerfile()
        elif "java" in self._image_tag:
            base_image = DEFAULT_IMAGES["java"]
            dockerfile = self._get_java_dockerfile()
        else:
            base_image = DEFAULT_IMAGES["python"]
            dockerfile = self._get_python_dockerfile()

        # Build image
        self._docker.images.build(
            fileobj=io.BytesIO(dockerfile.encode()),
            tag=self._image_tag,
            rm=True,
            forcerm=True,
            buildargs={"BASE_IMAGE": base_image},
        )

    def _get_python_dockerfile(self) -> str:
        return f"""
ARG BASE_IMAGE=python:3.11-slim
FROM $BASE_IMAGE

# Install test dependencies
RUN pip install --no-cache-dir hypothesis pytest

# Create workdir with write permissions (needed for read-only container with put_archive)
RUN mkdir -p /workdir && chmod 777 /workdir

WORKDIR /workdir
ENTRYPOINT ["python"]
"""

    def _get_java_dockerfile(self) -> str:
        return f"""
ARG BASE_IMAGE=maven:3.9-eclipse-temurin-8
FROM $BASE_IMAGE

# Create workdir with write permissions (needed for read-only container with put_archive)
RUN mkdir -p /workdir && chmod 777 /workdir

WORKDIR /workdir
ENTRYPOINT ["mvn"]
"""

    def _get_runtime_flags(self) -> dict[str, Any]:
        """Get Docker host_config flags based on backend."""
        if self._backend == BACKEND_GVISOR:
            return {
                "runtime": "runsc",
            }
        # Hardened Docker
        return {
            "runtime": "runc",
            **self._limits.as_flags_dict(),
        }

    def _get_host_config(self) -> dict[str, Any]:
        """Build host_config for container creation."""
        runtime_flags = self._get_runtime_flags()
        
        # Common security settings
        # Note: read_only is NOT set because we need put_archive to inject files
        # We use tmpfs for /tmp but NOT for /workdir, so put_archive files persist
        host_config = {
            "network_mode": "none",
            "cap_drop": ["ALL"],
            "mem_limit": self._limits.memory,
            "cpu_period": 100000,
            "cpu_quota": 50000,  # 0.5 CPU
            "pids_limit": 100,
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=100m",
            },
            **runtime_flags,
        }
        
        # On Windows with WSL2, we don't need special handling here
        # docker-py will communicate with the Docker daemon normally
        
        return host_config

    async def run(
        self,
        files: Mapping[str, str],
        argv: Sequence[str],
        *,
        limits: Optional[RuntimeLimits] = None,
        timeout: float = 120.0,
    ) -> ExecutionResult:
        """Execute argv in a container with files materialized.
        
        Args:
            files: Mapping of relative paths to file contents
            argv: Command and arguments to run
            limits: Optional runtime limits (uses instance limits if not provided)
            timeout: Maximum execution time in seconds
            
        Returns:
            ExecutionResult with stdout, stderr, return code
        """
        loop = asyncio.get_running_loop()
        active_limits = limits or self._limits
        
        # Create tar archive of files in memory
        tar_data = self._create_tar_archive(files)
        
        # Run container in thread pool to avoid blocking
        return await loop.run_in_executor(
            None,
            self._run_container_sync,
            tar_data,
            list(argv),
            active_limits,
            timeout,
        )

    def _create_tar_archive(self, files: Mapping[str, str]) -> bytes:
        """Create a tar archive of the files for injection into container."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            for rel_path, content in files.items():
                # Normalize path
                rel_path = rel_path.lstrip("/")
                data = content.encode("utf-8")
                tarinfo = tarfile.TarInfo(name=rel_path)
                tarinfo.size = len(data)
                tarinfo.mode = 0o644
                tar.addfile(tarinfo, io.BytesIO(data))
        return tar_buffer.getvalue()

    def _resolve_argv(self, argv: list[str]) -> list[str]:
        """Resolve special argv prefixes like ::python::, ::pytest::, ::mvn::."""
        argv = list(argv)
        if not argv:
            return argv
        if argv[0] == "::python::":
            # Inside container, use python3 from PATH
            argv[0] = "python3"
        elif argv[0] == "::pytest::":
            argv = [
                "python3",
                "-m",
                "pytest",
                "-p",
                "bluet_pytest_plugin",
                "-x",
                "test_parity.py",
            ]
        elif argv[0] == "::mvn::":
            argv = ["mvn", "test", "-Dtest=ParityTest", "-q"]
        elif argv[0] == "-c":
            # Python -c convention: use python3 from container PATH
            argv = ["python3"] + argv
        elif argv[0].endswith(".py"):
            # Python file: use python3 from container PATH
            argv = ["python3"] + argv
        return argv

    def _run_container_sync(
        self,
        tar_data: bytes,
        argv: list[str],
        limits: RuntimeLimits,
        timeout: float,
    ) -> ExecutionResult:
        """Synchronous container execution (run in thread pool)."""
        import time
        import threading
        
        container = None
        exec_id = None
        timed_out = False
        start_time = time.time()
        
        def timeout_handler():
            nonlocal timed_out
            timed_out = True
            if container:
                try:
                    self._docker.api.kill(container["Id"])
                except Exception:
                    pass
        
        timer = threading.Timer(timeout, timeout_handler)
        timer.start()
        
        try:
            # Resolve special argv prefixes
            argv = self._resolve_argv(argv)
            
            # Create container with a placeholder command that keeps it running
            host_config = self._docker.api.create_host_config(**self._get_host_config())
            
            container = self._docker.api.create_container(
                image=self._image_tag,
                command=["tail", "-f", "/dev/null"],  # Keep container running
                entrypoint=[""],  # Override entrypoint
                working_dir="/workdir",
                host_config=host_config,
                stdin_open=False,
                tty=False,
                detach=True,
            )
            
            container_id = container["Id"]
            
            # Start container first (activates tmpfs mounts)
            self._docker.api.start(container_id)
            
            # Inject files via tar archive into the running container
            self._docker.api.put_archive(container_id, "/workdir", tar_data)
            
            # Execute the actual command via docker exec
            exec_id = self._docker.api.exec_create(
                container_id,
                argv,
                workdir="/workdir",
                stdout=True,
                stderr=True,
            )["Id"]
            
            # Start exec and wait for completion with timeout
            exec_start = self._docker.api.exec_start(exec_id, stream=False)
            
            # Check if timed out
            if timed_out:
                return ExecutionResult(
                    returncode=-1,
                    stdout="",
                    stderr=f"Execution timed out after {timeout}s",
                    timed_out=True,
                    runtime_seconds=time.time() - start_time,
                )
            
            # Wait for exec to complete
            try:
                exec_inspect = self._docker.api.exec_inspect(exec_id)
                exit_code = exec_inspect.get("ExitCode", -1)
            except Exception as e:
                return ExecutionResult(
                    returncode=-1,
                    stdout="",
                    stderr=f"Container execution failed: {e}",
                    timed_out=timed_out,
                    runtime_seconds=time.time() - start_time,
                )
            
            timer.cancel()
            
            # Get exec output
            if isinstance(exec_start, bytes):
                logs = exec_start.decode("utf-8", errors="replace")
            else:
                logs = exec_start
            
            # Split stdout/stderr
            stdout, stderr = self._split_logs(logs)
            
            runtime = time.time() - start_time
            
            return ExecutionResult(
                returncode=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                runtime_seconds=runtime,
            )
            
        except DockerException as e:
            return ExecutionResult(
                returncode=-1,
                stdout="",
                stderr=f"Docker error: {e}",
                timed_out=False,
                runtime_seconds=time.time() - start_time,
            )
        except Exception as e:
            return ExecutionResult(
                returncode=-1,
                stdout="",
                stderr=f"Execution error: {e}",
                timed_out=False,
                runtime_seconds=time.time() - start_time,
            )
        finally:
            if timer:
                timer.cancel()
            # Always clean up container
            if container:
                try:
                    self._docker.api.remove_container(container["Id"], force=True, v=True)
                except Exception:
                    pass

    def _split_logs(self, logs: str) -> tuple[str, str]:
        """Split mixed Docker logs into stdout/stderr.
        
        Docker logs prefix each line with a header: 8 bytes (stream type + size).
        For simplicity, we return all as stdout if we can't parse.
        """
        # Simple heuristic: if logs contain obvious stderr markers, split
        # Otherwise return all as stdout
        lines = logs.splitlines()
        stdout_lines = []
        stderr_lines = []
        
        for line in lines:
            if line.startswith("stderr:") or "ERROR" in line.upper() or "EXCEPTION" in line.upper():
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
        
        return "\n".join(stdout_lines), "\n".join(stderr_lines)


def create_sandbox_runner(
    job: Any,
    session_factory: Any,
    *,
    docker_client: Optional[Any] = None,
) -> SandboxRunner:
    """Factory function to create appropriate sandbox runner for a job.
    
    Reads job.backend (populated from doctor cache) to determine isolation backend.
    Falls back to LocalRunner if Docker is unavailable.
    """
    from bluet.sandbox.execution import LocalRunner
    from bluet.sandbox.runtime import diagnose, RuntimeLimits
    
    # Get backend from job
    backend = getattr(job, "backend", None)
    
    if not backend:
        # Re-run doctor check inline as fallback
        diagnostics = diagnose(RuntimeLimits())
        backend = diagnostics.backend or BACKEND_HARDENED
    
    # Get host backend (invocation mode)
    host_backend = get_execution_backend()
    
    # Determine image tag based on target language
    # This would ideally come from job or be configurable
    image_tag = f"{BLUET_IMAGE_PREFIX}/python:latest"
    
    if backend == BACKEND_GVISOR or backend == BACKEND_HARDENED:
        try:
            return DockerSandboxRunner(
                backend=backend,
                host_backend=host_backend,
                limits=RuntimeLimits(),
                image_tag=image_tag,
                docker_client=docker_client,
            )
        except Exception as e:
            # Fall back to local runner with warning
            import logging
            logging.getLogger("bluet.sandbox").warning(
                f"Failed to create Docker runner ({e}), falling back to LocalRunner"
            )
    
    return LocalRunner()


# Add as_flags_dict method to RuntimeLimits for convenience
def _as_flags_dict(self) -> dict[str, Any]:
    """Convert RuntimeLimits to Docker host_config compatible dict."""
    flags = {
        "mem_limit": self.memory,
        "network_mode": self.network,
    }
    if self.read_only:
        flags["read_only"] = True
    if self.cap_drop:
        flags["cap_drop"] = list(self.cap_drop)
    return flags

RuntimeLimits.as_flags_dict = _as_flags_dict