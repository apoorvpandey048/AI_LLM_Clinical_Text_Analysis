"""
Ollama LLM Client Implementation.

Used for development with local Ollama instance.
Supports both standard and streaming generation.
"""

import json
import time
from typing import Any, Callable, Awaitable

import httpx

from app.config import get_settings
from app.llm.client import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)


class OllamaClient(LLMClient):
    """
    Ollama client for local LLM inference.

    Uses Ollama's REST API for generation with optional streaming.
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

    def set_model(self, model: str) -> None:
        """Change the active model at runtime."""
        self.model = model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
    ) -> LLMResponse:
        """
        Generate a response using Ollama (non-streaming).

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

        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "format": "json",
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

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama request timed out after {timeout}s"
            logger.error("ollama_timeout", error=error_msg, duration_ms=duration_ms)
            return LLMResponse(
                content="", parsed_json=None, tokens_input=0, tokens_output=0,
                model=self.model, duration_ms=duration_ms, success=False, error=error_msg,
            )

        except httpx.HTTPStatusError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama HTTP error: {e.response.status_code}"
            logger.error("ollama_http_error", status_code=e.response.status_code, error=str(e))
            return LLMResponse(
                content="", parsed_json=None, tokens_input=0, tokens_output=0,
                model=self.model, duration_ms=duration_ms, success=False, error=error_msg,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama error: {str(e)}"
            logger.error("ollama_error", error=str(e))
            return LLMResponse(
                content="", parsed_json=None, tokens_input=0, tokens_output=0,
                model=self.model, duration_ms=duration_ms, success=False, error=error_msg,
            )

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
        Generate a response with real-time token streaming.

        Streams tokens from Ollama and calls on_token callback for each chunk.
        Returns the complete LLMResponse when finished.

        Args:
            prompt: The user prompt to send
            system_prompt: Optional system prompt for context
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds
            on_token: Async callback invoked with each token string

        Returns:
            LLMResponse with the complete result
        """
        start_time = time.time()

        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "format": "json",
        }

        logger.debug(
            "ollama_stream_request",
            model=self.model,
            prompt_length=len(full_prompt),
            temperature=temperature,
        )

        accumulated_content = ""
        tokens_input = 0
        tokens_output = 0

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
                async with client.stream("POST", self.api_url, json=payload) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        token = chunk.get("response", "")
                        if token and on_token:
                            await on_token(token)

                        accumulated_content += token

                        # Final chunk contains token counts
                        if chunk.get("done", False):
                            tokens_input = chunk.get("prompt_eval_count", 0)
                            tokens_output = chunk.get("eval_count", 0)

            duration_ms = int((time.time() - start_time) * 1000)
            parsed_json = self._try_parse_json(accumulated_content)

            logger.info(
                "ollama_stream_response",
                model=self.model,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                duration_ms=duration_ms,
                json_parsed=parsed_json is not None,
                content_length=len(accumulated_content),
            )

            return LLMResponse(
                content=accumulated_content,
                parsed_json=parsed_json,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=True,
            )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama stream timed out after {timeout}s"
            logger.error("ollama_stream_timeout", error=error_msg, duration_ms=duration_ms)
            return LLMResponse(
                content=accumulated_content, parsed_json=None, tokens_input=0,
                tokens_output=0, model=self.model, duration_ms=duration_ms,
                success=False, error=error_msg,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Ollama stream error: {str(e)}"
            logger.error("ollama_stream_error", error=str(e))
            return LLMResponse(
                content=accumulated_content, parsed_json=None, tokens_input=0,
                tokens_output=0, model=self.model, duration_ms=duration_ms,
                success=False, error=error_msg,
            )

    async def health_check(self) -> bool:
        """Check if Ollama service is available."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/api/tags")
                return response.status_code == 200
        except Exception as e:
            logger.warning("ollama_health_check_failed", error=str(e))
            return False

    async def list_models(self) -> list[dict]:
        """
        List available models in Ollama with details.

        Returns:
            List of model info dicts with name, size, modified_at
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/api/tags")
                response.raise_for_status()
                data = response.json()
                models = []
                for m in data.get("models", []):
                    models.append({
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "size_gb": round(m.get("size", 0) / (1024**3), 1),
                        "modified_at": m.get("modified_at", ""),
                        "digest": m.get("digest", "")[:12],
                        "family": m.get("details", {}).get("family", ""),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                    })
                return models
        except Exception as e:
            logger.warning("ollama_list_models_failed", error=str(e))
            return []

    async def pull_model(self, model_name: str, on_progress: Callable | None = None) -> bool:
        """
        Pull a model from Ollama registry.

        Args:
            model_name: Name of the model to pull (e.g. 'llama3:8b')
            on_progress: Optional callback for progress updates

        Returns:
            True if pull succeeded
        """
        try:
            logger.info("ollama_pull_start", model=model_name)
            async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.host}/api/pull",
                    json={"name": model_name, "stream": True},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            if on_progress and "status" in data:
                                await on_progress(data)
                        except json.JSONDecodeError:
                            continue

            logger.info("ollama_pull_complete", model=model_name)
            return True

        except Exception as e:
            logger.error("ollama_pull_failed", model=model_name, error=str(e))
            return False

    async def delete_model(self, model_name: str) -> bool:
        """
        Delete a model from Ollama.

        Args:
            model_name: Name of the model to delete

        Returns:
            True if deletion succeeded
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{self.host}/api/delete",
                    json={"name": model_name},
                )
                return response.status_code == 200
        except Exception as e:
            logger.error("ollama_delete_failed", model=model_name, error=str(e))
            return False

    async def model_info(self, model_name: str) -> dict | None:
        """Get detailed info about a specific model."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{self.host}/api/show",
                    json={"name": model_name},
                )
                if response.status_code == 200:
                    return response.json()
                return None
        except Exception as e:
            logger.warning("ollama_model_info_failed", model=model_name, error=str(e))
            return None
