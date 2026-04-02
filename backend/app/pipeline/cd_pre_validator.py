"""
CD Grade Pre-Validator for SNAP-AI Pipeline.

Deterministic Python-only validation gate between Layer 2 and Layer 3.
Catches obvious CD grading inconsistencies before they reach the LLM
audit sub-layers, so the auditors can focus their attention.

This module is the FINAL AUTHORITY on rule violations — the LLM
sub-layers explain and supplement, but Python decides truth.
"""

from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# Exempt drugs: these do NOT constitute a Grade II complication
# when used alone (they are routine perioperative management)
EXEMPT_DRUGS = frozenset({
    "paracetamol", "acetaminophen", "metamizol", "novalgin",
    "ondansetron", "zofran", "metoclopramid", "metoclopramide",
    "ibuprofen", "diclofenac",
    # Electrolyte replacements
    "potassium", "kalium", "magnesium", "calcium", "phosphate",
    "sodium chloride", "nacl",
})

# Keywords that indicate an intervention (for Grade III validation)
INTERVENTION_KEYWORDS = frozenset({
    "drainage", "drain", "revision", "reoperation", "relaparotomy",
    "stent", "ercp", "embolization", "embolisation",
    "endoscopy", "endoscopic", "angiography", "angiographic",
    "percutaneous", "ct-guided", "ultrasound-guided",
    "surgical", "operative", "intervention",
    "sphincterotomy", "dilatation", "dilation",
    "thoracentesis", "paracentesis", "debridement",
    "vacuum", "vac", "lavage", "exploration",
})

# Keywords that indicate ICU / organ failure (for Grade IV validation)
ICU_ORGAN_KEYWORDS = frozenset({
    "icu", "ips", "intensivstation", "intensive care",
    "intubation", "ventilation", "beatmung", "respirator",
    "dialysis", "cvvh", "crrt", "hemofiltration",
    "vasopressor", "noradrenalin", "norepinephrine",
    "catecholamine", "inotrope", "dobutamin",
    "organ failure", "organversagen",
    "single organ", "multi-organ", "multiorgan",
})


def _text_contains_any(text: str, keywords: frozenset) -> bool:
    """Check if text contains any keyword (case-insensitive)."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _is_only_exempt_drugs(treatment: str) -> bool:
    """Check if treatment mentions ONLY exempt drugs."""
    lower = treatment.lower()
    # Check if ANY non-exempt therapeutic keyword is present
    non_exempt_indicators = {
        "antibiotic", "antibiotika", "ceftriaxon", "ciprofloxacin",
        "piperacillin", "tazobactam", "meropenem", "metronidazol",
        "amoxicillin", "cefuroxim", "vancomycin", "clindamycin",
        "transfusion", "erythrocyte", "blood", "blut",
        "tpn", "parenteral nutrition",
        "heparin", "enoxaparin", "therapeutic dose",
        "insulin", "octreotid",
    }
    has_non_exempt = any(ind in lower for ind in non_exempt_indicators)
    if has_non_exempt:
        return False

    # If only exempt drug names are found, flag it
    has_exempt = any(drug in lower for drug in EXEMPT_DRUGS)
    return has_exempt


def validate_complications(
    complications: list[dict],
) -> list[dict]:
    """
    Run deterministic pre-validation on Layer 2 complications.

    For each complication, checks CD grade consistency against
    the treatment description and returns a list of flags.

    These flags are passed to L3 sub-layers as guidance, and
    used by the Python aggregator as FINAL rule enforcement.

    Args:
        complications: List of complication dicts from Layer 2.

    Returns:
        List of flag dicts, one per complication with issues.
        Each contains: complication, cd_grade, flag_type, reason.
        Empty list = no issues found.
    """
    flags = []

    for i, comp in enumerate(complications):
        grade = comp.get("cd_grade", "")
        treatment = comp.get("treatment", "")
        name = comp.get("complication", f"Complication {i + 1}")

        # Rule 1: Grade III requires an intervention
        if grade in ("IIIa", "IIIb"):
            if not _text_contains_any(treatment, INTERVENTION_KEYWORDS):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_III_NO_INTERVENTION",
                    "reason": (
                        f"Grade {grade} requires a procedural intervention, "
                        f"but treatment describes: '{treatment[:120]}'"
                    ),
                    "suggested_action": "DOWNGRADE_TO_II",
                })

        # Rule 2: Grade II with only exempt drugs → should be Grade I
        if grade == "II":
            if treatment and _is_only_exempt_drugs(treatment):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_II_EXEMPT_ONLY",
                    "reason": (
                        f"Grade II requires non-exempt pharmacological therapy, "
                        f"but treatment contains only exempt drugs: '{treatment[:120]}'"
                    ),
                    "suggested_action": "DOWNGRADE_TO_I",
                })

        # Rule 3: Grade IV requires ICU / organ failure
        if grade in ("IVa", "IVb"):
            if not _text_contains_any(treatment, ICU_ORGAN_KEYWORDS):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_IV_NO_ORGAN_FAILURE",
                    "reason": (
                        f"Grade {grade} requires ICU with organ failure, "
                        f"but treatment describes: '{treatment[:120]}'"
                    ),
                    "suggested_action": "REVIEW_REQUIRED",
                })

    if flags:
        logger.info(
            "cd_pre_validation_flags",
            flag_count=len(flags),
            flags=[f["flag_type"] for f in flags],
        )
    else:
        logger.info("cd_pre_validation_clean", complication_count=len(complications))

    return flags
