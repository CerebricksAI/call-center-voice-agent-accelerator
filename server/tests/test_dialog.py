"""Integration walk — the Phase 3 verify gate.

Drives caller turns through the shared brain (gate -> tools -> FSM -> compose) with
no audio, exactly as the live handler will, and asserts compliance ordering.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.dialog import apply_action, handle_caller_turn  # noqa: E402
from app.orchestrator.fsm import CallContext, CallStateMachine  # noqa: E402
from app.orchestrator.tools import execute_tool, tools_for  # noqa: E402


def test_decline_close_effects_via_apply_action():
    # Decline is now decided SEMANTICALLY (the router), not by the keyword gate.
    # apply_action performs the same deterministic effect the router triggers.
    fsm = CallStateMachine(call_id="c1")
    ctx = CallContext(call_id="c1")
    assert handle_caller_turn("yes, that works", fsm, ctx) is None  # gate silent
    fsm.transition("QUALIFY", reason="consent")

    d1 = apply_action("DECLINE_CLOSE", fsm, ctx)
    assert d1.action == "DECLINE_CLOSE" and fsm.state == "DECLINE_CLOSE"
    assert ctx.disposition == "declined"

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
    assert "You won't be contacted again" in d.instructions or "will not be contacted" in d.instructions.lower()
    assert "LOAN PURPOSE" not in d.instructions
    assert d.tools == ["capture_borrower_field", "end_call"]


def test_end_call_refused_without_disposition():
    ctx = CallContext()
    refused = execute_tool("end_call", {}, ctx)
    assert refused == {"ok": False, "error": "refused: no disposition recorded"}
    assert ctx.ended is False

    execute_tool("log_disposition", {"disposition": "completed"}, ctx)
    ok = execute_tool("end_call", {}, ctx)
    assert ok["ok"] is True and ctx.ended is True


def test_callback_close_effects_via_apply_action():
    # "Busy / call me later" is now a semantic (router) decision; apply_action runs
    # the deterministic effect.
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    d = apply_action("CALLBACK_CLOSE", fsm, ctx)
    assert d.state == "CALLBACK_CLOSE" and ctx.disposition == "callback_requested"


def test_hard_optout_from_callback_promotes_to_dnc():
    # Mid-callback (already dispositioned callback_requested), a hard opt-out must
    # still fire add_to_do_not_call and overwrite disposition to do_not_call.
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    apply_action("CALLBACK_CLOSE", fsm, ctx)
    assert fsm.state == "CALLBACK_CLOSE" and ctx.disposition == "callback_requested"
    d = handle_caller_turn("don't want to be contacted again", fsm, ctx)
    assert d is not None and d.action == "DNC_CLOSE"
    assert fsm.state == "DNC_CLOSE"
    assert ctx.disposition == "do_not_call"
    assert ctx.dnc_recorded is True


def test_resume_qualify_after_close_clears_dnc():
    from app.orchestrator.dialog import resume_qualify_after_close

    fsm = CallStateMachine()
    ctx = CallContext()
    apply_action("DNC_CLOSE", fsm, ctx)
    assert ctx.dnc_recorded and ctx.disposition == "do_not_call"
    resume_qualify_after_close(fsm, ctx, reason="caller_resumed")
    assert fsm.state == "QUALIFY"
    assert ctx.disposition is None
    assert ctx.dnc_recorded is False
    assert any(r["tool"] == "rescind_close" for r in ctx.tool_log)


def test_handoff_tools_record_disposition_so_end_call_works():
    # When the MODEL calls a hand-off tool directly (not via the router's apply_action),
    # execute_tool must still record a disposition — otherwise the follow-up end_call is
    # refused and the call can never close (observed live on a model-initiated transfer).
    for tool, expected in [
        ("transfer_to_lo", "transferred"),
        ("route_language", "language_routed"),
        ("schedule_callback", "callback_requested"),
    ]:
        ctx = CallContext()
        execute_tool(tool, {}, ctx)
        assert ctx.disposition == expected, tool
        assert execute_tool("end_call", {}, ctx)["ok"] is True, tool


def test_escalate_fires_transfer_tool_in_code():
    # ESCALATE is a router (model) decision. apply_action must fire transfer_to_lo
    # in code so the hand-off is recorded regardless of whether the voice model
    # calls the tool — this is what makes behavior model-independent.
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    d = apply_action("ESCALATE", fsm, ctx)
    assert d.state == "TRANSFER" and ctx.disposition == "transferred"
    assert [r["tool"] for r in ctx.tool_log] == ["transfer_to_lo", "log_disposition"]


def test_language_route_fires_route_language_tool_in_code():
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    d = apply_action("LANGUAGE_ROUTE", fsm, ctx)
    assert d.state == "LANGUAGE_ROUTE" and ctx.disposition == "language_routed"
    assert [r["tool"] for r in ctx.tool_log] == ["route_language", "log_disposition"]


def test_dnc_still_only_fires_compliance_tools():
    # Guard: making router actions deterministic must NOT add tools to the opt-out
    # path (no gate weakened, no extra effects).
    fsm = CallStateMachine()
    ctx = CallContext()
    fsm.transition("QUALIFY", reason="consent")
    apply_action("DNC_CLOSE", fsm, ctx)
    assert [r["tool"] for r in ctx.tool_log] == ["add_to_do_not_call", "log_disposition"]


if __name__ == "__main__":
    test_decline_close_effects_via_apply_action()
    test_hard_optout_fires_tools_before_the_close_speaks()
    test_end_call_refused_without_disposition()
    test_callback_close_effects_via_apply_action()
    test_gate_still_catches_hard_optout_deterministically()
    test_hard_optout_from_callback_promotes_to_dnc()
    test_escalate_fires_transfer_tool_in_code()
    test_language_route_fires_route_language_tool_in_code()
    test_dnc_still_only_fires_compliance_tools()
    print("dialog: OK")
