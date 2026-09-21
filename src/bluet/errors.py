"""Shared exception types for bluet."""


class BluetEnvironmentError(RuntimeError):
    """Raised when the host environment cannot satisfy a bluet requirement."""