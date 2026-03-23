"""
Deterministic CCI (Comprehensive Complication Index) Calculator.

Computes CCI from Clavien-Dindo grades using the official formula
(Slankamenac et al., Ann Surg 2013) in Python code, ensuring
arithmetic accuracy independent of LLM computation.
"""

import math
from typing import Any


# Fixed wC weights per Clavien-Dindo grade
CD_WEIGHTS: dict[str, int] = {
    "I": 300,
    "II": 1750,
    "IIIa": 2750,
    "IIIb": 4550,
    "IVa": 7200,
    "IVb": 8550,
    "V": 0,  # sentinel — handled specially
}


def compute_cci(grades: list[str]) -> float:
    """
    Compute CCI from a list of Clavien-Dindo grade strings.

    Formula:
        R = sum of wC weights for all grades
        CCI = sqrt(R) / 2
        Rounded to 1 decimal place.

    Special cases:
        - Empty list → 0.0
        - Any Grade V → 100.0

    Args:
        grades: List of CD grade strings, e.g. ["I", "II", "IIIa"]

    Returns:
        CCI score rounded to 1 decimal place.
    """
    if not grades:
        return 0.0

    # Death overrides everything
    if "V" in grades:
        return 100.0

    # Sum weights (unknown grades contribute 0)
    R = sum(CD_WEIGHTS.get(g, 0) for g in grades)

    if R == 0:
        return 0.0

    cci = math.sqrt(R) / 2.0
    return round(cci, 1)


def extract_grades_from_complications(complications: list[dict]) -> list[str]:
    """
    Extract cd_grade strings from a list of complication dicts.

    Args:
        complications: List of complication dicts, each with a "cd_grade" key.

    Returns:
        List of grade strings.
    """
    grades = []
    for comp in complications:
        grade = comp.get("cd_grade", "")
        if grade:
            grades.append(grade)
    return grades


def extract_grades_from_layer3(layer3_output: dict) -> list[str] | None:
    """
    Extract cd_grade strings from Layer 3's audited final_episode_set.

    Returns None if Layer 3 output doesn't have a usable final_episode_set.
    Handles both dict-format and string-format episodes (v1.14 robustness).
    """
    audited = layer3_output.get("audited_result")
    if not audited or not isinstance(audited, dict):
        return None

    episodes = audited.get("final_episode_set")
    if episodes is None or not isinstance(episodes, list):
        return None

    # Empty list = Layer 3 rejected all episodes → 0 complications → return empty grades
    # (Do NOT return None here, as that would fall back to Layer 2's grades)
    grades = []

    # Known CD grades in order of specificity (longest first to avoid partial matches)
    KNOWN_GRADES = ["IIIb", "IIIa", "IVb", "IVa", "III", "IV", "II", "I", "V"]

    for ep in episodes:
        if isinstance(ep, dict):
            grade = ep.get("cd_grade", "")
            if grade:
                grades.append(grade)
        elif isinstance(ep, str):
            # Fallback: parse grade from string like "Complication – II" or "Complication - IIIa"
            # Try splitting on common separators
            found = False
            for sep in [" – ", " - ", " — ", "–", "-"]:
                if sep in ep:
                    last_part = ep.rsplit(sep, 1)[-1].strip()
                    if last_part in KNOWN_GRADES:
                        grades.append(last_part)
                        found = True
                        break
            if not found:
                # Try checking if the string ends with a grade
                for g in KNOWN_GRADES:
                    if ep.strip().endswith(g):
                        grades.append(g)
                        break

    # If we couldn't extract from episodes, try audited_cci.grade_list as fallback
    if not grades and episodes:
        cci_data = audited.get("audited_cci")
        if cci_data and isinstance(cci_data, dict):
            grade_list = cci_data.get("grade_list", [])
            if grade_list and isinstance(grade_list, list):
                grades = [g for g in grade_list if isinstance(g, str) and g in KNOWN_GRADES]

    return grades


def compute_cci_from_pipeline(
    layer2_output: dict | None,
    layer3_output: dict | None = None,
) -> float:
    """
    Compute CCI deterministically from pipeline outputs.

    Prefers Layer 3's audited final_episode_set if available;
    falls back to Layer 2's complications list.

    Args:
        layer2_output: Layer 2 JSON output dict
        layer3_output: Layer 3 JSON output dict (optional)

    Returns:
        CCI score rounded to 1 decimal place.
    """
    # Try Layer 3 first (audited grades)
    if layer3_output:
        grades = extract_grades_from_layer3(layer3_output)
        if grades is not None:
            return compute_cci(grades)

    # Fall back to Layer 2
    if layer2_output:
        complications = layer2_output.get("complications", [])
        if isinstance(complications, list):
            grades = extract_grades_from_complications(complications)
            return compute_cci(grades)

    return 0.0
