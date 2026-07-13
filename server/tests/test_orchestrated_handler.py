"""Wiring regression: OrchestratedWebHandler composes skills + tools per stage.

Offline (no live Voice Live connection) — exercises _session_config only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.handler import OrchestratedWebHandler  # noqa: E402

_DIVIDER = "═"  # the monolith's ═ section rule — must NOT appear in composed skills

CFG = {
    "AZURE_VOICE_LIVE_ENDPOINT": "https://example",
    "AZURE_VOICE_LIVE_API_KEY": "k",
    "VOICE_LIVE_MODEL": "gpt-4o-mini",
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID": None,
    "AMBIENT_PRESET": "none",
}


def _handler() -> OrchestratedWebHandler:
    h = OrchestratedWebHandler(CFG, voice_model="gpt-4o-mini", system_prompt=None)
    h.set_call_context("c-test", "web")
    return h


def test_starts_in_greeting_with_only_end_call():
    h = _handler()
    assert h._fsm.state == "GREETING"
    s = h._session_config()
    assert [t.name for t in s.tools] == ["end_call"]
    assert "Global guardrails" in s.instructions
    assert "read this disclosure" in s.instructions
    assert _DIVIDER not in s.instructions  # composed skills, not the monolith


def test_qualify_preserves_transcription_and_temp_and_tools():
    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    s = h._session_config()
    assert s.input_audio_transcription is not None   # kept from WebMediaHandler
    assert s.temperature == 0.6                        # Phase 1 anti-hallucination
    names = [t.name for t in s.tools]
    assert "capture_borrower_field" in names and "transfer_to_lo" in names
    assert "LOAN PURPOSE" in s.instructions


def test_dnc_close_locks_to_end_call_only():
    h = _handler()
    h._fsm.transition("DNC_CLOSE", reason="t")
    s = h._session_config()
    assert [t.name for t in s.tools] == ["end_call"]
    assert "You won't be contacted again" in s.instructions
    assert "LOAN PURPOSE" not in s.instructions


def test_orchestrator_owns_responses_disables_auto_reply():
    # The orchestrated session must turn OFF the server's automatic response so the
    # handler creates exactly one reply per turn (no double / no race).
    h = _handler()
    s = h._session_config()
    assert s.turn_detection is not None
    assert s.turn_detection.create_response is False


def test_base_auto_end_is_fully_suppressed():
    # The base transcript/fixed-timer auto-end must NEVER run under the orchestrator:
    # it bypasses the disposition gate and fires CallEnded early, cutting the goodbye.
    # The orchestrator ends the call via the hard-close (after the goodbye finishes).
    h = _handler()
    h._schedule_auto_end_call()
    assert getattr(h, "_auto_end_task", None) is None   # never scheduled
    # Still a no-op even once a disposition exists.
    h._ctx.disposition = "declined"
    h._ctx.ended = True
    h._schedule_auto_end_call()
    assert getattr(h, "_auto_end_task", None) is None


if __name__ == "__main__":
    test_starts_in_greeting_with_only_end_call()
    test_qualify_preserves_transcription_and_temp_and_tools()
    test_dnc_close_locks_to_end_call_only()
    test_orchestrator_owns_responses_disables_auto_reply()
    test_base_auto_end_is_fully_suppressed()
    print("orchestrated handler wiring: OK")
