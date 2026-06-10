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
# Keywords cover both German originals AND English paraphrases (L1B may translate)
ORGAN_SYSTEMS: dict[str, frozenset[str]] = {
    "renal": frozenset({
        # German
        "dialysis", "dialyse", "cvvh", "cvvhd", "cvvhdf", "crrt",
        "hemofiltration", "hämofiltration", "hämodiafiltration",
        "nierenversagen", "akutes nierenversagen", "anurie",
        "nierenersatz", "niereninsuffizienz", "oligurie",
        # English paraphrases (L1B may use these)
        "renal failure", "acute kidney", "kidney failure",
        "renal replacement", "anuria", "oliguria",
        "renal dysfunction", "kidney dysfunction",
        "renal insufficiency", "acute renal",
        "continuous hemofiltration", "hemodialysis",
        "renal support", "kidney support",
    }),
    "respiratory": frozenset({
        # German
        "intubation", "ventilation", "beatmung", "respirator",
        "ards", "maschinelle beatmung", "reintubation",
        "tracheostomy", "tracheotomie", "respiratorisches versagen",
        # English paraphrases
        "respiratory failure", "mechanical ventilation",
        "ventilator", "ventilatory support", "respiratory support",
        "oxygen therapy high-flow", "high-flow nasal",
        "respiratory distress", "pulmonary failure",
        "re-intubation", "ventilated", "intubated",
    }),
    "cardiovascular": frozenset({
        # German
        "vasopressor", "vasopressoren", "noradrenalin",
        "katecholamin", "inotropika", "kreislaufversagen",
        "kardiogener schock", "herzstillstand",
        # English paraphrases
        "norepinephrine", "dobutamin", "dobutamine",
        "catecholamine", "inotrope", "vasopressin",
        "cardiovascular failure", "hemodynamic instability",
        "cardiogenic shock", "cardiac arrest", "reanimation", "cpr",
        "vasopressor support", "circulatory failure",
        "hemodynamic support", "pressor", "pressors",
        "circulatory support", "shock",
    }),
    "hepatic": frozenset({
        # German
        "leberversagen", "akutes leberversagen",
        "hepatisches versagen", "koagulopathie",
        "hepatorenal", "leberdysfunktion",
        # English paraphrases
        "liver failure", "hepatic failure", "acute liver",
        "coagulopathy", "hepatic dysfunction",
        "liver dysfunction", "hepatic insufficiency",
        "liver insufficiency", "post-hepatectomy liver failure",
        "hepatorenal syndrome", "hepatic encephalopathy",
        "liver support", "phlf",  # Post-Hepatectomy Liver Failure
    }),
    "neurological": frozenset({
        # German
        "koma", "zerebral", "enzephalopathie",
        "intrakraniell", "hirntod",
        # English paraphrases
        "coma", "cerebral", "encephalopathy",
        "intracranial", "brain death",
        "altered mental status", "neurological failure",
        "neurological dysfunction", "consciousness",
        "obtunded", "unresponsive",
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


def guard_iv_requires_organ_support(
    episodes: list[dict],
    full_clinical_text: str = "",
) -> list[dict]:
    """
    Deterministic backstop for Grade IV (enforces layer3 RULE 7).

    For each episode graded IVa/IVb:
      * if NO organ-support token is found (in the episode's own text OR the full
        clinical text) → downgrade to IIIb. Grade IV requires explicit organ
        support (ventilation / vasopressors / dialysis / stated organ failure);
        an ICU/IMC location alone is not sufficient. IIIb is the conservative
        floor (the complication may still have warranted an intervention); the
        prompt rules resolve the finer II-vs-IIIb distinction.
      * else enforce IVa (1 organ) vs IVb (≥2 organs).

    Mutates the episode dicts in place. Returns a list of guard actions taken.

    Args:
        episodes: list of episode dicts, each with a "cd_grade" key
                  (Layer 3 final_episode_set or Layer 2 complications).
        full_clinical_text: cleaned clinical narrative for organ-token detection.

    Returns:
        List of {complication, from_grade, to_grade, organs} for each change.
    """
    actions: list[dict] = []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        grade = ep.get("cd_grade")
        if grade not in ("IVa", "IVb"):
            continue

        treatment = ep.get("treatment", "") or ""
        complication = ep.get("complication", "") or ""
        organs = detect_organ_failures(treatment, complication, full_clinical_text)

        if not organs:
            new_grade = "IIIb"
        elif len(organs) >= 2:
            new_grade = "IVb"
        else:
            new_grade = "IVa"

        if new_grade != grade:
            ep["cd_grade"] = new_grade
            action = {
                "complication": complication[:80],
                "from_grade": grade,
                "to_grade": new_grade,
                "organs": organs,
            }
            actions.append(action)
            logger.info(
                "iv_organ_support_guard",
                complication=complication[:60],
                from_grade=grade,
                to_grade=new_grade,
                organs=organs,
            )
    return actions
