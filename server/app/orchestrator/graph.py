"""LangGraph engine — the FSM expressed as a StateGraph, node-for-node.

The plan's "configuration change, not a rewrite": the gate becomes the router
(a conditional edge), each outcome becomes a node, and every node reuses the
UNCHANGED `intents.gate()` and `dialog.apply_action()` (which composes the same
skill files and fires the same tools). A MemorySaver checkpointer gives durable,
resumable per-call state for free.

State is kept as plain primitives (serializable for checkpointing); each node
reconstitutes a CallContext, runs the shared effect logic, and writes the fields
back. So behavior is byte-identical to the plain-FSM engine — only the routing
structure differs.

Selected live via ORCHESTRATOR_ENGINE=langgraph (default: fsm). Also runnable
offline for parity/replay tests.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.orchestrator.dialog import Decision, apply_action
from app.orchestrator.fsm import CallContext, CallStateMachine
from app.orchestrator.intents import gate

_ACTION_TO_NODE = {
    "DNC_CLOSE": "dnc_close",
    "DECLINE_CLOSE": "decline_close",
    "CALLBACK_CLOSE": "callback_close",
    "ESCALATE": "transfer",
    "LANGUAGE_ROUTE": "language_route",
    "REBUTTAL_ONCE": "rebuttal",
}


class CallState(TypedDict, total=False):
    transcript: str
    call_id: str
    borrower_name: str
    lead_source: str
    prior_notes: str
    rebuttal_used: bool
    disposition: Optional[str]
    dnc_recorded: bool
    callback_scheduled: bool
    ended: bool
    tool_log: list[dict[str, Any]]
    fields: dict[str, Any]
    fsm_state: str
    action: Optional[str]
    instructions: str
    tools: list[str]


def _to_ctx(state: CallState) -> CallContext:
    ctx = CallContext(
        call_id=state.get("call_id", "local"),
        borrower_name=state.get("borrower_name", ""),
        rebuttal_used=state.get("rebuttal_used", False),
        disposition=state.get("disposition"),
        dnc_recorded=state.get("dnc_recorded", False),
        callback_scheduled=state.get("callback_scheduled", False),
        ended=state.get("ended", False),
        tool_log=list(state.get("tool_log", [])),
        fields=dict(state.get("fields", {})),
    )
    if state.get("lead_source"):
        ctx.lead_source = state["lead_source"]
    if state.get("prior_notes"):
        ctx.prior_notes = state["prior_notes"]
    return ctx


def _from_ctx(ctx: CallContext) -> dict[str, Any]:
    return {
        "rebuttal_used": ctx.rebuttal_used,
        "disposition": ctx.disposition,
        "dnc_recorded": ctx.dnc_recorded,
        "callback_scheduled": ctx.callback_scheduled,
        "ended": ctx.ended,
        "tool_log": ctx.tool_log,
        "fields": ctx.fields,
    }


def _listen(state: CallState) -> dict[str, Any]:
    """Run the (unchanged) gate on this turn; may spend the rebuttal budget."""
    ctx = _to_ctx(state)
    action = gate(state.get("transcript", ""), ctx)
    upd = _from_ctx(ctx)
    upd["action"] = action
    return upd


def _route(state: CallState) -> str:
    action = state.get("action")
    return _ACTION_TO_NODE.get(action, "cont") if action else "cont"


def _cont(state: CallState) -> dict[str, Any]:
    # No gate hit — the model responds normally; nothing to change.
    return {"action": None}


def _make_outcome(action: str) -> Callable[[CallState], dict[str, Any]]:
    def node(state: CallState) -> dict[str, Any]:
        ctx = _to_ctx(state)
        fsm = CallStateMachine(state=state.get("fsm_state", "QUALIFY"), call_id=ctx.call_id)
        decision = apply_action(action, fsm, ctx)  # SAME effect logic as the FSM engine
        upd = _from_ctx(ctx)
        upd.update(
            {
                "fsm_state": fsm.state,
                "action": action,
                "instructions": decision.instructions,
                "tools": decision.tools,
            }
        )
        return upd

    return node


def build_app(checkpointer: Any = None):
    """Compile the single-turn routing graph (one caller turn per invoke)."""
    g = StateGraph(CallState)
    g.add_node("listen", _listen)
    g.add_node("cont", _cont)
    for action, node_name in _ACTION_TO_NODE.items():
        g.add_node(node_name, _make_outcome(action))

    g.add_edge(START, "listen")
    g.add_conditional_edges(
        "listen",
        _route,
        {"cont": "cont", **{v: v for v in _ACTION_TO_NODE.values()}},
    )
    g.add_edge("cont", END)
    for node_name in _ACTION_TO_NODE.values():
        g.add_edge(node_name, END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


class GraphEngine:
    """Drop-in for the dialog module: `handle_caller_turn(text, fsm, ctx, sink=...)`.

    Routes each turn through the compiled LangGraph, then syncs the primitive
    result back onto the live CallContext / CallStateMachine so the handler's
    `_session_config()` composes from the correct stage.
    """

    def __init__(self) -> None:
        self.app = build_app()

    def handle_caller_turn(
        self,
        text: str,
        fsm: CallStateMachine,
        ctx: CallContext,
        *,
        sink: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> Optional[Decision]:
        before = len(ctx.tool_log)
        result = self.app.invoke(
            {
                "transcript": text,
                "call_id": ctx.call_id,
                "borrower_name": ctx.borrower_name,
                "lead_source": ctx.lead_source,
                "prior_notes": ctx.prior_notes,
                "fsm_state": fsm.state,
                "rebuttal_used": ctx.rebuttal_used,
                "disposition": ctx.disposition,
                "dnc_recorded": ctx.dnc_recorded,
                "callback_scheduled": ctx.callback_scheduled,
                "ended": ctx.ended,
                "tool_log": list(ctx.tool_log),
                "fields": dict(ctx.fields),
            },
            config={"configurable": {"thread_id": ctx.call_id}},
        )

        # Sync graph result back onto the live objects.
        ctx.rebuttal_used = result.get("rebuttal_used", ctx.rebuttal_used)
        ctx.disposition = result.get("disposition", ctx.disposition)
        ctx.dnc_recorded = result.get("dnc_recorded", ctx.dnc_recorded)
        ctx.callback_scheduled = result.get("callback_scheduled", ctx.callback_scheduled)
        ctx.ended = result.get("ended", ctx.ended)
        ctx.tool_log = result.get("tool_log", ctx.tool_log)
        ctx.fields = result.get("fields", ctx.fields)
        fsm.state = result.get("fsm_state", fsm.state)

        if sink:
            for record in ctx.tool_log[before:]:
                sink(ctx.call_id, record)

        action = result.get("action")
        if action is None:
            return None
        return Decision(
            action=action,
            state=fsm.state,
            instructions=result["instructions"],
            tools=result["tools"],
        )
