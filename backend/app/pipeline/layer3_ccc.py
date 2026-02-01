"""
Layer 3: CCC (Clinical Consistency Challenger)

Audits and verifies Layer 2 outputs for consistency and accuracy.
"""

import json
from app.pipeline.base import BaseLayer


class Layer3CCC(BaseLayer):
    """
    Clinical Consistency Challenger.

    Input: Layer 1 clean text + Layer 2 JSON output
    Output: Verification verdict with audit details
    """

    @property
    def layer_name(self) -> str:
        return "layer3_ccc"

    @property
    def prompt_filename(self) -> str:
        return f"layer3_ccc_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        clean_text: str,
        layer2_output: dict,
        **kwargs
    ) -> str:
        """
        Build user prompt with Layer 1 text and Layer 2 output.

        Args:
            clean_text: The pre-processed text from Layer 1
            layer2_output: The JSON output from Layer 2

        Returns:
            User prompt for the LLM
        """
        layer2_json = json.dumps(layer2_output, indent=2, ensure_ascii=False)

        return f"""Audit the following Layer 2 output against the source text.

=== LAYER 1 CLEAN TEXT ===
{clean_text}

=== LAYER 2 OUTPUT ===
{layer2_json}

Verify evidence anchoring, check for rule violations, identify omissions, and verify CCI.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 3 output schema.

        Required fields:
        - verdict: string
        - rule_violation: bool
        - episode_checks: list
        - omission_probes: dict
        - cci_check: dict
        """
        required_fields = [
            "verdict",
            "rule_violation",
            "episode_checks",
            "omission_probes",
            "cci_check",
        ]

        for field in required_fields:
            if field not in output:
                return False, f"Missing required field: {field}"

        # Validate verdict values
        valid_verdicts = {
            "PASS",
            "PASS_WITH_WARNINGS",
            "FAIL_REVIEW_REQUIRED",
            "FAIL_REEXTRACTION_RECOMMENDED",
        }
        if output["verdict"] not in valid_verdicts:
            return False, f"Invalid verdict: {output['verdict']}"

        if not isinstance(output["rule_violation"], bool):
            return False, "rule_violation must be a boolean"

        if not isinstance(output["episode_checks"], list):
            return False, "episode_checks must be an array"

        if not isinstance(output["omission_probes"], dict):
            return False, "omission_probes must be an object"

        if not isinstance(output["cci_check"], dict):
            return False, "cci_check must be an object"

        # Validate cci_check has required fields
        cci_check_fields = ["reported_cci", "expected_cci", "cci_mismatch"]
        for field in cci_check_fields:
            if field not in output["cci_check"]:
                return False, f"cci_check missing field: {field}"

        return True, None
