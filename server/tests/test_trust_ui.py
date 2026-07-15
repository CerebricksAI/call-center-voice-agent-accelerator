"""Trust UI helpers — labels only; no compliance side-effects."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.trust_ui import (  # noqa: E402
    briefing_for_state,
    dnc_record_id,
    fsm_state_history,
    ribbon_stages,
    stage_label,
)
from app.orchestrator.fsm import CallStateMachine  # noqa: E402


def test_stage_labels():
    assert stage_label("GREETING") == "INTRO"
    assert stage_label("DNC_CLOSE") == "DNC CLOSE"


def test_ribbon_pre_call_highlights_classify():
    nodes = ribbon_stages("PRE_CALL")
    assert nodes[0]["label"].startswith("CLASSIFY")
    assert nodes[0]["status"] == "active"
    assert nodes[1]["status"] == "pending"


def test_ribbon_greeting_highlights_intro():
    nodes = ribbon_stages("GREETING")
    assert nodes[0]["status"] == "done"
    assert any(n["label"] == "INTRO" and n["status"] == "active" for n in nodes)


def test_ribbon_qualify_active():
    nodes = ribbon_stages("QUALIFY")
    assert nodes[0]["status"] == "done"
    assert any(n["label"] == "QUALIFY" and n["status"] == "active" for n in nodes)


def test_ribbon_dnc_close_active():
    nodes = ribbon_stages("DNC_CLOSE")
    assert any(n["label"] == "DNC CLOSE" and n["status"] == "active" for n in nodes)


def test_ribbon_keeps_callback_when_moving_to_dnc():
    # CALLBACK → DNC must leave CALLBACK visible (done), not overwrite it.
    hist = ["GREETING", "QUALIFY", "CALLBACK_CLOSE", "DNC_CLOSE"]
    nodes = ribbon_stages("DNC_CLOSE", history=hist)
    labels = [n["label"] for n in nodes]
    assert labels == [
        "CLASSIFY · human",
        "INTRO",
        "QUALIFY",
        "CALLBACK CLOSE",
        "DNC CLOSE",
    ]
    assert nodes[-2]["status"] == "done"
    assert nodes[-1]["status"] == "active"


def test_fsm_state_history_walks_transitions():
    fsm = CallStateMachine(state="GREETING", call_id="t")
    fsm.transition("QUALIFY", reason="consent")
    fsm.transition("CALLBACK_CLOSE", reason="CALLBACK_CLOSE")
    fsm.transition("DNC_CLOSE", reason="DNC_CLOSE")
    assert fsm_state_history(fsm) == [
        "GREETING",
        "QUALIFY",
        "CALLBACK_CLOSE",
        "DNC_CLOSE",
    ]


def test_briefing_has_bytes():
    b = briefing_for_state("QUALIFY")
    assert "guardrails" in b["label"]
    assert b["bytes"] > 100


def test_dnc_record_id():
    assert dnc_record_id("abcd-ef12").startswith("DNC ")
