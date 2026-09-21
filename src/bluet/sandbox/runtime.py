"""Runtime sandbox diagnostics: Docker + gVisor probing and backend selection.

``bluet doctor`` and the auto-gate at the start of ``bluet run`` both build on
:func:`diagnose`. On Windows, every docker invocation is wrapped through the
running WSL2 distro confirmed by :mod:`bluet.sandbox.platform`, so this module
never opens a socket to a host docker daemon on Windows.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from bluet.errors import BluetEnvironmentError
from bluet.sandbox.platform import default_wsl_distro, get_execution_backend, host_os

BACKEND_GVISOR = "gvisor"
BACKEND_HARDENED = "hardened-docker"

DOCKER_INSTALL_URL = "https://docs.docker.com/get-docker/"
GVISOR_INSTALL_URL = "https://gvisor.dev/docs/user_guide/install/"

DOCKER_INFO_TIMEOUT_SECONDS = 15.0
GVISOR_DRYRUN_TIMEOUT_SECONDS = 30.0

GVISOR_DRYRUN = ["run", "--rm", "--runtime=runsc", "hello-world"]

WEAKER_BOUNDARY_WARNING = (
    "Docker is running but the gVisor runtime (runsc) is unavailable; "
    "verification will run with a weaker isolation boundary than gVisor "
    "provides (hardened standard Docker)."
)


@dataclass(frozen=True)
class RuntimeLimits:
    """Resource limits applied to the hardened-Docker fallback backend."""

    memory: str = "2g"
    network: str = "none"
    read_only: bool = True
    cap_drop: tuple[str, ...] = ("ALL",)

    def as_flags(self) -> list[str]:
        flags = [f"--memory={self.memory}", f"--network={self.network}"]
        if self.read_only:
            flags.append("--read-only")
        for cap in self.cap_drop:
            flags.append(f"--cap-drop={cap}")
        return flags

    def as_text(self) -> str:
        parts = [f"memory={self.memory}", f"network={self.network}"]
        parts.append("read-only" if self.read_only else "read-write")
        parts.extend(f"cap-drop={cap}" for cap in self.cap_drop)
        return ", ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Diagnostics:
    """Result of a runtime probe: docker + gVisor status and backend decision."""

    docker_ok: bool
    docker_error: str | None
    gvisor_available: bool
    backend: str | None
    limits: RuntimeLimits
    host_backend: str
    warnings: tuple[str, ...] = ()


def docker_command(args: Sequence[str]) -> list[str]:
    """Return the argv that runs a docker command on this host.

    On Linux/macOS the host docker CLI is used directly; on Windows the command
    is routed through the running WSL2 distro (``wsl.exe -d <distro> docker``).
    """
    system = host_os()
    if system != "Windows":
        return ["docker", *args]
    distro = default_wsl_distro()
    if distro is None:
        raise BluetEnvironmentError(
            "Windows sandboxing requires a running WSL2 distro; none is running. "
            "Start one with `wsl -d <distro>` before running bluet."
        )
    return ["wsl.exe", "-d", distro, "docker", *args]


def _safe_run(
    cmd: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run a command, capturing output; return ``None`` if the binary is absent."""
    argv = list(cmd)
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 1, stderr="command timed out")
    except OSError as exc:  # e.g. permission-denied executing the binary
        return subprocess.CompletedProcess(argv, 1, stderr=str(exc))


def check_docker() -> tuple[bool, str | None]:
    """Probe the docker daemon via ``docker info``.

    Returns ``(True, None)`` when healthy; ``(False, reason)`` otherwise, where
    reason is a short, human-actionable string.
    """
    cmd = docker_command(["info"])
    proc = _safe_run(cmd, DOCKER_INFO_TIMEOUT_SECONDS)
    if proc is None:
        return False, (
            "Docker CLI not found on PATH - is Docker installed? "
            f"Install: {DOCKER_INSTALL_URL}"
        )
    if proc.returncode == 0:
        return True, None
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, (
        f"docker daemon not reachable: {detail or 'docker info failed'}. "
        "Is the Docker daemon running?"
    )


def check_gvisor() -> bool:
    """Probe gVisor with a dry run: ``docker run --rm --runtime=runsc hello-world``."""
    proc = _safe_run(docker_command(GVISOR_DRYRUN), GVISOR_DRYRUN_TIMEOUT_SECONDS)
    return proc is not None and proc.returncode == 0


def require_sandbox_ready(diagnostics: Diagnostics) -> None:
    """Raise if sandboxed execution is impossible (docker down/absent).

    BLUET cannot execute sandboxed verification at all without Docker, so this
    hard-fails rather than degrading.
    """
    if not diagnostics.docker_ok:
        raise BluetEnvironmentError(
            "BLUET cannot execute sandboxed verification without Docker. "
            f"{diagnostics.docker_error or 'Docker is unavailable.'} "
            f"Install: {DOCKER_INSTALL_URL}"
        )


def diagnose(limits: RuntimeLimits | None = None) -> Diagnostics:
    """Run the full runtime probe and decide the sandbox backend.

    Returns rather than raising except for environment-level failures (WSL2
    missing on Windows, unsupported host OS) which :func:`get_execution_backend`
    raises on.
    """
    limits = limits or RuntimeLimits()
    host_backend = get_execution_backend()

    docker_ok, docker_error = check_docker()
    if not docker_ok:
        return Diagnostics(
            docker_ok=False,
            docker_error=docker_error,
            gvisor_available=False,
            backend=None,
            limits=limits,
            host_backend=host_backend,
        )

    gvisor_ok = check_gvisor()
    if gvisor_ok:
        return Diagnostics(
            docker_ok=True,
            docker_error=None,
            gvisor_available=True,
            backend=BACKEND_GVISOR,
            limits=limits,
            host_backend=host_backend,
        )
    return Diagnostics(
        docker_ok=True,
        docker_error=None,
        gvisor_available=False,
        backend=BACKEND_HARDENED,
        limits=limits,
        host_backend=host_backend,
        warnings=(WEAKER_BOUNDARY_WARNING,),
    )


__all__ = [
    "BACKEND_GVISOR",
    "BACKEND_HARDENED",
    "DOCKER_INSTALL_URL",
    "GVISOR_INSTALL_URL",
    "WEAKER_BOUNDARY_WARNING",
    "Diagnostics",
    "RuntimeLimits",
    "check_docker",
    "check_gvisor",
    "diagnose",
    "docker_command",
    "require_sandbox_ready",
]