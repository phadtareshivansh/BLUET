"""Host platform detection and sandbox execution backend selection.

Determines at runtime how bluet's sandbox (gVisor) should be reached on the
host OS: natively on Linux, inside Docker Desktop's Linux VM on macOS, or via
WSL2 on Windows. Backends never degrade silently to unsandboxed execution.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

from bluet.errors import BluetEnvironmentError

WSL_EXE = "wsl.exe"
WSL_LIST_TIMEOUT_SECONDS = 15

WSL2_INSTALL_INSTRUCTION = "wsl --install"

BACKEND_NATIVE = "native-gvisor"
BACKEND_DOCKER = "docker-gvisor"
BACKEND_WSL2 = "wsl2-gvisor"


def host_os() -> str:
    """Return the host operating system ("Linux", "Darwin", "Windows", ...)."""
    return platform.system()


def _wsl_executable() -> str | None:
    """Resolve ``wsl.exe`` on PATH, or ``None`` if not installed."""
    return shutil.which(WSL_EXE)


def _wsl_running_distros() -> list[str]:
    """Return names of WSL2 distros currently ``Running``.

    Runs ``wsl.exe --list --verbose`` and parses each distro line, keeping
    only entries whose state is ``Running`` with version ``2``. Returns an
    empty list on any failure (wsl not present, nonzero exit, timeout, etc.)
    so callers fail loudly rather than guessing.
    """
    wsl = _wsl_executable()
    if wsl is None:
        return []

    try:
        proc = subprocess.run(
            [wsl, "--list", "--verbose"],
            capture_output=True,
            timeout=WSL_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if proc.returncode != 0:
        return []

    stdout = _decode_wsl_output(proc.stdout)
    distros = []
    for line in stdout.splitlines():
        tokens = line.split()
        if len(tokens) < 3:
            continue
        state = tokens[-2].lower()
        version = tokens[-1]
        if state == "running" and version == "2":
            distros.append(tokens[0].lstrip("*").strip())
    return distros


def _decode_wsl_output(data: bytes) -> str:
    """Decode ``wsl.exe`` output, handling its UTF-16LE-with-NUL quirk."""
    if b"\x00" in data:
        return data.decode("utf-16-le", errors="replace").replace("\x00", "")
    return data.decode("utf-8", errors="replace")


def _wsl2_available() -> bool:
    """True only if wsl.exe is present with at least one running WSL2 distro."""
    return bool(_wsl_running_distros())


def default_wsl_distro() -> str | None:
    """Return the first running WSL2 distro name, or ``None`` if none is running."""
    distros = _wsl_running_distros()
    return distros[0] if distros else None


def get_execution_backend() -> str:
    """Return the sandbox execution backend for the current host.

    Raises:
        BluetEnvironmentError: if the host is unsupported, or if gVisor cannot
            be reached — notably Windows without WSL2, in which case no
            fallback to unsandboxed execution occurs.
    """
    system = host_os()
    if system == "Linux":
        return BACKEND_NATIVE
    if system == "Darwin":
        return BACKEND_DOCKER
    if system == "Windows":
        if _wsl2_available():
            return BACKEND_WSL2
        raise BluetEnvironmentError(
            "gVisor on Windows requires WSL2 with a running distro; none was found. "
            "Install it with: "
            f"`{WSL2_INSTALL_INSTRUCTION}` and then run `wsl --set-default-version 2` "
            "and start a distro (e.g. `wsl -d Ubuntu`). "
            "Bluet will not fall back to unsandboxed execution."
        )
    raise BluetEnvironmentError(
        f"Unsupported host OS {system!r}: bluet's gVisor sandbox is only "
        "supported on Linux, macOS (Docker Desktop), and Windows (WSL2)."
    )