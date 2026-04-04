"""
Pipeline Orchestrator.

Coordinates the multi-layer pipeline execution with optional streaming.
Supports additional custom layers that run after the built-in pipeline.

Pipeline v2.0 Architecture:
  L1 (LLM) → validate + retry
  L2 (LLM) → validate → Python CD pre-check
  L3A (LLM - evidence)  ┐
  L3B (LLM - rules)      ├─ run PARALLEL
  L3C (LLM - omissions)  ┘
  → Python aggregator (merge + enforce rules + FINAL AUTHORITY)
  → Python verdict calculator
  → Python CCI calculator
"""

import asyncio
import json

# Maximum characters for injected previous-layer JSON to prevent context window overflow
_MAX_CHAIN_CONTEXT_CHARS = 12000


def _build_chain_context(label: str, output: dict) -> str:
    """Build a context injection block for chained execution.

    Serialises *output* to JSON and truncates at key boundaries if it
    exceeds the safety limit.  This guarantees the injected JSON is
    always syntactically valid (unlike mid-string truncation).

    Returns a markdown-style block that is **prepended** to the user prompt.
    """
    raw = json.dumps(output, indent=2, ensure_ascii=False)
    if len(raw) <= _MAX_CHAIN_CONTEXT_CHARS:
        return f"=== {label} ===\n{raw}\n\n"

    # Truncate at key boundaries to keep valid JSON
    truncated = {}
    budget = _MAX_CHAIN_CONTEXT_CHARS - 200  # reserve room for structure
    remaining_keys = list(output.keys())
    for key in remaining_keys:
        entry_json = json.dumps({key: output[key]}, ensure_ascii=False)
        if budget - len(entry_json) < 0:
            omitted = remaining_keys[len(truncated):]
            truncated["__truncation_note__"] = f"Omitted keys due to size limit: {omitted}"
            break
        truncated[key] = output[key]
        budget -= len(entry_json)

    raw = json.dumps(truncated, indent=2, ensure_ascii=False)
    return f"=== {label} ===\n{raw}\n\n"

from app.pipeline.cci_calculator import compute_cci_from_pipeline
from app.pipeline.cd_pre_validator import validate_complications
from app.pipeline.layer1_validator import validate_layer1
from app.pipeline.layer2_aggregator import aggregate_layer2
from app.pipeline.layer3_aggregator import aggregate_layer3
from app.pipeline.verdict_calculator import apply_deterministic_verdict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.llm import LLMClient
from app.pipeline.layer1_ctp import Layer1CTP
from app.pipeline.layer1a_extraction import Layer1AExtraction
from app.pipeline.layer1b_structuring import Layer1BStructuring
from app.pipeline.layer2_cie import Layer2CIE
from app.pipeline.layer2a_extraction import Layer2AExtraction
from app.pipeline.layer2b_grading import Layer2BGrading
from app.pipeline.layer3_ccc import Layer3CCC
from app.pipeline.layer3a_evidence import Layer3AEvidence
from app.pipeline.layer3b_rules import Layer3BRules
from app.pipeline.layer3c_omissions import Layer3COmissions
from app.pipeline.layer3d_auditor import Layer3DAuditor
from app.pipeline.base import LayerResult, _UNSET
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
        self.layer1 = Layer1CTP(llm_client)  # kept for backward compat / dynamic mode
        # v2.0 split sub-layers
        self.layer1a = Layer1AExtraction(llm_client)
        self.layer1b = Layer1BStructuring(llm_client)
        self.layer2 = Layer2CIE(llm_client)  # kept for backward compat / dynamic mode
        self.layer2a = Layer2AExtraction(llm_client)
        self.layer2b = Layer2BGrading(llm_client)
        self.layer3 = Layer3CCC(llm_client)  # kept for backward compat / dynamic mode
        self.layer3a = Layer3AEvidence(llm_client)
        self.layer3b = Layer3BRules(llm_client)
        self.layer3c = Layer3COmissions(llm_client)
        self.layer3d = Layer3DAuditor(llm_client)

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
        seed: int | None = _UNSET,
        chained_mode: bool = False,
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
            seed: Seed for reproducible sampling (_UNSET = use config default, None = no seed)
            chained_mode: When True, inject previous layer outputs as context into subsequent layers

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
                seed=seed,
                chained_mode=chained_mode,
            )

        # ── Legacy execution path ──
        logger.info("pipeline_start", text_length=len(raw_text), temperature=temperature, chained_mode=chained_mode)

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

        # ====== Layer 1: Split Sub-Layers (SEQUENTIAL) ======
        if on_layer_start:
            await on_layer_start("layer1_ctp")  # Grouped: "Layer 1 (Processing)"
        logger.info("LAYER_START", layer="layer1_ctp")

        # ── L1A: Conservative Extraction ──
        if on_layer_start:
            await on_layer_start("layer1_ctp:extract")

        try:
            l1a_result = await self.layer1a.execute(
                raw_text=raw_text,
                custom_prompt=custom_prompts.get("layer1a_extraction"),
                on_token=make_token_cb("layer1a_extraction"),
                temperature_override=temperature,
                seed_override=seed,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer1a_extraction")
            raise

        total_duration_ms += l1a_result.duration_ms
        total_tokens_input += l1a_result.tokens_input
        total_tokens_output += l1a_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer1_ctp:extract", l1a_result.success, l1a_result.duration_ms)

        logger.info(
            "l1a_complete",
            success=l1a_result.success,
            raw_course_len=len(l1a_result.output.get("raw_course_text", "")) if l1a_result.success else 0,
        )

        if not l1a_result.success:
            logger.error("pipeline_l1a_failed", error=l1a_result.error)
            if on_layer_complete:
                await on_layer_complete("layer1_ctp", False, l1a_result.duration_ms)
            return PipelineResult(
                success=False,
                layer1_result=l1a_result,
                layer2_result=None,
                layer3_result=None,
                total_duration_ms=total_duration_ms,
                total_tokens_input=total_tokens_input,
                total_tokens_output=total_tokens_output,
                final_verdict=None,
                final_cci=None,
                error=f"Layer 1A (extraction) failed: {l1a_result.error}",
            )

        raw_course_text = l1a_result.output.get("raw_course_text", "")

        # ── L1B: Structuring + Normalization ──
        if on_layer_start:
            await on_layer_start("layer1_ctp:structure")

        try:
            l1b_result = await self.layer1b.execute(
                raw_course_text=raw_course_text,
                custom_prompt=custom_prompts.get("layer1b_structuring"),
                on_token=make_token_cb("layer1b_structuring"),
                temperature_override=temperature,
                seed_override=seed,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer1b_structuring")
            raise

        total_duration_ms += l1b_result.duration_ms
        total_tokens_input += l1b_result.tokens_input
        total_tokens_output += l1b_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer1_ctp:structure", l1b_result.success, l1b_result.duration_ms)

        logger.info(
            "l1b_complete",
            success=l1b_result.success,
            clean_len=len(l1b_result.output.get("clean_course_text", "")) if l1b_result.success else 0,
        )

        # ── Python L1 Validation (loss detection) ──
        clean_course_text = l1b_result.output.get("clean_course_text", "") if l1b_result.success else raw_course_text
        l1_validation_flags = validate_layer1(
            raw_text=raw_text,
            raw_course_text=raw_course_text,
            clean_course_text=clean_course_text,
        )

        if l1_validation_flags:
            logger.warning(
                "l1_validation_flags_raised",
                flag_count=len(l1_validation_flags),
                flags=[f["flag_type"] for f in l1_validation_flags],
            )

        # Build synthetic LayerResult matching existing schema expectations
        l1_total_duration = l1a_result.duration_ms + l1b_result.duration_ms
        layer1_result = LayerResult(
            success=l1b_result.success,
            output=l1b_result.output if l1b_result.success else {"clean_course_text": raw_course_text},
            raw_response=json.dumps({
                "l1a": l1a_result.raw_response[:500] if l1a_result.raw_response else "",
                "l1b": l1b_result.raw_response[:500] if l1b_result.raw_response else "",
            }),
            tokens_input=l1a_result.tokens_input + l1b_result.tokens_input,
            tokens_output=l1a_result.tokens_output + l1b_result.tokens_output,
            duration_ms=l1_total_duration,
            error=l1b_result.error,
            layer_name="layer1_ctp",
            prompt_version=self.layer1a.settings.prompt_version,
        )

        # Store validation flags and raw course text for debugging
        layer1_result.output["_l1_validation_flags"] = l1_validation_flags
        layer1_result.output["_l1a_raw_course_text"] = raw_course_text[:2000]  # truncated for storage

        if on_layer_complete:
            await on_layer_complete("layer1_ctp", layer1_result.success, l1_total_duration)
        logger.info("LAYER_END", layer="layer1_ctp", success=layer1_result.success)

        if not layer1_result.success:
            logger.error("pipeline_layer1_failed", error=layer1_result.error)
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

        # ── Chained mode: inject L1 output as context for L2 ──
        l2_clean_text = clean_text
        if chained_mode and layer1_result.output:
            chain_ctx = _build_chain_context("PREVIOUS LAYER OUTPUT (Layer 1 — Clinical Text Processor)", layer1_result.output)
            l2_clean_text = chain_ctx + clean_text
            logger.info("chained_context_injected", target_layer="layer2_cie", context_chars=len(chain_ctx))

        # ====== Layer 2: Split Sub-Layers (SEQUENTIAL) ======
        if on_layer_start:
            await on_layer_start("layer2_cie")  # Grouped: "Layer 2 (Extraction)"
        logger.info("LAYER_START", layer="layer2_cie")

        # ── L2A: Fact Extraction (no grading) ──
        if on_layer_start:
            await on_layer_start("layer2_cie:extract")

        try:
            l2a_result = await self.layer2a.execute(
                clean_text=l2_clean_text,
                custom_prompt=custom_prompts.get("layer2a_extraction"),
                on_token=make_token_cb("layer2a_extraction"),
                temperature_override=temperature,
                seed_override=seed,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer2a_extraction")
            raise

        total_duration_ms += l2a_result.duration_ms
        total_tokens_input += l2a_result.tokens_input
        total_tokens_output += l2a_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer2_cie:extract", l2a_result.success, l2a_result.duration_ms)

        logger.info(
            "l2a_complete",
            success=l2a_result.success,
            event_count=len(l2a_result.output.get("events", [])) if l2a_result.success else 0,
        )

        if not l2a_result.success:
            logger.error("pipeline_l2a_failed", error=l2a_result.error)
            # L2A failure is fatal — we have no events to grade
            if on_layer_complete:
                await on_layer_complete("layer2_cie", False, l2a_result.duration_ms)
            return PipelineResult(
                success=False,
                layer1_result=layer1_result,
                layer2_result=l2a_result,
                layer3_result=None,
                total_duration_ms=total_duration_ms,
                total_tokens_input=total_tokens_input,
                total_tokens_output=total_tokens_output,
                final_verdict=None,
                final_cci=None,
                error=f"Layer 2A (extraction) failed: {l2a_result.error}",
            )

        # ── L2B: CD Grading (treatment-first) ──
        if on_layer_start:
            await on_layer_start("layer2_cie:grade")

        extracted_events = l2a_result.output.get("events", [])

        try:
            l2b_result = await self.layer2b.execute(
                events=extracted_events,
                custom_prompt=custom_prompts.get("layer2b_grading"),
                on_token=make_token_cb("layer2b_grading"),
                temperature_override=temperature,
                seed_override=seed,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer2b_grading")
            raise

        total_duration_ms += l2b_result.duration_ms
        total_tokens_input += l2b_result.tokens_input
        total_tokens_output += l2b_result.tokens_output

        if on_layer_complete:
            await on_layer_complete("layer2_cie:grade", l2b_result.success, l2b_result.duration_ms)

        logger.info(
            "l2b_complete",
            success=l2b_result.success,
            complication_count=len(l2b_result.output.get("complications", [])) if l2b_result.success else 0,
        )

        # ── Python L2 Aggregator (FINAL AUTHORITY) ──
        aggregated_l2 = aggregate_layer2(
            l2a_output=l2a_result.output if l2a_result.success else None,
            l2b_output=l2b_result.output if l2b_result.success else None,
            clean_text=clean_text,
            raw_text=raw_text,
        )

        # Build synthetic LayerResult matching existing schema expectations
        l2_total_duration = l2a_result.duration_ms + l2b_result.duration_ms
        layer2_result = LayerResult(
            success=True,  # Aggregator is pure Python, always succeeds
            output=aggregated_l2,
            raw_response=json.dumps({
                "l2a": l2a_result.raw_response[:500] if l2a_result.raw_response else "",
                "l2b": l2b_result.raw_response[:500] if l2b_result.raw_response else "",
            }),
            tokens_input=l2a_result.tokens_input + l2b_result.tokens_input,
            tokens_output=l2a_result.tokens_output + l2b_result.tokens_output,
            duration_ms=l2_total_duration,
            error=None,
            layer_name="layer2_cie",
            prompt_version=self.layer2a.settings.prompt_version,
        )

        if on_layer_complete:
            await on_layer_complete("layer2_cie", True, l2_total_duration)
        logger.info("LAYER_END", layer="layer2_cie", success=True)

        # ====== Python CD Pre-Validation Gate (for L3) ======
        logger.info("cd_pre_validation_start")
        complications = layer2_result.output.get("complications", [])
        pre_validation_flags = layer2_result.output.get("_l2_pre_validation_flags", [])
        # re-validate after aggregation to catch any remaining issues
        pre_validation_flags = validate_complications(complications)
        logger.info("cd_pre_validation_complete", flag_count=len(pre_validation_flags))

        # ====== Layer 3: Split Sub-Layers (PARALLEL) ======
        if on_layer_start:
            await on_layer_start("layer3_ccc")  # Grouped: "Layer 3 (Auditing)"
        logger.info("LAYER_START", layer="layer3_ccc")

        # Chained mode: inject L1 output as additional context
        l3_clean_text = clean_text
        if chained_mode and layer1_result.output:
            chain_ctx = _build_chain_context("PREVIOUS LAYER OUTPUT (Layer 1 — Clinical Text Processor)", layer1_result.output)
            l3_clean_text = chain_ctx + clean_text
            logger.info("chained_context_injected", target_layer="layer3", context_chars=len(chain_ctx))

        # Sub-progress: Evidence check
        if on_layer_start:
            await on_layer_start("layer3_ccc:evidence")

        # Run L3A, L3B, L3C in PARALLEL via asyncio.gather
        try:
            l3a_task = self.layer3a.execute(
                clean_text=l3_clean_text,
                complications=complications,
                custom_prompt=custom_prompts.get("layer3a_evidence"),
                on_token=make_token_cb("layer3a_evidence"),
                temperature_override=temperature,
                seed_override=seed,
            )
            l3b_task = self.layer3b.execute(
                complications=complications,
                pre_validation_flags=pre_validation_flags,
                custom_prompt=custom_prompts.get("layer3b_rules"),
                on_token=make_token_cb("layer3b_rules"),
                temperature_override=temperature,
                seed_override=seed,
            )
            l3c_task = self.layer3c.execute(
                clean_text=l3_clean_text,
                complications=complications,
                custom_prompt=custom_prompts.get("layer3c_omissions"),
                on_token=make_token_cb("layer3c_omissions"),
                temperature_override=temperature,
                seed_override=seed,
            )

            l3a_result, l3b_result, l3c_result = await asyncio.gather(
                l3a_task, l3b_task, l3c_task,
                return_exceptions=False,
            )
        except Exception as exc:
            logger.exception("PIPELINE_LAYER_CRASH", layer="layer3_parallel")
            raise

        # Accumulate tokens/timing from all sub-layers
        for sub_result in (l3a_result, l3b_result, l3c_result):
            total_duration_ms += sub_result.duration_ms
            total_tokens_input += sub_result.tokens_input
            total_tokens_output += sub_result.tokens_output

        # Sub-progress events
        if on_layer_complete:
            await on_layer_complete("layer3_ccc:evidence", l3a_result.success, l3a_result.duration_ms)
        if on_layer_start:
            await on_layer_start("layer3_ccc:rules")
        if on_layer_complete:
            await on_layer_complete("layer3_ccc:rules", l3b_result.success, l3b_result.duration_ms)
        if on_layer_start:
            await on_layer_start("layer3_ccc:omissions")
        if on_layer_complete:
            await on_layer_complete("layer3_ccc:omissions", l3c_result.success, l3c_result.duration_ms)

        logger.info(
            "layer3_sublayers_complete",
            l3a_success=l3a_result.success,
            l3b_success=l3b_result.success,
            l3c_success=l3c_result.success,
        )

        # ====== Python Aggregation (FINAL AUTHORITY) ======
        aggregated_output = aggregate_layer3(
            layer2_output=layer2_result.output,
            l3a_output=l3a_result.output if l3a_result.success else None,
            l3b_output=l3b_result.output if l3b_result.success else None,
            l3c_output=l3c_result.output if l3c_result.success else None,
            pre_validation_flags=pre_validation_flags,
            clean_text=clean_text,
        )

        # Build a synthetic LayerResult for the aggregated L3 output
        # This keeps backward compatibility with the rest of the system
        l3_total_duration = l3a_result.duration_ms + l3b_result.duration_ms + l3c_result.duration_ms
        layer3_result = LayerResult(
            success=True,  # Aggregator is pure Python, always succeeds
            output=aggregated_output,
            raw_response=json.dumps({
                "l3a": l3a_result.raw_response[:500] if l3a_result.raw_response else "",
                "l3b": l3b_result.raw_response[:500] if l3b_result.raw_response else "",
                "l3c": l3c_result.raw_response[:500] if l3c_result.raw_response else "",
            }),
            tokens_input=l3a_result.tokens_input + l3b_result.tokens_input + l3c_result.tokens_input,
            tokens_output=l3a_result.tokens_output + l3b_result.tokens_output + l3c_result.tokens_output,
            duration_ms=l3_total_duration,
            error=None,
            layer_name="layer3_ccc",
            prompt_version=self.layer3a.settings.prompt_version,
        )

        if on_layer_complete:
            await on_layer_complete("layer3_ccc", True, l3_total_duration)
        logger.info("LAYER_END", layer="layer3_ccc", success=True)

        # Apply deterministic verdict (overrides LLM verdict with boolean logic)
        apply_deterministic_verdict(layer3_result.output)

        # Extract final values
        final_verdict = layer3_result.output.get("verdict", "UNKNOWN")

        # Deterministic CCI: compute in Python from CD grades, not LLM arithmetic
        final_cci = compute_cci_from_pipeline(
            layer2_output=layer2_result.output,
            layer3_output=layer3_result.output,
        )
        logger.info("cci_deterministic", cci=final_cci, source="python_calculator")

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
                seed=seed,
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
        seed: int | None = _UNSET,
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
            seed: Seed for reproducible sampling

        Returns:
            Dict mapping layer_name to LayerResult
        """
        results = {}
        from app.config import get_settings
        config = get_settings()
        effective_temperature = temperature if temperature is not None else config.llm_temperature
        effective_seed = seed if seed is not _UNSET else config.llm_seed

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
                        seed=effective_seed,
                    )
                else:
                    response = await self.llm_client.generate(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=effective_temperature,
                        max_tokens=config.llm_max_tokens,
                        timeout=config.llm_timeout,
                        seed=effective_seed,
                    )

                result = LayerResult(
                    success=response.success,
                    output=response.parsed_json,
                    raw_response=response.reasoning or response.content,
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
        seed: int | None = _UNSET,
        chained_mode: bool = False,
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
                        seed_override=seed,
                    )
                    layer1_result = result
                    if result.success and result.output:
                        context["clean_text"] = result.output.get("clean_course_text", "")
                        context["layer1_output"] = result.output

                elif is_builtin and name == "layer2_cie":
                    l2_input = context.get("clean_text", "")
                    # Chained mode: prepend L1 output context
                    if chained_mode and context.get("layer1_output"):
                        chain_ctx = _build_chain_context("PREVIOUS LAYER OUTPUT (Layer 1 — Clinical Text Processor)", context["layer1_output"])
                        l2_input = chain_ctx + l2_input
                        logger.info("chained_context_injected", target_layer="layer2_cie", context_chars=len(chain_ctx))

                    l2a_result = None
                    l2b_result = None

                    # ── L2A: Fact Extraction (no grading) ──
                    try:
                        if on_layer_start:
                            await on_layer_start("layer2_cie:extract")

                        l2a_result = await self.layer2a.execute(
                            clean_text=l2_input,
                            on_token=make_token_cb("layer2_cie:extract"),
                            temperature_override=temperature,
                            seed_override=seed,
                        )

                        if on_layer_complete:
                            await _on_layer_complete_with_error("layer2_cie:extract", l2a_result.success, l2a_result.duration_ms)

                        logger.info("l2a_complete_dynamic", success=l2a_result.success,
                                    event_count=len(l2a_result.output.get("events", [])) if l2a_result.success else 0)

                    except Exception as l2a_exc:
                        logger.exception("L2A_CRASH_DYNAMIC", error=str(l2a_exc))
                        if on_layer_complete:
                            await _on_layer_complete_with_error("layer2_cie:extract", False, 0, error_message=str(l2a_exc))

                    # ── L2B: CD Grading (treatment-first) ──
                    if l2a_result and l2a_result.success:
                        try:
                            if on_layer_start:
                                await on_layer_start("layer2_cie:grade")

                            extracted_events = l2a_result.output.get("events", [])

                            l2b_result = await self.layer2b.execute(
                                events=extracted_events,
                                on_token=make_token_cb("layer2_cie:grade"),
                                temperature_override=temperature,
                                seed_override=seed,
                            )

                            if on_layer_complete:
                                await _on_layer_complete_with_error("layer2_cie:grade", l2b_result.success, l2b_result.duration_ms)

                            logger.info("l2b_complete_dynamic", success=l2b_result.success,
                                        complication_count=len(l2b_result.output.get("complications", [])) if l2b_result.success else 0)

                        except Exception as l2b_exc:
                            logger.exception("L2B_CRASH_DYNAMIC", error=str(l2b_exc))
                            if on_layer_complete:
                                await _on_layer_complete_with_error("layer2_cie:grade", False, 0, error_message=str(l2b_exc))

                    # ── Python L2 Aggregator (FINAL AUTHORITY — ALL 7 RULE ENGINES) ──
                    # The aggregator handles None inputs gracefully
                    clean_text_for_l2 = context.get("clean_text", "")
                    aggregated_l2 = aggregate_layer2(
                        l2a_output=l2a_result.output if (l2a_result and l2a_result.success) else None,
                        l2b_output=l2b_result.output if (l2b_result and l2b_result.success) else None,
                        clean_text=clean_text_for_l2,
                        raw_text=raw_text,
                    )

                    # Compute totals from sub-layers
                    l2_duration = (l2a_result.duration_ms if l2a_result else 0) + (l2b_result.duration_ms if l2b_result else 0)
                    l2_tok_in = (l2a_result.tokens_input if l2a_result else 0) + (l2b_result.tokens_input if l2b_result else 0)
                    l2_tok_out = (l2a_result.tokens_output if l2a_result else 0) + (l2b_result.tokens_output if l2b_result else 0)

                    # Build synthetic LayerResult with aggregated output
                    l2_success = bool(l2a_result and l2a_result.success)
                    result = LayerResult(
                        success=l2_success,
                        output=aggregated_l2,
                        raw_response=l2b_result.raw_response if l2b_result else (l2a_result.raw_response if l2a_result else ""),
                        duration_ms=l2_duration,
                        tokens_input=l2_tok_in,
                        tokens_output=l2_tok_out,
                        error=None if l2_success else (l2a_result.error if l2a_result else "L2A did not execute"),
                        layer_name="layer2_cie",
                        prompt_version=self.layer2a.settings.prompt_version,
                    )

                    layer2_result = result
                    if result.success and result.output:
                        context["layer2_output"] = result.output

                elif is_builtin and name == "layer3_ccc":
                    l2_out = context.get("layer2_output", {})
                    l3_input = context.get("clean_text", "")
                    # Chained mode: prepend L1 output context
                    if chained_mode and context.get("layer1_output"):
                        chain_ctx = _build_chain_context("PREVIOUS LAYER OUTPUT (Layer 1 — Clinical Text Processor)", context["layer1_output"])
                        l3_input = chain_ctx + l3_input
                        logger.info("chained_context_injected", target_layer="layer3_ccc", context_chars=len(chain_ctx))
                    result = await self.layer3.execute(
                        clean_text=l3_input,
                        layer2_output=l2_out,
                        custom_prompt=prompt,
                        on_token=make_token_cb(name),
                        temperature_override=temperature,
                        seed_override=seed,
                    )
                    layer3_result = result
                    if result.success and result.output:
                        context["layer3_output"] = result.output

                    # ── POST-L3: Run Final Clinical Auditor (L3D) ──
                    # This is a hardcoded internal step that runs AFTER L3
                    # to prune noise and enforce clinical consistency.
                    l2_complications = context.get("layer2_output", {}).get("complications", [])
                    if l2_complications:
                        # Compute preliminary CCI for L3D context
                        from app.pipeline.cci_calculator import compute_cci, extract_grades_from_complications
                        prelim_grades = extract_grades_from_complications(l2_complications)
                        prelim_cci = compute_cci(prelim_grades)
                        l3d_clean_text = context.get("clean_text", "")

                        logger.info("LAYER_START", layer="layer3d_auditor")
                        try:
                            l3d_result = await self.layer3d.execute(
                                clean_text=l3d_clean_text,
                                complications=l2_complications,
                                computed_cci=prelim_cci,
                                on_token=make_token_cb("layer3d_auditor"),
                                temperature_override=temperature,
                                seed_override=seed,
                            )

                            if l3d_result.success and l3d_result.output:
                                final_comps = l3d_result.output.get("final_complications", [])

                                # PYTHON HARD RULE: Grade V pass-through
                                # If L2 had Grade V, ensure L3D didn't drop it
                                l2_has_v = any(c.get("cd_grade") == "V" for c in l2_complications)
                                l3d_has_v = any(c.get("cd_grade") == "V" for c in final_comps)
                                if l2_has_v and not l3d_has_v:
                                    final_comps.append({
                                        "complication": "Death (in-hospital mortality)",
                                        "cd_grade": "V",
                                    })
                                    logger.warning("grade_v_restored_by_python", reason="l3d_dropped_grade_v")

                                # Store L3D output for CCI calculation
                                context["l3d_output"] = l3d_result.output
                                logger.info(
                                    "l3d_auditor_complete",
                                    input_count=len(l2_complications),
                                    output_count=len(final_comps),
                                    pruned=len(l2_complications) - len(final_comps),
                                )

                                total_duration_ms += l3d_result.duration_ms
                                total_tokens_input += l3d_result.tokens_input
                                total_tokens_output += l3d_result.tokens_output
                            else:
                                logger.warning("l3d_auditor_failed", error=l3d_result.error)
                        except Exception:
                            logger.exception("l3d_auditor_crash")
                        logger.info("LAYER_END", layer="layer3d_auditor")

                else:
                    # Custom layer — generic LLM execution
                    result = await self._execute_single_custom_layer(
                        layer_name=name,
                        system_prompt=prompt or "",
                        context=context,
                        prev_custom_results=extra_results,
                        on_token=on_token,
                        temperature=temperature,
                        seed=seed,
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
            apply_deterministic_verdict(layer3_result.output)
            final_verdict = layer3_result.output.get("verdict", "UNKNOWN")
        elif layer3_result and not layer3_result.success:
            final_verdict = "LAYER3_FAILED"

        # Deterministic CCI: compute in Python from CD grades, not LLM arithmetic
        # Priority: L3D output > L3 output > L2 output
        if layer2_result and layer2_result.output:
            l3d_output = context.get("l3d_output")
            if l3d_output and l3d_output.get("final_complications"):
                # Use L3D's pruned final list for CCI
                from app.pipeline.cci_calculator import compute_cci
                l3d_grades = [c.get("cd_grade", "") for c in l3d_output["final_complications"] if c.get("cd_grade")]
                final_cci = compute_cci(l3d_grades)
                logger.info("cci_deterministic", cci=final_cci, source="l3d_auditor", grades=l3d_grades)

                # Also update L3 output's audited_result for downstream consumers
                if layer3_result and layer3_result.success and layer3_result.output:
                    layer3_result.output["audited_result"] = layer3_result.output.get("audited_result", {})
                    layer3_result.output["audited_result"]["final_episode_set"] = l3d_output["final_complications"]
            else:
                final_cci = compute_cci_from_pipeline(
                    layer2_output=layer2_result.output,
                    layer3_output=layer3_result.output if (layer3_result and layer3_result.success) else None,
                )
                logger.info("cci_deterministic", cci=final_cci, source="python_calculator")

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
        seed: int | None = _UNSET,
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
            seed: Seed for reproducible sampling
        """
        from app.config import get_settings
        config = get_settings()
        effective_temperature = temperature if temperature is not None else config.llm_temperature
        effective_seed = seed if seed is not _UNSET else config.llm_seed

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
                    seed=effective_seed,
                )
            else:
                response = await self.llm_client.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=effective_temperature,
                    max_tokens=config.llm_max_tokens,
                    timeout=config.llm_timeout,
                    seed=effective_seed,
                )

            return LayerResult(
                success=response.success,
                output=response.parsed_json,
                raw_response=response.reasoning or response.content,
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
