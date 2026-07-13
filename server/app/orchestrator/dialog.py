"""The shared brain: turn a finalized caller turn into a workflow decision.

``handle_caller_turn`` is transport-agnostic and side-effect-honest:
  1. run the gate; if it stays silent (None), the model responds normally.
  2. otherwise fire any COMPLIANCE tools BEFORE the model speaks (so a close skill
     can truthfully say "already recorded"), set the disposition,
  3. transition the FSM, and
  4. recompose the skill instructions + tool allow-list for the new state.

The live handler (Phase 3b) calls this, then does session.update + response
cancel/create. The text driver in tests calls the exact same function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.orchestrator.fsm import CallContext, CallStateMachine
from app.orchestrator.intents import Action, gate
from app.orchestrator.tools import execute_tool, tools_for
from skills.loader import compose

# Gate Action -> the state to enter (REBUTTAL_ONCE stays in QUALIFY).
ACTION_TO_STATE: dict[Action, str] = {
    "DNC_CLOSE": "DNC_CLOSE",
    "ESCALATE": "TRANSFER",
    "LANGUAGE_ROUTE": "LANGUAGE_ROUTE",
    "CALLBACK_CLOSE": "CALLBACK_CLOSE",
    "DECLINE_CLOSE": "DECLINE_CLOSE",
    "REBUTTAL_ONCE": "QUALIFY",
}

# Disposition recorded in code when a gate forces a terminal state.
_DISPOSITION_FOR_ACTION: dict[Action, str] = {
    "DNC_CLOSE": "do_not_call",
    "DECLINE_CLOSE": "declined",
    "CALLBACK_CLOSE": "callback_requested",
    "ESCALATE": "transferred",
    "LANGUAGE_ROUTE": "language_routed",
}


@dataclass
class Decision:
    """What the workflow plane decided for one caller turn."""

    action: Action
    state: str
    instructions: str
    tools: list[str]


def apply_action(
    action: Action,
    fsm: CallStateMachine,
    ctx: CallContext,
    *,
    sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> Decision:
    """Apply a decided Action: fire compliance tools, transition, recompose.

    Shared by the plain-FSM engine and the LangGraph engine so both produce
    byte-identical effects — the graph only changes HOW the action is routed.
    """
    # Compliance / hand-off tools fire BEFORE the model speaks, in code — so the
    # effect is recorded regardless of whether the voice model calls the tool
    # (gpt-4o-mini and gpt-realtime-mini differ in tool-calling reliability; this
    # keeps behavior model-independent). Order is the guarantee: the primary action
    # first, then the disposition. schedule_callback stays out of here — it is
    # parametric (needs a time) and multi-turn; the handler fires it once the
    # callback window is captured.
    if action == "DNC_CLOSE":
        execute_tool("add_to_do_not_call", {"reason": "caller opt-out"}, ctx, sink=sink)
    elif action == "ESCALATE":
        execute_tool(
            "transfer_to_lo", {"reason": "caller requested escalation"}, ctx, sink=sink
        )
    elif action == "LANGUAGE_ROUTE":
        execute_tool("route_language", {"language": "caller_requested"}, ctx, sink=sink)
    disposition = _DISPOSITION_FOR_ACTION.get(action)
    if disposition is not None:
        execute_tool("log_disposition", {"disposition": disposition}, ctx, sink=sink)

    target = ACTION_TO_STATE[action]
    if fsm.state != target:
        fsm.transition(target, reason=action)

    return Decision(
        action=action,
        state=fsm.state,
        instructions=compose(fsm.state, ctx.facts()),
        tools=tools_for(fsm.state),
    )


def handle_caller_turn(
    text: str,
    fsm: CallStateMachine,
    ctx: CallContext,
    *,
    sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> Optional[Decision]:
    """Apply the gate to a finalized caller turn. Returns a Decision or None."""
    action = gate(text, ctx)
    if action is None:
        return None
    return apply_action(action, fsm, ctx, sink=sink)


async def classify_turn(turns: list[dict], *, thread_id: str = "local") -> Optional[Action]:
    """Semantic whole-conversation router (fsm engine). Returns a forced Action or None.

    Classification only — the caller (handler) applies the action after re-checking
    state, so a slow LLM result can't override a call that already moved on.
    """
    from app.orchestrator.semantic import route_conversation

    return await route_conversation(turns)
