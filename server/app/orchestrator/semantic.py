"""Semantic intent router — the model analyses the WHOLE conversation and decides.

This is the pure-semantic layer: there is NO keyword matching on the caller's words
here. ``route_conversation`` sends the running transcript to the model and asks for
one intent label, which maps to a forced ``Action`` (or None = continue). It runs on
every qualifying turn the deterministic opt-out gate (``intents.gate``) let through.
Reuses the extractor's text-only Voice Live path (no new dependency).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.orchestrator.intents import Action

logger = logging.getLogger(__name__)


def semantic_enabled() -> bool:
    """Feature flag (default on). Set SEMANTIC_INTENT_ENABLED=false to disable."""
    return os.getenv("SEMANTIC_INTENT_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# --- whole-conversation intent router ---------------------------------------

_ROUTER_SYSTEM = """You route a mortgage pre-qualification phone call. Read the whole conversation, but decide based on what the CALLER'S LATEST turn means IN CONTEXT. Reply with EXACTLY one label. The default is CONTINUE — only pick another label when the caller's intent is UNMISTAKABLE.

- CONTINUE — the caller is engaging with the call: answering, asking, hesitating, chit-chatting, OR wanting to keep going ("let's resume", "let's continue", "go on", "okay", "sure", "proceed", "start"). If in any doubt, choose this.
- OPT_OUT — the caller explicitly wants to NEVER be contacted again / be removed from the list / stop all future contact ("take me off your list", "don't ever call me", "remove me").
- DECLINE — the caller clearly does NOT want to continue this application at all ("I'm not interested", "I don't want to do this", "stop the application", "I'm done"). Note: a single "I don't want to proceed right now" may just be hesitation — only DECLINE if they clearly want to stop.
- CALLBACK — the caller explicitly wants to be contacted LATER or do this ANOTHER time ("call me back", "can we do this tomorrow", "not now, later", "I'm busy, another time"). NOT for "let's resume/continue now".
- ESCALATE — the caller wants a human / real loan officer, wants a specific rate/quote/program, is in financial hardship, or is hostile.
- LANGUAGE — the caller would rather continue in another language.

Rules:
- "Resume", "continue", "go on", "keep going", "proceed", "start" all mean CONTINUE — never CALLBACK.
- Changing an earlier answer or SWITCHING loan type (purchase <-> refinance <-> cash-out <-> home equity) is CONTINUE — the caller is still engaged, just changing a detail. Never DECLINE.
- "Changed my mind" followed by a NEW request (e.g. "...I'd like to refinance instead") is CONTINUE, not DECLINE.
- When unsure, answer CONTINUE. Only route on an unmistakable intent in the latest turn.
- Output the single label only — no punctuation, no explanation."""

# Router label -> forced gate Action (CONTINUE and anything unmatched -> None).
_ROUTER_LABELS: list[tuple[str, Action]] = [
    ("OPT_OUT", "DNC_CLOSE"),
    ("DECLINE", "DECLINE_CLOSE"),
    ("CALLBACK", "CALLBACK_CLOSE"),
    ("ESCALATE", "ESCALATE"),
    ("LANGUAGE", "LANGUAGE_ROUTE"),
]


def route_label(raw: str) -> Optional[Action]:
    """Map the router's one-word reply to a forced Action, or None (=continue)."""
    label = (raw or "").strip().upper()
    for token, action in _ROUTER_LABELS:
        if token in label:
            return action
    return None


def format_transcript(turns: list[dict]) -> str:
    """Render [{role, text}] turns as 'Caller:/Agent:' lines for the router prompt."""
    lines = []
    for t in turns:
        role = "Caller" if (t.get("role") or "").lower() in ("user", "caller") else "Agent"
        text = (t.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


async def route_conversation(turns: list[dict]) -> Optional[Action]:
    """Whole-conversation intent router. Returns a forced Action or None (=continue).

    Runs ONLY when the deterministic keyword gate is silent, so it can add coverage
    but never weakens the compliance floor.
    """
    transcript = format_transcript(turns)
    if not transcript:
        return None
    endpoint = os.getenv("AZURE_VOICE_LIVE_ENDPOINT", "").strip()
    if not endpoint:
        return None

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
            f"Conversation so far:\n{transcript}\n\nLabel the caller's current intent:",
            instructions=_ROUTER_SYSTEM,
            temperature=0.0,
            max_output_tokens=16,
        )
    except Exception:
        logger.debug("[Router] classification failed", exc_info=True)
        return None

    action = route_label(raw)
    if action is not None:
        logger.info("[Router] -> %s (%s)", raw.strip(), action)
    return action
