"""Refactor Agent failure types."""


class RefactorError(RuntimeError):
    """Raised when a refactor pass cannot complete (e.g. formatter missing).

    Carries an actionable message so callers can surface the exact remedy
    (install command, required tool) instead of a bare crash.
    """
