"""
Layer 2: CIE (Complication Inference Engine)

Extracts complications, assigns CD grades, computes CCI.
"""

from app.pipeline.base import BaseLayer


class Layer2CIE(BaseLayer):
    """
    Complication Inference Engine.

    Input: Clean text from Layer 1
    Output: Complications list with CD grades and CCI calculation
    """

    @property
    def layer_name(self) -> str:
        return "layer2_cie"

    @property
    def prompt_filename(self) -> str:
        return f"layer2_cie_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, clean_text: str, **kwargs) -> str:
        """
        Build user prompt with the cleaned clinical text.

        Args:
            clean_text: The pre-processed text from Layer 1

        Returns:
            User prompt for the LLM
        """
        return f"""Analyze the following clinical text for postoperative complications:

---
{clean_text}
---

Extract all complications, assign Clavien-Dindo grades, and compute CCI.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 2 output schema.

        Required fields:
        - complications: list

        Optional fields (kept for backward compatibility with older prompts):
        - cci_grade_list, cci_weights, cci_R, cci_total, cci_check_passed
          (CCI is now computed deterministically in Python; these are ignored)
        """
        # Only complications is strictly required
        if "complications" not in output:
            return False, "Missing required field: complications"

        if not isinstance(output["complications"], list):
            return False, "complications must be an array"

        # Validate each complication has required fields
        complication_fields = ["complication", "cd_grade"]
        for i, comp in enumerate(output["complications"]):
            for field in complication_fields:
                if field not in comp:
                    return False, f"Complication {i+1} missing field: {field}"

        # Validate CD grades are valid
        valid_grades = {"I", "II", "IIIa", "IIIb", "IVa", "IVb", "V"}
        for i, comp in enumerate(output["complications"]):
            if comp["cd_grade"] not in valid_grades:
                return False, f"Complication {i+1} has invalid cd_grade: {comp['cd_grade']}"

        return True, None
