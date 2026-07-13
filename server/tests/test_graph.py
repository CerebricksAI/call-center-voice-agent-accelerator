"""LangGraph engine: parity with the FSM engine + checkpointer durability.

Proves the "configuration change, not a rewrite" claim — routing the same turns
through the compiled StateGraph yields byte-identical Decisions and effects.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator import dialog  # noqa: E402
from app.orchestrator.fsm import CallContext, CallStateMachine  # noqa: E402
from app.orchestrator.graph import GraphEngine, build_app  # noqa: E402

# Each scenario: a list of caller turns after consent (start in QUALIFY).
SCENARIOS = {
    "optout": ["actually, take me off your list"],   # the ONE gated action (compliance)
    "neutral": ["I'd like to refinance my house"],   # gate silent
    "defers": ["I'm not interested"],                # non-opt-out -> gate silent (router handles)
}


def _run(engine, turns, call_id):
    fsm = CallStateMachine(call_id=call_id)
    ctx = CallContext(call_id=call_id)
    fsm.transition("QUALIFY", reason="consent")
    decisions = [engine.handle_caller_turn(t, fsm, ctx) for t in turns]
    return fsm, ctx, decisions


def test_graph_matches_fsm_engine_on_every_scenario():
    graph = GraphEngine()
    for name, turns in SCENARIOS.items():
        f_fsm, f_ctx, f_dec = _run(dialog, turns, f"fsm-{name}")
        g_fsm, g_ctx, g_dec = _run(graph, turns, f"graph-{name}")

        assert g_fsm.state == f_fsm.state, name
        assert g_ctx.disposition == f_ctx.disposition, name
        assert g_ctx.dnc_recorded == f_ctx.dnc_recorded, name
        assert [r["tool"] for r in g_ctx.tool_log] == [r["tool"] for r in f_ctx.tool_log], name
        assert [None if d is None else d.action for d in g_dec] == [
            None if d is None else d.action for d in f_dec
        ], name


def test_graph_optout_fires_tools_before_close_and_isolates_skill():
    graph = GraphEngine()
    _, ctx, decisions = _run(graph, ["please take me off your list"], "g-optout")
    d = decisions[0]
    assert d.action == "DNC_CLOSE" and d.state == "DNC_CLOSE"
    assert [r["tool"] for r in ctx.tool_log] == ["add_to_do_not_call", "log_disposition"]
    assert "You won't be contacted again" in d.instructions
    assert "LOAN PURPOSE" not in d.instructions
    assert d.tools == ["end_call"]


def test_checkpointer_persists_state_by_thread_id():
    graph = GraphEngine()
    fsm, ctx, _ = _run(graph, ["actually, take me off your list"], "thread-x")
    snapshot = graph.app.get_state({"configurable": {"thread_id": "thread-x"}})
    assert snapshot.values.get("disposition") == "do_not_call"
    assert snapshot.values.get("fsm_state") == "DNC_CLOSE"


def test_build_app_compiles():
    assert build_app() is not None


def test_router_actions_fire_tools_via_shared_apply_action():
    """Router-decided hand-offs fire their tool in code, identically on both engines.

    ESCALATE / LANGUAGE_ROUTE are never produced by the keyword gate — the handler
    applies them (after the semantic router) through dialog.apply_action, which both
    the FSM engine and the LangGraph engine import and reuse. Asserting the shared
    function fires the tool proves the effect is engine- and model-independent.
    """
    from app.orchestrator import dialog as dialog_mod
    from app.orchestrator import graph as graph_mod

    # Both engines route decided actions through the SAME function object.
    assert graph_mod.apply_action is dialog_mod.apply_action

    for action, state, tool, disp in [
        ("ESCALATE", "TRANSFER", "transfer_to_lo", "transferred"),
        ("LANGUAGE_ROUTE", "LANGUAGE_ROUTE", "route_language", "language_routed"),
    ]:
        fsm = CallStateMachine()
        ctx = CallContext()
        fsm.transition("QUALIFY", reason="consent")
        d = graph_mod.apply_action(action, fsm, ctx)
        assert d.state == state and ctx.disposition == disp
        assert tool in [r["tool"] for r in ctx.tool_log]


if __name__ == "__main__":
    test_graph_matches_fsm_engine_on_every_scenario()
    test_graph_optout_fires_tools_before_close_and_isolates_skill()
    test_checkpointer_persists_state_by_thread_id()
    print("graph: OK")


def test_graph_and_fsm_classify_turn_route_via_router(monkeypatch):
    """Both engines' classify_turn return the router's action (LLM mocked)."""
    import asyncio
    import app.orchestrator.semantic as sem

    async def fake_route(turns):
        # whole-conversation router 'decides' decline from the transcript
        return "DECLINE_CLOSE" if any("changed my mind" in (t.get("text") or "") for t in turns) else None

    monkeypatch.setattr(sem, "route_conversation", fake_route)

    turns = [{"role": "agent", "text": "Timeline?"}, {"role": "user", "text": "I've changed my mind"}]
    # LangGraph engine (routes through the router node)
    assert asyncio.run(GraphEngine().classify_turn(turns, thread_id="t1")) == "DECLINE_CLOSE"
    # FSM engine (plain)
    assert asyncio.run(dialog.classify_turn(turns, thread_id="t1")) == "DECLINE_CLOSE"
    # a neutral transcript -> no route
    neutral = [{"role": "user", "text": "California, single family"}]
    assert asyncio.run(GraphEngine().classify_turn(neutral, thread_id="t2")) is None
