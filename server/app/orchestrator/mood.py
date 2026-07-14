"""Caller mood heuristics — informs delivery feel (skills + voice style).

Not a compliance gate. Classification is deliberate and keyword-light so it stays
fast and deterministic on every turn. Default is calm/neutral.
"""

from __future__ import annotations

import re
from typing import Literal

Mood = Literal["frustrated", "excited", "rushed", "hesitant", "neutral"]

# Persona voice-style map keys used by agent_persona.AGENT_VOICE_STYLE_MAP
MOOD_TO_VOICE_CONTEXT: dict[Mood, str] = {
    "frustrated": "hardship",
    "excited": "excited",
    "rushed": "data_collection",
    "hesitant": "objection",
    "neutral": "default",
}

# Spoken-feel cues injected into instructions (behavior plane). Keep short.
MOOD_CUES: dict[Mood, str] = {
    "frustrated": (
        "MOOD (this turn): Caller sounds frustrated or stuck. Lead with genuine "
        "felt empathy in one short clause (not a blank 'got it'), keep calm and "
        "unhurried, then one clear next step — no cheerfulness, no rushing the form."
    ),
    "excited": (
        "MOOD (this turn): Caller sounds upbeat or excited. Match light warmth — "
        "one bright, understated reaction with a real smile in the wording — then "
        "the next question. Don't go bubbly or corporate."
    ),
    "rushed": (
        "MOOD (this turn): Caller sounds busy or rushed. Be crisp and respectful "
        "of their time: short human ack, one tight question, offer callback if they push."
    ),
    "hesitant": (
        "MOOD (this turn): Caller sounds unsure or tentative. Soften asks, no "
        "pressure, reassure that rough answers are fine, then one gentle question."
    ),
    "neutral": (
        "MOOD (this turn): Steady and warm — not flat. Brief natural reaction with "
        "a bit of personality to what they just said, then exactly one next question."
    ),
}

# Hardened on every non-silence turn (skills + custom prompt).
REACTION_FIRST = (
    "TURN SHAPE (every reply): First, a brief natural reaction to what the caller "
    "just said — matched to MOOD. Then exactly one next question or a clean close. "
    "Never jump straight to the next form field with no reaction."
)

_PATTERNS: list[tuple[Mood, re.Pattern[str]]] = [
    (
        "frustrated",
        re.compile(
            r"\b("
            r"frustrat\w*|annoyed|annoying|ridiculous|useless|broken|not working|"
            r"can'?t (?:do|get|access)|unable to|kept (?:failing|getting)|"
            r"sick of|fed up|this is (?:stupid|insane)|ugh+|argh+"
            r")\b",
            re.I,
        ),
    ),
    (
        "excited",
        re.compile(
            r"\b("
            r"excit\w*|awesome|amazing|can'?t wait|so happy|thrilled|"
            r"first (?:home|house)|congrats|wonderful|love (?:it|that)"
            r")\b",
            re.I,
        ),
    ),
    (
        "rushed",
        re.compile(
            r"\b("
            r"in a hurry|quick(?:ly)?|don'?t have (?:much )?time|gotta go|"
            r"running late|make it (?:fast|quick)|keep (?:it )?short|"
            r"i'?m busy|wrap it up"
            r")\b",
            re.I,
        ),
    ),
    (
        "hesitant",
        re.compile(
            r"\b("
            r"not sure|unsure|maybe|i (?:guess|think)|kind of|sort of|"
            r"nervous|worried|hesitat\w*|don'?t know|still figuring|"
            r"is that (?:okay|ok|fine)\?"
            r")\b",
            re.I,
        ),
    ),
]


def detect_mood(text: str) -> Mood:
    """Return the strongest mood hint in ``text``, else ``neutral``."""
    t = (text or "").strip()
    if not t:
        return "neutral"
    for mood, pat in _PATTERNS:
        if pat.search(t):
            return mood
    return "neutral"


def mood_cue(mood: Mood) -> str:
    return MOOD_CUES.get(mood, MOOD_CUES["neutral"])


def mood_voice_context(mood: Mood) -> str:
    return MOOD_TO_VOICE_CONTEXT.get(mood, "default")
