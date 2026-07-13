"""compose() produces guardrails + stage + verified facts, with placeholders filled."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/ on path

import pytest  # noqa: E402

from skills import loader  # noqa: E402


def test_compose_greeting_has_all_three_layers():
    composed = loader.compose("GREETING", {"borrower_name": "Alex Rivera"})
    assert "Global guardrails" in composed          # 00 always present
    assert "read this disclosure" in composed        # 10 stage body
    assert "VERIFIED FACTS" in composed              # facts block
    assert composed.count("---") >= 2                # two separators


def test_placeholders_substituted():
    composed = loader.compose("CALLBACK_CLOSE", {"borrower_name": "Alex Rivera"})
    assert "Alex" in composed                        # {first_name}/{close_name}
    assert "{first_name}" not in composed
    assert "{brokerage_name}" not in composed
    assert "{followup_window}" not in composed
    assert "{close_name}" not in composed


def test_no_name_greets_without_one():
    composed = loader.compose("GREETING", None)
    assert "greet without a name" in composed


def test_unknown_state_raises():
    with pytest.raises(KeyError):
        loader.compose("NOPE")


def test_reads_fresh_from_disk(tmp_path, monkeypatch):
    # Editing a skill changes the next compose() with no restart.
    original = (loader.SKILLS_DIR / "31_optout_dnc_close.md").read_text(encoding="utf-8")
    try:
        (loader.SKILLS_DIR / "31_optout_dnc_close.md").write_text(
            original + "\n\nMARKER_EDIT_TOKEN\n", encoding="utf-8"
        )
        assert "MARKER_EDIT_TOKEN" in loader.compose("DNC_CLOSE")
    finally:
        (loader.SKILLS_DIR / "31_optout_dnc_close.md").write_text(original, encoding="utf-8")
    assert "MARKER_EDIT_TOKEN" not in loader.compose("DNC_CLOSE")


if __name__ == "__main__":
    test_compose_greeting_has_all_three_layers()
    test_placeholders_substituted()
    test_no_name_greets_without_one()
    test_unknown_state_raises()
    print("compose: OK")
