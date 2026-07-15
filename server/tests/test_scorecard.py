"""Live scorecard helper used by /api/scorecard and the CLI runner."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evals.run_text as rt  # noqa: E402


def test_run_scorecard_fsm_has_overall():
    card = rt.run_scorecard("fsm")
    assert card["engine"] == "fsm"
    assert card["scenarioCount"] >= 1
    assert card["overall"]["total"] == card["scenarioCount"]
    assert card["overall"]["pass"] == card["scenarioCount"]
    assert any(c["id"] == "dnc" for c in card["categories"])
