"""Caller mood heuristics — delivery feel only (not a compliance gate)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.mood import (  # noqa: E402
    REACTION_FIRST,
    detect_mood,
    mood_cue,
    mood_voice_context,
)


def test_detect_mood_by_keyword():
    assert detect_mood("I'm so frustrated with this form") == "frustrated"
    assert detect_mood("Can't wait — first home, so exciting!") == "excited"
    assert detect_mood("I'm in a hurry, keep it short") == "rushed"
    assert detect_mood("Not sure, I guess maybe") == "hesitant"
    assert detect_mood("Looking to refinance in Texas") == "neutral"
    assert detect_mood("") == "neutral"


def test_mood_maps_to_voice_context():
    assert mood_voice_context("frustrated") == "hardship"
    assert mood_voice_context("excited") == "excited"
    assert mood_voice_context("rushed") == "data_collection"
    assert mood_voice_context("hesitant") == "objection"
    assert mood_voice_context("neutral") == "default"


def test_mood_cue_and_reaction_are_nonempty():
    assert "MOOD" in mood_cue("frustrated")
    assert "reaction" in REACTION_FIRST.lower()
