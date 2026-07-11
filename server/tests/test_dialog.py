"""Integration walk — the Phase 3 verify gate.

Drives caller turns through the shared brain (gate -> tools -> FSM -> compose) with
no audio, exactly as the live handler will, and asserts compliance ordering.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.dialog import handle_caller_turn  # noqa: E402
from app.orchestrator.fsm import CallContext, CallStateMachine  # noqa: E402
from app.orchestrator.tools import execute_tool  # noqa: E402


def test_greeting_to_qualify_to_decline_to_ended():
    fsm = CallStateMachine(call_id="c1")
    ctx = CallContext(call_id="c1")

    # Consent is model-driven — gate stays silent, model proceeds.
    assert handle_caller_turn("yes, that works", fsm, ctx) is None
    fsm.transition("QUALIFY", reason="consent")

    # First soft decline -> exactly one rebuttal, still qualifying.
    d1 = handle_caller_turn("I'm not interested", fsm, ctx)
    assert d1.action == "REBUTTAL_ONCE" and fsm.state == "QUALIFY"

    # Second decline -> graceful close, disposition recorded in code.
    d2 = handle_caller_turn("no thanks", fsm, ctx)
    assert d2.action == "DECLINE_CLOSE" and fsm.state == "DECLINE_CLOSE"
    assert ctx.disposition == "declined"

    # Now the call may end.
    assert fsm.end(ctx) is True and fsm.state == "ENDED"
    states = [t["to"] for t in fsm.transitions]
    assert states == ["QUALIFY", "DECLINE_CLOSE", "ENDED"]


def test_hard_optout_fires_tools_before_the_close_speaks():
    fsm = CallStateMachine(call_id="c2")
    ctx = CallContext(call_id="c2")
    fsm.transition("QUALIFY", reason="consent")

    d = handle_caller_turn("actually, take me off your list", fsm, ctx)

    assert d.action == "DNC_CLOSE" and d.state == "DNC_CLOSE"
    # Compliance recorded BEFORE any speech...
    assert ctx.dnc_recorded is True
    assert ctx.disposition == "do_not_call"
    # ...and in the right order: DNC record, then disposition.
    assert [r["tool"] for r in ctx.tool_log] == ["add_to_do_not_call", "log_disposition"]
    # The model now sees ONLY the opt-out close — no qualifying questions to wander into.
    assert "You won't be contacted again" in d.instructions
    assert "LOAN PURPOSE" not in d.instructions
    assert d.tools == ["end_call"]


def test_end_call_refused_without_disposition():
    ctx = CallContext()
    refused = execute_tool("end_call", {}, ctx)
    assert refused == {"ok": False, "error": "refused: no disposition recorded"}
    assert ctx.ended is False

    execute_tool("log_disposition", {"disposition": "completed"}, ctx)
    ok = execute_tool("end_call", {}, ctx)
    assert ok["ok"] is True and ctx.ended is True


def test_busy_routes_to_callback_close():
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    d = handle_caller_turn("sorry I'm busy, call me back later", fsm, ctx)
    assert d.state == "CALLBACK_CLOSE" and ctx.disposition == "callback_requested"


if __name__ == "__main__":
    test_greeting_to_qualify_to_decline_to_ended()
    test_hard_optout_fires_tools_before_the_close_speaks()
    test_end_call_refused_without_disposition()
    test_busy_routes_to_callback_close()
    print("dialog: OK")
