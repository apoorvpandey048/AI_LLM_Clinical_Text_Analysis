"""
Base Layer class for SNAP-AI pipeline.

All layers inherit from this base class.
"""

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
        Execute the layer with optional streaming.

        Args:
            custom_prompt: Optional custom prompt to use instead of file
            on_token: Optional async callback for each generated token
            temperature_override: If set, overrides settings temperature (for per-case dynamic control)
            seed_override: If set, overrides settings seed. Use _UNSET sentinel to indicate "use config default".
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

            # Check for JSON parse failure
            if response.parsed_json is None:
                error_msg = "Failed to parse LLM response as JSON"
                logger.error(
                    "layer_json_parse_error",
                    layer=self.layer_name,
                    raw_response_excerpt=response.content[:500],
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
            is_valid, validation_error = self.validate_output(response.parsed_json)
            if not is_valid:
                logger.error(
                    "layer_validation_error",
                    layer=self.layer_name,
                    error=validation_error,
                )
                return LayerResult(
                    success=False,
                    output=response.parsed_json,
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
                output=response.parsed_json,
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
