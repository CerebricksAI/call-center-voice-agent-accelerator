"""Skill loader — compose per-stage instructions from Markdown files.

The behavior plane. ``compose(state, ctx)`` returns:

    00_global_guardrails.md
    ---
    <the current stage's skill file>
    ---
    VERIFIED FACTS: <rendered from ctx>

Files are read fresh on every call, so editing a skill changes behavior with no
redeploy. Placeholders ({brokerage_name}, {first_name}, {followup_window},
{close_name}) are substituted from ctx, falling back to the persona constants.

Composition is wired into the live session by the orchestrator in Phase 3; until
then this module is unit-tested directly (see server/tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app.agent_persona import (
    BROKERAGE_NAME,
    FOLLOWUP_WINDOW,
    LEAD_SOURCE,
    PRIOR_NOTES,
)

SKILLS_DIR = Path(__file__).parent
GLOBAL_SKILL = "00_global_guardrails.md"
SEPARATOR = "\n\n---\n\n"

# One stage -> one skill file. The orchestrator FSM (Phase 3) drives these states.
SKILL_FOR_STATE: dict[str, str] = {
    "GREETING": "10_greeting_intro.md",
    "QUALIFY": "20_qualify_core.md",
    "DECLINE_CLOSE": "30_decline_close.md",
    "DNC_CLOSE": "31_optout_dnc_close.md",
    "CALLBACK_CLOSE": "32_callback_close.md",
    "TRANSFER": "40_transfer_escalation.md",
    "LANGUAGE_ROUTE": "50_language_route.md",
}


def _read(name: str) -> str:
    return (SKILLS_DIR / name).read_text(encoding="utf-8").strip()


def skill_for_state(state: str) -> str:
    """Return the skill filename for a state, or raise for an unknown state."""
    try:
        return SKILL_FOR_STATE[state]
    except KeyError as exc:
        raise KeyError(
            f"No skill mapped for state {state!r}; known: {sorted(SKILL_FOR_STATE)}"
        ) from exc


def available_skills() -> list[str]:
    """Every skill .md in the directory (excludes README)."""
    return sorted(
        p.name
        for p in SKILLS_DIR.glob("*.md")
        if p.name.lower() != "readme.md"
    )


def render_facts(ctx: Mapping[str, Any] | None) -> str:
    """Render the VERIFIED FACTS block from call context.

    The model may state only what appears here — this is the anti-hallucination
    boundary referenced by 00_global_guardrails.md.
    """
    ctx = ctx or {}
    name = str(ctx.get("borrower_name", "")).strip()
    lead_source = str(ctx.get("lead_source", LEAD_SOURCE)).strip()
    prior_notes = str(ctx.get("prior_notes", PRIOR_NOTES)).strip()

    lines = ["VERIFIED FACTS (state only what appears here; do not invent):"]
    lines.append(
        f"- Caller name: {name}" if name else "- Caller name: unknown (greet without a name)"
    )
    if lead_source:
        lines.append(f"- Lead source: {lead_source}")
    if prior_notes:
        lines.append(f"- Prior contact: {prior_notes}")
    return "\n".join(lines)


def _apply_placeholders(text: str, ctx: Mapping[str, Any] | None) -> str:
    ctx = ctx or {}
    first_name = str(ctx.get("borrower_name", "")).strip().split(" ")[0]
    brokerage = str(ctx.get("brokerage_name", "") or BROKERAGE_NAME).strip()
    followup = str(ctx.get("followup_window", "") or FOLLOWUP_WINDOW).strip()
    close_name = f", {first_name}" if first_name else ""
    return (
        text.replace("{brokerage_name}", brokerage)
        .replace("{first_name}", first_name)
        .replace("{followup_window}", followup)
        .replace("{close_name}", close_name)
    )


def compose(state: str, ctx: Mapping[str, Any] | None = None) -> str:
    """Compose the live instructions for a stage: guardrails + stage + facts."""
    parts = [_read(GLOBAL_SKILL), _read(skill_for_state(state)), render_facts(ctx)]
    composed = SEPARATOR.join(part for part in parts if part)
    return _apply_placeholders(composed, ctx)
