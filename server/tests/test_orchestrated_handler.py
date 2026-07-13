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


def test_base_end_detection_routes_to_safe_close_with_disposition():
    # The base handler's native auto-end fires CallEnded early and cuts the goodbye,
    # so the orchestrator re-routes that (correctly-timed) signal to its SAFE
    # hard-close — but only once a disposition exists, so greeting/qualify misfires
    # never end the call. This is what makes multi-turn closes (callback) end even
    # when the model skips end_call.
    h = _handler()
    calls = []
    h._schedule_hard_close = lambda: calls.append(True)  # stub — don't spawn the task
    # No disposition yet -> ignored (a greeting/qualify misfire must not end the call).
    h._ctx.disposition = None
    h._schedule_auto_end_call()
    assert calls == []
    # Disposition recorded (e.g. a multi-turn callback) -> routes to the safe close.
    h._ctx.disposition = "callback_requested"
    h._schedule_auto_end_call()
    assert calls == [True]


def test_playback_finished_control_message_records_drain():
    # The browser's "playback finished" signal is what lets the call end only after
    # the goodbye has fully played — the handler records its arrival time.
    import asyncio

    h = _handler()
    assert h._last_drain_at == 0.0
    handled = asyncio.run(h._handle_control_message('{"Kind": "PlaybackFinished"}'))
    assert handled is True
    assert h._last_drain_at > 0.0


def test_bridge_insights_populate_ctx_fields_and_sink():
    # Borrower fields captured by the model-agnostic transcript extractor must land
    # in the orchestrator's paper trail (ctx.fields + CRM sink) regardless of whether
    # the voice model called capture_borrower_field.
    h = _handler()
    captured: list[dict] = []
    h._sink = lambda cid, rec: captured.append(rec)
    h._emitted_insights = {
        "loan_purpose": {"value": "refinance", "confidence": 0.9},
        "state": {"value": "California", "confidence": 0.8},
        "empty_field": {"value": "", "confidence": 0.1},  # no value -> skipped
    }
    h._bridge_insights_to_ctx()
    assert h._ctx.fields["loan_purpose"]["value"] == "refinance"
    assert h._ctx.fields["state"]["value"] == "California"
    assert "empty_field" not in h._ctx.fields
    assert [r["tool"] for r in captured].count("capture_borrower_field") == 2
    # Idempotent: a second pass with the same insights adds nothing new.
    captured.clear()
    h._bridge_insights_to_ctx()
    assert captured == []


def test_bridge_fires_schedule_callback_when_time_captured():
    h = _handler()
    h._sink = lambda cid, rec: None  # isolate from the real CRM stub file
    h._fsm.transition("CALLBACK_CLOSE", reason="t")
    h._emitted_insights = {
        "preferred_callback_time": {"value": "tomorrow morning", "confidence": 0.7}
    }
    h._bridge_insights_to_ctx()
    assert h._ctx.callback_scheduled is True
    assert any(r["tool"] == "schedule_callback" for r in h._ctx.tool_log)


def test_callback_completed_disposition_schedules_hard_close():
    # CALLBACK_CLOSE is multi-turn so it can't close on entry; instead, recording the
    # terminal 'completed' disposition must schedule the close in code — even if the
    # model never calls end_call (unreliable on realtime-mini).
    import asyncio

    h = _handler()
    h._sink = lambda cid, rec: None
    h._fsm.transition("CALLBACK_CLOSE", reason="t")
    calls = []
    h._schedule_hard_close = lambda: calls.append(True)  # stub — don't spawn the task

    async def run():
        await h.on_function_call("log_disposition", None, '{"disposition": "completed"}')

    asyncio.run(run())
    assert calls == [True]


def test_non_terminal_disposition_does_not_close():
    # A normal capture mid-qualify must NOT trigger a close.
    import asyncio

    h = _handler()
    h._sink = lambda cid, rec: None
    h._fsm.transition("QUALIFY", reason="t")
    calls = []
    h._schedule_hard_close = lambda: calls.append(True)

    async def run():
        await h.on_function_call(
            "capture_borrower_field", None, '{"field": "loan_purpose", "value": "purchase"}'
        )

    asyncio.run(run())
    assert calls == []


def test_align_pcm16_carries_odd_byte_across_chunks():
    # An odd-length PCM16 chunk (a sample split across frames) must not be sent as-is
    # (native Voice Live rejects it). The even prefix goes out; the stray byte is
    # carried into the next chunk so the sample reassembles and nothing is dropped.
    h = _handler()
    assert h._align_pcm16(b"\x01\x02\x03") == b"\x01\x02"
    assert h._pcm_carry == b"\x03"
    assert h._align_pcm16(b"\x04\x05") == b"\x03\x04"  # carried byte reassembled
    assert h._pcm_carry == b"\x05"
    # Even input with no carry passes through untouched.
    h._pcm_carry = b""
    assert h._align_pcm16(b"\xaa\xbb\xcc\xdd") == b"\xaa\xbb\xcc\xdd"
    assert h._pcm_carry == b""


if __name__ == "__main__":
    test_starts_in_greeting_with_only_end_call()
    test_qualify_preserves_transcription_and_temp_and_tools()
    test_dnc_close_locks_to_end_call_only()
    test_orchestrator_owns_responses_disables_auto_reply()
    test_base_end_detection_routes_to_safe_close_with_disposition()
    test_playback_finished_control_message_records_drain()
    test_bridge_insights_populate_ctx_fields_and_sink()
    test_bridge_fires_schedule_callback_when_time_captured()
    test_callback_completed_disposition_schedules_hard_close()
    test_non_terminal_disposition_does_not_close()
    test_align_pcm16_carries_odd_byte_across_chunks()
    print("orchestrated handler wiring: OK")
