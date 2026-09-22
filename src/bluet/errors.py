"""Shared exception types for bluet."""


class BluetEnvironmentError(RuntimeError):
    """Raised when the host environment cannot satisfy a bluet requirement."""


class BluetLLMError(BluetEnvironmentError):
    """Base for every LLM-client failure.

    Sibling failure modes under one base so graph nodes can fail soft on the
    whole LLM step (backend-down, unusable model, or model output that never
    matched the requested schema) while ``run.py``/``doctor`` keep catching the
    shared :class:`BluetEnvironmentError` parent unchanged.
    """


class LLMUnavailableError(BluetLLMError):
    """No local inference backend (Ollama/vLLM) can be reached, or no model usable:
    the tier model is missing, was declined, and no installed fallback exists."""


class LLMSchemaOutputError(BluetLLMError):
    """The backend is reachable but the model could not emit structured output that
    validates against the requested response schema after Pydantic-AI's retries.

    Distinct from :class:`LLMUnavailableError`: the LLM answered, just not in the
    shape we asked for (common with very small models). The message carries the
    fix (larger model, or ``BLUET_LLM_MODEL`` override)."""
