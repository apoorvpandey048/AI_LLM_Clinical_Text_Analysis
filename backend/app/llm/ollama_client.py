"""
Ollama LLM Client Implementation.

Used for development with local Ollama instance.
"""

import time
from typing import Any

import httpx

from app.config import get_settings
from app.llm.client import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)


class OllamaClient(LLMClient):
    """
    Ollama client for local LLM inference.

    Uses Ollama's REST API for generation.
    """

    def __init__(self, host: str | None = None, model: str | None = None):
        """
        Initialize Ollama client.

        Args:
            host: Ollama server URL (default from config)
            model: Model name to use (default from config)
        """
        settings = get_settings()
        self.host = host or settings.ollama_host
        self.model = model or settings.ollama_model
        self.api_url = f"{self.host}/api/generate"

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
    ) -> LLMResponse:
        """
        Generate a response using Ollama.

        Args:
            prompt: The user prompt to send
            system_prompt: Optional system prompt for context
            temperature: Sampling temperature (0.0 = deterministic)
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds

        Returns:
            LLMResponse with the result
        """
        start_time = time.time()

        # Build the full prompt
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        # Request payload
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "format": "json",  # Request JSON output
        }

        logger.debug(
            "ollama_request",
            model=self.model,
            prompt_length=len(full_prompt),
            temperature=temperature,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.api_url, json=payload)
                response.raise_for_status()

            result = response.json()
            duration_ms = int((time.time() - start_time) * 1000)

            content = result.get("response", "")
            parsed_json = self._try_parse_json(content)

            # Extract token counts from Ollama response
            tokens_input = result.get("prompt_eval_count", 0)
            tokens_output = result.get("eval_count", 0)

            logger.info(
                "ollama_response",
                model=self.model,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                json_parsed=parsed_json is not None,
            )

            return LLMResponse(
                content=content,
                parsed_json=parsed_json,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=True,
            )

        except httpx.TimeoutException as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama request timed out after {timeout}s"
            logger.error("ollama_timeout", error=error_msg, duration_ms=duration_ms)

            return LLMResponse(
                content="",
                parsed_json=None,
                tokens_input=0,
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
            )

        except httpx.HTTPStatusError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama HTTP error: {e.response.status_code}"
            logger.error(
                "ollama_http_error",
                status_code=e.response.status_code,
                error=str(e),
            )

            return LLMResponse(
                content="",
                parsed_json=None,
                tokens_input=0,
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama error: {str(e)}"
            logger.error("ollama_error", error=str(e))

            return LLMResponse(
                content="",
                parsed_json=None,
                tokens_input=0,
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
            )

    async def health_check(self) -> bool:
        """
        Check if Ollama service is available.

        Returns:
            True if service is healthy
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/api/tags")
                return response.status_code == 200
        except Exception as e:
            logger.warning("ollama_health_check_failed", error=str(e))
            return False

    async def list_models(self) -> list[str]:
        """
        List available models in Ollama.

        Returns:
            List of model names
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/api/tags")
                response.raise_for_status()
                data = response.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.warning("ollama_list_models_failed", error=str(e))
            return []
