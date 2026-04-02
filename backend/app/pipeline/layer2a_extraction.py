"""
Layer 2A: Clinical Event Extraction (Facts Only)

Extracts all postoperative deviations as raw events with evidence,
timing, and treatment — NO CD grading. Each event gets a unique ID
for tracking through L2B and Python enforcement.
"""

import json
from app.pipeline.base import BaseLayer


class Layer2AExtraction(BaseLayer):
    """
    Fact extraction sub-layer.

    Input: Clean text from Layer 1
    Output: Raw events with evidence and event IDs
    """

    @property
    def layer_name(self) -> str:
        return "layer2a_extraction"

    @property
    def prompt_filename(self) -> str:
        return f"layer2a_extraction_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, clean_text: str, **kwargs) -> str:
        """
        Build user prompt with clean clinical text.

        Args:
            clean_text: Pre-processed text from Layer 1

        Returns:
            User prompt for the LLM
        """
        return f"""Extract all postoperative events from the following clinical text.

---
{clean_text}
---

For EACH event, provide: id, event, timing, treatment, evidence_snippet, uncertain, confidence.
Do NOT assign Clavien-Dindo grades. Extract facts ONLY.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 2A output schema.

        Expected: {"events": [...]}
        Each event: {id, event, timing, treatment, evidence_snippet, uncertain, confidence}
        """
        if "events" not in output:
            return False, "Missing required field: events"

        if not isinstance(output["events"], list):
            return False, "events must be an array"

        for i, event in enumerate(output["events"]):
            if "id" not in event:
                return False, f"events[{i}] missing field: id"
            if "event" not in event:
                return False, f"events[{i}] missing field: event"
            if "evidence_snippet" not in event:
                return False, f"events[{i}] missing field: evidence_snippet"
            if "treatment" not in event:
                return False, f"events[{i}] missing field: treatment"

        return True, None
