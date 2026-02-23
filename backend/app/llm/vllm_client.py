"""
vLLM Client Implementation.

Used for production deployment with vLLM server.
vLLM provides OpenAI-compatible API for high-performance inference.
"""

import json
import time
from typing import Any, Callable, Awaitable

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
        self.reasoning_level = settings.llm_reasoning_level

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
            # Add reasoning level hint for GPT-OSS models
            reasoning_hint = f"\nReasoning: {self.reasoning_level}" if "gpt-oss" in self.model.lower() else ""
            messages.append({"role": "system", "content": system_prompt + reasoning_hint})
        messages.append({"role": "user", "content": prompt})

        # Build request — GPT-OSS compatibility mode
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # GPT-OSS / vLLM: strip fields that cause HTTP 400
        if "gpt-oss" in self.model.lower():
            # Do NOT send response_format, tools, tool_choice — vLLM rejects them
            payload.pop("response_format", None)
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload.pop("parallel_tool_calls", None)
        else:
            payload["response_format"] = {"type": "json_object"}

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
            reasoning_content = ""
            if "choices" in result and len(result["choices"]) > 0:
                message = result["choices"][0].get("message", {})
                raw_content = message.get("content")
                raw_reasoning = message.get("reasoning")

                # For reasoning models (GPT-OSS), content can be null
                # while actual output is in the reasoning field
                content = raw_content if raw_content else ""
                reasoning_content = raw_reasoning if raw_reasoning else ""

                if not content and reasoning_content:
                    logger.warning(
                        "vllm_content_null_using_reasoning",
                        model=self.model,
                        reasoning_length=len(reasoning_content),
                        finish_reason=result["choices"][0].get("finish_reason"),
                    )
                    # Try using reasoning as the content source
                    content = reasoning_content

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
                has_content=bool(raw_content) if "choices" in result and result["choices"] else False,
                has_reasoning=bool(raw_reasoning) if "choices" in result and result["choices"] else False,
                content_length=len(content),
                finish_reason=result["choices"][0].get("finish_reason") if "choices" in result and result["choices"] else None,
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

    # ------------------------------------------------------------------
    # Streaming support (OpenAI SSE format)
    # ------------------------------------------------------------------

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
        Generate a response with real-time token streaming via SSE.

        Streams tokens from vLLM's OpenAI-compatible streaming endpoint
        and calls on_token for each chunk.  Falls back to non-streaming
        if the stream encounters errors.

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

        # Build messages
        messages = []
        if system_prompt:
            reasoning_hint = (
                f"\nReasoning: {self.reasoning_level}"
                if "gpt-oss" in self.model.lower()
                else ""
            )
            messages.append({"role": "system", "content": system_prompt + reasoning_hint})
        messages.append({"role": "user", "content": prompt})

        # Payload — GPT-OSS safe
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if "gpt-oss" in self.model.lower():
            payload.pop("response_format", None)
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        else:
            payload["response_format"] = {"type": "json_object"}

        logger.debug(
            "vllm_stream_request",
            model=self.model,
            prompt_length=len(prompt),
            temperature=temperature,
        )

        accumulated_content = ""
        accumulated_reasoning = ""
        finish_reason = None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=30.0)
            ) as client:
                async with client.stream(
                    "POST", self.chat_url, json=payload
                ) as response:
                    response.raise_for_status()

                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue

                        # SSE format: "data: {...}" or "data: [DONE]"
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()

                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        token = delta.get("content") or ""
                        reasoning_token = delta.get("reasoning") or ""

                        if token:
                            accumulated_content += token
                            if on_token:
                                await on_token(token)

                        if reasoning_token:
                            accumulated_reasoning += reasoning_token
                            # Optionally stream reasoning tokens too
                            if on_token and not token:
                                await on_token(reasoning_token)

                        fr = choices[0].get("finish_reason")
                        if fr:
                            finish_reason = fr

            duration_ms = int((time.time() - start_time) * 1000)

            # GPT-OSS: content may be null; fall back to reasoning
            content = accumulated_content
            if not content and accumulated_reasoning:
                logger.warning(
                    "vllm_stream_content_null_using_reasoning",
                    model=self.model,
                    reasoning_length=len(accumulated_reasoning),
                    finish_reason=finish_reason,
                )
                content = accumulated_reasoning

            parsed_json = self._try_parse_json(content)

            logger.info(
                "vllm_stream_response",
                model=self.model,
                duration_ms=duration_ms,
                content_length=len(content),
                json_parsed=parsed_json is not None,
                finish_reason=finish_reason,
            )

            return LLMResponse(
                content=content,
                parsed_json=parsed_json,
                tokens_input=0,  # usage not available in streaming
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=True,
            )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"vLLM stream timed out after {timeout}s"
            logger.error("vllm_stream_timeout", error=error_msg, duration_ms=duration_ms)
            return LLMResponse(
                content=accumulated_content,
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
            error_msg = f"vLLM stream error: {str(e)}"
            logger.error("vllm_stream_error", error=str(e))
            return LLMResponse(
                content=accumulated_content,
                parsed_json=None,
                tokens_input=0,
                tokens_output=0,
                model=self.model,
                duration_ms=duration_ms,
                success=False,
                error=error_msg,
            )

    async def list_models(self) -> list[str]:
        """
        List available models in vLLM.

        Returns:
            List of model names
        
        Raises:
            httpx.ConnectError: if vLLM server is unreachable
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.host}/v1/models")
                response.raise_for_status()
                data = response.json()
                return [m["id"] for m in data.get("data", [])]
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.error("vllm_unreachable", host=self.host, error=str(e))
            raise
        except Exception as e:
            logger.warning("vllm_list_models_failed", host=self.host, error=str(e))
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
