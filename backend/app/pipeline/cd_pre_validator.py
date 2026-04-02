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
    # Standard perioperative
    "laxative", "loperamide", "pantoprazole", "pantoprazol",
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
    "relaparotomie", "punktion",
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

# Non-exempt drugs that REQUIRE minimum Grade II
GRADE_II_INDICATORS = frozenset({
    "antibiotic", "antibiotika", "antibiotikatherapie",
    "ceftriaxon", "ciprofloxacin", "piperacillin", "tazobactam",
    "meropenem", "metronidazol", "amoxicillin", "cefuroxim",
    "vancomycin", "clindamycin", "ampicillin",
    "transfusion", "erythrocyte", "blood products", "blutprodukt",
    "erythrozytenkonzentrat", "thrombozytenkonzentrat",
    "tpn", "parenteral nutrition", "parenterale ernährung",
    "therapeutic heparin", "therapeutic anticoagulation",
    "octreotid", "sandostatin",
    "insulin drip", "insulin perfusor",
    "amiodarone", "amiodaron",
})

# Prophylaxis keywords — if present, antibiotics may NOT indicate Grade II
PROPHYLAXIS_KEYWORDS = frozenset({
    "prophylaxis", "prophylaxe", "prophylactic",
    "perioperative", "perioperativ",
    "single-shot", "single shot", "einmaldosis",
    "standard prophylaxis", "standardprophylaxe",
})


def _text_contains_any(text: str, keywords: frozenset) -> bool:
    """Check if text contains any keyword (case-insensitive)."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _is_only_exempt_drugs(treatment: str) -> bool:
    """Check if treatment mentions ONLY exempt drugs."""
    lower = treatment.lower()
    # Check if ANY non-exempt therapeutic keyword is present
    has_non_exempt = _text_contains_any(lower, GRADE_II_INDICATORS)
    if has_non_exempt:
        return False

    # Check if ANY intervention keyword is present
    has_intervention = _text_contains_any(lower, INTERVENTION_KEYWORDS)
    if has_intervention:
        return False

    # If only exempt drug names are found, flag it
    has_exempt = any(drug in lower for drug in EXEMPT_DRUGS)
    return has_exempt


def _is_prophylactic(treatment: str) -> bool:
    """Check if treatment is prophylactic (not therapeutic)."""
    return _text_contains_any(treatment, PROPHYLAXIS_KEYWORDS)


def _requires_minimum_grade_ii(treatment: str) -> bool:
    """
    Check if treatment requires at minimum Grade II.

    Returns True if treatment contains non-exempt pharmacotherapy
    AND is NOT prophylactic.
    """
    if not treatment:
        return False

    lower = treatment.lower()

    # Check for Grade II indicators
    has_grade_ii = _text_contains_any(lower, GRADE_II_INDICATORS)

    if not has_grade_ii:
        return False

    # Check for prophylaxis — prophylactic antibiotics do NOT make Grade II
    if _is_prophylactic(treatment):
        return False

    return True


def validate_complications(
    complications: list[dict],
) -> list[dict]:
    """
    Run deterministic pre-validation on Layer 2 complications.

    For each complication, checks CD grade consistency against
    the treatment description and returns a list of flags.

    These flags are passed to L3 sub-layers as guidance, and
    used by the Python aggregator as FINAL rule enforcement.

    Rules:
      1. Grade III requires a procedural intervention
      2. Grade II with only exempt drugs → should be Grade I
      3. Grade IV requires ICU / organ failure
      4. Grade I with therapeutic antibiotics/transfusion → should be Grade II
      5. Grade II with empty treatment → suspicious

    Args:
        complications: List of complication dicts from Layer 2.

    Returns:
        List of flag dicts, one per complication with issues.
        Empty list = no issues found.
    """
    flags = []

    for i, comp in enumerate(complications):
        grade = comp.get("cd_grade", "")
        treatment = comp.get("treatment", "")
        name = comp.get("complication", f"Complication {i + 1}")

        # Rule 1: Grade III requires an intervention
        # Check BOTH treatment AND complication name (LLM may put procedure in either)
        if grade in ("IIIa", "IIIb"):
            combined_for_intervention = name + " " + treatment
            if not _text_contains_any(combined_for_intervention, INTERVENTION_KEYWORDS):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_III_NO_INTERVENTION",
                    "reason": (
                        f"Grade {grade} requires a procedural intervention, "
                        f"but neither complication nor treatment mention one: '{treatment[:120]}'"
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
        # Check BOTH treatment AND complication name
        if grade in ("IVa", "IVb"):
            combined_for_icu = name + " " + treatment
            if not _text_contains_any(combined_for_icu, ICU_ORGAN_KEYWORDS):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_IV_NO_ORGAN_FAILURE",
                    "reason": (
                        f"Grade {grade} requires ICU with organ failure, "
                        f"but neither complication nor treatment mention it: '{treatment[:120]}'"
                    ),
                    "suggested_action": "REVIEW_REQUIRED",
                })

        # Rule 4: Grade I with therapeutic antibiotics/transfusion → upgrade to II
        if grade == "I":
            if _requires_minimum_grade_ii(treatment):
                flags.append({
                    "complication": name,
                    "cd_grade": grade,
                    "flag_type": "GRADE_I_HAS_GRADE_II_THERAPY",
                    "reason": (
                        f"Grade I but treatment contains non-exempt therapy "
                        f"requiring minimum Grade II: '{treatment[:120]}'"
                    ),
                    "suggested_action": "UPGRADE_TO_II",
                })

        # Rule 5: Grade II with no treatment at all → suspicious
        if grade == "II" and not treatment.strip():
            flags.append({
                "complication": name,
                "cd_grade": grade,
                "flag_type": "GRADE_II_NO_TREATMENT",
                "reason": "Grade II has no treatment documented",
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
