"""
Deterministic Verdict Calculator for SNAP-AI Layer 3.

Computes the pipeline verdict from Layer 3's JSON fields using
boolean logic, replacing LLM-generated verdicts that may be
internally inconsistent.

The LLM's original verdict is preserved as 'llm_suggested_verdict'
for human review; the deterministic verdict becomes the authoritative
'verdict' field.
"""

from app.utils import get_logger

logger = get_logger(__name__)


def compute_verdict(layer3_output: dict) -> str:
    """
    Deterministically compute verdict from Layer 3 JSON fields.

    Rules (in order of priority):
    1. FAIL_REEXTRACTION_RECOMMENDED — if LLM explicitly flagged it
       (LLM is best positioned to detect systematic extraction issues)
    2. FAIL_REVIEW_REQUIRED — rule violations combined with evidence gaps
    3. PASS_WITH_WARNINGS — any individual issue present
    4. PASS — no issues detected

    Args:
        layer3_output: The parsed Layer 3 JSON output

    Returns:
        One of: PASS, PASS_WITH_WARNINGS, FAIL_REVIEW_REQUIRED,
        FAIL_REEXTRACTION_RECOMMENDED
    """
    episode_checks = layer3_output.get("episode_checks", [])
    likely_omissions = layer3_output.get("likely_omissions", [])
    cci_check = layer3_output.get("cci_check", {})
    rule_violation = layer3_output.get("rule_violation", False)
    llm_verdict = layer3_output.get("verdict", "")

    # --- Derive boolean signals ---

    has_evidence_gaps = any(
        # Handle both Layer 3 schema variants
        c.get("evidence_insufficient", False) or
        c.get("evidence_sufficient") is False
        for c in episode_checks
    )

    has_omissions = len(likely_omissions) > 0

    has_cci_mismatch = cci_check.get("cci_mismatch", False)

    has_rejections = any(
        c.get("action", "").upper() in ("REJECT", "DOWNGRADE", "MERGE", "UPGRADE")
        for c in episode_checks
    )

    # --- Apply verdict rules ---

    # Rule 1: If LLM flagged reextraction, trust that assessment
    # (LLM has context about systematic extraction quality)
    if llm_verdict == "FAIL_REEXTRACTION_RECOMMENDED":
        return "FAIL_REEXTRACTION_RECOMMENDED"

    # Rule 2: Serious compound issues → FAIL
    if rule_violation and (has_evidence_gaps or has_omissions):
        return "FAIL_REVIEW_REQUIRED"

    # Rule 3: Multiple concerning signals → FAIL
    issue_count = sum([
        has_evidence_gaps,
        has_omissions,
        has_cci_mismatch,
        rule_violation,
    ])
    if issue_count >= 2:
        return "FAIL_REVIEW_REQUIRED"

    # Rule 4: Any single issue → PASS_WITH_WARNINGS
    if has_evidence_gaps or has_omissions or has_cci_mismatch or rule_violation or has_rejections:
        return "PASS_WITH_WARNINGS"

    # Rule 5: Clean
    return "PASS"


def apply_deterministic_verdict(layer3_output: dict) -> dict:
    """
    Apply deterministic verdict to Layer 3 output.

    Preserves the LLM's original verdict as 'llm_suggested_verdict'
    and overwrites 'verdict' with the deterministic result.

    Args:
        layer3_output: The parsed Layer 3 JSON output (modified in-place)

    Returns:
        The modified layer3_output dict
    """
    llm_verdict = layer3_output.get("verdict", "UNKNOWN")
    deterministic_verdict = compute_verdict(layer3_output)

    # Preserve LLM verdict for audit trail
    layer3_output["llm_suggested_verdict"] = llm_verdict
    layer3_output["verdict"] = deterministic_verdict

    if llm_verdict != deterministic_verdict:
        logger.warning(
            "verdict_override",
            llm_verdict=llm_verdict,
            deterministic_verdict=deterministic_verdict,
        )

    return layer3_output
