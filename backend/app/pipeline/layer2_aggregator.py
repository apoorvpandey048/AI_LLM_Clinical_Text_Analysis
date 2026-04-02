"""
Layer 2 Aggregator — Pure Python, No LLM.

Merges L2A (extraction) + L2B (grading) outputs into the final
Layer 2 schema. Applies Python-enforced rules as FINAL AUTHORITY.

RULE ENGINES (applied in order):
  1. Grade V hard rule (death detection)
  2. Over-extraction filter (evidence gating)
  3. IVa vs IVb organ failure enforcement
  4. Conservative merge engine (escalation chains)
  5. CD pre-validation (grade consistency)
  6. Final sanity checker (pattern validation)
"""

import re
from typing import Any

from app.pipeline.cd_pre_validator import validate_complications
from app.pipeline.organ_failure_detector import enforce_iv_grade
from app.utils import get_logger

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════
# 1. GRADE V HARD RULE — DEATH DETECTION (MANDATORY)
# ════════════════════════════════════════════════════════════

DEATH_PATTERNS = [
    r"\b(died|death|expired|deceased)\b",
    r"\b(verstorben|exitus\s*letalis|exitus|tod\s+des\s+patienten)\b",
    r"\b(mortem|postmortem|autopsy|obduktion)\b",
    r"\bin-hospital\s+mortality\b",
    r"\bpatient.*(?:died|verstorben|expired)\b",
]
_DEATH_RE = [re.compile(p, re.IGNORECASE) for p in DEATH_PATTERNS]


def _detect_grade_v(events: list[dict], clean_text: str = "") -> bool:
    """
    Hard rule: if death is mentioned anywhere → Grade V MUST exist.

    Uses regex with word boundaries to prevent false positives
    (e.g., 'mortality risk' won't match, but 'patient died' will).
    """
    # Build combined text from all sources
    combined = clean_text
    for ev in events:
        combined += " " + ev.get("event", "")
        combined += " " + ev.get("evidence_snippet", "")
        combined += " " + ev.get("treatment", "")
        combined += " " + ev.get("complication", "")

    for regex in _DEATH_RE:
        if regex.search(combined):
            return True
    return False


# ════════════════════════════════════════════════════════════
# 2. OVER-EXTRACTION FILTER (HYBRID: DROP CD I, FLAG CD ≥ II)
# ════════════════════════════════════════════════════════════

def _filter_weak_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Separate events into strong (keep) and weak (maybe drop).

    Weak events have:
      - No evidence snippet or very short snippet
      - Low confidence + uncertain flag

    Returns:
        (strong_events, weak_events)
    """
    strong = []
    weak = []

    for ev in events:
        snippet = ev.get("evidence_snippet", "")
        uncertain = ev.get("uncertain", False)
        confidence = ev.get("confidence", 1.0)

        is_weak = False

        # No evidence at all
        if not snippet or len(snippet.strip()) < 10:
            is_weak = True

        # Low confidence + uncertain
        if uncertain and confidence < 0.4:
            is_weak = True

        if is_weak:
            weak.append(ev)
        else:
            strong.append(ev)

    return strong, weak


def _apply_hybrid_filter(
    strong: list[dict],
    weak: list[dict],
) -> list[dict]:
    """
    Hybrid filtering: drop weak CD I events, keep+flag weak CD ≥ II.

    Returns:
        Final filtered event list.
    """
    filtered = list(strong)

    for ev in weak:
        grade = ev.get("cd_grade", "I")

        if grade == "I":
            # CD I + weak evidence → DROP
            logger.info(
                "event_dropped_weak_cd1",
                event=ev.get("complication", ev.get("event", "")),
                confidence=ev.get("confidence", 0),
            )
            continue
        else:
            # CD ≥ II + weak evidence → KEEP but flag
            ev["_weak_evidence_flag"] = True
            logger.info(
                "event_flagged_weak_evidence",
                event=ev.get("complication", ev.get("event", "")),
                grade=grade,
            )
            filtered.append(ev)

    return filtered


# ════════════════════════════════════════════════════════════
# 3. CONSERVATIVE MERGE ENGINE
# ════════════════════════════════════════════════════════════

# Organ/system clusters for determining if events are related
CLINICAL_CLUSTERS: dict[str, frozenset[str]] = {
    "biliary": frozenset({
        "bile", "biliary", "gallbladder", "choledoch", "hepaticojejunostomy",
        "bile leak", "biliom", "biloma", "cholangitis", "gallengang",
        "gallenleck", "galle", "biliär",
    }),
    "pancreatic": frozenset({
        "pancrea", "pankreas", "fistula pancreatic", "popf",
        "amylase", "lipase", "pankreatitis",
    }),
    "wound": frozenset({
        "wound", "surgical site", "ssi", "wunde", "wundinfektion",
        "fascia", "dehiscence", "evisceration",
    }),
    "pulmonary": frozenset({
        "pneumonia", "pneumonie", "pleural", "pleura",
        "atelectasis", "atelektase", "pulmonary", "pulmonal",
        "respiratory", "respiratorisch", "lung", "lunge",
    }),
    "cardiac": frozenset({
        "cardiac", "kardial", "atrial fibrillation", "vorhofflimmern",
        "arrhythmia", "arrhythmie", "nstemi", "stemi", "myocardial",
        "heart", "herz",
    }),
    "renal_urinary": frozenset({
        "renal", "kidney", "niere", "urinary", "harnweg",
        "uti", "harnverhalt", "retention", "catheter", "katheter",
        "creatinine", "kreatinin",
    }),
    "gi": frozenset({
        "ileus", "obstruction", "nausea", "vomiting", "erbrechen",
        "gastroparesis", "gastric", "anastomotic", "anastomose",
        "diarrhea", "diarrhö", "gi bleed", "melena",
    }),
    "vascular": frozenset({
        "thrombosis", "thrombose", "embolism", "embolie",
        "dvt", "pe", "pulmonary embolism", "lungenembolie",
        "hemorrhage", "blutung", "bleeding",
    }),
    "infectious": frozenset({
        "sepsis", "septic", "septisch", "abscess", "abszess",
        "infection", "infektion", "bacteremia", "bakteriämie",
        "fever", "fieber",
    }),
}


def _get_clinical_cluster(text: str) -> str | None:
    """Determine which clinical cluster an event belongs to."""
    lower = text.lower()
    best_match = None
    best_count = 0

    for cluster, keywords in CLINICAL_CLUSTERS.items():
        count = sum(1 for kw in keywords if kw in lower)
        if count > best_count:
            best_count = count
            best_match = cluster

    return best_match if best_count > 0 else None


def _is_escalation(ev_a: dict, ev_b: dict) -> bool:
    """
    Determine if ev_b is an escalation of ev_a.

    Conservative: requires SAME clinical cluster AND logical progression.
    """
    name_a = (ev_a.get("complication", "") + " " + ev_a.get("event", "")).lower()
    name_b = (ev_b.get("complication", "") + " " + ev_b.get("event", "")).lower()

    cluster_a = _get_clinical_cluster(name_a)
    cluster_b = _get_clinical_cluster(name_b)

    # Must be same cluster
    if cluster_a is None or cluster_b is None:
        return False
    if cluster_a != cluster_b:
        return False

    # Check for escalation keywords
    escalation_words = {
        "then", "subsequently", "escalated", "progressed",
        "worsened", "deteriorated", "followed by",
        "danach", "anschließend", "verschlechtert",
    }

    evidence_a = ev_a.get("evidence_snippet", "").lower()
    evidence_b = ev_b.get("evidence_snippet", "").lower()

    for word in escalation_words:
        if word in evidence_b or word in name_b:
            return True

    # Same cluster + LLM suggested merge → trust
    if ev_b.get("merge_suggestion", ""):
        return True

    return False


def _is_near_duplicate(ev_a: dict, ev_b: dict) -> bool:
    """
    Detect near-duplicate events (same condition described differently).
    """
    name_a = (ev_a.get("complication", "") + " " + ev_a.get("event", "")).lower()
    name_b = (ev_b.get("complication", "") + " " + ev_b.get("event", "")).lower()

    # Exact match
    if name_a.strip() == name_b.strip():
        return True

    # High word overlap (>70% shared words, min 3 words)
    words_a = set(w for w in name_a.split() if len(w) > 3)
    words_b = set(w for w in name_b.split() if len(w) > 3)

    if not words_a or not words_b:
        return False

    overlap = len(words_a & words_b)
    max_len = max(len(words_a), len(words_b))

    if max_len >= 3 and overlap / max_len > 0.7:
        return True

    return False


def _conservative_merge(events: list[dict]) -> list[dict]:
    """
    Conservative merge engine. Rules:

    1. DEDUP: Near-duplicate events → merge, keep highest grade
    2. ESCALATION: Same cluster + escalation → single episode, highest grade
    3. PARALLEL: Different clusters → separate episodes (NO merge)

    Returns merged event list.
    """
    if len(events) <= 1:
        return events

    grade_order = {"I": 0, "II": 1, "IIIa": 2, "IIIb": 3, "IVa": 4, "IVb": 5, "V": 6}

    # Track which events have been consumed by a merge
    consumed = set()
    merged_events = []

    for i, ev_a in enumerate(events):
        if i in consumed:
            continue

        chain = [ev_a]
        chain_indices = {i}

        for j, ev_b in enumerate(events):
            if j <= i or j in consumed:
                continue

            if _is_near_duplicate(ev_a, ev_b) or _is_escalation(ev_a, ev_b):
                chain.append(ev_b)
                chain_indices.add(j)

        if len(chain) > 1:
            # Merge: keep the one with highest grade
            grades = [c.get("cd_grade", "I") for c in chain]
            highest_grade = max(grades, key=lambda g: grade_order.get(g, -1))
            primary = next(
                (c for c in chain if c.get("cd_grade") == highest_grade),
                chain[0],
            )

            merged_event = {
                **primary,
                "cd_grade": highest_grade,
                "merged_from": [c.get("id", "") for c in chain],
                "_merge_reason": "dedup_or_escalation",
            }
            merged_events.append(merged_event)
            consumed.update(chain_indices)

            logger.info(
                "python_merge_applied",
                merged_count=len(chain),
                result_grade=highest_grade,
                result_name=primary.get("complication", primary.get("event", "")),
            )
        else:
            merged_events.append(ev_a)
            consumed.add(i)

    return merged_events


# ════════════════════════════════════════════════════════════
# 4. FINAL SANITY CHECKER
# ════════════════════════════════════════════════════════════

def _final_sanity_check(events: list[dict]) -> list[dict]:
    """
    Final sanity check to catch implausible patterns.

    Rules:
    1. If Grade V present → remove minor noise (CD I events)
    2. If too many CD I (>3) → prune lowest confidence ones
    3. If ALL events are CD I with low confidence → likely false positives
    4. If Grade III+ present → drop untreated CD I (clinical noise)
    """
    grade_order = {"I": 0, "II": 1, "IIIa": 2, "IIIb": 3, "IVa": 4, "IVb": 5, "V": 6}
    grade_v_present = any(e.get("cd_grade") == "V" for e in events)
    highest = max((grade_order.get(e.get("cd_grade", "I"), 0) for e in events), default=0) if events else 0

    # Rule 1: Grade V + minor noise → prune CD I
    if grade_v_present:
        pruned = [e for e in events if e.get("cd_grade") != "I"]
        if len(pruned) < len(events):
            logger.info(
                "sanity_prune_grade_v_noise",
                removed_count=len(events) - len(pruned),
            )
        events = pruned

    # Rule 2: Too many CD I → prune low-confidence
    cd_i_events = [e for e in events if e.get("cd_grade") == "I"]
    non_cd_i = [e for e in events if e.get("cd_grade") != "I"]

    if len(cd_i_events) > 3:
        # Sort by confidence descending, keep top 3
        cd_i_events.sort(key=lambda e: e.get("confidence", 0.5), reverse=True)
        kept = cd_i_events[:3]
        pruned_count = len(cd_i_events) - 3
        events = non_cd_i + kept

        logger.info(
            "sanity_prune_excess_cd1",
            original_cd1_count=len(cd_i_events),
            pruned_count=pruned_count,
        )

    # Rule 3: ALL events are CD I with low avg confidence → likely false positives
    if events and all(e.get("cd_grade") == "I" for e in events):
        avg_confidence = sum(e.get("confidence", 0.5) for e in events) / len(events)
        if avg_confidence < 0.6:
            logger.info(
                "sanity_prune_all_low_confidence_cd1",
                count=len(events),
                avg_confidence=round(avg_confidence, 2),
            )
            events = []
        elif len(events) == 1 and events[0].get("confidence", 0.5) < 0.5:
            logger.info(
                "sanity_prune_single_weak_cd1",
                confidence=events[0].get("confidence", 0.5),
            )
            events = []

    # Rule 4: Clinical noise pruner — if Grade IIIa+ exists, drop untreated CD I
    # In serious surgical cases, transient Grade I symptoms (mild nausea, brief fever)
    # are clinical noise when a major complication dominates the case.
    if highest >= 2:  # IIIa or higher
        TRIVIAL_TREATMENTS = frozenset({
            "", "observation", "beobachtung", "spontaneous resolution",
            "spontane besserung", "conservative", "konservativ",
            "monitoring", "überwachung", "watchful waiting",
            "no treatment", "keine therapie", "none",
            "self-limiting", "selbstlimitierend",
        })

        survivors = []
        pruned_noise = 0
        for e in events:
            if e.get("cd_grade") == "I":
                treatment = e.get("treatment", "").strip().lower()
                if treatment in TRIVIAL_TREATMENTS or not treatment:
                    pruned_noise += 1
                    logger.info(
                        "sanity_prune_clinical_noise",
                        complication=e.get("complication", ""),
                        reason="untreated_cd1_with_major_complication",
                    )
                    continue
            survivors.append(e)

        if pruned_noise:
            events = survivors
            logger.info("sanity_clinical_noise_pruned", count=pruned_noise)

    return events


# ════════════════════════════════════════════════════════════
# MAIN AGGREGATOR
# ════════════════════════════════════════════════════════════

def _highest_grade(grades: list[str]) -> str:
    """Return the highest CD grade from a list."""
    order = {"I": 0, "II": 1, "IIIa": 2, "IIIb": 3, "IVa": 4, "IVb": 5, "V": 6}
    if not grades:
        return "I"
    return max(grades, key=lambda g: order.get(g, -1))


def aggregate_layer2(
    l2a_output: dict | None,
    l2b_output: dict | None,
    clean_text: str = "",
    raw_text: str = "",
) -> dict:
    """
    Merge L2A + L2B outputs into the final Layer 2 schema.

    Python is the FINAL AUTHORITY on merge/split and grade enforcement.

    Rule engines applied in order:
      1. Grade V hard rule
      2. Over-extraction filter (hybrid)
      3. IVa vs IVb organ enforcement
      4. Conservative merge
      5. CD pre-validation
      6. Final sanity check

    Args:
        l2a_output: Layer 2A extraction output (or None if failed)
        l2b_output: Layer 2B grading output (or None if failed)
        clean_text: L1-processed clean course text for death detection
        raw_text: Original raw discharge summary (for organ failure detection)

    Returns:
        Aggregated Layer 2 output dict
    """
    # Handle failures gracefully
    if l2b_output is None and l2a_output is None:
        logger.error("layer2_aggregation_both_failed")
        return {"complications": []}

    # If L2B failed, we have events but no grades — assign Grade I default
    if l2b_output is None:
        events = l2a_output.get("events", []) if l2a_output else []
        return {
            "complications": [
                {
                    "complication": ev.get("event", ""),
                    "cd_grade": "I",
                    "treatment": ev.get("treatment", ""),
                    "evidence_snippet": ev.get("evidence_snippet", ""),
                    "id": ev.get("id", f"E{i+1}"),
                    "uncertain": True,
                    "confidence": 0.3,
                }
                for i, ev in enumerate(events)
            ]
        }

    # Normal path: L2B output has graded complications
    complications = l2b_output.get("complications", [])
    events = l2a_output.get("events", []) if l2a_output else []

    # Build event lookup by ID for cross-referencing
    event_by_id = {ev.get("id", ""): ev for ev in events}

    # ── Step 0: Enrich complications with L2A evidence ──
    enriched = []
    for comp in complications:
        event_id = comp.get("id", "")
        source_event = event_by_id.get(event_id, {})

        enriched_comp = {
            "complication": comp.get("complication", comp.get("event", "")),
            "cd_grade": comp.get("cd_grade", "I"),
            "treatment": comp.get("treatment", source_event.get("treatment", "")),
            "evidence_snippet": source_event.get("evidence_snippet", ""),
            "reasoning": comp.get("reasoning", ""),
            "id": event_id,
            "uncertain": comp.get("uncertain", source_event.get("uncertain", False)),
            "confidence": comp.get("confidence", source_event.get("confidence", 0.8)),
            "merge_suggestion": source_event.get("merge_suggestion", ""),
        }
        enriched.append(enriched_comp)

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 1: Grade V Hard Rule
    # ══════════════════════════════════════════════════════
    has_grade_v_in_events = any(e.get("cd_grade") == "V" for e in enriched)
    death_detected = _detect_grade_v(enriched, clean_text)

    if death_detected and not has_grade_v_in_events:
        # Force-inject Grade V
        enriched.append({
            "complication": "Death (in-hospital mortality)",
            "cd_grade": "V",
            "treatment": "N/A",
            "evidence_snippet": "Death detected by Python rule engine",
            "reasoning": "PYTHON_HARD_RULE: Death keyword detected in clinical text",
            "id": "EV_DEATH",
            "uncertain": False,
            "confidence": 1.0,
            "python_override": "GRADE_V_FORCED",
        })
        logger.warning("grade_v_forced_by_python", reason="death_keyword_detected")

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 2: Over-Extraction Filter (Hybrid)
    # ══════════════════════════════════════════════════════
    strong, weak = _filter_weak_events(enriched)
    enriched = _apply_hybrid_filter(strong, weak)

    logger.info(
        "over_extraction_filter",
        strong_count=len(strong),
        weak_count=len(weak),
        final_count=len(enriched),
    )

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 3: IVa vs IVb Organ Failure Enforcement
    # ══════════════════════════════════════════════════════
    # Use raw_text as primary source for organ detection (preserves original
    # German/clinical terminology before L1B normalization), with clean_text as fallback
    organ_detection_text = raw_text if raw_text else clean_text

    for comp in enriched:
        if comp.get("cd_grade") in ("IVa", "IVb"):
            original = comp["cd_grade"]
            corrected = enforce_iv_grade(
                current_grade=original,
                treatment_text=comp.get("treatment", ""),
                complication_text=comp.get("complication", ""),
                full_clinical_text=organ_detection_text,
            )
            if corrected != original:
                comp["cd_grade"] = corrected
                comp["python_override"] = f"IV_GRADE_{original}_TO_{corrected}"

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 4: Conservative Merge
    # ══════════════════════════════════════════════════════
    pre_merge_count = len(enriched)
    enriched = _conservative_merge(enriched)
    post_merge_count = len(enriched)

    logger.info(
        "merge_engine",
        pre_merge=pre_merge_count,
        post_merge=post_merge_count,
        merges=pre_merge_count - post_merge_count,
    )

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 5: CD Pre-Validation (grade consistency)
    # ══════════════════════════════════════════════════════
    validation_flags = validate_complications(enriched)

    for flag in validation_flags:
        flag_name = flag.get("complication", "").lower()
        suggested = flag.get("suggested_action", "")

        for comp in enriched:
            if comp.get("complication", "").lower() == flag_name:
                original_grade = comp["cd_grade"]
                if suggested == "DOWNGRADE_TO_II" and original_grade in ("IIIa", "IIIb"):
                    comp["cd_grade"] = "II"
                    comp["python_override"] = flag.get("flag_type")
                    logger.info(
                        "l2_python_downgrade",
                        complication=comp["complication"],
                        from_grade=original_grade,
                        to_grade="II",
                    )
                elif suggested == "DOWNGRADE_TO_I" and original_grade == "II":
                    comp["cd_grade"] = "I"
                    comp["python_override"] = flag.get("flag_type")
                    logger.info(
                        "l2_python_downgrade",
                        complication=comp["complication"],
                        from_grade=original_grade,
                        to_grade="I",
                    )
                elif suggested == "UPGRADE_TO_II" and original_grade == "I":
                    comp["cd_grade"] = "II"
                    comp["python_override"] = flag.get("flag_type")
                    logger.info(
                        "l2_python_upgrade",
                        complication=comp["complication"],
                        from_grade=original_grade,
                        to_grade="II",
                    )

    # ══════════════════════════════════════════════════════
    # RULE ENGINE 6: Final Sanity Check
    # ══════════════════════════════════════════════════════
    pre_sanity = len(enriched)
    enriched = _final_sanity_check(enriched)
    post_sanity = len(enriched)

    if pre_sanity != post_sanity:
        logger.info(
            "sanity_check_pruned",
            removed=pre_sanity - post_sanity,
        )

    # ── Build final output ──
    final_output = {
        "complications": enriched,
        "_l2_pre_validation_flags": validation_flags,
        "_l2_aggregator_meta": {
            "l2a_event_count": len(events),
            "l2b_complication_count": len(complications),
            "death_detected": death_detected,
            "strong_events": len(strong),
            "weak_events_dropped": len(weak) - (len(enriched) - len(strong)),
            "merges": pre_merge_count - post_merge_count,
            "python_flags": len(validation_flags),
            "sanity_pruned": pre_sanity - post_sanity,
            "final_count": len(enriched),
        },
    }

    logger.info(
        "layer2_aggregation_complete",
        events_extracted=len(events),
        complications_graded=len(complications),
        death_detected=death_detected,
        final_count=len(enriched),
    )

    return final_output
