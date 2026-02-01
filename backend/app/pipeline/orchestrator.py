"""
Pipeline Orchestrator.

Coordinates the 3-layer pipeline execution.
"""

from dataclasses import dataclass
from typing import Any

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
    total_duration_ms: int
    total_tokens_input: int
    total_tokens_output: int
    final_verdict: str | None
    final_cci: float | None
    error: str | None


class PipelineOrchestrator:
    """
    Orchestrates the 3-layer SNAP-AI pipeline.

    Executes layers sequentially, passing outputs between them.
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

    async def execute(self, raw_text: str) -> PipelineResult:
        """
        Execute the complete 3-layer pipeline.

        Args:
            raw_text: The raw clinical text to process

        Returns:
            PipelineResult with all layer outputs
        """
        logger.info("pipeline_start", text_length=len(raw_text))

        total_duration_ms = 0
        total_tokens_input = 0
        total_tokens_output = 0

        # ====== Layer 1: CTP ======
        logger.info("pipeline_layer1_start")
        layer1_result = await self.layer1.execute(raw_text=raw_text)

        total_duration_ms += layer1_result.duration_ms
        total_tokens_input += layer1_result.tokens_input
        total_tokens_output += layer1_result.tokens_output

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

        # ====== Layer 2: CIE ======
        logger.info("pipeline_layer2_start")
        layer2_result = await self.layer2.execute(clean_text=clean_text)

        total_duration_ms += layer2_result.duration_ms
        total_tokens_input += layer2_result.tokens_input
        total_tokens_output += layer2_result.tokens_output

        if not layer2_result.success:
            logger.error("pipeline_layer2_failed", error=layer2_result.error)
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
        logger.info("pipeline_layer3_start")
        layer3_result = await self.layer3.execute(
            clean_text=clean_text,
            layer2_output=layer2_result.output,
        )

        total_duration_ms += layer3_result.duration_ms
        total_tokens_input += layer3_result.tokens_input
        total_tokens_output += layer3_result.tokens_output

        if not layer3_result.success:
            logger.error("pipeline_layer3_failed", error=layer3_result.error)
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

        logger.info(
            "pipeline_complete",
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            verdict=final_verdict,
            cci=final_cci,
        )

        return PipelineResult(
            success=True,
            layer1_result=layer1_result,
            layer2_result=layer2_result,
            layer3_result=layer3_result,
            total_duration_ms=total_duration_ms,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            final_verdict=final_verdict,
            final_cci=final_cci,
            error=None,
        )
