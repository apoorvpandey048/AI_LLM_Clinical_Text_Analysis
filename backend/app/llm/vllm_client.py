"""
vLLM Client Implementation.

Used for production deployment with vLLM server.
vLLM provides OpenAI-compatible API for high-performance inference.
"""

import time
from typing import Any

import httpx

from app.config import get_settings
from app.llm.client import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)


class VLLMClient(LLMClient):
    """
    vLLM client for production LLM inference.

    Uses vLLM's OpenAI-compatible API for generation.
    vLLM provides high-throughput serving with paged attention.
    """

    def __init__(self, host: str | None = None, model: str | None = None):
        """
        Initialize vLLM client.

        Args:
            host: vLLM server URL (default from config)
            model: Model name to use (default from config)
        """
        settings = get_settings()
        self.host = host or settings.vllm_host
        self.model = model or settings.vllm_model
        self.api_url = f"{self.host}/v1/completions"
        self.chat_url = f"{self.host}/v1/chat/completions"

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
    ) -> LLMResponse:
        """
        Generate a response using vLLM.

        Uses the chat completions API for better prompt handling.

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

        # Build messages for chat API
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Build request with JSON mode
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},  # Enable JSON mode
        }

        logger.debug(
            "vllm_request",
            model=self.model,
            prompt_length=len(prompt),
            system_prompt_length=len(system_prompt) if system_prompt else 0,
            temperature=temperature,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.chat_url, json=payload)
                response.raise_for_status()

            result = response.json()
            duration_ms = int((time.time() - start_time) * 1000)

            # Extract content from chat completions response
            content = ""
            if "choices" in result and len(result["choices"]) > 0:
                message = result["choices"][0].get("message", {})
                content = message.get("content", "")

            parsed_json = self._try_parse_json(content)

            # Extract token counts from usage
            usage = result.get("usage", {})
            tokens_input = usage.get("prompt_tokens", 0)
            tokens_output = usage.get("completion_tokens", 0)

            logger.info(
                "vllm_response",
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
            error_msg = f"vLLM request timed out after {timeout}s"
            logger.error("vllm_timeout", error=error_msg, duration_ms=duration_ms)

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
            error_msg = f"vLLM HTTP error: {e.response.status_code}"
            
            # Try to extract error details
            try:
                error_body = e.response.json()
                if "error" in error_body:
                    error_msg = f"{error_msg} - {error_body['error'].get('message', '')}"
            except Exception:
                pass

            logger.error(
                "vllm_http_error",
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
            error_msg = f"vLLM error: {str(e)}"
            logger.error("vllm_error", error=str(e))

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
        Check if vLLM service is available.

        Returns:
            True if service is healthy
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/v1/models")
                return response.status_code == 200
        except Exception as e:
            logger.warning("vllm_health_check_failed", error=str(e))
            return False

    async def list_models(self) -> list[str]:
        """
        List available models in vLLM.

        Returns:
            List of model names
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/v1/models")
                response.raise_for_status()
                data = response.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.warning("vllm_list_models_failed", error=str(e))
            return []

    async def get_model_info(self) -> dict[str, Any]:
        """
        Get information about the loaded model.

        Returns:
            Model information dict
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/v1/models")
                response.raise_for_status()
                data = response.json()
                models = data.get("data", [])
                for model in models:
                    if model["id"] == self.model:
                        return model
                return {}
        except Exception as e:
            logger.warning("vllm_get_model_info_failed", error=str(e))
            return {}
