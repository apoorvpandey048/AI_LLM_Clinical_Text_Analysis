from typing import Callable, Awaitable

from app.llm.client import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)

class FallbackLLMClient(LLMClient):
    """
    An LLM client wrapper that attempts a primary client,
    and falls back to a secondary client if the primary fails.
    """
    def __init__(self, primary: LLMClient, fallback: LLMClient):
        self.primary = primary
        self.fallback = fallback

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
        seed: int | None = None,
    ) -> LLMResponse:
        response = await self.primary.generate(
            prompt, system_prompt, temperature, max_tokens, timeout, seed
        )
        if response.error:
            logger.warning("primary_client_failed_fallback_triggered", error=response.error)
            # Add fallback duration to primary duration so we don't lose the total time
            primary_duration = response.duration_ms or 0
            fb_response = await self.fallback.generate(
                prompt, system_prompt, temperature, max_tokens, timeout, seed
            )
            fb_response.duration_ms = (fb_response.duration_ms or 0) + primary_duration
            return fb_response
        return response

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
        seed: int | None = None,
    ) -> LLMResponse:
        response = await self.primary.generate_stream(
            prompt, system_prompt, on_token, temperature, max_tokens, timeout, seed
        )
        if response.error:
            logger.warning("primary_client_stream_failed_fallback_triggered", error=response.error)
            primary_duration = response.duration_ms or 0
            fb_response = await self.fallback.generate_stream(
                prompt, system_prompt, on_token, temperature, max_tokens, timeout, seed
            )
            fb_response.duration_ms = (fb_response.duration_ms or 0) + primary_duration
            return fb_response
        return response

    async def health_check(self) -> bool:
        # Check both; but primarily the primary
        return await self.primary.health_check() or await self.fallback.health_check()
