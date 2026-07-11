"""Semantic disengagement classifier — the control plane's fallback intent gate.

The deterministic keyword gate (``intents.gate``) is the compliance floor and runs
first. This classifier runs ONLY when the keyword gate finds nothing, so it can add
coverage for phrasings we didn't hard-code ("I'm done here", "gotta run", "let's
wrap this up") but can never weaken the guaranteed keyword detection.

It reuses the same text-only Voice Live path as the conversation extractor (no new
dependency) and returns a forced ``Action`` (``DNC_CLOSE`` / ``DECLINE_CLOSE``) or
None. When unsure it returns None — the model then responds normally.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.orchestrator.intents import Action

logger = logging.getLogger(__name__)

_SYSTEM = """You classify ONE utterance from the CALLER on a phone call with a mortgage pre-qualification agent. Decide the caller's intent and reply with EXACTLY one word:

- OPT_OUT — the caller wants to never be contacted again, be removed from the list, or stop all future contact.
- END — the caller clearly wants to hang up / stop this call now AND is not asking to continue later (e.g. "I'm done", "I have to go, goodbye", "let's just stop", "please hang up"). Do NOT use END if they want to be removed from a list (that is OPT_OUT).
- CONTINUE — everything else. This INCLUDES answering or asking a question, hesitating, changing an answer, chit-chat, being unclear, AND wanting to reconnect LATER / be called back / do this another time / "we'll connect later" / "call me later" / "I'm in a hurry" (the agent arranges callbacks itself — that is NOT END).

Rules:
- Judge ONLY the caller's words; do not infer beyond what they clearly express.
- When in doubt, answer CONTINUE. Only answer END or OPT_OUT when the intent is unmistakable and the caller wants no further conversation now or later.
- Output the single word only — no punctuation, no explanation."""

# Which forced Action each label maps to. OPT_OUT is the stronger signal.
_ACTION_FOR_LABEL: list[tuple[str, Action]] = [
    ("OPT_OUT", "DNC_CLOSE"),
    ("OPTOUT", "DNC_CLOSE"),
    ("END", "DECLINE_CLOSE"),
]


def semantic_enabled() -> bool:
    """Feature flag (default on). Set SEMANTIC_INTENT_ENABLED=false to disable."""
    return os.getenv("SEMANTIC_INTENT_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def map_label(raw: str) -> Optional[Action]:
    """Map the classifier's one-word reply to a forced Action, or None."""
    label = (raw or "").strip().upper()
    for token, action in _ACTION_FOR_LABEL:
        if token in label:
            return action
    return None


async def classify_disengagement(utterance: str) -> Optional[Action]:
    """Classify a caller turn's disengagement intent. None = let the model respond."""
    text = (utterance or "").strip()
    if not text:
        return None
    endpoint = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").strip()
    if not endpoint:
        return None

    # Lazy import: reuse the extractor's text-only Voice Live path (no new client).
    from app.conversation_extractor import (
        _build_extract_credential,
        _voicelive_text_completion,
        resolve_extract_model,
    )

    credential = _build_extract_credential()
    if credential is None:
        return None
    try:
        raw, _usage = await _voicelive_text_completion(
            endpoint,
            credential,
            resolve_extract_model(),
            f'Caller just said: "{text}"',
            instructions=_SYSTEM,
            temperature=0.0,
            max_output_tokens=16,
        )
    except Exception:
        logger.debug("[Semantic] classification failed", exc_info=True)
        return None

    action = map_label(raw)
    if action is not None:
        logger.info("[Semantic] '%s' -> %s (%s)", text[:60], raw.strip(), action)
    return action
