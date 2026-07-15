"""Unit tests for the silence policy scheduler (T11)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.silence import (  # noqa: E402
    SilencePolicy,
    load_silence_policy,
    next_silence_event,
    next_silence_step,
    quiet_elapsed,
    seconds_until_next,
)


def test_load_policy_matches_session_yaml():
    policy = load_silence_policy()
    assert policy.reprompt_at_s == (5.0, 8.0)
    assert policy.close_at_s == 10.0
    assert policy.disposition == "no_response"


def test_fresh_gaps_after_each_agent_utterance():
    """Each wait is a full gap from zero after agent speech — not leftover cumulatives."""
    policy = SilencePolicy()
    fired: set[str] = set()
    # After playback ends: 5s until first check-in
    assert next_silence_step(fired=fired, policy=policy) == ("reprompt", 0, 5.0)
    fired.add("reprompt:0")
    # After check-in #1 finishes playing: 3s more (8-5)
    assert next_silence_step(fired=fired, policy=policy) == ("reprompt", 1, 3.0)
    fired.add("reprompt:1")
    # After check-in #2: 2s more until close (10-8)
    assert next_silence_step(fired=fired, policy=policy) == ("close", -1, 2.0)


def test_events_and_seconds_helpers():
    policy = SilencePolicy()
    fired: set[str] = set()
    assert next_silence_event(4.9, fired=fired, policy=policy) is None
    assert next_silence_event(5.0, fired=fired, policy=policy) == ("reprompt", 0)
    assert seconds_until_next(0.0, fired=fired, policy=policy) == 5.0
    fired.add("reprompt:0")
    assert seconds_until_next(0.0, fired=fired, policy=policy) == 3.0
    fired |= {"reprompt:1", "close"}
    assert next_silence_step(fired=fired, policy=policy) is None
    assert seconds_until_next(0.0, fired=fired, policy=policy) is None


def test_quiet_elapsed_excludes_agent_speaking():
    assert quiet_elapsed(100.0, anchor=80.0, paused_total=5.0) == 15.0
    assert quiet_elapsed(
        103.0, anchor=80.0, paused_total=5.0, paused_at=100.0
    ) == 15.0


def test_silence_watch_covers_intro_qualify_callback_dnc():
    from app.orchestrator.silence import SILENCE_WATCH_STATES

    assert SILENCE_WATCH_STATES == {
        "GREETING",
        "QUALIFY",
        "CALLBACK_CLOSE",
        "DNC_CLOSE",
    }
