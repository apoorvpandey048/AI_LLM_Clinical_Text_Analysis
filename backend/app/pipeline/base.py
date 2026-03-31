"""
Base Layer class for SNAP-AI pipeline.

All layers inherit from this base class.
"""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

from app.config import get_settings
from app.llm import LLMClient, LLMResponse
from app.utils import get_logger

logger = get_logger(__name__)

# Sentinel to distinguish "not passed" from "None" for seed_override
_UNSET = object()

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAYS = [0, 1, 3]  # seconds before each retry


def repair_json(raw: str) -> dict | None:
    """
    Attempt to repair common LLM JSON errors.

    Handles: markdown fences, trailing commas, unclosed braces/brackets.
    Returns parsed dict on success, None on failure.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip().lstrip('\ufeff')

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fix trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # Try again after trailing comma fix
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fix unclosed braces/brackets
    open_braces = text.count('{') - text.count('}')
    if open_braces > 0:
        text += '}' * open_braces

    open_brackets = text.count('[') - text.count(']')
    if open_brackets > 0:
        text += ']' * open_brackets

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


@dataclass
class LayerResult:
    """
    Result from a pipeline layer.

    Attributes:
        success: Whether the layer executed successfully
        output: The parsed JSON output (if successful)
        raw_response: The raw LLM response text
        tokens_input: Input tokens used
        tokens_output: Output tokens generated
        duration_ms: Processing time in milliseconds
        error: Error message (if failed)
        layer_name: Name of the layer
        prompt_version: Version of the prompt used
    """

    success: bool
    output: dict | None
    raw_response: str
    tokens_input: int
    tokens_output: int
    duration_ms: int
    error: str | None
    layer_name: str
    prompt_version: str


class BaseLayer(ABC):
    """
    Abstract base class for pipeline layers.

    Each layer reads its prompt from a versioned file and
    executes it against the LLM client.
    """

    def __init__(self, llm_client: LLMClient):
        """
        Initialize the layer.

        Args:
            llm_client: The LLM client to use for inference
        """
        self.llm_client = llm_client
        self.settings = get_settings()
        self._prompt_cache: str | None = None

    @property
    @abstractmethod
    def layer_name(self) -> str:
        """Layer identifier (e.g., 'layer1_ctp')."""
        pass

    @property
    @abstractmethod
    def prompt_filename(self) -> str:
        """Prompt file name (e.g., 'layer1_ctp_v1.3.md')."""
        pass

    def get_prompt_path(self) -> Path:
        """Get the full path to the prompt file."""
        # Prompts are stored in /app/prompts/snapai/
        base_path = Path("/app/prompts/snapai")
        return base_path / self.prompt_filename

    def load_prompt(self, custom_prompt: str | None = None) -> str:
        """
        Load the prompt, preferring custom prompt if provided.

        Args:
            custom_prompt: Optional custom prompt from user/DB

        Returns:
            The prompt text

        Raises:
            FileNotFoundError: If prompt file doesn't exist
        """
        # Use custom prompt if provided
        if custom_prompt:
            logger.info("using_custom_prompt", layer=self.layer_name)
            return custom_prompt

        if self._prompt_cache is not None:
            return self._prompt_cache

        prompt_path = self.get_prompt_path()

        if not prompt_path.exists():
            # Try relative path for development
            dev_path = Path(__file__).parent.parent.parent.parent / "prompts" / "snapai" / self.prompt_filename
            if dev_path.exists():
                prompt_path = dev_path
            else:
                raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

        self._prompt_cache = prompt_path.read_text(encoding="utf-8")
        logger.debug("prompt_loaded", layer=self.layer_name, path=str(prompt_path))
        return self._prompt_cache

    def clear_prompt_cache(self) -> None:
        """Clear the cached prompt to force reload."""
        self._prompt_cache = None

    @abstractmethod
    def build_user_prompt(self, **kwargs) -> str:
        """
        Build the user prompt with input data.

        Args:
            **kwargs: Layer-specific input data

        Returns:
            The complete user prompt
        """
        pass

    @abstractmethod
    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate the layer output against expected schema.

        Args:
            output: The parsed JSON output

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass

    async def execute(
        self,
        custom_prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        temperature_override: float | None = None,
        seed_override: int | None = _UNSET,
        **kwargs,
    ) -> LayerResult:
        """
        Execute the layer with retry logic for JSON/validation failures.

        Retries up to MAX_RETRIES times on recoverable errors (JSON parse
        failures, validation errors).  Non-recoverable errors (missing
        prompt file) fail immediately.

        Args:
            custom_prompt: Optional custom prompt to use instead of file
            on_token: Optional async callback for each generated token
            temperature_override: If set, overrides settings temperature
            seed_override: If set, overrides settings seed
            **kwargs: Layer-specific input data

        Returns:
            LayerResult with the execution result
        """
        last_result = None

        for attempt in range(MAX_RETRIES):
            if attempt > 0:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.warning(
                    "layer_retry",
                    layer=self.layer_name,
                    attempt=attempt + 1,
                    max_retries=MAX_RETRIES,
                    delay_s=delay,
                    last_error=last_result.error if last_result else None,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

            result = await self._execute_once(
                custom_prompt=custom_prompt,
                on_token=on_token,
                temperature_override=temperature_override,
                seed_override=seed_override,
                **kwargs,
            )

            if result.success:
                if attempt > 0:
                    logger.info(
                        "layer_retry_succeeded",
                        layer=self.layer_name,
                        attempt=attempt + 1,
                    )
                return result

            last_result = result

            # Don't retry non-recoverable errors
            if result.error and "Prompt file not found" in result.error:
                return result
            if result.error and "Unexpected error" in result.error:
                return result

            # Recoverable: JSON parse failure, validation failure → retry
            logger.warning(
                "layer_attempt_failed",
                layer=self.layer_name,
                attempt=attempt + 1,
                error=result.error,
            )

        # All retries exhausted
        logger.error(
            "layer_all_retries_exhausted",
            layer=self.layer_name,
            attempts=MAX_RETRIES,
            final_error=last_result.error if last_result else None,
        )
        return last_result

    async def _execute_once(
        self,
        custom_prompt: str | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        temperature_override: float | None = None,
        seed_override: int | None = _UNSET,
        **kwargs,
    ) -> LayerResult:
        """
        Execute a single LLM call attempt.

        Args:
            custom_prompt: Optional custom prompt to use instead of file
            on_token: Optional async callback for each generated token
            temperature_override: If set, overrides settings temperature
            seed_override: If set, overrides settings seed
            **kwargs: Layer-specific input data

        Returns:
            LayerResult with the execution result
        """
        # Resolve temperature: per-case override > settings default
        effective_temperature = temperature_override if temperature_override is not None else self.settings.llm_temperature
        # Resolve seed: per-case override > settings default (None = no seed / non-deterministic)
        effective_seed = seed_override if seed_override is not _UNSET else self.settings.llm_seed

        logger.info("layer_execution_start", layer=self.layer_name, streaming=on_token is not None, temperature=effective_temperature, seed=effective_seed)

        try:
            # Load system prompt (custom or file)
            system_prompt = self.load_prompt(custom_prompt=custom_prompt)

            # Build user prompt
            user_prompt = self.build_user_prompt(**kwargs)

            # Execute LLM (streaming or non-streaming)
            if on_token is not None:
                response: LLMResponse = await self.llm_client.generate_stream(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=effective_temperature,
                    max_tokens=self.settings.llm_max_tokens,
                    timeout=self.settings.llm_timeout,
                    on_token=on_token,
                    seed=effective_seed,
                )
            else:
                response: LLMResponse = await self.llm_client.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=effective_temperature,
                    max_tokens=self.settings.llm_max_tokens,
                    timeout=self.settings.llm_timeout,
                    seed=effective_seed,
                )

            # Check for LLM errors
            if not response.success:
                logger.error(
                    "layer_llm_error",
                    layer=self.layer_name,
                    error=response.error,
                )
                return LayerResult(
                    success=False,
                    output=None,
                    raw_response=response.reasoning or response.content,
                    tokens_input=response.tokens_input,
                    tokens_output=response.tokens_output,
                    duration_ms=response.duration_ms,
                    error=response.error,
                    layer_name=self.layer_name,
                    prompt_version=self.settings.prompt_version,
                )

            # Check for JSON parse failure — try repair before giving up
            parsed = response.parsed_json
            if parsed is None:
                logger.warning(
                    "layer_json_parse_failed_trying_repair",
                    layer=self.layer_name,
                    raw_response_excerpt=response.content[:300] if response.content else "",
                )
                parsed = repair_json(response.content)
                if parsed is not None:
                    logger.info("layer_json_repair_succeeded", layer=self.layer_name)
                else:
                    error_msg = "Failed to parse LLM response as JSON (repair also failed)"
                    logger.error(
                        "layer_json_parse_error",
                        layer=self.layer_name,
                        raw_response_excerpt=response.content[:500] if response.content else "",
                    )
                    return LayerResult(
                        success=False,
                        output=None,
                        raw_response=response.reasoning or response.content,
                        tokens_input=response.tokens_input,
                        tokens_output=response.tokens_output,
                        duration_ms=response.duration_ms,
                        error=error_msg,
                        layer_name=self.layer_name,
                        prompt_version=self.settings.prompt_version,
                    )

            # Validate output schema
            is_valid, validation_error = self.validate_output(parsed)
            if not is_valid:
                logger.error(
                    "layer_validation_error",
                    layer=self.layer_name,
                    error=validation_error,
                )
                return LayerResult(
                    success=False,
                    output=parsed,
                    raw_response=response.reasoning or response.content,
                    tokens_input=response.tokens_input,
                    tokens_output=response.tokens_output,
                    duration_ms=response.duration_ms,
                    error=validation_error,
                    layer_name=self.layer_name,
                    prompt_version=self.settings.prompt_version,
                )

            # Success
            logger.info(
                "layer_execution_complete",
                layer=self.layer_name,
                tokens_input=response.tokens_input,
                tokens_output=response.tokens_output,
                duration_ms=response.duration_ms,
            )

            # Prefer reasoning text (model's thinking chain) as raw output
            # when available; otherwise fall back to the raw content string
            raw_response_text = response.reasoning or response.content

            return LayerResult(
                success=True,
                output=parsed,
                raw_response=raw_response_text,
                tokens_input=response.tokens_input,
                tokens_output=response.tokens_output,
                duration_ms=response.duration_ms,
                error=None,
                layer_name=self.layer_name,
                prompt_version=self.settings.prompt_version,
            )

        except FileNotFoundError as e:
            error_msg = f"Prompt file not found: {e}"
            logger.error("layer_prompt_not_found", layer=self.layer_name, error=str(e))
            return LayerResult(
                success=False,
                output=None,
                raw_response="",
                tokens_input=0,
                tokens_output=0,
                duration_ms=0,
                error=error_msg,
                layer_name=self.layer_name,
                prompt_version=self.settings.prompt_version,
            )

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error("layer_unexpected_error", layer=self.layer_name, error=str(e))
            return LayerResult(
                success=False,
                output=None,
                raw_response="",
                tokens_input=0,
                tokens_output=0,
                duration_ms=0,
                error=error_msg,
                layer_name=self.layer_name,
                prompt_version=self.settings.prompt_version,
            )

