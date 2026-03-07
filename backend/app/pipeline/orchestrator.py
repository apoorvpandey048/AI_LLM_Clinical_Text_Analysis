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
    Orchestrates the SNAP-AI pipeline.

    Supports two execution modes:
    1. Legacy: hardcoded L1→L2→L3 + optional extra_layers (backward compatible)
    2. Dynamic: executes layers from a sorted pipeline_layers config list

    Dynamic mode is used when a pipeline_snapshot is provided (Phase 2).
    """

    BUILTIN_LAYER_NAMES = frozenset({"layer1_ctp", "layer2_cie", "layer3_ccc"})

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
        pipeline_layers: list[dict] | None = None,
        on_token: Callable[[str, str], Awaitable[None]] | None = None,
        on_layer_start: Callable[[str], Awaitable[None]] | None = None,
        on_layer_complete: Callable[[str, bool, int], Awaitable[None]] | None = None,
        temperature: float | None = None,
    ) -> PipelineResult:
        """
        Execute the complete pipeline.

        If pipeline_layers is provided, uses dynamic execution mode (Phase 2):
            Iterates layers from the snapshot in order, supports custom + built-in.

        Otherwise, falls back to legacy mode:
            Hardcoded L1→L2→L3 + optional extra_layers appended at end.

        Args:
            raw_text: The raw clinical text to process
            custom_prompts: Optional dict of {layer_name: prompt_text} for built-in layers (legacy)
            extra_layers: Optional list of dicts [{layer_name, prompt}] for custom layers (legacy)
            pipeline_layers: Sorted list of layer configs from pipeline snapshot (dynamic mode)
            on_token: Async callback (layer_name, token) for streaming
            on_layer_start: Async callback (layer_name) when layer begins
            on_layer_complete: Async callback (layer_name, success, duration_ms)
            temperature: Runtime temperature override

        Returns:
            PipelineResult with all layer outputs
        """
        # ── Dynamic execution path (Phase 2) ──
        if pipeline_layers is not None:
            return await self._execute_dynamic(
                raw_text=raw_text,
                pipeline_layers=pipeline_layers,
                on_token=on_token,
                on_layer_start=on_layer_start,
                on_layer_complete=on_layer_complete,
                temperature=temperature,
            )

        # ── Legacy execution path (unchanged) ──
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
        logger.info("LAYER_START", layer="layer1_ctp")

        try:
            layer1_result = await self.layer1.execute(
                raw_text=raw_text,
                custom_prompt=custom_prompts.get("layer1_ctp"),
                on_token=make_token_cb("layer1_ctp"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer1_ctp")
            raise

        total_duration_ms += layer1_result.duration_ms
        total_tokens_input += layer1_result.tokens_input
        total_tokens_output += layer1_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer1_ctp", layer1_result.success, layer1_result.duration_ms)

        logger.info("LAYER_END", layer="layer1_ctp", success=layer1_result.success)
        if not layer1_result.success:
            logger.error("pipeline_layer1_failed", layer="layer1_ctp", error=layer1_result.error)
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
        logger.info("LAYER_START", layer="layer2_cie")

        try:
            layer2_result = await self.layer2.execute(
                clean_text=clean_text,
                custom_prompt=custom_prompts.get("layer2_cie"),
                on_token=make_token_cb("layer2_cie"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer2_cie")
            raise

        total_duration_ms += layer2_result.duration_ms
        total_tokens_input += layer2_result.tokens_input
        total_tokens_output += layer2_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer2_cie", layer2_result.success, layer2_result.duration_ms)

        logger.info("LAYER_END", layer="layer2_cie", success=layer2_result.success)
        if not layer2_result.success:
            logger.error("pipeline_layer2_failed", layer="layer2_cie", error=layer2_result.error)
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
        logger.info("LAYER_START", layer="layer3_ccc")

        try:
            layer3_result = await self.layer3.execute(
                clean_text=clean_text,
                layer2_output=layer2_result.output,
                custom_prompt=custom_prompts.get("layer3_ccc"),
                on_token=make_token_cb("layer3_ccc"),
                temperature_override=temperature,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer3_ccc")
            raise

        total_duration_ms += layer3_result.duration_ms
        total_tokens_input += layer3_result.tokens_input
        total_tokens_output += layer3_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer3_ccc", layer3_result.success, layer3_result.duration_ms)

        logger.info("LAYER_END", layer="layer3_ccc", success=layer3_result.success)
        if not layer3_result.success:
            logger.error("pipeline_layer3_failed", layer="layer3_ccc", error=layer3_result.error)
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

    # ================================================================
    # Dynamic Execution (Phase 2)
    # ================================================================

    async def _execute_dynamic(
        self,
        raw_text: str,
        pipeline_layers: list[dict],
        on_token: Callable[[str, str], Awaitable[None]] | None = None,
        on_layer_start: Callable[[str], Awaitable[None]] | None = None,
        on_layer_complete: Callable[[str, bool, int], Awaitable[None]] | None = None,
        temperature: float | None = None,
    ) -> PipelineResult:
        """
        Execute pipeline with dynamic layer ordering from a snapshot.

        Built-in layers use their specialised classes; custom layers use
        the generic LLM execution path.  All layers share an accumulated
        context dict so later layers can reference earlier outputs.

        Args:
            raw_text: Raw clinical text
            pipeline_layers: Sorted list of layer config dicts (from snapshot)
            on_token / on_layer_start / on_layer_complete: streaming callbacks
            temperature: Runtime temperature override

        Returns:
            PipelineResult (backward-compatible with legacy fields)
        """
        # Wrap on_layer_complete to accept optional error_message kwarg
        _on_layer_complete_orig = on_layer_complete

        async def _on_layer_complete_with_error(name: str, success: bool, duration_ms: int, error_message: str | None = None):
            if _on_layer_complete_orig:
                try:
                    await _on_layer_complete_orig(name, success, duration_ms, error_message=error_message)
                except TypeError:
                    await _on_layer_complete_orig(name, success, duration_ms)
        layer_count = len(pipeline_layers)
        logger.info(
            "pipeline_dynamic_start",
            text_length=len(raw_text),
            layer_count=layer_count,
            temperature=temperature,
        )

        total_duration_ms = 0
        total_tokens_input = 0
        total_tokens_output = 0

        # Backward-compat result slots
        layer1_result: LayerResult | None = None
        layer2_result: LayerResult | None = None
        layer3_result: LayerResult | None = None
        extra_results: dict[str, LayerResult] = {}

        # Accumulated context for custom layers
        context: dict[str, Any] = {
            "raw_text": raw_text,
            "clean_text": "",
        }

        # Helper to create layer-specific token callback
        def make_token_cb(layer_name: str):
            if on_token is None:
                return None
            async def cb(token: str):
                await on_token(layer_name, token)
            return cb

        error_msg: str | None = None

        for layer_cfg in pipeline_layers:
            name = layer_cfg["layer_name"]
            prompt = layer_cfg.get("prompt")
            is_builtin = layer_cfg.get("is_builtin", name in self.BUILTIN_LAYER_NAMES)

            if on_layer_start:
                await on_layer_start(name)
            logger.info("LAYER_START", layer=name)

            try:
                if is_builtin and name == "layer1_ctp":
                    result = await self.layer1.execute(
                        raw_text=raw_text,
                        custom_prompt=prompt,
                        on_token=make_token_cb(name),
                        temperature_override=temperature,
                    )
                    layer1_result = result
                    if result.success and result.output:
                        context["clean_text"] = result.output.get("clean_course_text", "")
                        context["layer1_output"] = result.output

                elif is_builtin and name == "layer2_cie":
                    result = await self.layer2.execute(
                        clean_text=context.get("clean_text", ""),
                        custom_prompt=prompt,
                        on_token=make_token_cb(name),
                        temperature_override=temperature,
                    )
                    layer2_result = result
                    if result.success and result.output:
                        context["layer2_output"] = result.output

                elif is_builtin and name == "layer3_ccc":
                    l2_out = context.get("layer2_output", {})
                    result = await self.layer3.execute(
                        clean_text=context.get("clean_text", ""),
                        layer2_output=l2_out,
                        custom_prompt=prompt,
                        on_token=make_token_cb(name),
                        temperature_override=temperature,
                    )
                    layer3_result = result
                    if result.success and result.output:
                        context["layer3_output"] = result.output

                else:
                    # Custom layer — generic LLM execution
                    result = await self._execute_single_custom_layer(
                        layer_name=name,
                        system_prompt=prompt or "",
                        context=context,
                        prev_custom_results=extra_results,
                        on_token=on_token,
                        temperature=temperature,
                    )
                    extra_results[name] = result

            except Exception:
                logger.exception("PIPELINE_LAYER_CRASH", layer=name)
                raise

            total_duration_ms += result.duration_ms
            total_tokens_input += result.tokens_input
            total_tokens_output += result.tokens_output

            if _on_layer_complete_orig:
                await _on_layer_complete_with_error(name, result.success, result.duration_ms, error_message=result.error if not result.success else None)
            logger.info("LAYER_END", layer=name, success=result.success)

            # Failure handling
            if not result.success:
                if is_builtin and name in ("layer1_ctp", "layer2_cie"):
                    # Fatal: L1/L2 failure stops pipeline
                    logger.error(
                        "pipeline_layer_failed",
                        layer=name,
                        error=result.error,
                    )
                    return PipelineResult(
                        success=False,
                        layer1_result=layer1_result,
                        layer2_result=layer2_result,
                        layer3_result=layer3_result,
                        extra_layer_results=extra_results,
                        total_duration_ms=total_duration_ms,
                        total_tokens_input=total_tokens_input,
                        total_tokens_output=total_tokens_output,
                        error=f"{name} failed: {result.error}",
                    )
                elif is_builtin and name == "layer3_ccc":
                    # Non-fatal — partial success
                    logger.error(
                        "pipeline_layer_failed",
                        layer=name,
                        error=result.error,
                    )
                    error_msg = f"Layer 3 failed: {result.error}"
                else:
                    # Custom layer failure — log and continue
                    logger.warning("custom_layer_failed", layer=name, error=result.error)

        # Derive final values
        final_verdict: str | None = None
        final_cci: float | None = None

        if layer3_result and layer3_result.success and layer3_result.output:
            final_verdict = layer3_result.output.get("verdict", "UNKNOWN")
        elif layer3_result and not layer3_result.success:
            final_verdict = "LAYER3_FAILED"

        if layer2_result and layer2_result.output:
            final_cci = layer2_result.output.get("cci_total", 0.0)

        # Pipeline is successful if at least L1+L2 succeeded
        success = True
        if layer1_result and not layer1_result.success:
            success = False
        if layer2_result and not layer2_result.success:
            success = False

        logger.info(
            "pipeline_dynamic_complete",
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            verdict=final_verdict,
            cci=final_cci,
            extra_layers=list(extra_results.keys()),
        )

        return PipelineResult(
            success=success,
            layer1_result=layer1_result,
            layer2_result=layer2_result,
            layer3_result=layer3_result,
            extra_layer_results=extra_results,
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            final_verdict=final_verdict,
            final_cci=final_cci,
            error=error_msg,
        )

    async def _execute_single_custom_layer(
        self,
        layer_name: str,
        system_prompt: str,
        context: dict,
        prev_custom_results: dict[str, LayerResult],
        on_token: Callable[[str, str], Awaitable[None]] | None = None,
        temperature: float | None = None,
    ) -> LayerResult:
        """
        Execute a single custom layer with the accumulated pipeline context.

        Args:
            layer_name: Layer identifier
            system_prompt: System prompt for this layer
            context: Accumulated context (raw_text, clean_text, layerN_output, ...)
            prev_custom_results: Results from earlier custom layers (for chaining)
            on_token: Token streaming callback
            temperature: Temperature override
        """
        from app.config import get_settings
        config = get_settings()
        effective_temperature = temperature if temperature is not None else config.llm_temperature

        # Build user prompt with pipeline context
        user_prompt = (
            "Below is the clinical case data processed through the SNAP-AI pipeline.\n"
            "Use this context to perform your analysis.\n\n"
            f"## Original Clinical Text\n{context.get('raw_text', '')}\n\n"
            f"## Cleaned Text (Layer 1 Output)\n{context.get('clean_text', '')}\n\n"
        )

        # Include built-in layer outputs if available
        for key in ("layer2_output", "layer3_output"):
            val = context.get(key)
            if val:
                label = key.replace("_output", "").replace("_", " ").title()
                user_prompt += f"## {label} Output\n{json.dumps(val, indent=2)}\n\n"

        # Include previous custom layer outputs
        for prev_name, prev_result in prev_custom_results.items():
            if prev_result.success and prev_result.output:
                user_prompt += f"## {prev_name} Output\n{json.dumps(prev_result.output, indent=2)}\n\n"

        # Token callback
        token_cb = None
        if on_token:
            async def _cb(token: str):
                await on_token(layer_name, token)
            token_cb = _cb

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

            return LayerResult(
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
            logger.error("custom_layer_error", layer=layer_name, error=str(e))
            return LayerResult(
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
