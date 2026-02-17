"""
Abstract LLM Client Interface.

Defines the contract for LLM clients (Ollama, vLLM).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Awaitable


@dataclass
class LLMResponse:
    """
    Standardized response from LLM inference.

    Attributes:
        content: The raw text response from the LLM
        parsed_json: Parsed JSON if response was valid JSON, else None
        tokens_input: Number of input tokens used
        tokens_output: Number of output tokens generated
        model: Model name used for inference
        duration_ms: Inference duration in milliseconds
        success: Whether the inference succeeded
        error: Error message if failed
    """

    content: str
    parsed_json: dict | None
    tokens_input: int
    tokens_output: int
    model: str
    duration_ms: int
    success: bool
    error: str | None = None


class LLMClient(ABC):
    """
    Abstract base class for LLM clients.

    Implementations must provide the generate method.
    """

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
    ) -> LLMResponse:
        """
        Generate a response from the LLM.

        Args:
            prompt: The user prompt to send
            system_prompt: Optional system prompt for context
            temperature: Sampling temperature (0.0 = deterministic)
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds

        Returns:
            LLMResponse with the result

        Raises:
            No exceptions - errors are captured in LLMResponse
        """
        pass

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
        on_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """
        Generate a response with streaming tokens.

        Default implementation falls back to non-streaming generate().
        Subclasses can override for true streaming support.

        Args:
            prompt: The user prompt to send
            system_prompt: Optional system prompt for context
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds
            on_token: Async callback invoked with each token chunk

        Returns:
            LLMResponse with the complete result
        """
        # Default: fall back to non-streaming
        return await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the LLM service is available.

        Returns:
            True if service is healthy, False otherwise
        """
        pass

    def _try_parse_json(self, content: str) -> dict | None:
        """
        Attempt to parse JSON from LLM response.

        Handles cases where JSON is wrapped in markdown code blocks.

        Args:
            content: Raw LLM response text

        Returns:
            Parsed JSON dict or None if parsing fails
        """
        import json
        import re

        if not content:
            return None

        # Try direct parse first
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to extract JSON from markdown code blocks
        patterns = [
            r"```json\s*([\s\S]*?)\s*```",  # ```json ... ```
            r"```\s*([\s\S]*?)\s*```",  # ``` ... ```
            r"\{[\s\S]*\}",  # Raw JSON object
        ]

        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                try:
                    json_str = match.group(1) if match.lastindex else match.group(0)
                    return json.loads(json_str)
                except (json.JSONDecodeError, IndexError):
                    continue

        return None
