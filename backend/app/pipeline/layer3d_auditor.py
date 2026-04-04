"""
Layer 3D: Final Clinical Auditor

The LAST LLM call in the pipeline. Runs AFTER L3 (CCC) to produce
the final, pruned complication list. Acts as a senior attending
physician reviewing the case before sign-off.

This layer:
  1. Receives L2 complications + clean text + Python-computed CCI
  2. Prunes noise (weak Grade I/II events)
  3. Enforces Grade V pass-through
  4. Deduplicates escalation chains
  5. Returns the minimal sufficient set of complications

Its output is used for the FINAL CCI calculation.
"""

import json
from app.pipeline.base import BaseLayer


class Layer3DAuditor(BaseLayer):
    """
    Final Clinical Auditor sub-layer.

    Input: Clean text from L1 + complications from L2 + computed CCI
    Output: Pruned final complication list
    """

    @property
    def layer_name(self) -> str:
        return "layer3d_auditor"

    @property
    def prompt_filename(self) -> str:
        return f"layer3d_auditor_v{self.settings.prompt_version}.md"

    def build_user_prompt(
        self,
        clean_text: str,
        complications: list[dict],
        computed_cci: float,
        **kwargs,
    ) -> str:
        """
        Build user prompt with clean text, L2 complications, and computed CCI.

        Args:
            clean_text: Pre-processed text from Layer 1
            complications: Complications list from Layer 2 output
            computed_cci: Python-computed CCI score from current grades

        Returns:
            User prompt for the LLM
        """
        # Simplify complications for the LLM — only pass what matters
        simplified = []
        for comp in complications:
            simplified.append({
                "complication": comp.get("complication", ""),
                "cd_grade": comp.get("cd_grade", ""),
                "treatment": comp.get("treatment", ""),
                "evidence_snippet": comp.get("evidence_snippet", ""),
                "confidence": comp.get("confidence", 0.8),
                "uncertain": comp.get("uncertain", False),
            })

        complications_json = json.dumps(simplified, indent=2, ensure_ascii=False)

        return f"""Make the FINAL clinical decision on these postoperative complications.

=== CLINICAL TEXT ===
{clean_text}

=== CURRENT COMPLICATIONS (from extraction + grading pipeline) ===
{complications_json}

=== COMPUTED CCI ===
{computed_cci}

Review each complication. Remove noise, keep only clinically real complications, enforce the rules.
Output STRICT JSON only."""

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 3D output schema.

        Expected: {"final_complications": [...], "pruning_notes": "..."}
        Each complication: {"complication": "...", "cd_grade": "..."}
        """
        if "final_complications" not in output:
            return False, "Missing required field: final_complications"

        if not isinstance(output["final_complications"], list):
            return False, "final_complications must be an array"

        VALID_GRADES = {"I", "II", "IIIa", "IIIb", "IVa", "IVb", "V"}

        for i, comp in enumerate(output["final_complications"]):
            if "complication" not in comp:
                return False, f"final_complications[{i}] missing field: complication"
            if "cd_grade" not in comp:
                return False, f"final_complications[{i}] missing field: cd_grade"
            if comp["cd_grade"] not in VALID_GRADES:
                return False, f"final_complications[{i}] invalid cd_grade: {comp['cd_grade']}"

        return True, None
