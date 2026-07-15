"""Caller silence policy — quiet gaps after each agent utterance.

Model (matches phone UX):
  * Agent starts speaking  → silence wait is cancelled / reset.
  * Agent finishes speaking → a fresh quiet countdown starts.
  * After check-in #1 finishes → another full gap before check-in #2 (not
    leftover seconds from a running cumulative clock).

``reprompt_at_s`` / ``close_at_s`` in session.yaml are absolute marks; we derive
gaps between them: e.g. [15, 30] + close 45 → waits of 15s, then 15s, then 15s.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SilenceEvent = Literal["reprompt", "close"]


@dataclass(frozen=True)
class SilencePolicy:
    # Absolute marks in session.yaml; gaps between them drive each post-speech wait.
    # Keep generous enough for mid-turn thinking.
    reprompt_at_s: tuple[float, ...] = (15.0, 30.0)
    close_at_s: float = 45.0
    disposition: str = "no_response"


SILENCE_WATCH_STATES = frozenset({"QUALIFY"})

REPROMPT_CUES: tuple[str, ...] = (
    "The caller has been quiet mid-qualify. Say ONE short sentence checking if "
    "they are still there — e.g. \"Just checking — are you still with me?\" "
    "Then stop and wait. Do NOT ask a new loan question.",
    "Still quiet. Say ONE short sentence that you can pick back up when they're "
    "ready — e.g. \"No rush — I'm here when you want to continue.\" "
    "Do NOT ask to wrap up. Do NOT ask a new loan question.",
)

SILENCE_CHECKIN_RULES = """
HARD RULES for this single turn:
- Speak ONLY one short check-in sentence. Then stop.
- Do NOT ask loan purpose, buy vs refinance, cash-out, amount, income, timeline,
  or any other qualifying question.
- Do NOT pretend the caller answered. Never say "Got it", "Thanks for letting me
  know", "Perfect", or continue the script.
- Do NOT call tools.
""".strip()

DECLINE_CLOSE_RULES = """
HARD RULES for this DECLINE CLOSE turn:
- Speak ONLY one or two short goodbye sentences. Then stop.
- Do NOT ask buy vs refinance, location, timeline, credit, income, or any loan question.
- Do NOT try to keep qualifying or "focus on the basics."
- Do NOT offer options that continue the application.
""".strip()

NO_RESPONSE_CLOSE_RULES = """
HARD RULES for this NO-RESPONSE CLOSE turn:
- Speak ONLY one short goodbye (you may have lost them / wrapping up / take care).
- Do NOT ask if they are still there again.
- Do NOT ask buy vs refinance, location, timeline, credit, income, or any loan question.
- Do NOT continue qualifying or offer a callback.
- Do NOT invent that they answered anything.
""".strip()

DNC_CLOSE_RULES = """
HARD RULES for this DNC CLOSE turn:
- Speak ONLY a brief respectful goodbye. Then stop.
- Do NOT ask any loan or qualifying questions.
- Do NOT offer a callback or future contact.
""".strip()


def quiet_elapsed(
    now: float,
    *,
    anchor: float,
    paused_total: float = 0.0,
    paused_at: float | None = None,
) -> float:
    """Seconds of caller-silence with agent-speaking intervals excluded."""
    paused = paused_total
    if paused_at is not None:
        paused += max(0.0, now - paused_at)
    return max(0.0, now - anchor - paused)


def load_silence_policy(*, config_path: Path | None = None) -> SilencePolicy:
    """Read silence: from session.yaml when present; else built-in defaults."""
    path = config_path or (
        Path(__file__).resolve().parents[2] / "config" / "session.yaml"
    )
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        block = raw.get("silence") or {}
        reprompts = block.get("reprompt_at_s") or [15, 30]
        close_at = float(block.get("close_at_s") or 45)
        disposition = str(block.get("disposition") or "no_response")
        return SilencePolicy(
            reprompt_at_s=tuple(float(x) for x in reprompts),
            close_at_s=close_at,
            disposition=disposition,
        )
    except Exception:
        return SilencePolicy()


def _marks(policy: SilencePolicy) -> list[tuple[str, SilenceEvent, int, float]]:
    """Ordered (key, event, index, absolute_mark_s)."""
    out: list[tuple[str, SilenceEvent, int, float]] = []
    for i, at in enumerate(policy.reprompt_at_s):
        out.append((f"reprompt:{i}", "reprompt", i, float(at)))
    out.append(("close", "close", -1, float(policy.close_at_s)))
    return out


def next_silence_step(
    *,
    fired: set[str],
    policy: SilencePolicy | None = None,
) -> tuple[SilenceEvent, int, float] | None:
    """Next (event, index, quiet_gap_s) after the agent just finished speaking.

    Gap is measured from a fresh zero — a full wait after this utterance ends —
    not leftover time on a cumulative clock.
    """
    policy = policy or SilencePolicy()
    prev = 0.0
    for key, kind, index, mark in _marks(policy):
        if key not in fired:
            return (kind, index, max(0.05, mark - prev))
        prev = mark
    return None


def next_silence_event(
    elapsed_s: float,
    *,
    fired: set[str],
    policy: SilencePolicy | None = None,
) -> tuple[SilenceEvent, int] | None:
    """Return the next due event if ``elapsed_s`` already covers its gap from zero."""
    step = next_silence_step(fired=fired, policy=policy)
    if step is None:
        return None
    kind, index, gap = step
    if elapsed_s >= gap:
        return (kind, index)
    return None


def seconds_until_next(
    elapsed_s: float,
    *,
    fired: set[str],
    policy: SilencePolicy | None = None,
) -> float | None:
    """Quiet seconds still needed (from a fresh post-speech window) for the next step."""
    step = next_silence_step(fired=fired, policy=policy)
    if step is None:
        return None
    _, _, gap = step
    remain = gap - elapsed_s
    return remain if remain > 0 else 0.0
