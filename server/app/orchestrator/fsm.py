"""Call state machine + call memory — small, boring, and therefore trustworthy.

The FSM knows which stage a call is in and which moves are legal. It never speaks;
it decides the stage and holds the verified facts. A call can never end without a
disposition (``can_end``), so no call slips away undispositioned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent_persona import LEAD_SOURCE, PRIOR_NOTES

STATES = frozenset(
    {
        "GREETING",
        "QUALIFY",
        "DECLINE_CLOSE",
        "DNC_CLOSE",
        "CALLBACK_CLOSE",
        "NO_RESPONSE_CLOSE",
        "TRANSFER",
        "LANGUAGE_ROUTE",
        "ENDED",
    }
)

# Close states — reachable from any active state via a gate, and lead to ENDED.
CLOSE_STATES = frozenset(
    {
        "DECLINE_CLOSE",
        "DNC_CLOSE",
        "CALLBACK_CLOSE",
        "NO_RESPONSE_CLOSE",
        "LANGUAGE_ROUTE",
        "TRANSFER",
    }
)


@dataclass
class CallContext:
    """Everything the workflow plane remembers about one call."""

    call_id: str = "local"
    borrower_name: str = ""
    lead_source: str = LEAD_SOURCE
    prior_notes: str = PRIOR_NOTES

    rebuttal_used: bool = False
    silence_count: int = 0

    disposition: str | None = None
    dnc_recorded: bool = False
    callback_scheduled: bool = False
    ended: bool = False

    fields: dict[str, Any] = field(default_factory=dict)
    tool_log: list[dict[str, Any]] = field(default_factory=list)

    def facts(self) -> dict[str, Any]:
        """The verified-facts view handed to the skill loader (compose)."""
        return {
            "borrower_name": self.borrower_name,
            "lead_source": self.lead_source,
            "prior_notes": self.prior_notes,
        }


class CallStateMachine:
    """Tracks the current stage and logs every transition (call_id, from, to, reason)."""

    def __init__(self, state: str = "GREETING", *, call_id: str = "local") -> None:
        if state not in STATES:
            raise ValueError(f"Unknown initial state {state!r}")
        self.call_id = call_id
        self.state = state
        self.transitions: list[dict[str, str]] = []

    def transition(self, to: str, *, reason: str) -> None:
        if to not in STATES:
            raise ValueError(f"Unknown target state {to!r}")
        self.transitions.append(
            {"call_id": self.call_id, "from": self.state, "to": to, "reason": reason}
        )
        self.state = to

    def can_end(self, ctx: CallContext) -> bool:
        """A call may only end once a disposition has been recorded."""
        return ctx.disposition is not None

    def end(self, ctx: CallContext, *, reason: str = "completed") -> bool:
        """Move to ENDED iff a disposition exists. Returns whether it ended."""
        if not self.can_end(ctx):
            return False
        if self.state != "ENDED":
            self.transition("ENDED", reason=reason)
        return True
