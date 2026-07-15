"""When a preferred callback window is concrete enough to record (UI/tools only)."""

from __future__ import annotations

import re

# Reject incomplete half-answers like "morning" / "anytime" that still need a follow-up.
_VAGUE_ONLY = re.compile(
    r"^(?:"
    r"morning|afternoon|evening|tonight|later|soon|anytime|any\s*time|"
    r"anything|whenever|flexible|works|yes|yeah|ok|okay|sure"
    r")\.?$",
    re.IGNORECASE,
)

_HAS_CLOCK = re.compile(
    r"\d|"
    r"\b(?:a\.?m\.?|p\.?m\.?|noon|midnight)\b",
    re.IGNORECASE,
)

_HAS_DAY = re.compile(
    r"\b(?:"
    r"today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"next\s+week|this\s+week"
    r")\b",
    re.IGNORECASE,
)

_HAS_PERIOD = re.compile(
    r"\b(?:morning|afternoon|evening|night)\b",
    re.IGNORECASE,
)


def is_concrete_callback_time(value: str | None) -> bool:
    """True when ``value`` is a schedulable window — not a vague half-answer.

    Accepts e.g. ``"9 a.m. tomorrow"``, ``"tomorrow morning"``.
    Rejects e.g. ``"morning"``, ``"anything"``, ``"later"``.
    """
    t = " ".join((value or "").split()).strip()
    if len(t) < 4:
        return False
    if _VAGUE_ONLY.match(t):
        return False
    if _HAS_CLOCK.search(t):
        return True
    # Day + period is the skill's "concrete window" (tomorrow morning).
    if _HAS_DAY.search(t) and _HAS_PERIOD.search(t):
        return True
    return False
