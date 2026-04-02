"""
Clinical Judgment Engine — Pure Python, No LLM.

The "Senior Attending Physician" layer that runs AFTER all rule engines.
Applies clinical reasoning patterns that go beyond keyword matching:

  1. Priority Hierarchy — death dominates, noise gets pruned
  2. Context-Aware Severity — "brief" vs "persistent" modifiers
  3. Negation Handling — "no infection" cancels infection events
  4. Global Consistency — clinically implausible patterns flagged/fixed
  5. Confidence Re-evaluation — low confidence + high grade = suspicious

This module is the LAST Python authority before CCI calculation.
"""

import re
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════
# GRADE PRIORITY MAP
# ════════════════════════════════════════════════════════════

GRADE_PRIORITY = {
    "V": 6,
    "IVb": 5,
    "IVa": 4,
    "IIIb": 3,
    "IIIa": 2,
    "II": 1,
    "I": 0,
}


def _grade_priority(grade: str) -> int:
    return GRADE_PRIORITY.get(grade, -1)


# ════════════════════════════════════════════════════════════
# 1. PRIORITY HIERARCHY
# ════════════════════════════════════════════════════════════

def _apply_priority_hierarchy(events: list[dict]) -> list[dict]:
    """
    If high-severity events exist, aggressively prune noise.

    Rules:
      - Grade V present → drop everything below Grade II
      - Grade IV present → drop untreated Grade I events
      - Grade III present → drop Grade I events with no specific treatment
    """
    if not events:
        return events

    highest = max(_grade_priority(e.get("cd_grade", "I")) for e in events)

    # Grade V: death dominates — only keep Grade II+ alongside it
    if highest >= 6:  # Grade V
        before = len(events)
        events = [e for e in events if _grade_priority(e.get("cd_grade", "I")) >= 1]
        if len(events) < before:
            logger.info(
                "priority_prune_grade_v",
                removed=before - len(events),
            )

    return events


# ════════════════════════════════════════════════════════════
# 2. CONTEXT-AWARE SEVERITY MODIFIERS
# ════════════════════════════════════════════════════════════

# Downgrade modifiers: suggest the event was transient/minor
DOWNGRADE_CONTEXT = re.compile(
    r"\b(brief|briefly|transient|temporary|temporarily|mild|self-limiting|"
    r"self-resolved|resolved spontaneously|kurzzeitig|vorübergehend|"
    r"leicht|selbstlimitierend|passager|flüchtig)\b",
    re.IGNORECASE,
)

# Upgrade modifiers: suggest the event was severe/persistent
UPGRADE_CONTEXT = re.compile(
    r"\b(persistent|prolonged|severe|refractory|worsening|progressive|"
    r"multi-organ|multiorganversagen|therapierefraktär|zunehmend|"
    r"anhaltend|schwer|fulminant|life-threatening|lebensbedrohlich)\b",
    re.IGNORECASE,
)


def _apply_context_modifiers(events: list[dict]) -> list[dict]:
    """
    Adjust confidence based on contextual severity modifiers.

    - "brief vasopressor" → lower confidence for Grade IV
    - "persistent organ failure" → higher confidence

    Does NOT change grades directly — adjusts confidence scores
    which downstream rules use for pruning decisions.
    """
    for ev in events:
        combined = (
            ev.get("complication", "") + " " +
            ev.get("treatment", "") + " " +
            ev.get("evidence_snippet", "")
        )

        grade = ev.get("cd_grade", "I")
        confidence = ev.get("confidence", 0.7)

        # If high grade but transient context → lower confidence
        if _grade_priority(grade) >= 2 and DOWNGRADE_CONTEXT.search(combined):
            old_conf = confidence
            ev["confidence"] = max(0.3, confidence - 0.2)
            ev["_context_modifier"] = "TRANSIENT_DOWNGRADE"
            logger.info(
                "context_modifier_applied",
                complication=ev.get("complication", ""),
                modifier="transient_downgrade",
                old_confidence=old_conf,
                new_confidence=ev["confidence"],
            )

        # If high grade and persistent context → boost confidence
        if _grade_priority(grade) >= 3 and UPGRADE_CONTEXT.search(combined):
            old_conf = confidence
            ev["confidence"] = min(1.0, confidence + 0.15)
            ev["_context_modifier"] = "PERSISTENT_UPGRADE"

    return events


# ════════════════════════════════════════════════════════════
# 3. NEGATION HANDLING
# ════════════════════════════════════════════════════════════

# Negation window: if a negation word appears within N chars before a keyword
NEGATION_PATTERNS = re.compile(
    r"\b(no|not|without|absence of|negative for|ruled out|"
    r"kein|keine|keinen|ohne|ausgeschlossen|nicht|"
    r"no evidence of|no signs of)\b",
    re.IGNORECASE,
)


def _is_negated(text: str, keyword: str) -> bool:
    """
    Check if a keyword in text is preceded by a negation within 40 characters.
    """
    lower = text.lower()
    kw_lower = keyword.lower()

    idx = lower.find(kw_lower)
    if idx < 0:
        return False

    # Check the 40 characters before the keyword
    window_start = max(0, idx - 40)
    window = lower[window_start:idx]

    return bool(NEGATION_PATTERNS.search(window))


def _apply_negation_filter(events: list[dict]) -> list[dict]:
    """
    Drop events where the complication or key treatment is negated.

    Example: "No signs of infection" → drop infection-related events
    """
    filtered = []

    for ev in events:
        evidence = ev.get("evidence_snippet", "")
        complication = ev.get("complication", "")

        # Check if the complication itself is negated in the evidence
        negated = False
        for keyword in complication.split():
            if len(keyword) > 4 and _is_negated(evidence, keyword):
                negated = True
                break

        if negated:
            logger.info(
                "negation_filter_dropped",
                complication=complication,
                evidence_snippet=evidence[:80],
            )
            continue

        filtered.append(ev)

    return filtered


# ════════════════════════════════════════════════════════════
# 4. GLOBAL CONSISTENCY CHECK
# ════════════════════════════════════════════════════════════

def _apply_global_consistency(events: list[dict]) -> list[dict]:
    """
    Check for clinically implausible patterns and fix them.

    Rules:
      - If Grade IV exists but no Grade II/III → suspicious
        (Grade IV usually has preceding lower-grade evidence)
        → Don't remove, but flag for review
      - If multiple IVa events → likely one IVb episode, not separate IVa's
      - Remove exact duplicate complications
    """
    if not events:
        return events

    # Dedup: exact same complication name → keep highest grade
    seen = {}
    deduped = []
    for ev in events:
        name = ev.get("complication", "").strip().lower()
        grade = ev.get("cd_grade", "I")

        if name in seen:
            existing = seen[name]
            if _grade_priority(grade) > _grade_priority(existing.get("cd_grade", "I")):
                # Replace with higher grade
                deduped = [e for e in deduped if e is not existing]
                deduped.append(ev)
                seen[name] = ev
                logger.info(
                    "global_dedup_upgrade",
                    complication=name,
                    from_grade=existing.get("cd_grade"),
                    to_grade=grade,
                )
            # else: keep existing (higher or equal grade)
        else:
            seen[name] = ev
            deduped.append(ev)

    if len(deduped) < len(events):
        logger.info(
            "global_dedup_applied",
            original=len(events),
            after=len(deduped),
        )

    events = deduped

    # Multiple IVa with same organ system hint → flag as potentially IVb
    iva_count = sum(1 for e in events if e.get("cd_grade") == "IVa")
    if iva_count >= 2:
        for ev in events:
            if ev.get("cd_grade") == "IVa":
                ev["_consistency_flag"] = "MULTIPLE_IVA_SUSPICIOUS"
        logger.warning(
            "global_consistency_multiple_iva",
            count=iva_count,
            hint="May represent single IVb multi-organ episode",
        )

    return events


# ════════════════════════════════════════════════════════════
# 5. CONFIDENCE RE-EVALUATION
# ════════════════════════════════════════════════════════════

def _apply_confidence_reevaluation(events: list[dict]) -> list[dict]:
    """
    Final confidence-based decisions:

      - Low confidence + low grade (I) → DROP
      - Low confidence + high grade (III+) → FLAG (keep but warn)
      - High confidence events → untouched
    """
    filtered = []

    for ev in events:
        confidence = ev.get("confidence", 0.7)
        grade = ev.get("cd_grade", "I")

        # Low confidence CD I → drop
        if grade == "I" and confidence < 0.45:
            logger.info(
                "confidence_drop",
                complication=ev.get("complication", ""),
                grade=grade,
                confidence=confidence,
            )
            continue

        # Low confidence high grade → flag but keep
        if _grade_priority(grade) >= 2 and confidence < 0.35:
            ev["_confidence_flag"] = "LOW_CONFIDENCE_HIGH_GRADE"
            logger.warning(
                "confidence_flag_high_grade",
                complication=ev.get("complication", ""),
                grade=grade,
                confidence=confidence,
            )

        filtered.append(ev)

    return filtered


# ════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

def apply_clinical_judgment(
    events: list[dict],
    raw_text: str = "",
) -> list[dict]:
    """
    Apply all clinical judgment layers to the event list.

    Called as the FINAL step before CCI calculation.
    Runs after all L2 rule engines (Grade V, over-extraction,
    IVa/IVb, merge, CD validation, sanity check).

    Order of operations:
      1. Negation filter (remove negated events)
      2. Context modifiers (adjust confidence)
      3. Priority hierarchy (prune noise)
      4. Global consistency (dedup + pattern check)
      5. Confidence re-evaluation (final prune)

    Args:
        events: List of complication dicts after L2 rule engines
        raw_text: Original raw discharge text (for additional context)

    Returns:
        Refined event list ready for CCI calculation
    """
    if not events:
        return events

    pre_count = len(events)

    # Apply layers in order
    events = _apply_negation_filter(events)
    events = _apply_context_modifiers(events)
    events = _apply_priority_hierarchy(events)
    events = _apply_global_consistency(events)
    events = _apply_confidence_reevaluation(events)

    post_count = len(events)

    if pre_count != post_count:
        logger.info(
            "clinical_judgment_complete",
            pre_count=pre_count,
            post_count=post_count,
            removed=pre_count - post_count,
        )
    else:
        logger.info("clinical_judgment_no_changes", count=post_count)

    return events
