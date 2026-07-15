"""Trust UI helpers — labels only; no compliance side-effects."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.trust_ui import (  # noqa: E402
    briefing_for_state,
    dnc_record_id,
    ribbon_stages,
    stage_label,
)


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


def test_briefing_has_bytes():
    b = briefing_for_state("QUALIFY")
    assert "guardrails" in b["label"]
    assert b["bytes"] > 100


def test_dnc_record_id():
    assert dnc_record_id("abcd-ef12").startswith("DNC ")
