"""
Layer 3A: Evidence Checker

Focused sub-layer that verifies each L2 complication has
verbatim evidence in the source clinical text.
"""

import json
from app.pipeline.base import BaseLayer


class Layer3AEvidence(BaseLayer):
    """
    Evidence verification sub-layer.

    Input: Clean text from L1 + complications from L2
    Output: Evidence check results per complication
    """

    @property
    def layer_name(self) -> str:
        return "layer3a_evidence"

    @property
    def prompt_filename(self) -> str:
        return f"layer3a_evidence_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        clean_text: str,
        complications: list[dict],
        **kwargs,
    ) -> str:
        """
        Build user prompt with clean text and L2 complications.

        Args:
            clean_text: Pre-processed text from Layer 1
            complications: Complications list from Layer 2 output

        Returns:
            User prompt for the LLM
        """
        complications_json = json.dumps(complications, indent=2, ensure_ascii=False)

        return f"""Verify evidence for each complication in the source text.

=== SOURCE TEXT ===
{clean_text}

=== COMPLICATIONS TO VERIFY ===
{complications_json}

For EACH complication, find the exact supporting evidence snippet (max 25 words, verbatim or near-verbatim from the source text).
If no evidence exists, set evidence_insufficient=true.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 3A output schema.

        Expected: {"evidence_checks": [...]}
        Each check: {complication, evidence_snippet, evidence_insufficient}
        """
        if "evidence_checks" not in output:
            return False, "Missing required field: evidence_checks"

        if not isinstance(output["evidence_checks"], list):
            return False, "evidence_checks must be an array"

        for i, check in enumerate(output["evidence_checks"]):
            if "complication" not in check:
                return False, f"evidence_checks[{i}] missing field: complication"
            if "evidence_snippet" not in check:
                return False, f"evidence_checks[{i}] missing field: evidence_snippet"
            if "evidence_insufficient" not in check:
                return False, f"evidence_checks[{i}] missing field: evidence_insufficient"
            if not isinstance(check["evidence_insufficient"], bool):
                return False, f"evidence_checks[{i}].evidence_insufficient must be boolean"

        return True, None
