"""
Structured JSON Preamble for SNAP-AI Pipeline.

Auto-prepended to all system prompts to enforce strict JSON output
from the LLM. This acts as a soft constraint; the retry loop with
self-correction provides the hard recovery path.
"""

STRUCTURED_JSON_PREAMBLE = (
    "CRITICAL OUTPUT RULES (binding):\n"
    "- Your output MUST be valid JSON.\n"
    "- No markdown fences, no explanations, no text outside the JSON object.\n"
    "- Do NOT include ```json or ``` wrappers.\n"
    "- Do NOT include comments inside the JSON.\n"
    "- All keys must exist exactly as specified in the schema.\n"
    "- No trailing commas. Strings must be properly escaped.\n"
    "- If uncertain about a value, use an explicit flag "
    "(e.g., \"uncertain\": true) — NEVER omit a required field.\n"
    "- You are being evaluated by a strict machine parser. "
    "Invalid JSON = total failure.\n\n"
)
