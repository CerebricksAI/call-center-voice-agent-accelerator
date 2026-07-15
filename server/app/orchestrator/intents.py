"""Compliance floor — the ONE deterministic gate, in code, never in a prompt.

``gate()`` runs on every finalized caller turn before the model responds and fires
ONLY on a hard opt-out ("do not call") — the single intent that legally must never
be missed (TCPA). Every other intent (decline, callback, escalate, language) is
understood semantically by the router (``app.orchestrator.semantic``), so the gate
stays silent on those and lets the model's analysis decide.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

Action = Literal[
    "DNC_CLOSE",       # hard opt-out — must be honored (TCPA); the ONLY gated action
    "ESCALATE",        # hand to a human (router-decided)
    "LANGUAGE_ROUTE",  # continue in another language (router-decided)
    "CALLBACK_CLOSE",  # arrange a callback (router-decided)
    "DECLINE_CLOSE",   # caller declines — close gracefully (router-decided)
]

# Hard opt-out patterns — the compliance backstop. Deliberately broad: over-honoring
# an opt-out is safe; MISSING one is a legal violation, so this stays deterministic
# even though all other intent is now semantic.
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
    r"\bsecond time i'?ve (told|asked)\b",
    # T9 creative opt-outs — honor over-broadly (missing one is the legal risk)
    r"\bquit calling\b",
    r"\bcancel (it|this|the call)\b",
    r"\bdon'?t (want to )?be contacted\b",
    r"\bdo not (want to )?be contacted\b",
    r"\b(don'?t|do not) contact me (again|anymore)\b",
    r"\bnever contact\b",
    r"\bno (more )?contact\b",
    # Soft "don't contact me again" / "not this call again" — DNC, not mere decline
    r"\b(this )?call again\b",
    r"\bproceed .{0,40}\bagain\b",
    r"\b(would not|wouldn'?t|don'?t|do not) (like to )?proceed .{0,40}again\b",
    r"\bnot (want to )?do this (call )?again\b",
]


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def matched_opt_out(text: str) -> Optional[str]:
    """Return the matched opt-out phrase for UI receipts — does not change gate logic."""
    text = (text or "").strip()
    if not text:
        return None
    for pattern in OPT_OUT_HARD:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def gate(text: str, ctx: Any = None) -> Optional[Action]:
    """Compliance floor: return ``DNC_CLOSE`` on a hard opt-out, otherwise None.

    Anything that is not an unambiguous opt-out returns None so the semantic router
    can analyse it. This is the only deterministic keyword check in the system.
    """
    text = (text or "").strip()
    if not text:
        return None
    if _hit(OPT_OUT_HARD, text):
        return "DNC_CLOSE"
    return None
