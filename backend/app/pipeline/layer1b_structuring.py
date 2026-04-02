"""
Layer 1B: Clinical Text Structuring + Normalization

Takes the raw course text from L1A and produces:
- De-identified text
- Chronologically ordered narrative
- Drug brand → generic normalization
- Therapy audit trail
- Date extraction

Does NOT interpret or filter clinical content.
"""

import json
from app.pipeline.base import BaseLayer


class Layer1BStructuring(BaseLayer):
    """
    Text structuring and normalization sub-layer.

    Input: Raw course text from L1A
    Output: Clean, structured, de-identified text with metadata
    """

    @property
    def layer_name(self) -> str:
        return "layer1b_structuring"

    @property
    def prompt_filename(self) -> str:
        return f"layer1b_structuring_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, raw_course_text: str, **kwargs) -> str:
        """
        Build user prompt with the raw course text from L1A.

        Args:
            raw_course_text: Extracted clinical course from L1A

        Returns:
            User prompt for the LLM
        """
        return f"""Structure and normalize the following clinical course text.

---
{raw_course_text}
---

De-identify, normalize drug names, organize chronologically.
Do NOT remove any clinical information.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 1B output schema.

        Expected:
        {
            "clean_course_text": "...",
            "extracted_dates": {"admission": "", "operation": "", "discharge": ""},
            "drug_normalization": {"performed": true, "notes": ""},
            "therapy_audit": "..."
        }
        """
        if "clean_course_text" not in output:
            return False, "Missing required field: clean_course_text"

        if not isinstance(output["clean_course_text"], str):
            return False, "clean_course_text must be a string"

        if len(output["clean_course_text"].strip()) < 20:
            return False, "clean_course_text is too short — possible structuring failure"

        if "extracted_dates" not in output:
            return False, "Missing required field: extracted_dates"

        if not isinstance(output["extracted_dates"], dict):
            return False, "extracted_dates must be an object"

        if "drug_normalization" not in output:
            return False, "Missing required field: drug_normalization"

        return True, None
