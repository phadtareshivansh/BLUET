"""Language formatters run on generated code; fail gracefully when missing.

The formatting step is part of "after generation": every produced file goes
through the registered formatter before it is returned as :class:`ProposedCode`.
A missing tool is a recoverable, clearly-worded :class:`RefactorError` — never a
bare crash — and the generated code is never handed back unformatted.
"""

from __future__ import annotations

import shutil
import subprocess

from bluet.agents.refactor.errors import RefactorError

#: target language -> (binary, argv tail). Code is piped on stdin and read back
#: from stdout. Python lands here now; Java slots in during Prompt 2.6.
FORMATTERS: dict[str, tuple[str, list[str]]] = {
    "python": ("ruff", ["format", "-"]),
}

_INSTALL_HINTS: dict[str, str] = {
    "ruff": "install it with `pip install ruff` (or `uv add --dev ruff`)",
}


def format_code(code: str, language: str, *, timeout: float = 30.0) -> str:
    """Format ``code`` for ``language`` in a subprocess; return formatted text.

    Raises:
        RefactorError: when no formatter is registered for ``language``, the
            formatter binary is not installed, or the formatter fails/exits non-
            zero — each with an actionable message.
    """
    spec = FORMATTERS.get(language)
    if spec is None:
        raise RefactorError(
            f"no formatter configured for target_language={language!r} "
            "(python is supported; java lands in a later prompt)"
        )
    binary, args = spec
    if shutil.which(binary) is None:
        raise RefactorError(
            f"formatter {binary!r} for target_language={language!r} is not installed. "
            f"{_INSTALL_HINTS.get(binary, 'Install it and retry')}. "
            "Formatting is required; the generated file is not written unformatted."
        )
    try:
        proc = subprocess.run(
            [binary, *args],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RefactorError(
            f"formatter {binary!r} is not installed on PATH. "
            f"{_INSTALL_HINTS.get(binary, 'Install it and retry')}."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RefactorError(
            f"formatter {binary!r} timed out after {timeout}s on {language!r} output."
        ) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise RefactorError(
            f"formatter {binary!r} exited {proc.returncode} on {language!r} output"
            + (f": {stderr}" if stderr else "")
        )
    return proc.stdout
