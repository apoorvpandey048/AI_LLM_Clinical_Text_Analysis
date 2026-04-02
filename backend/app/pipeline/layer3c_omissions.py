"""
Layer 3C: Omission Detector

Focused sub-layer that scans the clean clinical text for
complications that Layer 2 may have missed.
"""

import json
from app.pipeline.base import BaseLayer


class Layer3COmissions(BaseLayer):
    """
    Omission detection sub-layer.

    Input: Clean text from L1 + complications already extracted by L2
    Output: List of potentially missed complications with confidence
    """

    @property
    def layer_name(self) -> str:
        return "layer3c_omissions"

    @property
    def prompt_filename(self) -> str:
        return f"layer3c_omissions_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        clean_text: str,
        complications: list[dict],
        **kwargs,
    ) -> str:
        """
        Build user prompt with clean text and existing complications.

        Args:
            clean_text: Pre-processed text from Layer 1
            complications: Complications already extracted by Layer 2

        Returns:
            User prompt for the LLM
        """
        complications_json = json.dumps(complications, indent=2, ensure_ascii=False)

        return f"""Scan for complications that may have been MISSED by the extraction.

=== SOURCE TEXT ===
{clean_text}

=== ALREADY EXTRACTED COMPLICATIONS ===
{complications_json}

Check for potentially missed complications in these categories:
A) Antibiotics beyond standard prophylaxis (duration > 24h, non-standard agent)
B) Invasive procedures not already captured
C) ICU organ support (ventilation, dialysis, vasopressors beyond routine post-op)
D) Significant electrolyte corrections (beyond routine replacement)

IMPORTANT EXEMPTIONS (do NOT flag these as omissions):
- Routine post-major-surgery ICU monitoring without organ failure
- Low-dose vasopressors (noradrenaline < 0.1 µg/kg/min) for < 24h, weaned without escalation
- Standard prophylactic antibiotics
- Routine electrolyte replacement (oral potassium, standard IV fluids)

For each potential omission, provide a confidence level (low/moderate/high).
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 3C output schema.

        Expected: {"omissions": [...]}
        Each omission: {description, suggested_cd_grade, confidence}
        """
        if "omissions" not in output:
            return False, "Missing required field: omissions"

        if not isinstance(output["omissions"], list):
            return False, "omissions must be an array"

        valid_grades = {"I", "II", "IIIa", "IIIb", "IVa", "IVb", "V"}
        valid_confidence = {"low", "moderate", "high"}

        for i, om in enumerate(output["omissions"]):
            if "description" not in om:
                return False, f"omissions[{i}] missing field: description"
            if "suggested_cd_grade" not in om:
                return False, f"omissions[{i}] missing field: suggested_cd_grade"
            if om["suggested_cd_grade"] not in valid_grades:
                return False, f"omissions[{i}] has invalid suggested_cd_grade: {om['suggested_cd_grade']}"
            if "confidence" not in om:
                return False, f"omissions[{i}] missing field: confidence"
            if om["confidence"] not in valid_confidence:
                return False, f"omissions[{i}] has invalid confidence: {om['confidence']} (must be low/moderate/high)"

        return True, None
