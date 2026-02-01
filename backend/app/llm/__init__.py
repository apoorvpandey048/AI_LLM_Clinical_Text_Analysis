"""LLM client package."""

from app.llm.client import LLMClient, LLMResponse
from app.llm.ollama_client import OllamaClient
from app.llm.vllm_client import VLLMClient


def get_llm_client() -> LLMClient:
    """
    Factory function to get the appropriate LLM client based on config.
    
    Returns:
        LLMClient instance (OllamaClient or VLLMClient)
    """
    from app.config import get_settings
    settings = get_settings()
    
    if settings.llm_backend == "vllm":
        return VLLMClient()
    return OllamaClient()


__all__ = ["LLMClient", "LLMResponse", "OllamaClient", "VLLMClient", "get_llm_client"]


