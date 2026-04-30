"""
OpenRouter Client Implementation.

Used for connecting to the OpenRouter API.
"""

import json
import time
from typing import Any, Callable, Awaitable

import httpx

from app.config import get_settings
from app.llm.client import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)


class OpenRouterClient(LLMClient):
    """
    OpenRouter client for LLM inference.
    """

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-120b"):
        """
        Initialize OpenRouter client.

        Args:
            api_key: OpenRouter API Key
            model: Model name to use
        """
        self.api_key = api_key
        self.model = model
        self.chat_url = "https://openrouter.ai/api/v1/chat/completions"
        self.app_name = "SNAP-AI"
        self.app_url = "http://localhost:5173"  # or current app origin

    def _get_headers(self) -> dict:
        """Get required headers for OpenRouter API."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.app_url,
            "X-Title": self.app_name,
        }

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
        seed: int | None = None,
    ) -> LLMResponse:
        """
        Generate a response using OpenRouter.
        """
        start_time = time.time()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            payload["seed"] = seed

        logger.debug(
            "openrouter_request",
            model=self.model,
            prompt_length=len(prompt),
        )

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.post(
                    self.chat_url,
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)

            # Extract response
            choices = data.get("choices", [])
            output = ""
            if choices:
                output = choices[0].get("message", {}).get("content", "")

            usage = data.get("usage", {})
            tokens_input = usage.get("prompt_tokens", 0)
            tokens_output = usage.get("completion_tokens", 0)

            logger.info(
                "openrouter_success",
                duration_ms=duration_ms,
                tokens_output=tokens_output,
            )

            parsed_json = self._try_parse_json(output)

            return LLMResponse(
                content=output,
                parsed_json=parsed_json,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=True,
                error=None,
            )

        except httpx.HTTPStatusError as e:
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error("openrouter_http_error", error=error_msg)
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
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            logger.error("openrouter_error", error=str(e))
            return LLMResponse(
                content="",
                parsed_json=None,
                tokens_input=0,
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=str(e),
            )

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
        """
        Generate a response with token streaming using OpenRouter.
        """
        start_time = time.time()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if seed is not None:
            payload["seed"] = seed

        logger.debug(
            "openrouter_stream_request",
            model=self.model,
            prompt_length=len(prompt),
        )

        full_output = ""
        tokens_input = 0  # Not always returned in stream
        tokens_output = 0
        raw_chunks = []

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    self.chat_url,
                    headers=self._get_headers(),
                    json=payload,
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data_str)
                                raw_chunks.append(chunk)

                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        full_output += content
                                        tokens_output += 1
                                        if on_token:
                                            await on_token(content)
                                            
                                # Capture usage info if present in OpenRouter stream chunk
                                if chunk.get("usage"):
                                    tokens_input = chunk["usage"].get("prompt_tokens", 0)
                                    tokens_output = chunk["usage"].get("completion_tokens", tokens_output)
                                    
                            except json.JSONDecodeError:
                                logger.warning("openrouter_stream_decode_error", line=line)

            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)

            logger.info(
                "openrouter_stream_success",
                duration_ms=duration_ms,
                tokens_output=tokens_output,
            )

            parsed_json = self._try_parse_json(full_output)

            return LLMResponse(
                content=full_output,
                parsed_json=parsed_json,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=True,
                error=None,
            )

        except httpx.HTTPStatusError as e:
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            
            # For HTTP errors during streaming setup
            error_text = ""
            try:
                await e.response.aread()
                error_text = e.response.text
            except:
                pass
                
            error_msg = f"HTTP {e.response.status_code}: {error_text}"
            logger.error("openrouter_stream_http_error", error=error_msg)
            return LLMResponse(
                content=full_output,
                parsed_json=None,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
            )
        except Exception as e:
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            logger.error("openrouter_stream_error", error=str(e))
            return LLMResponse(
                content=full_output,
                parsed_json=None,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=str(e),
            )

    async def health_check(self) -> bool:
        """
        Check if OpenRouter API is reachable.
        """
        try:
            # OpenRouter does not have a dedicated health endpoint for API keys, 
            # so we'll test with a minimal completion request if necessary, or just a GET.
            # But the requirement implies we should just test a minimal request.
            # However, for a general health check, we can just ping the models endpoint.
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.get("https://openrouter.ai/api/v1/models")
                return response.status_code == 200
        except Exception:
            return False
