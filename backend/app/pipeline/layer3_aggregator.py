"""
Layer 3 Aggregator — Pure Python, No LLM.

Merges L3A (evidence), L3B (rules), L3C (omissions) outputs into
the final Layer 3 schema that verdict_calculator.py and
cci_calculator.py consume.

CRITICAL: This module is the FINAL AUTHORITY on rule enforcement.
The LLM sub-layers explain and suggest; Python decides truth.
"""

from typing import Any

from app.pipeline.cd_pre_validator import validate_complications
from app.utils import get_logger

logger = get_logger(__name__)


def verify_evidence_snippets(
    evidence_checks: list[dict],
    clean_text: str,
) -> list[dict]:
    """
    Python verification of L3A evidence snippets.

    Checks that each evidence_snippet actually appears (approximately)
    in the source text. If not, overrides evidence_insufficient to True.

    Args:
        evidence_checks: L3A output evidence checks
        clean_text: The original clean clinical text

    Returns:
        Updated evidence_checks with Python-verified flags
    """
    lower_text = clean_text.lower()
    verified = []

    for check in evidence_checks:
        snippet = check.get("evidence_snippet", "")
        is_insufficient = check.get("evidence_insufficient", False)

        # If LLM already flagged as insufficient, trust that
        if is_insufficient:
            verified.append(check)
            continue

        # Python verification: check if snippet is grounded in text
        if snippet and len(snippet.strip()) > 5:
            # Check if key words from snippet appear in text
            snippet_words = [
                w.lower() for w in snippet.split()
                if len(w) > 3  # skip short words
            ]
            if snippet_words:
                match_ratio = sum(
                    1 for w in snippet_words if w in lower_text
                ) / len(snippet_words)

                if match_ratio < 0.5:
                    # Less than half the words match — flag as ungrounded
                    logger.warning(
                        "evidence_verification_failed",
                        complication=check.get("complication", "?"),
                        snippet=snippet[:80],
                        match_ratio=round(match_ratio, 2),
                    )
                    check = {**check, "evidence_insufficient": True,
                             "python_override": "snippet_not_grounded"}

        verified.append(check)

    return verified


def aggregate_layer3(
    layer2_output: dict,
    l3a_output: dict | None,
    l3b_output: dict | None,
    l3c_output: dict | None,
    pre_validation_flags: list[dict],
    clean_text: str = "",
) -> dict:
    """
    Merge L3A/L3B/L3C outputs into the final Layer 3 schema.

    Python is the FINAL AUTHORITY: pre_validation_flags from the
    deterministic CD pre-validator override LLM suggestions.

    The output schema matches what verdict_calculator.py and
    cci_calculator.py expect:
    {
        "verdict": "...",          (set by verdict_calculator later)
        "rule_violation": bool,
        "episode_checks": [...],
        "omission_probes": {...},
        "cci_check": {...},        (kept for backward compatibility)
        "audited_result": {...},
    }

    Args:
        layer2_output: Full Layer 2 output dict
        l3a_output: Layer 3A evidence checker output (or None if failed)
        l3b_output: Layer 3B rule explanation output (or None if failed)
        l3c_output: Layer 3C omission detector output (or None if failed)
        pre_validation_flags: Flags from Python CD pre-validator
        clean_text: Clean text for evidence snippet verification

    Returns:
        Aggregated Layer 3 output dict
    """
    complications = layer2_output.get("complications", [])

    # ── L3A: Evidence checks → episode_checks ──
    evidence_checks = []
    if l3a_output and "evidence_checks" in l3a_output:
        evidence_checks = l3a_output["evidence_checks"]
        # Python verification of evidence snippets
        if clean_text:
            evidence_checks = verify_evidence_snippets(evidence_checks, clean_text)

    # Build episode_checks in the format verdict_calculator expects
    episode_checks = []
    for comp in complications:
        comp_name = comp.get("complication", "")

        # Find matching evidence check
        ev_check = next(
            (e for e in evidence_checks
             if e.get("complication", "").lower() == comp_name.lower()),
            None,
        )

        # Find matching Python pre-validation flag
        py_flag = next(
            (f for f in pre_validation_flags
             if f.get("complication", "").lower() == comp_name.lower()),
            None,
        )

        # Find matching LLM rule violation
        llm_violation = None
        if l3b_output and "violations" in l3b_output:
            llm_violation = next(
                (v for v in l3b_output["violations"]
                 if v.get("complication", "").lower() == comp_name.lower()),
                None,
            )

        # ── FINAL AUTHORITY: Python pre-validation flags override LLM ──
        action = "CONFIRM"
        if py_flag:
            suggested = py_flag.get("suggested_action", "")
            if suggested == "DOWNGRADE_TO_II":
                action = "DOWNGRADE"
            elif suggested == "DOWNGRADE_TO_I":
                action = "DOWNGRADE"
            elif suggested == "REVIEW_REQUIRED":
                action = "REVIEW"

        episode_check = {
            "complication": comp_name,
            "cd_grade": comp.get("cd_grade", ""),
            "evidence_snippet": ev_check.get("evidence_snippet", "") if ev_check else "",
            "evidence_insufficient": ev_check.get("evidence_insufficient", False) if ev_check else True,
            "action": action,
            "python_flag": py_flag.get("flag_type") if py_flag else None,
            "llm_explanation": llm_violation.get("explanation", "") if llm_violation else None,
        }
        episode_checks.append(episode_check)

    # ── L3B: Rule violations (Python is final authority) ──
    # Python flags take precedence; LLM violations are supplemental
    has_python_violations = len(pre_validation_flags) > 0
    has_llm_violations = (
        l3b_output.get("rule_violation", False)
        if l3b_output else False
    )
    rule_violation = has_python_violations or has_llm_violations

    # ── L3C: Omissions ──
    omissions = []
    if l3c_output and "omissions" in l3c_output:
        # Only keep moderate/high confidence omissions
        omissions = [
            om for om in l3c_output["omissions"]
            if om.get("confidence", "low") in ("moderate", "high")
        ]

    # Build omission_probes in the format the old schema expects
    omission_probes = {
        "likely_omissions": omissions,
        "omission_count": len(omissions),
    }

    # ── Build final_episode_set for CCI calculator ──
    # Start with L2 complications, apply Python-enforced downgrades
    final_episodes = []
    for comp in complications:
        comp_name = comp.get("complication", "")
        grade = comp.get("cd_grade", "")

        # Apply Python downgrades
        py_flag = next(
            (f for f in pre_validation_flags
             if f.get("complication", "").lower() == comp_name.lower()),
            None,
        )
        if py_flag:
            suggested = py_flag.get("suggested_action", "")
            if suggested == "DOWNGRADE_TO_II" and grade in ("IIIa", "IIIb"):
                grade = "II"
                logger.info(
                    "python_downgrade_applied",
                    complication=comp_name,
                    from_grade=comp.get("cd_grade"),
                    to_grade=grade,
                    reason=py_flag.get("reason", ""),
                )
            elif suggested == "DOWNGRADE_TO_I" and grade == "II":
                grade = "I"
                logger.info(
                    "python_downgrade_applied",
                    complication=comp_name,
                    from_grade=comp.get("cd_grade"),
                    to_grade=grade,
                    reason=py_flag.get("reason", ""),
                )

        final_episodes.append({
            "complication": comp_name,
            "cd_grade": grade,
        })

    # Add high-confidence omissions to final episodes
    for om in omissions:
        if om.get("confidence") == "high":
            final_episodes.append({
                "complication": om["description"],
                "cd_grade": om["suggested_cd_grade"],
            })

    # ── CCI check (backward compatibility placeholder) ──
    cci_check = {
        "reported_cci": 0,
        "expected_cci": 0,
        "cci_mismatch": False,
        "note": "CCI computed deterministically by Python cci_calculator.py",
    }

    # ── Assemble final output ──
    aggregated = {
        "verdict": "PENDING",  # Will be overwritten by verdict_calculator
        "rule_violation": rule_violation,
        "episode_checks": episode_checks,
        "likely_omissions": omissions,
        "omission_probes": omission_probes,
        "cci_check": cci_check,
        "audited_result": {
            "final_episode_set": final_episodes,
        },
        # Metadata for debugging
        "_aggregator_meta": {
            "l3a_success": l3a_output is not None,
            "l3b_success": l3b_output is not None,
            "l3c_success": l3c_output is not None,
            "python_flags_count": len(pre_validation_flags),
            "omissions_kept": len(omissions),
            "final_episode_count": len(final_episodes),
        },
    }

    logger.info(
        "layer3_aggregation_complete",
        episode_count=len(final_episodes),
        rule_violation=rule_violation,
        python_flags=len(pre_validation_flags),
        omissions=len(omissions),
    )

    return aggregated
