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
) -> list[str]:
    """
    Detect which organ systems are involved based on text.

    Args:
        treatment_text: Treatment/therapy description
        complication_text: Complication description (optional)

    Returns:
        List of organ system names that are involved.
    """
    combined = (treatment_text + " " + complication_text).lower()

    involved = []
    for organ, keywords in ORGAN_SYSTEMS.items():
        if any(kw in combined for kw in keywords):
            involved.append(organ)

    return involved


def enforce_iv_grade(
    current_grade: str,
    treatment_text: str,
    complication_text: str = "",
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

    Returns:
        Corrected grade string (IVa or IVb), or original if no override.
    """
    if current_grade not in ("IVa", "IVb"):
        return current_grade

    organs = detect_organ_failures(treatment_text, complication_text)

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
