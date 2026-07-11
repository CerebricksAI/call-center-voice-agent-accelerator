"""Disengagement / routing gate — compliance in code, never in a prompt.

``gate()`` runs on every finalized caller turn BEFORE the model gets a vote. It
returns a forced Action (deterministic) or None (let the model respond normally).
Order is load-bearing: a hard opt-out outranks everything, including politeness.
"""

from __future__ import annotations

import re
from typing import Literal, Optional, Protocol

Action = Literal[
    "DNC_CLOSE",       # hard opt-out — must be honored (TCPA)
    "ESCALATE",        # hand to a human now (hardship / requested human / abuse)
    "LANGUAGE_ROUTE",  # continue in another language
    "CALLBACK_CLOSE",  # caller is busy — arrange a callback
    "REBUTTAL_ONCE",   # first soft decline — one respectful nudge is allowed
    "DECLINE_CLOSE",   # decline after the nudge is spent — close gracefully
]


class _HasRebuttal(Protocol):
    rebuttal_used: bool


# Case-insensitive substring/phrase patterns. Kept plain and readable on purpose.
OPT_OUT_HARD = [
    r"\bstop calling\b",
    r"\bstop\b.*\bcall",
    r"\bdo not call\b",
    r"\bdon'?t call\b",
    r"\btake me off\b",
    r"\boff (your|the) (call |contact )?list\b",  # strike/get/take ... off your list
    r"\bremove me\b",
    r"\bi want to be removed\b",
    r"\bunsubscribe\b",
    r"\bnever call\b",
    r"\bhang up\b",
    r"\bsecond time i'?ve (told|asked)\b",  # the soft path already failed once
]

ESCALATE = [
    r"\breal person\b",
    r"\bspeak (to|with) (a )?(human|person|someone|agent|rep)\b",
    r"\btalk to (a )?(human|person|someone)\b",
    r"\bloan officer now\b",
    r"\bbehind on (my )?payments?\b",
    r"\bbankruptc",
    r"\bforeclos",
    r"\bgoing through a divorce\b",
    r"\bhardship\b",
]

LANGUAGE = [
    r"\bespa[nñ]ol\b",
    r"\bspanish\b",
    r"\bno hablo ingl[eé]s\b",
    r"\bhablas espa[nñ]ol\b",
]

BUSY_CALLBACK = [
    r"\bi'?m busy\b",
    r"\bbad time\b",
    r"\bnot a good time\b",
    r"\bcall me (back )?later\b",
    r"\bcall me back\b",
    r"\bi'?m driving\b",
    r"\bin a meeting\b",
]

END_NOW = [
    r"\bend (this|the) call\b",
    r"\bend (this|the) conversation\b",
    r"\blet'?s end\b",
]

DECLINE_SOFT = [
    r"\bnot interested\b",
    r"\bno thanks?\b",
    r"\bno thank you\b",
    r"\bi'?m good\b",
    r"\bnot right now\b",
    r"\bdon'?t want\b",
    r"\bnot looking\b",
]


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def gate(text: str, ctx: _HasRebuttal) -> Optional[Action]:
    """Return a forced Action for this caller turn, or None to let the model act."""
    text = (text or "").strip()
    if not text:
        return None

    if _hit(OPT_OUT_HARD, text):
        return "DNC_CLOSE"
    if _hit(ESCALATE, text):
        return "ESCALATE"
    if _hit(LANGUAGE, text):
        return "LANGUAGE_ROUTE"
    if _hit(BUSY_CALLBACK, text):
        return "CALLBACK_CLOSE"
    if _hit(END_NOW, text):
        # Explicit termination request — close gracefully, never nudge. Ranks above
        # DECLINE_SOFT so "end this call" doesn't burn a rebuttal on "don't want".
        return "DECLINE_CLOSE"
    if _hit(DECLINE_SOFT, text):
        # Exactly one rebuttal, ever — enforced here in code, not by the prompt.
        if getattr(ctx, "rebuttal_used", False):
            return "DECLINE_CLOSE"
        ctx.rebuttal_used = True
        return "REBUTTAL_ONCE"
    return None
