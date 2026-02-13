"""LLM client package."""

from app.llm.client import LLMClient, LLMResponse
from app.llm.ollama_client import OllamaClient
from app.llm.vllm_client import VLLMClient
from app.llm.stub_client import StubLLMClient


def get_llm_client() -> LLMClient:
    """Factory: return an LLM client based on config.

    Supports: `vllm`, `ollama`, and `stub` (local testing).
    """
    from app.config import get_settings

    settings = get_settings()
    backend = (settings.llm_backend or "vllm").lower()

    if backend == "vllm":
        return VLLMClient()
    if backend == "ollama":
        return OllamaClient()
    if backend == "stub":
        return StubLLMClient()

    # default
    return VLLMClient()


__all__ = ["LLMClient", "LLMResponse", "OllamaClient", "VLLMClient", "StubLLMClient", "get_llm_client"]


