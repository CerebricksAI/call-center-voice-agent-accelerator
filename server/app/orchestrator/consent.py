"""TCPA consent helpers for the GREETING disclosure turn.

Live routing prefers the semantic router (``semantic.route_conversation``) so
phrases like \"let's go\" don't need a keyword list. This module remains as:

  * a fast refuse floor for unmistakable disclosure rejection (no LLM wait), and
  * an offline fallback when ``SEMANTIC_INTENT_ENABLED`` is off / no endpoint.
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
    r"\bno\b.*\b(compliance|consent|record|recording)\b",
    r"\b(compliance|consent|record|recording)\b.*\bno\b",
]

# Clear confirm of the disclosure question ("Does that work for you?").
_AFFIRM = [
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|alright|all right)\b",
    r"\b(yes|yeah|yep)\b.*\b(work|fine|ok|okay|good)\b",
    r"\b(that'|that )?works\b",
    r"\bgo ahead\b",
    r"\blet'?s go\b",
    r"\blets go\b",
    r"\bdive in\b",
    r"\bthat'?s fine\b",
    r"\bsounds good\b",
    r"\bof course\b",
    r"\bi'?m ready\b",
    r"\bready\b",
    r"\bi (consent|agree)\b",
    r"\bcontinue\b",
    r"\bproceed\b",
    r"\babsolutely\b",
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
