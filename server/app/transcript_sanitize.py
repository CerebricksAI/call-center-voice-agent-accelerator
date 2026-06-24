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

_END_CALL_DETECT = re.compile(
    rf"(?:\[|\*|^|\s)(?:Call\s+)?end_call(?:\s+with\s+reason\s*:\s*{_REASON})?",
    re.IGNORECASE,
)
_ENDING_CALL_DETECT = re.compile(
    r"\b(?:I['']m\s+)?(?:Ending|End(?:ing)?)\s+(?:the\s+)?call(?:\s+now)?",
    re.IGNORECASE,
)
# Spoken goodbyes when end_call tool is not wired (model closes without tool marker).
_GOODBYE_DAY_RE = re.compile(r"have a (?:great|wonderful) day\b", re.IGNORECASE)
_FAREWELL_RE = re.compile(
    r"\b(?:"
    r"have a (?:great|wonderful) day"
    r"|take care"
    r"|good\s*bye"
    r"|thanks again"
    r"|thank you(?: so much)? for (?:your time|calling)"
    r")\b",
    re.IGNORECASE,
)
_WRAP_UP_RE = re.compile(
    r"\b(?:"
    r"everything i need"
    r"|got everything"
    r"|pass (?:this )?along"
    r"|loan officer"
    r"|reach out"
    r")\b",
    re.IGNORECASE,
)
_CLOSING_PHRASES = (
    "thank you",
    "thanks",
    "you're welcome",
    "you’re welcome",
    "you are welcome",
    "pass this along",
    "pass along",
    "everything i need",
    "you're very welcome",
    "you’re very welcome",
)


def transcript_has_farewell(text: str) -> bool:
    """True when text contains an explicit spoken farewell."""
    return bool(_FAREWELL_RE.search(text or ""))


def _natural_close_detect(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    if re.search(r"\b(?:take care|good\s*bye)\b", lowered):
        return True
    if re.search(r"\bthank you(?: so much)? for (?:your time|calling)\b", lowered):
        return True
    if _GOODBYE_DAY_RE.search(lowered):
        if any(phrase in lowered for phrase in _CLOSING_PHRASES):
            return True
        if _WRAP_UP_RE.search(lowered):
            return True
    return False


_CALLER_PREFIX_RE = re.compile(
    r"^(?:The caller|Caller)(?:\s+is|\s+was|'s|\s+has|\s+had|\s+mentioned|\s+said|\s+confirmed|\s+looking)?\s+",
    re.IGNORECASE,
)
_VALUE_PREFIX_RE = re.compile(
    r"^(?:"
    r"looking(?:\s+(?:to|in|at|for))?"
    r"|considering(?:\s+a)?"
    r"|currently"
    r"|plans?\s+to"
    r"|they(?:'re| are)"
    r"|in the state of"
    r"|the best way for the loan officer to reach the caller is"
    r")\s+",
    re.IGNORECASE,
)


def transcript_requests_end_call(text: str) -> bool:
    """True when assistant transcript signals the call should end."""
    if not (text or "").strip():
        return False
    return bool(
        _END_CALL_DETECT.search(text)
        or _ENDING_CALL_DETECT.search(text)
        or _natural_close_detect(text)
    )


def normalize_insight_value(value: str) -> str:
    """Strip narrative prefixes for form-style display."""
    cleaned = (value or "").strip()
    for _ in range(3):
        next_val = _CALLER_PREFIX_RE.sub("", cleaned)
        next_val = _VALUE_PREFIX_RE.sub("", next_val)
        if next_val == cleaned:
            break
        cleaned = next_val
    cleaned = cleaned.strip(" .")
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def sanitize_assistant_transcript(text: str) -> str:
    """Remove tool-call markers the model sometimes speaks (not for the borrower)."""
    if not (text or "").strip():
        return ""
    cleaned = text
    for pattern in _PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.rstrip()
