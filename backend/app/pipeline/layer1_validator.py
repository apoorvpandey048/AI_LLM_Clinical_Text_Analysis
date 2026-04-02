"""
Layer 1 Validator — Pure Python, No LLM.

Validates that L1B did not lose critical information from L1A.
Detects over-cleaning, dropped therapies, and missing keywords.

If validation flags are raised, they trigger a self-correction
retry in the orchestrator.
"""

import re
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# Keywords that should survive from raw → clean text
# If present in raw but missing in clean, flag a DATA_LOSS
CRITICAL_THERAPY_KEYWORDS = [
    # German therapy terms
    "therapie", "behandlung", "antibioti",
    "piperacillin", "tazobactam", "metronidazol",
    "ciprofloxacin", "ceftriaxon", "meropenem",
    "transfusion", "erythrozytenkonzentrat",
    "drainage", "punktion", "revision",
    "relaparotomie", "reoperation",
    "magensonde", "dauerkatheter",
    "noradrenalin", "dobutamin", "vasopressin",
    "beatmung", "ventilation", "intubation",
    "dialyse", "cvvh",
    "haloperidol", "quetiapin",
    "octreotid", "sandostatin",
    "tamsulosin", "pradif",
    "metoclopramid", "paspertin",
    "heparin", "clexane", "enoxaparin",
    "amiodaron", "cordarone",
    # English therapy terms
    "antibiotic", "transfusion", "drainage",
    "reoperation", "ventilation", "dialysis",
    "vasopressor", "icu", "intensive care",
    "nasogastric", "catheter",
    "tpn", "parenteral nutrition",
]

# Structural keywords that indicate important sections
STRUCTURAL_KEYWORDS = [
    "icu", "intensiv", "imc", "intermediate",
    "komplikation", "complication",
    "reoperation", "revision",
    "sepsis", "schock", "shock",
    "organversagen", "organ failure",
    "beatmung", "ventilat",
]


def validate_layer1(
    raw_text: str,
    raw_course_text: str,
    clean_course_text: str,
) -> list[dict]:
    """
    Validate that L1B preserved critical information from L1A.

    Returns a list of validation flags. Empty list = all OK.

    Args:
        raw_text: Original input text
        raw_course_text: L1A output (extracted course)
        clean_course_text: L1B output (structured/normalized)

    Returns:
        List of validation flag dicts with type and details
    """
    flags = []

    # ── Check 1: Length sanity ──
    # If clean text is less than 50% of raw course text, possible over-cleaning
    if len(raw_course_text) > 100:
        ratio = len(clean_course_text) / len(raw_course_text)
        if ratio < 0.4:
            flags.append({
                "flag_type": "POSSIBLE_OVERCLEANING",
                "severity": "HIGH",
                "detail": f"Clean text is only {ratio:.0%} of raw course text length ({len(clean_course_text)} vs {len(raw_course_text)} chars)",
                "suggested_action": "RETRY_L1B",
            })
            logger.warning(
                "l1_overcleaning_detected",
                ratio=ratio,
                raw_len=len(raw_course_text),
                clean_len=len(clean_course_text),
            )

    # ── Check 2: Critical therapy keyword preservation ──
    raw_lower = raw_course_text.lower()
    clean_lower = clean_course_text.lower()

    lost_keywords = []
    for keyword in CRITICAL_THERAPY_KEYWORDS:
        if keyword in raw_lower and keyword not in clean_lower:
            lost_keywords.append(keyword)

    if lost_keywords:
        flags.append({
            "flag_type": "THERAPY_KEYWORD_LOSS",
            "severity": "HIGH",
            "detail": f"Therapy keywords in raw but missing in clean: {lost_keywords[:10]}",
            "lost_keywords": lost_keywords,
            "suggested_action": "RETRY_L1B",
        })
        logger.warning(
            "l1_therapy_loss_detected",
            lost_count=len(lost_keywords),
            keywords=lost_keywords[:5],
        )

    # ── Check 3: Structural keyword preservation ──
    lost_structural = []
    for keyword in STRUCTURAL_KEYWORDS:
        if keyword in raw_lower and keyword not in clean_lower:
            lost_structural.append(keyword)

    if lost_structural:
        flags.append({
            "flag_type": "STRUCTURAL_KEYWORD_LOSS",
            "severity": "MEDIUM",
            "detail": f"Structural keywords missing: {lost_structural}",
            "lost_keywords": lost_structural,
            "suggested_action": "RETRY_L1B",
        })

    # ── Check 4: Therapy line count ──
    # Count "Therapie:" lines in raw vs clean
    raw_therapy_lines = len(re.findall(r"(?i)therapie\s*:", raw_course_text))
    clean_therapy_lines = len(re.findall(r"(?i)therap", clean_course_text))

    if raw_therapy_lines > 0 and clean_therapy_lines == 0:
        flags.append({
            "flag_type": "THERAPY_SECTION_DROPPED",
            "severity": "CRITICAL",
            "detail": f"Raw has {raw_therapy_lines} 'Therapie:' lines but clean has none",
            "suggested_action": "RETRY_L1B",
        })

    # ── Check 5: Clean text not empty ──
    if len(clean_course_text.strip()) < 50:
        flags.append({
            "flag_type": "EMPTY_OUTPUT",
            "severity": "CRITICAL",
            "detail": f"Clean text is only {len(clean_course_text.strip())} chars",
            "suggested_action": "RETRY_L1B",
        })

    logger.info(
        "l1_validation_complete",
        flag_count=len(flags),
        raw_len=len(raw_course_text),
        clean_len=len(clean_course_text),
        therapy_lines_raw=raw_therapy_lines,
    )

    return flags
