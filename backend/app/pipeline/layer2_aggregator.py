"""
Layer 2 Aggregator — Pure Python, No LLM.

Merges L2A (extraction) + L2B (grading) outputs into the final
Layer 2 schema. Applies Python-enforced merge logic, grade
corrections, and filtering.

CRITICAL: Python is the FINAL AUTHORITY on:
  - Episode merge/split decisions
  - CD grade enforcement
  - Uncertain event filtering
"""

from typing import Any

from app.pipeline.cd_pre_validator import validate_complications
from app.utils import get_logger

logger = get_logger(__name__)

# Keywords for detecting escalation chains (same pathological process)
ESCALATION_INDICATORS = frozenset({
    "then", "subsequently", "escalated", "followed by",
    "danach", "anschließend", "eskalation", "im verlauf",
    "progressed", "worsened", "deteriorated",
})


def _detect_escalation_chains(events: list[dict]) -> list[list[str]]:
    """
    Detect potential escalation chains among events.

    Looks for events with the same anatomical/pathological focus
    that likely represent escalation of one underlying process.

    Returns list of groups, each group is a list of event IDs
    that should be merged.
    """
    # Simple heuristic: group by anatomical keyword overlap
    groups = []
    used = set()

    for i, ev_a in enumerate(events):
        if ev_a.get("id", "") in used:
            continue
        chain = [ev_a.get("id", f"E{i+1}")]
        name_a = ev_a.get("event", "").lower()

        for j, ev_b in enumerate(events):
            if j <= i or ev_b.get("id", "") in used:
                continue
            name_b = ev_b.get("event", "").lower()

            # Check if they share significant clinical keywords
            words_a = set(w for w in name_a.split() if len(w) > 4)
            words_b = set(w for w in name_b.split() if len(w) > 4)
            overlap = words_a & words_b

            if len(overlap) >= 2:
                chain.append(ev_b.get("id", f"E{j+1}"))

        if len(chain) > 1:
            groups.append(chain)
            used.update(chain)

    return groups


def _highest_grade(grades: list[str]) -> str:
    """Return the highest CD grade from a list."""
    order = {"I": 0, "II": 1, "IIIa": 2, "IIIb": 3, "IVa": 4, "IVb": 5, "V": 6}
    if not grades:
        return "I"
    return max(grades, key=lambda g: order.get(g, -1))


def aggregate_layer2(
    l2a_output: dict | None,
    l2b_output: dict | None,
) -> dict:
    """
    Merge L2A + L2B outputs into the final Layer 2 schema.

    Python is the FINAL AUTHORITY on merge/split and grade enforcement.

    The output schema matches what the rest of the pipeline expects:
    {"complications": [{complication, cd_grade, treatment, ...}]}

    Args:
        l2a_output: Layer 2A extraction output (or None if failed)
        l2b_output: Layer 2B grading output (or None if failed)

    Returns:
        Aggregated Layer 2 output dict
    """
    # Handle failures gracefully
    if l2b_output is None and l2a_output is None:
        logger.error("layer2_aggregation_both_failed")
        return {"complications": []}

    # If L2B failed, we have events but no grades — assign "UNGRADED"
    if l2b_output is None:
        events = l2a_output.get("events", []) if l2a_output else []
        return {
            "complications": [
                {
                    "complication": ev.get("event", ""),
                    "cd_grade": "I",  # Default to lowest
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

    # ── Step 1: Enrich complications with L2A evidence ──
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
            "uncertain": comp.get("uncertain", False),
            "confidence": comp.get("confidence", 0.8),
        }
        enriched.append(enriched_comp)

    # ── Step 2: Python merge logic (FINAL AUTHORITY) ──
    # Detect escalation chains and merge into single episodes
    chains = _detect_escalation_chains(enriched)

    if chains:
        merged_ids = set()
        merged_complications = []

        for chain in chains:
            chain_comps = [c for c in enriched if c.get("id") in chain]
            if not chain_comps:
                continue

            # Merge: keep name of most severe, assign highest grade
            grades = [c["cd_grade"] for c in chain_comps]
            highest = _highest_grade(grades)
            primary = next(
                (c for c in chain_comps if c["cd_grade"] == highest),
                chain_comps[0],
            )

            merged_comp = {
                **primary,
                "cd_grade": highest,
                "merged_from": [c.get("id") for c in chain_comps],
            }
            merged_complications.append(merged_comp)
            merged_ids.update(chain)

            logger.info(
                "python_merge_applied",
                merged_ids=list(chain),
                result_grade=highest,
                result_name=primary.get("complication", ""),
            )

        # Add non-merged complications
        for comp in enriched:
            if comp.get("id") not in merged_ids:
                merged_complications.append(comp)

        enriched = merged_complications

    # ── Step 3: Run CD pre-validation (FINAL AUTHORITY) ──
    validation_flags = validate_complications(enriched)

    # Apply Python-enforced downgrades
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

    # ── Step 4: Store pre-validation flags for L3 ──
    # These get passed to L3B for context
    final_output = {
        "complications": enriched,
        "_l2_pre_validation_flags": validation_flags,
        "_l2_aggregator_meta": {
            "l2a_event_count": len(events),
            "l2b_complication_count": len(complications),
            "merge_chains": len(chains),
            "python_flags": len(validation_flags),
            "final_count": len(enriched),
        },
    }

    logger.info(
        "layer2_aggregation_complete",
        events_extracted=len(events),
        complications_graded=len(complications),
        merges=len(chains),
        python_flags=len(validation_flags),
        final_count=len(enriched),
    )

    return final_output
