"""
Organ Failure Detector — Pure Python, No LLM.

Detects organ system involvement from treatment/complication text
to enforce IVa (single-organ) vs IVb (multi-organ) classification.

FINAL AUTHORITY: Python overrides LLM on IVa/IVb distinction.
"""

import re
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# Organ system keyword maps — each set represents one organ system
ORGAN_SYSTEMS: dict[str, frozenset[str]] = {
    "renal": frozenset({
        "dialysis", "dialyse", "cvvh", "crrt", "hemofiltration",
        "hämofiltration", "nierenversagen", "renal failure",
        "acute kidney", "akutes nierenversagen", "anurie", "anuria",
        "renal replacement", "nierenersatz",
    }),
    "respiratory": frozenset({
        "intubation", "ventilation", "beatmung", "respirator",
        "ards", "respiratory failure", "respiratorisches versagen",
        "mechanical ventilation", "maschinelle beatmung",
        "reintubation", "tracheostomy", "tracheotomie",
        "oxygen therapy high-flow",  # high-flow only, not nasal cannula
    }),
    "cardiovascular": frozenset({
        "vasopressor", "vasopressoren", "noradrenalin", "norepinephrine",
        "dobutamin", "dobutamine", "catecholamine", "katecholamin",
        "inotrope", "inotropika", "vasopressin",
        "cardiovascular failure", "kreislaufversagen",
        "cardiogenic shock", "kardiogener schock",
        "cardiac arrest", "herzstillstand", "reanimation", "cpr",
    }),
    "hepatic": frozenset({
        "liver failure", "leberversagen", "hepatic failure",
        "hepatisches versagen", "coagulopathy", "koagulopathie",
        "hepatorenal", "acute liver", "akutes leberversagen",
        "enc encephalopathy hepatic",
    }),
    "neurological": frozenset({
        "coma", "koma", "cerebral", "zerebral",
        "encephalopathy", "enzephalopathie",
        "intracranial", "intrakraniell",
        "brain death", "hirntod",
    }),
}


def detect_organ_failures(
    treatment_text: str,
    complication_text: str = "",
    full_clinical_text: str = "",
) -> list[str]:
    """
    Detect which organ systems are involved based on text.

    Checks the event's own text first. If that yields only 1 organ,
    falls back to the full clinical text to detect multi-organ
    involvement documented elsewhere in the discharge summary.

    Args:
        treatment_text: Treatment/therapy description
        complication_text: Complication description (optional)
        full_clinical_text: Full discharge summary text (optional fallback)

    Returns:
        List of organ system names that are involved.
    """
    # First check the event-specific text
    event_text = (treatment_text + " " + complication_text).lower()

    involved = []
    for organ, keywords in ORGAN_SYSTEMS.items():
        if any(kw in event_text for kw in keywords):
            involved.append(organ)

    # If only 1 organ found in event text but we have the full clinical text,
    # scan the full text for additional organ system involvement.
    # This catches cases where organ failures are documented across the
    # discharge summary, not just in one event's description.
    if len(involved) <= 1 and full_clinical_text:
        full_lower = full_clinical_text.lower()
        full_involved = []
        for organ, keywords in ORGAN_SYSTEMS.items():
            if any(kw in full_lower for kw in keywords):
                full_involved.append(organ)

        # Only use the full scan if it finds MORE organs
        if len(full_involved) > len(involved):
            logger.info(
                "organ_detection_expanded",
                event_organs=involved,
                full_text_organs=full_involved,
            )
            involved = full_involved

    return involved


def enforce_iv_grade(
    current_grade: str,
    treatment_text: str,
    complication_text: str = "",
    full_clinical_text: str = "",
) -> str:
    """
    Enforce IVa vs IVb based on organ system count.

    FINAL AUTHORITY: Python overrides LLM classification.

    Rules:
        - ≥2 organ systems involved → IVb (multi-organ dysfunction)
        - 1 organ system involved → IVa (single-organ dysfunction)
        - 0 organ systems detected → no override (trust LLM)

    Only applies when current_grade is IVa or IVb.

    Args:
        current_grade: The LLM-assigned CD grade
        treatment_text: Treatment description
        complication_text: Complication description
        full_clinical_text: Full discharge summary for fallback organ detection

    Returns:
        Corrected grade string (IVa or IVb), or original if no override.
    """
    if current_grade not in ("IVa", "IVb"):
        return current_grade

    organs = detect_organ_failures(
        treatment_text, complication_text, full_clinical_text
    )

    if len(organs) >= 2:
        correct_grade = "IVb"
    elif len(organs) == 1:
        correct_grade = "IVa"
    else:
        # Can't determine — trust LLM
        return current_grade

    if correct_grade != current_grade:
        logger.info(
            "iv_grade_override",
            from_grade=current_grade,
            to_grade=correct_grade,
            organs_detected=organs,
        )

    return correct_grade
