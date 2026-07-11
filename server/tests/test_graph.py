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
    "optout": ["actually, take me off your list"],
    "decline_two_turns": ["I'm not interested", "no thanks"],
    "busy": ["I'm busy, call me back later"],
    "language": ["can we do this in Spanish"],
    "escalate": ["let me speak to a human"],
    "neutral": ["I'd like to refinance my house"],
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
    fsm, ctx, _ = _run(graph, ["I'm not interested", "no thanks"], "thread-x")
    snapshot = graph.app.get_state({"configurable": {"thread_id": "thread-x"}})
    assert snapshot.values.get("disposition") == "declined"
    assert snapshot.values.get("rebuttal_used") is True


def test_build_app_compiles():
    assert build_app() is not None


if __name__ == "__main__":
    test_graph_matches_fsm_engine_on_every_scenario()
    test_graph_optout_fires_tools_before_close_and_isolates_skill()
    test_checkpointer_persists_state_by_thread_id()
    print("graph: OK")
