"""
Layer 1A: Conservative Clinical Text Extraction

Extracts ONLY in-hospital clinical course text from a full
discharge summary. Near-lossless — keeps everything that might
be relevant. Does NOT normalize, structure, or clean.

Safety rule: when in doubt, KEEP the text.
"""

from app.pipeline.base import BaseLayer


class Layer1AExtraction(BaseLayer):
    """
    Conservative text extraction sub-layer.

    Input: Raw discharge summary
    Output: Raw course text (near-lossless)
    """

    @property
    def layer_name(self) -> str:
        return "layer1a_extraction"

    @property
    def prompt_filename(self) -> str:
        return f"layer1a_extraction_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, raw_text: str, **kwargs) -> str:
        """
        Build user prompt with the full raw clinical text.

        Args:
            raw_text: The complete discharge summary

        Returns:
            User prompt for the LLM
        """
        return f"""Extract the in-hospital clinical course from the following discharge summary.

---
{raw_text}
---

Include ALL operations, postoperative events, treatments, complications, and ICU stays.
If unsure whether text is relevant, KEEP IT.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 1A output schema.

        Expected: {"raw_course_text": "..."}
        """
        if "raw_course_text" not in output:
            return False, "Missing required field: raw_course_text"

        if not isinstance(output["raw_course_text"], str):
            return False, "raw_course_text must be a string"

        if len(output["raw_course_text"].strip()) < 20:
            return False, "raw_course_text is too short — possible extraction failure"

        return True, None
