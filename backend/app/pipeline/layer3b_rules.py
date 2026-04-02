"""
Layer 3B: Rule Explanation Sub-Layer

Focused sub-layer that checks CD grading rules and provides
clinical explanations. The LLM EXPLAINS potential violations;
Python (cd_pre_validator + aggregator) makes the FINAL decision.
"""

import json
from app.pipeline.base import BaseLayer


class Layer3BRules(BaseLayer):
    """
    Rule explanation sub-layer.

    Input: Complications from L2 + Python pre-validation flags
    Output: Rule check explanations (LLM supplements, Python decides)
    """

    @property
    def layer_name(self) -> str:
        return "layer3b_rules"

    @property
    def prompt_filename(self) -> str:
        return f"layer3b_rules_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        complications: list[dict],
        pre_validation_flags: list[dict] | None = None,
        **kwargs,
    ) -> str:
        """
        Build user prompt with complications and pre-validation flags.

        Args:
            complications: Complications list from Layer 2 output
            pre_validation_flags: Flags from Python CD pre-validator

        Returns:
            User prompt for the LLM
        """
        complications_json = json.dumps(complications, indent=2, ensure_ascii=False)

        flags_section = ""
        if pre_validation_flags:
            flags_json = json.dumps(pre_validation_flags, indent=2, ensure_ascii=False)
            flags_section = f"""

=== PYTHON PRE-VALIDATION FLAGS (pay special attention to these) ===
{flags_json}
"""

        return f"""Check Clavien-Dindo grading rules for each complication.

=== COMPLICATIONS ===
{complications_json}
{flags_section}
For each complication, check:
1. Grade III requires a procedural intervention (endoscopy, drainage, reoperation, etc.)
2. Grade II requires non-exempt pharmacological therapy (antibiotics, transfusion, TPN, etc.)
3. Grade IV requires ICU admission with organ failure

Explain your reasoning for any violations found.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 3B output schema.

        Expected: {"rule_violation": bool, "violations": [...]}
        Each violation: {complication, violation_type, explanation}
        """
        if "rule_violation" not in output:
            return False, "Missing required field: rule_violation"

        if not isinstance(output["rule_violation"], bool):
            return False, "rule_violation must be a boolean"

        if "violations" not in output:
            return False, "Missing required field: violations"

        if not isinstance(output["violations"], list):
            return False, "violations must be an array"

        for i, v in enumerate(output["violations"]):
            if "complication" not in v:
                return False, f"violations[{i}] missing field: complication"

        return True, None
