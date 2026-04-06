"""
JSON extraction and safe parsing utilities for LLM outputs.

Handles messy LLM responses: markdown fences, trailing text,
multiple JSON blocks, truncated output, and control characters.
"""

import json
import re
from typing import Any


def extract_json(text: str) -> str | None:
    """Extract the best JSON substring from messy LLM text.

    Handles:
    - Markdown code fences (```json ... ```)
    - Raw JSON objects embedded in prose
    - Multiple JSON blocks (returns the largest)
    - Control characters and trailing commas
    - Truncated output (unclosed braces/brackets)

    Args:
        text: Raw LLM output text.

    Returns:
        The extracted JSON string, or None if no JSON found.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Try direct parse first — input is already clean JSON
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, TypeError):
        pass

    # Strip markdown code fences
    stripped = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    stripped = re.sub(r'\n?```\s*$', '', stripped, flags=re.MULTILINE)
    stripped = stripped.strip().lstrip('\ufeff')

    if stripped != text:
        try:
            json.loads(stripped)
            return stripped
        except (json.JSONDecodeError, TypeError):
            pass

    # Try regex patterns in priority order
    patterns = [
        r"```json\s*([\s\S]*?)\s*```",   # ```json ... ```
        r"```\s*([\s\S]*?)\s*```",         # ``` ... ```
    ]

    candidates: list[str] = []

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            if candidate:
                candidates.append(candidate)

    # Also try finding raw JSON objects
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidates.append(text[brace_start:brace_end + 1])

    # Also try JSON arrays
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        candidates.append(text[bracket_start:bracket_end + 1])

    # Try each candidate, preferring the longest valid one
    best = None
    for candidate in candidates:
        try:
            json.loads(candidate)
            if best is None or len(candidate) > len(best):
                best = candidate
        except (json.JSONDecodeError, TypeError):
            # Try repairing
            repaired = _repair_json_string(candidate)
            if repaired is not None:
                try:
                    json.loads(repaired)
                    if best is None or len(repaired) > len(best):
                        best = repaired
                except (json.JSONDecodeError, TypeError):
                    pass

    return best


def _repair_json_string(text: str) -> str | None:
    """Attempt to repair common JSON issues in LLM output.

    Fixes:
    - Control characters
    - Trailing commas
    - Unclosed braces/brackets

    Returns repaired string or None if unfixable.
    """
    if not text:
        return None

    repaired = text

    # Remove control characters except newline/tab
    repaired = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', repaired)

    # Fix trailing commas before } or ]
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)

    # Try parsing after basic fixes
    try:
        json.loads(repaired)
        return repaired
    except (json.JSONDecodeError, TypeError):
        pass

    # Fix unclosed braces/brackets
    open_braces = repaired.count('{') - repaired.count('}')
    open_brackets = repaired.count('[') - repaired.count(']')

    if open_braces > 0 or open_brackets > 0:
        # Strip trailing partial content
        stripped = repaired.rstrip()
        if stripped and stripped[-1] not in '{}[],"0123456789tfn':
            last_good = max(
                stripped.rfind(','),
                stripped.rfind('}'),
                stripped.rfind(']'),
                stripped.rfind('"'),
            )
            if last_good > 0:
                stripped = stripped[:last_good + 1]
                stripped = re.sub(r',\s*$', '', stripped)

        stripped += ']' * max(0, open_brackets) + '}' * max(0, open_braces)

        try:
            json.loads(stripped)
            return stripped
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def safe_json_parse(text: str) -> dict | list | None:
    """Safely parse JSON from LLM output text.

    Uses extract_json() to handle messy outputs, then parses.
    Returns None on any failure — never raises.

    Args:
        text: Raw LLM output that may contain JSON.

    Returns:
        Parsed JSON (dict or list) or None if parsing fails.
    """
    if not text or not text.strip():
        return None

    try:
        # Fast path: direct parse
        result = json.loads(text)
        if isinstance(result, (dict, list)):
            return result
        return None
    except (json.JSONDecodeError, TypeError):
        pass

    # Slow path: extract and parse
    try:
        extracted = extract_json(text)
        if extracted:
            result = json.loads(extracted)
            if isinstance(result, (dict, list)):
                return result
    except (json.JSONDecodeError, TypeError):
        pass

    return None
