"""Strip internal tool-call narration from assistant speech transcripts."""

import re

# Spoken or written tool markers the model emits when tools are not wired up.
_TOOL_MARKER = (
    r"(?:"
    r"end_call|end\s+call|"
    r"transfer_to_lo|transfer\s+to\s+lo|"
    r"schedule_callback|schedule\s+callback|"
    r"capture_borrower_field|capture\s+borrower\s+field"
    r")"
)

_REASON = r"[\w_]+"

_PATTERNS = (
    # [Call end_call with reason: completed]
    re.compile(
        rf"\s*\[(?:Call\s+)?{_TOOL_MARKER}(?:\s+with\s+reason\s*:\s*{_REASON})?[^\]]*\]",
        re.IGNORECASE,
    ),
    # *End call with reason: no_tcpa_consent.*
    re.compile(
        rf"\s*\*+\s*(?:Call\s+)?{_TOOL_MARKER}\s+with\s+reason\s*:\s*{_REASON}\.?\s*\*+",
        re.IGNORECASE,
    ),
    # Trailing End call with reason: … (no brackets/asterisks)
    re.compile(
        rf"\s*(?:Call\s+)?{_TOOL_MARKER}\s+with\s+reason\s*:\s*{_REASON}\.?\s*",
        re.IGNORECASE,
    ),
    # Trailing snake_case tool call without reason
    re.compile(
        rf"\s*(?:Call\s+)?{_TOOL_MARKER}\.?\s*$",
        re.IGNORECASE,
    ),
    # Partial streaming chunk (open bracket or asterisk)
    re.compile(
        rf"\s*(?:\[|\*+)\s*(?:Call\s+)?{_TOOL_MARKER}.*$",
        re.IGNORECASE,
    ),
    # [Ending the call now.] — spoken stage direction, not borrower-facing dialogue
    re.compile(
        r"\s*\[(?:[^[\]]*\b(?:Ending|End(?:ing)?)\s+(?:the\s+)?call\b[^[\]]*)\]\s*",
        re.IGNORECASE,
    ),
    # Trailing "Ending the call now." without brackets
    re.compile(
        r"\s*\b(?:I['']m\s+)?(?:Ending|End(?:ing)?)\s+(?:the\s+)?call(?:\s+now)?\.?\s*$",
        re.IGNORECASE,
    ),
    # Partial streaming: [Ending the call now.
    re.compile(
        r"\s*\[(?:I['']m\s+)?(?:Ending|End(?:ing)?)\s+(?:the\s+)?call.*$",
        re.IGNORECASE,
    ),
)


def sanitize_assistant_transcript(text: str) -> str:
    """Remove tool-call markers the model sometimes speaks (not for the borrower)."""
    if not (text or "").strip():
        return ""
    cleaned = text
    for pattern in _PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.rstrip()
