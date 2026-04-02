"""
Layer 2B: Clavien-Dindo Grading

Assigns one CD grade per extracted event based ONLY on the treatment
described. Treatment-first grading — NOT condition-first.

References event IDs from L2A for exact alignment.
"""

import json
from app.pipeline.base import BaseLayer


class Layer2BGrading(BaseLayer):
    """
    CD grading sub-layer.

    Input: Events from Layer 2A
    Output: Graded complications with reasoning
    """

    @property
    def layer_name(self) -> str:
        return "layer2b_grading"

    @property
    def prompt_filename(self) -> str:
        return f"layer2b_grading_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        events: list[dict],
        **kwargs,
    ) -> str:
        """
        Build user prompt with L2A extracted events.

        Args:
            events: Event list from Layer 2A output

        Returns:
            User prompt for the LLM
        """
        events_json = json.dumps(events, indent=2, ensure_ascii=False)

        return f"""Assign Clavien-Dindo grades to the following extracted events.

=== EVENTS TO GRADE ===
{events_json}

For EACH event, assign ONE CD grade based on the TREATMENT described.
Grade by treatment, NOT by condition severity.
Reference the event ID from the input.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 2B output schema.

        Expected: {"complications": [...]}
        Each: {id, complication, cd_grade, treatment, reasoning, uncertain, confidence}
        """
        if "complications" not in output:
            return False, "Missing required field: complications"

        if not isinstance(output["complications"], list):
            return False, "complications must be an array"

        valid_grades = {"I", "II", "IIIa", "IIIb", "IVa", "IVb", "V"}

        for i, comp in enumerate(output["complications"]):
            if "id" not in comp:
                return False, f"complications[{i}] missing field: id"
            if "complication" not in comp:
                return False, f"complications[{i}] missing field: complication"
            if "cd_grade" not in comp:
                return False, f"complications[{i}] missing field: cd_grade"
            if comp["cd_grade"] not in valid_grades:
                return False, f"complications[{i}] has invalid cd_grade: {comp['cd_grade']}"

        return True, None
