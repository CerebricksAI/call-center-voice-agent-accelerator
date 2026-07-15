"""Trust-console UI helpers — display labels only; never change gate outcomes.

Emits ride the existing AgentEvent channel. Stage labels and briefing chips are
for the pre-qual console ribbon/receipt; compliance logic stays in intents/dialog.
"""

from __future__ import annotations

from typing import Any

from skills.loader import SKILL_FOR_STATE, skill_for_state

# Workflow ribbon: FSM state -> customer-facing stage node.
STAGE_LABEL: dict[str, str] = {
    "GREETING": "INTRO",
    "QUALIFY": "QUALIFY",
    "DECLINE_CLOSE": "DECLINE CLOSE",
    "DNC_CLOSE": "DNC CLOSE",
    "CALLBACK_CLOSE": "CALLBACK CLOSE",
    "NO_RESPONSE_CLOSE": "NO RESPONSE",
    "TRANSFER": "TRANSFER",
    "LANGUAGE_ROUTE": "LANGUAGE",
    "ENDED": "ENDED",
}

# Briefing chip: skill file -> short name (matches concept "guardrails + …").
SKILL_SHORT: dict[str, str] = {
    "00_global_guardrails.md": "guardrails",
    "10_greeting_intro.md": "intro",
    "20_qualify_core.md": "qualify",
    "30_decline_close.md": "decline close",
    "31_optout_dnc_close.md": "dnc close",
    "32_callback_close.md": "callback close",
    "33_no_response_close.md": "no-response close",
    "40_transfer_escalation.md": "transfer",
    "50_language_route.md": "language",
}

# Measured gates shown in the trust rail (targets are product SLOs, not hard cutoffs).
GATE_TTFA_P50_S = 0.80
GATE_INTERRUPT_MS = 300
GATE_TURNS_AFTER_OPT_OUT = 1

# Linear stage order for the ribbon (classify is always armable on live web).
RIBBON_CORE = ("CLASSIFY · human", "INTRO", "QUALIFY")

# Side / close stages that append onto the ribbon (kept visible when moving on).
_RIBBON_BRANCH = frozenset(
    {
        "DECLINE_CLOSE",
        "DNC_CLOSE",
        "CALLBACK_CLOSE",
        "NO_RESPONSE_CLOSE",
        "TRANSFER",
        "LANGUAGE_ROUTE",
        "ENDED",
    }
)


def stage_label(state: str) -> str:
    return STAGE_LABEL.get(state, state.replace("_", " "))


def briefing_for_state(state: str) -> dict[str, Any]:
    """Return human briefing chip + approx byte size of composed skills."""
    from skills.loader import compose

    try:
        skill = skill_for_state(state)
    except KeyError:
        skill = ""
    parts = ["guardrails"]
    short = SKILL_SHORT.get(skill)
    if short:
        parts.append(short)
    text = compose(state, {}) if state in SKILL_FOR_STATE else ""
    return {
        "skills": parts,
        "label": " + ".join(parts),
        "bytes": len(text.encode("utf-8")),
        "skillFile": skill,
    }


def fsm_state_history(fsm: Any) -> list[str]:
    """Ordered unique-adjacent FSM states visited this call (for the ribbon trail)."""
    transitions = list(getattr(fsm, "transitions", None) or [])
    current = getattr(fsm, "state", None)
    if not transitions:
        return [str(current)] if current else []
    out = [str(transitions[0].get("from") or "")]
    for t in transitions:
        to = str(t.get("to") or "")
        if to and (not out or out[-1] != to):
            out.append(to)
    if current and (not out or out[-1] != str(current)):
        out.append(str(current))
    return [s for s in out if s]


def ribbon_stages(
    state: str, *, history: list[str] | None = None
) -> list[dict[str, str]]:
    """Build ribbon nodes with status done | active | pending.

    Progression the UI expects:
      PRE_CALL / IDLE  → CLASSIFY active (before Start Call / connecting)
      GREETING           → CLASSIFY done, INTRO active (live call started)
      QUALIFY            → INTRO done, QUALIFY active (consent / first real turn)
      close states       → … + each visited branch (CALLBACK then DNC stays visible)

    ``history`` is the ordered FSM trail (e.g. from ``fsm_state_history``). When
    the caller moves CALLBACK → DNC, both close nodes remain on the ribbon —
    earlier branches are ``done``, the current state is ``active``.
    """
    st = (state or "PRE_CALL").strip().upper()
    if st in ("PRE_CALL", "IDLE", "CLASSIFY", ""):
        return [
            {"id": "classify", "label": RIBBON_CORE[0], "status": "active"},
            {"id": "intro", "label": "INTRO", "status": "pending"},
            {"id": "qualify", "label": "QUALIFY", "status": "pending"},
        ]

    hist = [str(s).strip().upper() for s in (history or []) if s]
    if not hist or hist[-1] != st:
        hist = hist + [st]

    nodes: list[dict[str, str]] = [
        {"id": "classify", "label": RIBBON_CORE[0], "status": "done"},
    ]
    if st == "GREETING":
        nodes.append({"id": "intro", "label": "INTRO", "status": "active"})
        nodes.append({"id": "qualify", "label": "QUALIFY", "status": "pending"})
        return nodes
    if st == "QUALIFY":
        nodes.append({"id": "intro", "label": "INTRO", "status": "done"})
        nodes.append({"id": "qualify", "label": "QUALIFY", "status": "active"})
        return nodes

    # Past QUALIFY (or jumped into a branch): core done, then every visited branch.
    nodes.append({"id": "intro", "label": "INTRO", "status": "done"})
    nodes.append({"id": "qualify", "label": "QUALIFY", "status": "done"})

    branch: list[str] = []
    for s in hist:
        if s in _RIBBON_BRANCH and s not in branch:
            branch.append(s)
    if st in _RIBBON_BRANCH and st not in branch:
        branch.append(st)
    if not branch:
        branch = [st]

    for s in branch:
        nodes.append(
            {
                "id": f"branch-{s.lower()}",
                "label": stage_label(s),
                "status": "active" if s == st else "done",
            }
        )
    return nodes


def dnc_record_id(call_id: str | None) -> str:
    raw = (call_id or "local").replace("-", "")
    suffix = (raw[-4:] or "0000").upper()
    return f"DNC {suffix}"


def call_short_id(call_id: str | None) -> str:
    raw = (call_id or "local").replace("-", "").upper()
    return (raw[:6] or "LOCAL") if len(raw) >= 6 else raw or "LOCAL"


# Receipt header: FSM action / close -> "do not call event" style labels.
RECEIPT_EVENT_LABEL: dict[str, str] = {
    "DNC_CLOSE": "do not call event",
    "CALLBACK_CLOSE": "callback event",
    "DECLINE_CLOSE": "decline event",
    "ESCALATE": "transfer event",
    "TRANSFER": "transfer event",
    "LANGUAGE_ROUTE": "language event",
    "NO_RESPONSE_CLOSE": "no-response event",
    "REBUTTAL_ONCE": "rebuttal event",
}


def receipt_event_label(action_or_state: str) -> str:
    return RECEIPT_EVENT_LABEL.get(action_or_state, "call event")


def caller_quote(text: str, *, max_len: int = 48) -> str:
    """Compact caller snippet for receipt lines (display only)."""
    t = " ".join((text or "").split())
    if not t:
        return "…"
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t

