"""Agent package.

``LLMClient`` (in :mod:`bluet.agents.llm_client`) is the shared local-inference
client that agents use to produce structured output: it probes Ollama/vLLM,
selects a hardware-appropriate model, and completes with Pydantic-AI structured
output. Agent subpackages (``analyzer``, ``refactor``, ``verifier``) live
alongside it.
"""

from bluet.agents.llm_client import (
    MODEL_TIERS,
    LLMClient,
    detect_ram_gb,
    detect_vram_gb,
    is_apple_silicon,
    pick_fallback_model,
    resolve_model_tier,
)

__all__ = [
    "MODEL_TIERS",
    "LLMClient",
    "detect_ram_gb",
    "detect_vram_gb",
    "is_apple_silicon",
    "pick_fallback_model",
    "resolve_model_tier",
]
