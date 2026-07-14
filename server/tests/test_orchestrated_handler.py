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


def test_no_response_close_locks_to_end_call_only():
    h = _handler()
    h._fsm.transition("NO_RESPONSE_CLOSE", reason="silence")
    s = h._session_config()
    assert [t.name for t in s.tools] == ["end_call"]
    assert "lost you" in s.instructions.lower() or "wrap up" in s.instructions.lower()
    assert "LOAN PURPOSE" not in s.instructions


def test_silence_checkin_instructions_exclude_qualify():
    """Silence check-ins must not carry the QUALIFY skill (invented answers)."""
    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    h._silence_cue = "Say ONE short check-in."
    s = h._session_config()
    text = s.instructions.lower()
    assert "hard rules" in text
    assert "buy vs refinance" in text
    assert "got it" in text  # mentioned as forbidden example
    assert s.tools == []
    # Must not include the qualify stage agenda.
    assert "so — let's start with this" not in text
    assert "approximate loan amount" not in text


def test_custom_prompt_overrides_skills():
    h = _handler()
    h.system_prompt = "you are a bank advisor — ask about banking problems only."
    s = h._session_config()
    assert "bank advisor" in s.instructions
    assert "Global guardrails" not in s.instructions
    assert [t.name for t in s.tools] == ["end_call"]
    # Mood + reaction-first still harden custom-prompt conversational edges.
    assert "TURN SHAPE" in s.instructions
    assert "MOOD (this turn)" in s.instructions


def test_qualify_appends_mood_and_reaction():
    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    h._mood = "excited"
    s = h._session_config()
    assert "Caller sounds upbeat or excited" in s.instructions
    assert "TURN SHAPE" in s.instructions


def test_dnc_close_skips_delivery_suffix():
    h = _handler()
    h._fsm.transition("DNC_CLOSE", reason="t")
    h._mood = "frustrated"
    s = h._session_config()
    assert "TURN SHAPE" not in s.instructions
    assert "MOOD (this turn)" not in s.instructions


def test_silence_checkin_skips_mood_suffix():
    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    h._mood = "rushed"
    h._silence_cue = "Say ONE short check-in."
    s = h._session_config()
    assert "TURN SHAPE" not in s.instructions
    assert "MOOD (this turn)" not in s.instructions


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


def test_on_message_quart_str_endcall_hangs_up():
    # Quart Websocket.receive() yields a raw str for text frames — not an ASGI
    # dict. EndCall must hang up, not be coerced into fake PCM.
    import asyncio

    h = _handler()
    calls: list[str] = []

    async def _end(*, source: str = "client") -> None:
        calls.append(source)
        h._finalizing = True

    h.request_end_call = _end  # type: ignore[method-assign]
    audio_calls: list[bytes] = []
    h.handle_audio = lambda pcm: audio_calls.append(pcm)  # type: ignore[method-assign]

    asyncio.run(h.on_message('{"Kind": "EndCall"}'))
    assert calls == ["client"]
    assert audio_calls == []

    asyncio.run(h.on_message('{"Kind": "PlaybackFinished"}'))
    assert h._last_drain_at > 0.0
    assert audio_calls == []


def test_silence_close_pending_after_both_reprompts():
    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    assert h._silence_close_pending() is False
    h._silence_fired = {"reprompt:0", "reprompt:1"}
    assert h._silence_close_pending() is True
    h._silence_fired.add("close")
    assert h._silence_close_pending() is False


def test_silence_tail_junk_transcript_rearms_close():
    import asyncio

    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    h._silence_fired = {"reprompt:0", "reprompt:1"}
    armed: list[bool] = []
    h._arm_silence_watch = lambda *, clear_fired=True: armed.append(clear_fired)  # type: ignore[method-assign]
    h._engine.handle_caller_turn = lambda *a, **k: None  # type: ignore[method-assign]
    h._spawn_router = lambda: None  # type: ignore[method-assign]

    asyncio.run(h.on_user_transcript_done("Sheep for something else"))
    assert armed == [False]


def test_silence_close_schedules_hard_close_before_goodbye():
    import asyncio

    h = _handler()
    h._fsm.transition("QUALIFY", reason="t")
    h._silence_fired = {"reprompt:0", "reprompt:1"}
    h._last_audio_delta_at = 100.0
    h._last_drain_at = 100.0
    order: list[str] = []
    created: list[bool] = []

    def _sched():
        order.append(f"sched:{getattr(h, '_close_wait_since_audio', -1)}")

    h._schedule_hard_close = _sched  # type: ignore[method-assign]
    h._update_session = lambda: asyncio.sleep(0)  # type: ignore[method-assign]

    async def _create(**kwargs):
        order.append("create")
        created.append(True)

    h._create_response = _create  # type: ignore[method-assign]
    asyncio.run(h._silence_close())
    # Create goodbye first, then arm hard-close (baseline locked before create).
    assert order == ["create", "sched:100.0"]
    assert created == [True]
    assert h._hard_close_baseline_locked is True
    assert h._fsm.state == "NO_RESPONSE_CLOSE"
    assert "close" in h._silence_fired


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
