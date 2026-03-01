"""
Pipeline Orchestrator.

Coordinates the 3-layer pipeline execution with optional streaming.
Supports additional custom layers that run after the built-in pipeline.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.llm import LLMClient
from app.pipeline.layer1_ctp import Layer1CTP
from app.pipeline.layer2_cie import Layer2CIE
from app.pipeline.layer3_ccc import Layer3CCC
from app.pipeline.base import LayerResult
from app.utils import get_logger

logger = get_logger(__name__)


@dataclass
class PipelineResult:
    """
    Complete pipeline execution result.

    Attributes:
        success: Whether all layers completed successfully
        layer1_result: Layer 1 (CTP) result
        layer2_result: Layer 2 (CIE) result
        layer3_result: Layer 3 (CCC) result
        extra_layer_results: Dict of {layer_name: LayerResult} for custom layers
        total_duration_ms: Total processing time
        total_tokens_input: Total input tokens across all layers
        total_tokens_output: Total output tokens across all layers
        final_verdict: Layer 3 verdict (if available)
        final_cci: Final CCI value (from Layer 2 or 3)
        error: Error message if pipeline failed
    """

    success: bool
    layer1_result: LayerResult | None
    layer2_result: LayerResult | None
    layer3_result: LayerResult | None
    extra_layer_results: dict[str, LayerResult] = field(default_factory=dict)
    total_duration_ms: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    final_verdict: str | None = None
    final_cci: float | None = None
    error: str | None = None


class PipelineOrchestrator:
    """
    Orchestrates the 3-layer SNAP-AI pipeline.

    Executes layers sequentially, passing outputs between them.
    Supports streaming token callbacks and custom prompts per layer.
    """

    def __init__(self, llm_client: LLMClient):
        """
        Initialize the orchestrator.

        Args:
            llm_client: The LLM client to use for all layers
        """
        self.llm_client = llm_client
        self.layer1 = Layer1CTP(llm_client)
        self.layer2 = Layer2CIE(llm_client)
        self.layer3 = Layer3CCC(llm_client)

    async def execute(
        self,
        raw_text: str,
        custom_prompts: dict[str, str] | None = None,
        extra_layers: list[dict] | None = None,
        on_token: Callable[[str, str], Awaitable[None]] | None = None,
        on_layer_start: Callable[[str], Awaitable[None]] | None = None,
        on_layer_complete: Callable[[str, bool, int], Awaitable[None]] | None = None,
        temperature: float | None = None,
    ) -> PipelineResult:
        """
        Execute the complete pipeline (3 built-in layers + optional custom layers).

        Args:
            raw_text: The raw clinical text to process
            custom_prompts: Optional dict of {layer_name: prompt_text} for built-in layers
            extra_layers: Optional list of dicts [{layer_name, prompt}] for custom layers
            on_token: Async callback (layer_name, token) for streaming
            on_layer_start: Async callback (layer_name) when layer begins
            on_layer_complete: Async callback (layer_name, success, duration_ms)
            temperature: Runtime temperature override (read from Redis per case)

        Returns:
            PipelineResult with all layer outputs
        """
        logger.info("pipeline_start", text_length=len(raw_text), temperature=temperature)

        total_duration_ms = 0
        total_tokens_input = 0
        total_tokens_output = 0
        custom_prompts = custom_prompts or {}

        # Helper to create layer-specific token callback
        def make_token_cb(layer_name: str):
            if on_token is None:
                return None
            async def cb(token: str):
                await on_token(layer_name, token)
            return cb

        # ====== Layer 1: CTP ======
        if on_layer_start:
            await on_layer_start("layer1_ctp")
        logger.info("LAYER_START", event="LAYER_START", layer="layer1_ctp")

        try:
            layer1_result = await self.layer1.execute(
                raw_text=raw_text,
                custom_prompt=custom_prompts.get("layer1_ctp"),
                on_token=make_token_cb("layer1_ctp"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", event="PIPELINE_LAYER_CRASH", layer="layer1_ctp")
            raise

        total_duration_ms += layer1_result.duration_ms
        total_tokens_input += layer1_result.tokens_input
        total_tokens_output += layer1_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer1_ctp", layer1_result.success, layer1_result.duration_ms)

        logger.info("LAYER_END", event="LAYER_END", layer="layer1_ctp", success=layer1_result.success)
        if not layer1_result.success:
            logger.error("pipeline_layer1_failed", event="PIPELINE_LAYER_CRASH", layer="layer1_ctp", error=layer1_result.error)
            return PipelineResult(
                success=False,
                layer1_result=layer1_result,
                layer2_result=None,
                layer3_result=None,
                total_duration_ms=total_duration_ms,
                total_tokens_input=total_tokens_input,
                total_tokens_output=total_tokens_output,
                final_verdict=None,
                final_cci=None,
                error=f"Layer 1 failed: {layer1_result.error}",
            )

        # Extract clean text for Layer 2
        clean_text = layer1_result.output.get("clean_course_text", "")

        # ====== Layer 2: CIE ======
        if on_layer_start:
            await on_layer_start("layer2_cie")
        logger.info("LAYER_START", event="LAYER_START", layer="layer2_cie")

        try:
            layer2_result = await self.layer2.execute(
                clean_text=clean_text,
                custom_prompt=custom_prompts.get("layer2_cie"),
                on_token=make_token_cb("layer2_cie"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", event="PIPELINE_LAYER_CRASH", layer="layer2_cie")
            raise

        total_duration_ms += layer2_result.duration_ms
        total_tokens_input += layer2_result.tokens_input
        total_tokens_output += layer2_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer2_cie", layer2_result.success, layer2_result.duration_ms)

        logger.info("LAYER_END", event="LAYER_END", layer="layer2_cie", success=layer2_result.success)
        if not layer2_result.success:
            logger.error("pipeline_layer2_failed", event="PIPELINE_LAYER_CRASH", layer="layer2_cie", error=layer2_result.error)
            return PipelineResult(
                success=False,
                layer1_result=layer1_result,
                layer2_result=layer2_result,
                layer3_result=None,
                total_duration_ms=total_duration_ms,
                total_tokens_input=total_tokens_input,
                total_tokens_output=total_tokens_output,
                final_verdict=None,
                final_cci=None,
                error=f"Layer 2 failed: {layer2_result.error}",
            )

        # ====== Layer 3: CCC ======
        if on_layer_start:
            await on_layer_start("layer3_ccc")
        logger.info("LAYER_START", event="LAYER_START", layer="layer3_ccc")

        try:
            layer3_result = await self.layer3.execute(
                clean_text=clean_text,
                layer2_output=layer2_result.output,
                custom_prompt=custom_prompts.get("layer3_ccc"),
                on_token=make_token_cb("layer3_ccc"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", event="PIPELINE_LAYER_CRASH", layer="layer3_ccc")
            raise

        total_duration_ms += layer3_result.duration_ms
        total_tokens_input += layer3_result.tokens_input
        total_tokens_output += layer3_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer3_ccc", layer3_result.success, layer3_result.duration_ms)

        logger.info("LAYER_END", event="LAYER_END", layer="layer3_ccc", success=layer3_result.success)
        if not layer3_result.success:
            logger.error("pipeline_layer3_failed", event="PIPELINE_LAYER_CRASH", layer="layer3_ccc", error=layer3_result.error)
            # Layer 3 failure is not fatal - we still have Layer 2 results
            return PipelineResult(
                success=True,  # Partial success - L1 and L2 completed
                layer1_result=layer1_result,
                layer2_result=layer2_result,
                layer3_result=layer3_result,
                total_duration_ms=total_duration_ms,
                total_tokens_input=total_tokens_input,
                total_tokens_output=total_tokens_output,
                final_verdict="LAYER3_FAILED",
                final_cci=layer2_result.output.get("cci_total"),
                error=f"Layer 3 failed: {layer3_result.error}",
            )

        # Extract final values
        final_verdict = layer3_result.output.get("verdict", "UNKNOWN")
        final_cci = layer2_result.output.get("cci_total", 0.0)

        # ====== Extra Custom Layers ======
        extra_layer_results = {}
        if extra_layers:
            extra_layer_results = await self._execute_extra_layers(
                extra_layers=extra_layers,
                context={
                    "raw_text": raw_text,
                    "clean_text": clean_text,
                    "layer1_output": layer1_result.output,
                    "layer2_output": layer2_result.output,
                    "layer3_output": layer3_result.output,
                },
                on_token=on_token,
                on_layer_start=on_layer_start,
                on_layer_complete=on_layer_complete,
                temperature=temperature,
            )
            for r in extra_layer_results.values():
                total_duration_ms += r.duration_ms
                total_tokens_input += r.tokens_input
                total_tokens_output += r.tokens_output

        logger.info(
            "pipeline_complete",
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            verdict=final_verdict,
            cci=final_cci,
            extra_layers=list(extra_layer_results.keys()),
        )

        return PipelineResult(
            success=True,
            layer1_result=layer1_result,
            layer2_result=layer2_result,
            layer3_result=layer3_result,
            extra_layer_results=extra_layer_results,
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            final_verdict=final_verdict,
            final_cci=final_cci,
            error=None,
        )

    async def _execute_extra_layers(
        self,
        extra_layers: list[dict],
        context: dict,
        on_token: Callable[[str, str], Awaitable[None]] | None = None,
        on_layer_start: Callable[[str], Awaitable[None]] | None = None,
        on_layer_complete: Callable[[str, bool, int], Awaitable[None]] | None = None,
        temperature: float | None = None,
    ) -> dict[str, LayerResult]:
        """
        Execute custom/extra layers sequentially.

        Each custom layer receives the accumulated pipeline context
        as JSON in the user prompt, with its custom prompt as the system prompt.

        Args:
            extra_layers: List of dicts with 'layer_name' and 'prompt' keys
            context: Accumulated pipeline context from built-in layers
            on_token: Token streaming callback
            on_layer_start: Layer start callback
            on_layer_complete: Layer complete callback
            temperature: Runtime temperature override

        Returns:
            Dict mapping layer_name to LayerResult
        """
        results = {}
        from app.config import get_settings
        config = get_settings()
        effective_temperature = temperature if temperature is not None else config.llm_temperature

        for layer_config in extra_layers:
            layer_name = layer_config.get("layer_name", "")
            system_prompt = layer_config.get("prompt", "")

            if not layer_name or not system_prompt:
                continue

            logger.info("extra_layer_start", layer=layer_name)
            if on_layer_start:
                await on_layer_start(layer_name)

            # Build context prompt for the custom layer
            user_prompt = (
                "Below is the clinical case data processed through the SNAP-AI pipeline.\n"
                "Use this context to perform your analysis.\n\n"
                f"## Original Clinical Text\n{context.get('raw_text', '')}\n\n"
                f"## Cleaned Text (Layer 1 Output)\n{context.get('clean_text', '')}\n\n"
                f"## Layer 2 (CIE) Output\n{json.dumps(context.get('layer2_output', {}), indent=2)}\n\n"
                f"## Layer 3 (CCC) Output\n{json.dumps(context.get('layer3_output', {}), indent=2)}\n"
            )

            # Add previous custom layer outputs for chaining
            for prev_name, prev_result in results.items():
                if prev_result.success and prev_result.output:
                    user_prompt += f"\n## {prev_name} Output\n{json.dumps(prev_result.output, indent=2)}\n"

            # Token callback for this layer
            token_cb = None
            if on_token:
                def _make_cb(ln):
                    async def cb(token: str):
                        await on_token(ln, token)
                    return cb
                token_cb = _make_cb(layer_name)

            try:
                if token_cb:
                    response = await self.llm_client.generate_stream(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=effective_temperature,
                        max_tokens=config.llm_max_tokens,
                        timeout=config.llm_timeout,
                        on_token=token_cb,
                    )
                else:
                    response = await self.llm_client.generate(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=effective_temperature,
                        max_tokens=config.llm_max_tokens,
                        timeout=config.llm_timeout,
                    )

                result = LayerResult(
                    success=response.success,
                    output=response.parsed_json,
                    raw_response=response.content,
                    tokens_input=response.tokens_input,
                    tokens_output=response.tokens_output,
                    duration_ms=response.duration_ms,
                    error=response.error,
                    layer_name=layer_name,
                    prompt_version="custom",
                )

            except Exception as e:
                logger.error("extra_layer_error", layer=layer_name, error=str(e))
                result = LayerResult(
                    success=False,
                    output=None,
                    raw_response="",
                    tokens_input=0,
                    tokens_output=0,
                    duration_ms=0,
                    error=str(e),
                    layer_name=layer_name,
                    prompt_version="custom",
                )

            results[layer_name] = result

            if on_layer_complete:
                await on_layer_complete(layer_name, result.success, result.duration_ms)

            logger.info(
                "extra_layer_complete",
                layer=layer_name,
                success=result.success,
                duration_ms=result.duration_ms,
            )

        return results
