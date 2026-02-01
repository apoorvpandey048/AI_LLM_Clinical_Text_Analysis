"""
Layer 1: CTP (Clinical Text Pre-Processor)

Extracts and cleans clinical text, de-identifies PHI, normalizes drug names.
"""

from app.pipeline.base import BaseLayer


class Layer1CTP(BaseLayer):
    """
    Clinical Text Pre-Processor.

    Input: Raw clinical text
    Output: De-identified, normalized text with metadata
    """

    @property
    def layer_name(self) -> str:
        return "layer1_ctp"

    @property
    def prompt_filename(self) -> str:
        return f"layer1_ctp_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, raw_text: str, **kwargs) -> str:
        """
        Build user prompt with the raw clinical text.

        Args:
            raw_text: The raw clinical text to process

        Returns:
            User prompt for the LLM
        """
        return f"""Process the following clinical text:

---
{raw_text}
---

Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 1 output schema.

        Required fields:
        - clean_course_text: str
        - extracted_dates: dict
        - drug_normalization: dict
        """
        required_fields = ["clean_course_text", "extracted_dates", "drug_normalization"]

        for field in required_fields:
            if field not in output:
                return False, f"Missing required field: {field}"

        if not isinstance(output["clean_course_text"], str):
            return False, "clean_course_text must be a string"

        if not isinstance(output["extracted_dates"], dict):
            return False, "extracted_dates must be an object"

        if not isinstance(output["drug_normalization"], dict):
            return False, "drug_normalization must be an object"

        return True, None
