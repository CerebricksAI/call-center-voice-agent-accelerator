"""TCPA consent confirm/refuse detection for the GREETING disclosure turn.

Runs in the orchestrator (not skills): a hard refuse to \"Does that work for you?\"
must not advance into QUALIFY.
"""

from __future__ import annotations

import re

# Explicit refuse of the recording/contact disclosure.
_REFUSAL = [
    r"\bdoesn'?t work\b",
    r"\bdoes not work\b",
    r"\bdon'?t (agree|consent)\b",
    r"\bdo not (agree|consent)\b",
    r"\bi (don'?t|do not) (agree|consent|want)\b",
    r"\bno (thanks|thank you)\b",
    r"\bnot (interested|comfortable|okay|ok)\b",
    r"\bi disagree\b",
    r"\bwithout (my )?consent\b",
    r"\bnot (giving|giving you) consent\b",
    r"\bi'?m not (giving|ok|okay|comfortable)\b",
    r"\bstop\b",
    r"\bno\b.*\b(compliance|consent|record|recording)\b",
    r"\b(compliance|consent|record|recording)\b.*\bno\b",
]

# Clear confirm of the disclosure question.
_AFFIRM = [
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|alright|all right)\b",
    r"\b(yes|yeah|yep)\b.*\b(work|fine|ok|okay|good)\b",
    r"\b(that'|that )?works\b",
    r"\bgo ahead\b",
    r"\bthat'?s fine\b",
    r"\bi (consent|agree)\b",
    r"\bcontinue\b",
    r"\bproceed\b",
]


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def is_consent_refusal(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return _hit(_REFUSAL, t)


def is_consent_affirm(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if is_consent_refusal(t):
        return False
    return _hit(_AFFIRM, t)
