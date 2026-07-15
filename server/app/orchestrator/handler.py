"""Live wiring — mix the orchestrator into the Voice Live web handler.

One brain, wired at three seams:
  * _session_config() — compose skills for the current FSM state + register that
    state's tools (reuses WebMediaHandler's transcription setup via super()).
  * on_user_transcript_done() — run the gate on every finalized caller turn; when
    it fires, swap the model's briefing and re-drive the response.
  * on_function_call() — execute the model's tool call, record it, reply with a
    FunctionCallOutputItem, and advance the FSM for state-changing tools.

Enabled always for web (see ``ORCHESTRATOR_ON`` in this module / server.py).
When the UI supplies a custom system prompt, that text becomes the spoken
behaviour for the call; skills are skipped. Compliance gates still run in code.
Silence check-ins still apply after the caller engages (QUALIFY).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from azure.ai.voicelive.models import FunctionCallOutputItem

from app.handler.web_media_handler import WebMediaHandler
from app.agent_persona import (
    resolve_agent_voice_lead_silence_ms,
    resolve_agent_voice_name,
    resolve_agent_voice_rate,
    resolve_agent_voice_style,
    voice_name_is_openai,
)
from app.orchestrator.callback_time import is_concrete_callback_time
from app.orchestrator.consent import is_consent_affirm, is_consent_refusal
from app.orchestrator.dialog import apply_action
from app.orchestrator.fsm import CallContext, CallStateMachine
from app.orchestrator.intents import matched_opt_out
from app.orchestrator.mood import (
    Mood,
    REACTION_FIRST,
    detect_mood,
    mood_cue,
    mood_voice_context,
    pace_cue,
    resolve_delivery_pace,
)
from app.orchestrator.semantic import semantic_enabled
from app.orchestrator.silence import (
    REPROMPT_CUES,
    DECLINE_CLOSE_RULES,
    DNC_CLOSE_RULES,
    DNC_FEEDBACK_ASK_RULES,
    DNC_FEEDBACK_FOLLOWUP_RULES,
    NO_RESPONSE_CLOSE_RULES,
    SILENCE_CHECKIN_RULES,
    SILENCE_WATCH_STATES,
    load_silence_policy,
    next_silence_step,
    quiet_elapsed,
)
from app.orchestrator.tools import execute_tool, function_tools, tools_for
from app.orchestrator.trust_ui import (
    GATE_INTERRUPT_MS,
    GATE_TTFA_P50_S,
    GATE_TURNS_AFTER_OPT_OUT,
    briefing_for_state,
    call_short_id,
    caller_quote,
    dnc_record_id,
    fsm_state_history,
    receipt_event_label,
    ribbon_stages,
    stage_label,
)
from skills.loader import compose

logger = logging.getLogger(__name__)

# Product default: orchestrator is always on for web. Do not require .env.
# Set only for one-off local experiments against the classic non-orchestrated
# handler — prefer leaving this True.
ORCHESTRATOR_ON = True


def orchestrator_enabled() -> bool:
    """Whether web calls use the orchestrated handler.

    Code constant wins as the product default. An explicit env value of
    ``false``/``0``/``off`` can still force the classic handler for debugging.
    """
    raw = os.getenv("ORCHESTRATOR_ENABLED", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return ORCHESTRATOR_ON


def _select_engine():
    """Pick the decision engine: LangGraph or the plain FSM (default).

    Both expose ``handle_caller_turn(text, fsm, ctx, sink=...) -> Decision | None``.
    """
    if os.getenv("ORCHESTRATOR_ENGINE", "fsm").strip().lower() == "langgraph":
        from app.orchestrator.graph import GraphEngine

        logger.info("[Orchestrator] decision engine = langgraph")
        return GraphEngine()
    from app.orchestrator import dialog

    logger.info("[Orchestrator] decision engine = fsm")
    return dialog

# Tools that move the call to a new stage when the model calls them.
_TOOL_STATE_CHANGE: dict[str, str] = {
    "transfer_to_lo": "TRANSFER",
    "schedule_callback": "CALLBACK_CLOSE",
    "route_language": "LANGUAGE_ROUTE",
}

# Single-turn goodbye states: once entered, the call ends after the goodbye plays —
# schedule the hard-close on entry rather than depending on the model to call
# end_call (which gpt-4o-mini does unreliably). CALLBACK_CLOSE is intentionally
# excluded: it is a multi-turn flow (gather time -> schedule_callback -> end_call).
# Single-turn goodbyes that hang up after speech. DNC is multi-turn (feedback ask
# first); hard-close only after feedback or a wrap without it.
_TERMINAL_CLOSE_STATES = {"DECLINE_CLOSE", "NO_RESPONSE_CLOSE"}
_DNC_FEEDBACK_SKIP = frozenset(
    {
        "no",
        "nope",
        "nothing",
        "n/a",
        "na",
        "none",
        "just remove me",
        "just stop calling",
        "that's all",
        "thats all",
        "goodbye",
        "bye",
        "no thanks",
        "no thank you",
    }
)

# Mood + reaction-first cues apply on conversational stages (not hard closes).
_DELIVERY_STATES = frozenset(
    {"GREETING", "QUALIFY", "CALLBACK_CLOSE", "TRANSFER", "LANGUAGE_ROUTE"}
)

# Skip the human think-pause — TCPA opt-out must answer immediately.
_URGENT_NO_PAUSE = frozenset({"DNC_CLOSE"})

# Dispositions that mean the call has reached its outcome. Recording one of these
# ends the call in code even if the model never calls end_call — the hard-close
# still waits for the goodbye to finish first. ``callback_requested`` and
# ``do_not_call`` are NOT here: CALLBACK gathers a time; DNC asks brief feedback
# before wrapping (list is already written on the opt-out gate).
_END_DISPOSITIONS = {
    "completed",
    "declined",
    "no_response",
    "no_tcpa_consent",
}
_CLOSE_STATES = {"DECLINE_CLOSE", "DNC_CLOSE", "CALLBACK_CLOSE", "NO_RESPONSE_CLOSE"}


class OrchestratorMixin:
    """Adds the gate + FSM + skills + tools to a VoiceLiveMediaHandler subclass."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fsm = CallStateMachine(call_id="local")
        self._ctx = CallContext(call_id="local")
        data_dir = Path(__file__).resolve().parents[2] / "data"
        from app.orchestrator.tools import jsonl_sink

        self._sink = jsonl_sink(data_dir / "crm_stub.jsonl")
        self._engine = _select_engine()
        self._silence_policy = load_silence_policy()
        self._silence_task: asyncio.Task | None = None
        self._silence_anchor: float | None = None
        self._silence_fired: set[str] = set()
        self._silence_paused_at: float | None = None
        self._silence_paused_total: float = 0.0
        self._silence_reprompting = False
        self._silence_cue: str | None = None
        # Bumped to invalidate deferred arm-after-settle tasks when speech aborts silence.
        self._silence_arm_token = 0
        # How many in-flight silence response.done events to ignore after an abort.
        self._silence_ignore_dones = 0
        # Last real QUALIFY ask (not a silence check-in) — used to resume after pauses.
        self._open_question: str | None = None
        self._silence_awaiting_transcript = False
        self._silence_ignore_speech_until = 0.0
        self._silence_closing = False
        self._silence_close_token = 0
        self._silence_settle_active = False
        # Quiet clock armed only after client PlaybackFinished (not generate-done).
        self._silence_pending_arm = False
        self._silence_pending_peak = 0.0
        self._silence_pending_clear_fired = True
        # True while Voice Live reports caller speech (start → stop).
        self._caller_speech_active = False
        self._silence_phantom_task: asyncio.Task | None = None
        self._mood: Mood = "neutral"
        self._last_user_text: str = ""
        # Trust-console feeds (UI-only): never gate speech / compliance on these.
        self._intake_frozen: bool = False
        # Lock qualify field capture at DNC gate; UI freeze banner waits until
        # feedback (+ optional follow-up) finishes.
        self._qualify_locked: bool = False
        self._opt_out_at: float | None = None
        self._turns_after_opt_out: int = 0
        self._dnc_awaiting_feedback: bool = False
        self._dnc_awaiting_followup: bool = False
        self._last_interrupt_ms: int | None = None
        self._trust_t0: float = time.perf_counter()
        # Dedupe receipt keys so gate + tool paths don't reprint the same line.
        self._receipt_keys: set[str] = set()
        self._receipt_event_label: str | None = None
        logger.info("[Orchestrator] enabled — starting in %s", self._fsm.state)

    def set_call_context(self, call_id, channel="web"):
        super().set_call_context(call_id, channel)
        self._fsm.call_id = call_id
        self._ctx.call_id = call_id
        self._trust_snapshot(reason="call_start")

    # --- UI notices: surface each orchestrator action to the browser as a pop-up ---

    # action -> (level, message). ``level`` drives the toast colour on the client
    # (danger = red for the do-not-call removal).
    _ACTION_NOTICE = {
        "DNC_CLOSE": ("danger", "Removing you from our contact list — you won't be contacted again."),
        "DECLINE_CLOSE": ("info", "Understood — wrapping up the call."),
        "CALLBACK_CLOSE": ("success", "Arranging your callback…"),
        "ESCALATE": ("info", "Connecting you to a loan officer — transferring shortly…"),
        "LANGUAGE_ROUTE": ("info", "Routing you to someone who can continue in your language…"),
    }

    def _notice(self, level: str, text: str, *, kind: str = "notice") -> None:
        """Fire-and-forget UI pop-up to the browser (best-effort; rides AgentEvent)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (e.g. offline tests) — skip silently
        payload = {
            "Kind": "AgentEvent",
            "event": "notice",
            "level": level,
            "text": text,
            "noticeKind": kind,
        }
        logger.info("[Orchestrator] notice -> [%s] %s", level, text)
        task = loop.create_task(self._send_notice(payload))
        # Keep a strong reference until it finishes — the event loop only holds a
        # weak one, so a fire-and-forget task can otherwise be GC'd before it runs.
        bag = self.__dict__.setdefault("_notice_tasks", set())
        bag.add(task)
        task.add_done_callback(bag.discard)

    async def _send_notice(self, payload: dict) -> None:
        """Send a notice payload, surfacing (not swallowing) any transport error."""
        try:
            await self.on_agent_event(payload)
        except Exception:
            logger.exception("[Orchestrator] notice send FAILED")

    def _notice_action(self, action: str) -> None:
        pair = self._ACTION_NOTICE.get(action)
        if pair:
            self._notice(pair[0], pair[1], kind="route")

    # --- Trust console (additive AgentEvent feed; never blocks the call) -------

    def _trust_elapsed_s(self) -> float:
        return round(time.perf_counter() - self._trust_t0, 3)

    def _trust(self, kind: str, **fields) -> None:
        """Fire-and-forget trust UI event (best-effort; rides AgentEvent)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        payload = {
            "Kind": "AgentEvent",
            "event": "trust",
            "trustKind": kind,
            "t": self._trust_elapsed_s(),
            "callId": getattr(self._ctx, "call_id", None),
            "callShort": call_short_id(getattr(self._ctx, "call_id", None)),
            "state": self._fsm.state,
            "stage": stage_label(self._fsm.state),
            **fields,
        }
        task = loop.create_task(self._send_notice(payload))
        bag = self.__dict__.setdefault("_notice_tasks", set())
        bag.add(task)
        task.add_done_callback(bag.discard)

    def _trust_snapshot(self, *, reason: str = "snapshot") -> None:
        """Push ribbon + briefing + gate arming state to the browser."""
        brief = briefing_for_state(self._fsm.state)
        self._trust(
            "snapshot",
            reason=reason,
            ribbon=ribbon_stages(
                self._fsm.state, history=fsm_state_history(self._fsm)
            ),
            rebuttalUsed=bool(getattr(self._ctx, "rebuttal_used", False)),
            rebuttalBudget=1,
            briefing=brief,
            gatesArmed=True,
            intakeFrozen=self._intake_frozen,
            disposition=getattr(self._ctx, "disposition", None),
            dncRecorded=bool(getattr(self._ctx, "dnc_recorded", False)),
            gateTargets={
                "ttfaP50s": GATE_TTFA_P50_S,
                "interruptMs": GATE_INTERRUPT_MS,
                "turnsAfterOptOut": GATE_TURNS_AFTER_OPT_OUT,
            },
            turnsAfterOptOut=self._turns_after_opt_out,
            lastInterruptMs=self._last_interrupt_ms,
        )

    def _trust_receipt_line(
        self,
        text: str,
        *,
        ok: bool = False,
        record_id: str | None = None,
        once_key: str | None = None,
        event_label: str | None = None,
        highlight: str | None = None,
    ) -> None:
        """Append one AUDIT COPY line (UI only). ``once_key`` dedupes gate+tool duplicates."""
        if once_key:
            if once_key in self._receipt_keys:
                return
            self._receipt_keys.add(once_key)
        if event_label:
            self._receipt_event_label = event_label
        self._trust(
            "receipt",
            line=text,
            ok=ok,
            recordId=record_id,
            highlight=highlight,
            onceKey=once_key,
            eventLabel=self._receipt_event_label,
            intakeFrozen=self._intake_frozen,
            turnsAfterOptOut=self._turns_after_opt_out,
        )

    def _trust_promise(self, spoken: str, record_id: str) -> None:
        self._trust("promise", spoken=spoken, recordId=record_id, backed=True)

    def _trust_eng_log(self, text: str) -> None:
        self._trust("eng_log", text=text)

    async def _flush_line_audio(self) -> int:
        """Cancel + StopAudio; return ms to silence (trust metric only)."""
        t0 = time.perf_counter()
        await self._cancel_active_response_if_needed()
        try:
            await self.send_message(
                json.dumps({"Kind": "StopAudio", "AudioData": None, "StopAudio": {}})
            )
        except Exception:
            logger.debug("[Orchestrator] StopAudio flush skipped", exc_info=True)
        ms = max(0, int((time.perf_counter() - t0) * 1000))
        self._last_interrupt_ms = ms
        return ms

    async def _emit_gate_trust(self, action: str, caller_text: str) -> None:
        """Append AUDIT COPY lines for a forced workflow action (UI only; never gates speech)."""
        quote = caller_quote(caller_text)
        label = receipt_event_label(action)
        phrase = matched_opt_out(caller_text) if action == "DNC_CLOSE" else None

        self._trust(
            "gate_hit",
            action=action,
            pattern=phrase or quote,
            message=(
                f'GATE · opt out matched "{phrase}"'
                if action == "DNC_CLOSE" and phrase
                else f"GATE · {action}"
            ),
        )

        if action == "DNC_CLOSE":
            self._opt_out_at = time.perf_counter()
            self._turns_after_opt_out = 0
            # Lock further qualify fields; do NOT freeze the UI banner yet —
            # wait until feedback (and optional follow-up) is done.
            self._qualify_locked = True
            self._intake_frozen = False
            hit = phrase or quote
            self._trust_receipt_line(
                f'opt out matched "{hit}"',
                event_label=label,
                highlight=hit,
            )
            self._trust_eng_log(f'gate=OPT_OUT_HARD pattern="{hit}"')
            flush_ms = await self._flush_line_audio()
            self._trust_receipt_line("model response cancelled")
            self._trust_receipt_line(
                f"line audio flushed · {flush_ms} ms to silence", ok=True
            )
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )
            rid = dnc_record_id(self._ctx.call_id)
            self._trust_receipt_line(
                f"do not call record {rid} written",
                ok=True,
                record_id=rid,
                once_key=f"dnc:{rid}",
            )
            disp = getattr(self._ctx, "disposition", None) or "do_not_call"
            self._trust_receipt_line(
                f"disposition set: {disp}", ok=True, once_key=f"disp:{disp}"
            )
            self._trust_receipt_line(
                "waiting on opt-out feedback · intake not frozen yet",
                ok=True,
                once_key="intake_waiting_feedback",
            )
            self._trust_promise("we won't call again", rid)
            self._dnc_awaiting_feedback = True
            self._dnc_awaiting_followup = False
            self._mood = "hesitant"

        elif action == "CALLBACK_CLOSE":
            self._trust_receipt_line(
                f'callback matched "{quote}"',
                event_label=label,
                highlight=quote,
            )
            flush_ms = await self._flush_line_audio()
            self._trust_receipt_line("model response cancelled")
            self._trust_receipt_line(
                f"line audio flushed · {flush_ms} ms to silence", ok=True
            )
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )
            disp = getattr(self._ctx, "disposition", None) or "callback_requested"
            self._trust_receipt_line(
                f"disposition set: {disp}", ok=True, once_key=f"disp:{disp}"
            )

        elif action == "DECLINE_CLOSE":
            self._trust_receipt_line(
                f'decline matched "{quote}"',
                event_label=label,
                highlight=quote,
            )
            flush_ms = await self._flush_line_audio()
            self._trust_receipt_line("model response cancelled")
            self._trust_receipt_line(
                f"line audio flushed · {flush_ms} ms to silence", ok=True
            )
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )
            disp = getattr(self._ctx, "disposition", None) or "declined"
            self._trust_receipt_line(
                f"disposition set: {disp}", ok=True, once_key=f"disp:{disp}"
            )

        elif action in ("ESCALATE", "TRANSFER"):
            self._trust_receipt_line(
                f'transfer matched "{quote}"',
                event_label=label,
                highlight=quote,
            )
            flush_ms = await self._flush_line_audio()
            self._trust_receipt_line("model response cancelled")
            self._trust_receipt_line(
                f"line audio flushed · {flush_ms} ms to silence", ok=True
            )
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )
            disp = getattr(self._ctx, "disposition", None) or "transferred"
            self._trust_receipt_line(
                f"disposition set: {disp}", ok=True, once_key=f"disp:{disp}"
            )

        elif action == "LANGUAGE_ROUTE":
            self._trust_receipt_line(
                f'language route matched "{quote}"',
                event_label=label,
                highlight=quote,
            )
            flush_ms = await self._flush_line_audio()
            self._trust_receipt_line("model response cancelled")
            self._trust_receipt_line(
                f"line audio flushed · {flush_ms} ms to silence", ok=True
            )
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )
            disp = getattr(self._ctx, "disposition", None) or "language_routed"
            self._trust_receipt_line(
                f"disposition set: {disp}", ok=True, once_key=f"disp:{disp}"
            )

        else:
            self._trust_receipt_line(
                f"workflow forced → {action}",
                event_label=label,
            )
            flush_ms = await self._flush_line_audio()
            self._trust(
                "gate_flush",
                interruptMs=flush_ms,
                message=f"agent stopped in {flush_ms} ms",
            )

        self._trust_snapshot(reason=f"action:{action}")

    def _note_agent_turn_after_opt_out(self) -> None:
        if self._opt_out_at is None:
            return
        self._turns_after_opt_out += 1
        self._trust_receipt_line(
            f"close spoken · {self._turns_after_opt_out} turn after opt out",
            ok=self._turns_after_opt_out <= GATE_TURNS_AFTER_OPT_OUT,
        )
        self._trust(
            "opt_out_turns",
            turnsAfterOptOut=self._turns_after_opt_out,
        )
    # --- model-agnostic capture: bridge extracted insights into the paper trail --

    # Insight-key hints for a preferred callback time/window. This matches the
    # extractor's OUTPUT keys (which the model produced), never the caller's raw
    # words, and only drives a non-compliance side-effect — schedule_callback also
    # stays in the CALLBACK_CLOSE tool allow-list as a fallback.
    _CALLBACK_TIME_HINTS = ("callback", "call_back", "call back")

    def _captured_callback_time(self, emitted: dict) -> str | None:
        for key, row in emitted.items():
            k = (key or "").lower()
            hit = any(h in k for h in self._CALLBACK_TIME_HINTS) or (
                "time" in k and any(w in k for w in ("prefer", "contact", "follow"))
            )
            if hit and (row or {}).get("value"):
                return str(row["value"])
        return None

    def _bridge_insights_to_ctx(self) -> None:
        """Copy model-agnostic extracted fields into the orchestrator's paper trail.

        The transcript extractor (a text model, running regardless of the voice
        model) populates self._emitted_insights in the background. Mirror any NEW
        ones into ctx.fields + the CRM sink via execute_tool, so borrower capture no
        longer depends on the voice model calling capture_borrower_field. Also fire
        schedule_callback in code once a callback time is captured during the callback
        close (idempotent via ctx.callback_scheduled).
        """
        # After hard opt-out, stop writing new borrower fields (except the
        # orchestrator-owned dnc_feedback write). UI freeze banner is separate.
        if self._qualify_locked or self._intake_frozen:
            return
        emitted = getattr(self, "_emitted_insights", None) or {}
        for key, row in emitted.items():
            if not key or key in self._ctx.fields:
                continue
            value = (row or {}).get("value")
            if value in (None, ""):
                continue
            execute_tool(
                "capture_borrower_field",
                {
                    "field": key,
                    "value": str(value),
                    "confidence": (row or {}).get("confidence"),
                },
                self._ctx,
                sink=self._sink,
            )
        if self._fsm.state == "CALLBACK_CLOSE" and not self._ctx.callback_scheduled:
            preferred = self._captured_callback_time(emitted)
            # Do not lock a vague half-answer ("morning" / "anytime") — that cut
            # the multi-turn schedule dialogue short and hard-closed mid-speech.
            if preferred and is_concrete_callback_time(preferred):
                result = execute_tool(
                    "schedule_callback",
                    {"preferred_time": preferred},
                    self._ctx,
                    sink=self._sink,
                )
                rid = (result or {}).get("record_id")
                if rid:
                    self._trust_promise(f"callback · {preferred}", str(rid))
                    self._trust_receipt_line(
                        f"callback recorded · {preferred} {rid}",
                        ok=True,
                        record_id=str(rid),
                        once_key=f"cb:{rid}",
                        event_label=receipt_event_label("CALLBACK_CLOSE"),
                        highlight=str(rid),
                    )
                    self._trust_receipt_line(
                        "confirmation matches record",
                        ok=True,
                        once_key=f"cb_confirm:{rid}",
                    )

    # --- end-of-call is the orchestrator's alone --------------------------

    def _schedule_auto_end_call(self) -> None:
        """Route the base handler's (mature) end detection to the SAFE hard-close.

        WebMediaHandler reliably detects when the agent has delivered its goodbye and
        the caller is done — but its NATIVE auto-end fires CallEnded immediately, which
        cuts the goodbye. So instead of running that native path, we route the signal
        to the orchestrator's ``_schedule_hard_close`` (which waits for the goodbye to
        finish playing, then +2s). Gated on a disposition existing, so greeting/qualify
        misfires never end the call. This is what closes multi-turn flows (callback)
        even when the model skips end_call.
        """
        if self._ctx.disposition is None:
            logger.debug("[Orchestrator] base end-detection ignored — no disposition yet")
            return
        # CALLBACK sets disposition=callback_requested on entry while time is still
        # being gathered — never treat that as "goodbye finished, hang up".
        if self._ctx.disposition not in _END_DISPOSITIONS:
            logger.debug(
                "[Orchestrator] base end-detection ignored — disposition %s not terminal",
                self._ctx.disposition,
            )
            return
        logger.info(
            "[Orchestrator] base end-detection fired (disposition=%s) — scheduling close",
            self._ctx.disposition,
        )
        self._schedule_hard_close()

    def _schedule_hard_close(self) -> None:
        """Authoritatively tear the call down after end_call is accepted.

        The base finalize sends a CallEnded message and relies on the browser to
        hang up, which proved unreliable — the WebSocket stayed open and the caller
        kept talking. Since the agent has decided to end (end_call requires a
        recorded disposition), the server closes the client socket itself after a
        short grace period for the goodbye. Closing it triggers the normal
        disconnect cleanup (persist + Voice Live teardown).
        """
        task = getattr(self, "_hard_close_task", None)
        if task is not None and not task.done():
            return
        # Snapshot only if the close path has not already locked a pre-goodbye
        # baseline (silence close locks before response.create so late TTS still counts).
        if not getattr(self, "_hard_close_baseline_locked", False):
            self._close_wait_since_audio = getattr(self, "_last_audio_delta_at", 0.0)
        self._hard_close_scheduled_at = time.perf_counter()
        if not getattr(self, "_trust_close_receipt_sent", False):
            self._trust_close_receipt_sent = True
            self._trust_receipt_line("call ended clean", ok=True)
            self._trust("receipt_total", held=True, label="RECORD BEFORE PROMISE")
        self._hard_close_task = asyncio.create_task(self._hard_close_after_grace())

    def _abort_pending_hard_close(self) -> None:
        """Cancel an in-flight hang-up so a late DNC/decline goodbye can play."""
        pending = getattr(self, "_hard_close_task", None)
        if pending is not None and not pending.done():
            pending.cancel()
            self._hard_close_task = None
        self._hard_close_baseline_locked = False
        self._trust_close_receipt_sent = False

    async def request_end_call(self, *, source: str = "client") -> None:
        """Client/agent hang-up — never let a silence nudge speak after this.

        Manual End Call used to leave the silence watcher armed, so ~15s quiet
        (or an in-flight check-in) could still utter \"are you still with me?\"
        after the user already ended.
        """
        logger.info("[Orchestrator] request_end_call source=%s — abort silence/TTS", source)
        self._cancel_silence_watch()
        router = getattr(self, "_router_task", None)
        if router is not None and not router.done():
            router.cancel()
        self._ctx.ended = True
        # Stop any in-flight agent audio (silence check-in or mid-sentence).
        try:
            await self._cancel_active_response_if_needed()
        except Exception:
            logger.debug("[Orchestrator] cancel on end_call failed", exc_info=True)
        try:
            await self.send_message(
                json.dumps({"Kind": "StopAudio", "AudioData": None, "StopAudio": {}})
            )
        except Exception:
            logger.debug("[Orchestrator] StopAudio on end_call failed", exc_info=True)
        await super().request_end_call(source=source)

    async def cleanup(self):
        """Tear down — always kill silence so disconnect cannot leave nudges running."""
        self._cancel_silence_watch()
        self._ctx.ended = True
        try:
            await self._cancel_active_response_if_needed()
        except Exception:
            pass
        return await super().cleanup()

    # End-of-call timing. The call ends only after the agent's goodbye has FULLY
    # played on the client, then a short pause — never mid-sentence.
    _POST_SPEECH_GRACE_S = 2.0      # pause after speech finishes, before ending
    _PLAYBACK_START_CAP_S = 12.0    # wait for goodbye audio to start (TTS + session.update)
    _PLAYBACK_END_CAP_S = 25.0      # overall cap waiting for playback to finish
    _AUDIO_SETTLED_S = 0.4          # no new audio for this long => stream settled
    _SETTLED_FALLBACK_S = 3.0       # if the client never signals, close after this quiet
    _NO_AUDIO_ABANDON_S = 12.0      # never hang waiting forever if TTS produces nothing

    async def _hard_close_after_grace(self) -> None:
        """End the call only after the agent's final speech is fully delivered.

        Timeline: wait for the goodbye to START, then for the client to report it
        FINISHED playing (a real signal, not a server-side guess) -> pause
        _POST_SPEECH_GRACE_S -> finalize (summary + persist on the still-open
        socket) -> brief settle -> close the socket as a safety net.
        Nothing cuts the goodbye short, and the summary is not dropped by an
        early WebSocket close.
        """
        # Ask the client to report when it finishes PLAYING the goodbye. It only
        # forwards PlaybackFinished once armed, so the channel stays quiet during
        # normal conversation (the worklet drains between every utterance).
        try:
            await self.send_message(json.dumps({"Kind": "AwaitPlaybackEnd"}))
        except Exception:
            logger.debug("[Orchestrator] arm playback-end signal skipped", exc_info=True)
        # Final capture of anything the extractor produced late in the call.
        self._bridge_insights_to_ctx()
        # Prefetch summary while goodbye audio plays — still silent in the UI until
        # request_end_call emits the loading banner.
        try:
            self._prefetch_call_summary()
        except Exception:
            logger.debug("[Orchestrator] summary prefetch skipped", exc_info=True)
        try:
            await self._wait_for_playback_end()
            await asyncio.sleep(self._POST_SPEECH_GRACE_S)
        except asyncio.CancelledError:
            raise
        # Hang up the interview, generate summary, persist — all while the client
        # WebSocket stays open so "Generating call summary…" can resolve into text.
        try:
            if not getattr(self, "_finalizing", False):
                await self.request_end_call(source="agent")
            await self.await_finalize_complete()
        except Exception:
            logger.exception("[Orchestrator] finalize on hard-close failed")
        # Give the browser a beat to paint CallSummary / CallSaved before we force
        # the socket down (client normally closes itself after CallSaved).
        try:
            await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            raise
        ws = getattr(self, "client_ws", None)
        if ws is None:
            return
        try:
            logger.info(
                "[Orchestrator] hard-closing client WebSocket after summary+persist"
            )
            await ws.close(1000)
        except Exception:
            logger.debug("[Orchestrator] client WS close skipped", exc_info=True)

    async def _wait_for_playback_end(self) -> None:
        """Block until the agent's goodbye has fully played on the client.

        Uses two timestamps on the same perf_counter clock: ``_last_audio_delta_at``
        (last audio chunk forwarded to the client) and ``_last_drain_at`` (browser
        reported its playback buffer emptied). Playback is done when NEW goodbye
        audio (after ``_close_wait_since_audio``) has SETTLED and the client drained
        after that final chunk. Stale pre-close drains never count. If TTS never
        starts, abandon after ``_NO_AUDIO_ABANDON_S`` (must be long enough for
        session.update + first audio — a short settle fallback here previously tore
        the call down mid-goodbye).
        """
        loop = asyncio.get_event_loop()
        baseline = getattr(self, "_close_wait_since_audio", 0.0)
        scheduled_at = getattr(self, "_hard_close_scheduled_at", time.perf_counter())
        goodbye_peak = baseline
        saw_goodbye = False

        # 1. Wait for goodbye audio to start (must exceed pre-close baseline).
        start_deadline = loop.time() + self._PLAYBACK_START_CAP_S
        while loop.time() < start_deadline:
            last = getattr(self, "_last_audio_delta_at", 0.0)
            if last > baseline:
                goodbye_peak = last
                saw_goodbye = True
                break
            await asyncio.sleep(0.1)

        # 2. Wait for that goodbye stream to settle and the client's final drain.
        end_deadline = loop.time() + self._PLAYBACK_END_CAP_S
        while loop.time() < end_deadline:
            now = time.perf_counter()
            last_audio = getattr(self, "_last_audio_delta_at", 0.0)
            last_drain = getattr(self, "_last_drain_at", 0.0)
            if last_audio > baseline:
                goodbye_peak = max(goodbye_peak, last_audio)
                saw_goodbye = True
            quiet = now - last_audio
            if saw_goodbye and quiet >= self._AUDIO_SETTLED_S and (
                last_drain >= goodbye_peak
                or quiet >= self._SETTLED_FALLBACK_S
            ):
                self._debug_silence_log(
                    "playback_wait_done",
                    {
                        "sawGoodbye": True,
                        "waitMs": int((now - scheduled_at) * 1000),
                        "quietMs": int(quiet * 1000),
                    },
                    "H4_AUDIO",
                )
                return
            if not saw_goodbye and (now - scheduled_at) >= self._NO_AUDIO_ABANDON_S:
                self._debug_silence_log(
                    "playback_wait_no_audio",
                    {
                        "sawGoodbye": False,
                        "waitMs": int((now - scheduled_at) * 1000),
                        "baseline": baseline,
                        "lastAudio": last_audio,
                    },
                    "H4_AUDIO",
                )
                logger.info(
                    "[Orchestrator] no goodbye audio after %.1fs — closing anyway",
                    now - scheduled_at,
                )
                return
            await asyncio.sleep(0.1)
        logger.info("[Orchestrator] playback-end wait capped; closing anyway")
        self._debug_silence_log(
            "playback_wait_capped",
            {"sawGoodbye": saw_goodbye, "baseline": baseline},
            "H4_AUDIO",
        )

    # --- semantic router (whole conversation; only when the keyword gate is silent) --

    def _spawn_router(self) -> None:
        task = getattr(self, "_router_task", None)
        if task is not None and not task.done():
            task.cancel()  # supersede any in-flight pass with the latest turn
        self._router_task = asyncio.create_task(self._route_conversation())

    async def _route_conversation(self) -> None:
        """Whole-conversation router, then exactly one reply (no speculative race).

        Speculative QUALIFY speech (T17) was creating a visible double-speak when the
        router later forced CALLBACK/ESCALATE — the first answer had often already
        finished ``response.create`` (``speculativeDone=true``) so cancel/StopAudio
        could not prevent two agent turns. Prefer +1–2s wait over two spoken replies.
        """
        started_in = self._fsm.state
        try:
            action = await self._engine.classify_turn(
                list(self._call_turns), thread_id=self._ctx.call_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[Orchestrator] router failed", exc_info=True)
            action = None
        if self._ctx.ended:
            return

        # GREETING: semantic CONTINUE / None = consent to proceed into QUALIFY.
        if started_in == "GREETING" and self._fsm.state == "GREETING":
            if action == "DECLINE_CLOSE":
                logger.info("[Orchestrator] router greeting -> DECLINE_CLOSE")
                apply_action(action, self._fsm, self._ctx, sink=self._sink)
                execute_tool(
                    "log_disposition",
                    {"disposition": "no_tcpa_consent"},
                    self._ctx,
                    sink=self._sink,
                )
                self._notice_action(action)
                await self._emit_gate_trust(action, self._last_user_text)
                await self._update_session()
                await self._create_response(cancel=True, think_pause=False)
                self._schedule_hard_close()
                return
            if action in (
                "DNC_CLOSE",
                "CALLBACK_CLOSE",
                "ESCALATE",
                "LANGUAGE_ROUTE",
            ):
                logger.info("[Orchestrator] router greeting -> %s", action)
                apply_action(action, self._fsm, self._ctx, sink=self._sink)
                self._notice_action(action)
                await self._emit_gate_trust(action, self._last_user_text)
                await self._update_session()
                await self._create_response(
                    cancel=True,
                    think_pause=action not in _URGENT_NO_PAUSE,
                )
                if self._fsm.state in _TERMINAL_CLOSE_STATES:
                    self._schedule_hard_close()
                return
            # CONTINUE / None / unclear → treat as consent and start qualifying.
            logger.info(
                "[Orchestrator] router greeting continue (action=%s) -> QUALIFY",
                action,
            )
            self._fsm.transition("QUALIFY", reason="consent_affirmed_semantic")
            await self._update_session()
            await self._create_response(cancel=True)
            return

        if action and self._fsm.state == "QUALIFY":
            logger.info("[Orchestrator] router -> %s (single reply)", action)
            apply_action(action, self._fsm, self._ctx, sink=self._sink)
            self._notice_action(action)
            await self._emit_gate_trust(action, self._last_user_text)
            await self._update_session()
            await self._create_response(
                cancel=True,
                think_pause=action not in _URGENT_NO_PAUSE,
            )
            if self._fsm.state in _TERMINAL_CLOSE_STATES:
                self._schedule_hard_close()
            return

        # CALLBACK CLOSE: still honor late opt-out / decline (must never miss DNC).
        if started_in == "CALLBACK_CLOSE" and self._fsm.state == "CALLBACK_CLOSE":
            if action in ("DNC_CLOSE", "DECLINE_CLOSE"):
                logger.info("[Orchestrator] router callback -> %s", action)
                # Abort any pending callback hang-up so the DNC/decline goodbye can play.
                self._abort_pending_hard_close()
                apply_action(action, self._fsm, self._ctx, sink=self._sink)
                self._notice_action(action)
                await self._emit_gate_trust(action, self._last_user_text)
                await self._update_session()
                await self._create_response(
                    cancel=True,
                    think_pause=action not in _URGENT_NO_PAUSE,
                )
                if self._fsm.state in _TERMINAL_CLOSE_STATES:
                    self._schedule_hard_close()
                return
            await self._create_response()
            return

        await self._create_response()

    # --- silence policy (T11): 5s / 8s check-ins → no_response after +10s -----
    # Armed on INTRO, QUALIFY, CALLBACK, and DNC (until wrap). Not on terminal closes.
    # After EVERY agent utterance ends: start a fresh quiet countdown.
    # Agent starts speaking → cancel wait. Never count silence while she talks.

    def _silence_quiet_elapsed(self) -> float:
        if self._silence_anchor is None:
            return 0.0
        return quiet_elapsed(
            time.monotonic(),
            anchor=self._silence_anchor,
            paused_total=self._silence_paused_total,
            paused_at=self._silence_paused_at,
        )

    def _cancel_silence_watch(self, *, reset: bool = True) -> None:
        task = self._silence_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
        self._silence_task = None
        self._silence_arm_token += 1
        self._cancel_phantom_rearm()
        if self._silence_reprompting:
            self._silence_ignore_dones += 1
        self._silence_reprompting = False
        if reset:
            self._silence_anchor = None
            self._silence_fired = set()
            self._silence_paused_at = None
            self._silence_paused_total = 0.0
            self._silence_cue = None
            self._ctx.silence_count = 0

    def _abort_silence_close(self) -> None:
        """Cancel an in-flight no-response/DNC silence wrap so caller speech wins."""
        if self._silence_closing:
            self._silence_close_token += 1
            self._silence_closing = False
            logger.info("[Orchestrator] silence close aborted — caller speaking")
        self._silence_fired.discard("close")

    def _silence_close_committed(self, token: int) -> bool:
        return token == self._silence_close_token and not self._ctx.ended

    def _silence_close_pending(self) -> bool:
        """Both check-ins fired — next silence step is mandatory NO_RESPONSE close."""
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return False
        if self._silence_closing:
            return True
        return (
            "reprompt:0" in self._silence_fired
            and "reprompt:1" in self._silence_fired
            and "close" not in self._silence_fired
        )

    def _caller_turn_counts_during_silence_tail(self, text: str) -> bool:
        """During the close-pending tail, ignore junk STT that would restart qualify."""
        t = (text or "").strip().lower()
        if not t:
            return False
        resume_markers = (
            "still here",
            "i'm here",
            "im here",
            "go ahead",
            "continue",
            "carry on",
            "yes",
            "yeah",
            "yep",
            "sure",
            "okay",
            "ok",
        )
        if any(m in t for m in resume_markers):
            return True
        # Substantive qualify answer — not random STT noise.
        if len(t.split()) >= 4 and any(
            w in t
            for w in ("refinance", "purchase", "buy", "mortgage", "loan", "home", "cash")
        ):
            return True
        return False

    async def try_silence_close_before_expiry(self) -> bool:
        """Play NO_RESPONSE goodbye before idle timeout tears the call down."""
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return False
        if "close" in self._silence_fired:
            return False
        if not (
            self._silence_close_pending()
            or self._ctx.silence_count >= len(self._silence_policy.reprompt_at_s)
        ):
            return False
        logger.info("[Orchestrator] idle expiry — forcing silence NO_RESPONSE close")
        await self._silence_close()
        return True

    def _debug_silence_log(self, message: str, data: dict, hypothesis_id: str) -> None:
        # #region agent log
        try:
            with open(
                r"c:\Users\FaisalImam(QuadrantT\Desktop\call-center-voice-agent-accelerator\debug-b18aa2.log",
                "a",
                encoding="utf-8",
            ) as _f:
                _f.write(
                    json.dumps(
                        {
                            "sessionId": "b18aa2",
                            "runId": "silence-close",
                            "hypothesisId": hypothesis_id,
                            "location": "handler.py",
                            "message": message,
                            "data": data,
                            "timestamp": int(time.time() * 1000),
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion

    def _pause_silence_timer_only(self) -> None:
        """Stop the pending quiet countdown without wiping reprompt progress."""
        task = self._silence_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
        self._silence_task = None
        self._silence_arm_token += 1
        self._silence_anchor = None  # stop any late loop from treating window as live
        if self._silence_reprompting:
            self._silence_ignore_dones += 1
        self._silence_reprompting = False
        self._silence_cue = None

    def _pause_silence_clock(self) -> None:
        """Agent is about to speak — cancel any quiet countdown (full reset of window)."""
        task = self._silence_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
            self._silence_task = None
        self._silence_anchor = None
        self._silence_paused_at = None
        self._silence_paused_total = 0.0

    # Echo blips right after Maya finishes used to be ignored for silence — that
    # also ignored real quick answers and let check-ins fire mid-utterance.
    # Phantom re-arm waits for speech_stopped + transcript lag, not speech_started.
    _SILENCE_PHANTOM_TRANSCRIPT_S = 5.0

    def _cancel_phantom_rearm(self) -> None:
        task = self._silence_phantom_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
        self._silence_phantom_task = None

    def _schedule_phantom_rearm(self) -> None:
        """If no transcript arrives after speech_stopped, resume the quiet ladder."""
        self._cancel_phantom_rearm()
        token = self._silence_arm_token
        task = asyncio.create_task(self._rearm_silence_if_phantom(token))
        self._silence_phantom_task = task
        bag = self.__dict__.setdefault("_notice_tasks", set())
        bag.add(task)
        task.add_done_callback(bag.discard)

    def _arm_silence_watch(self, *, clear_fired: bool = True) -> None:
        """Start a fresh quiet countdown after the agent finished speaking."""
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            self._cancel_silence_watch()
            return
        # Never start a quiet clock while the caller is mid-utterance or we are
        # still waiting on ASR for a turn we already heard.
        if self._caller_speech_active or self._silence_awaiting_transcript:
            logger.info(
                "[Orchestrator] silence arm deferred "
                "(speech_active=%s awaiting_transcript=%s)",
                self._caller_speech_active,
                self._silence_awaiting_transcript,
            )
            return
        self._silence_anchor = time.monotonic()
        self._silence_paused_at = None
        self._silence_paused_total = 0.0
        self._silence_cue = None
        self._silence_awaiting_transcript = False
        self._silence_ignore_speech_until = 0.0
        if clear_fired:
            self._silence_fired = set()
            self._ctx.silence_count = 0
        task = self._silence_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
        step = next_silence_step(fired=self._silence_fired, policy=self._silence_policy)
        gap = float(step[2]) if step else None
        logger.info(
            "[Orchestrator] silence armed (state=%s, next_gap=%.1fs, fired=%s)",
            self._fsm.state,
            gap if gap is not None else -1.0,
            sorted(self._silence_fired),
        )
        self._trust(
            "silence",
            status="armed",
            nextGapS=gap,
            checkinsDone=int(self._ctx.silence_count),
            fired=sorted(self._silence_fired),
        )
        self._silence_task = asyncio.create_task(self._silence_watch_loop())

    async def _rearm_silence_if_phantom(self, token: int) -> None:
        """Resume quiet countdown when speech_stopped never produced a transcript."""
        try:
            await asyncio.sleep(self._SILENCE_PHANTOM_TRANSCRIPT_S)
        except asyncio.CancelledError:
            raise
        if token != self._silence_arm_token:
            return
        if not self._silence_awaiting_transcript:
            return
        if self._caller_speech_active:
            return
        self._silence_awaiting_transcript = False
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        logger.info(
            "[Orchestrator] silence re-armed after speech_stopped with no transcript"
        )
        self._arm_silence_watch(clear_fired=False)

    async def _silence_watch_loop(self) -> None:
        """Wait one full quiet gap, then fire the next silence step once."""
        policy = self._silence_policy
        try:
            if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
                return
            if self._silence_anchor is None:
                return
            step = next_silence_step(fired=self._silence_fired, policy=policy)
            if step is None:
                return
            kind, index, gap = step
            logger.info(
                "[Orchestrator] silence wait %.1fs until %s (fresh after agent speech)",
                gap,
                kind if kind == "close" else f"reprompt#{index + 1}",
            )
            await asyncio.sleep(gap)
            if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
                return
            if self._silence_anchor is None:
                return  # agent started speaking again — window cancelled
            if kind == "reprompt":
                self._silence_fired.add(f"reprompt:{index}")
                self._ctx.silence_count = index + 1
                await self._silence_reprompt(index)
                return
            # Do NOT mark "close" fired until _silence_close commits — otherwise
            # a late caller utterance cannot cancel the wrap.
            await self._silence_close()
        except asyncio.CancelledError:
            raise

    def _silence_checkin_instructions(self, cue: str) -> str:
        """Silence-only briefing — never the QUALIFY skill (prevents invented answers)."""
        from app.agent_persona import BROKERAGE_NAME

        if self.system_prompt:
            # Stay in the caller's custom persona; only constrain this one nudge.
            return (
                f"{self.system_prompt}\n\n"
                f"---\nTEMPORARY SILENCE CHECK-IN (this turn only):\n{cue}\n\n"
                f"{SILENCE_CHECKIN_RULES}"
            )
        return (
            f"You are Maya, a mortgage assistant at {BROKERAGE_NAME}.\n\n"
            f"{cue}\n\n"
            f"{SILENCE_CHECKIN_RULES}"
        )

    def _current_delivery_pace(self):
        """Turn pace from mood, FSM stage, and high-stakes content hints."""
        return resolve_delivery_pace(
            self._mood,
            self._fsm.state,
            open_question=self._open_question or "",
            user_text=self._last_user_text or "",
        )

    def _delivery_suffix(self) -> str:
        """Mood cue + pace + reaction-first — skills and custom prompt edges."""
        pace = self._current_delivery_pace()
        return f"\n\n{mood_cue(self._mood)}\n{pace_cue(pace)}\n{REACTION_FIRST}"

    def _apply_mood_voice(self, session) -> None:
        """Refresh Azure voice style + rate from mood/pace (OpenAI voices: skip)."""
        if voice_name_is_openai(resolve_agent_voice_name()):
            return
        voice = getattr(session, "voice", None)
        if voice is None:
            return
        pace = self._current_delivery_pace()
        if hasattr(voice, "style"):
            voice.style = resolve_agent_voice_style(mood_voice_context(self._mood))
        if hasattr(voice, "rate"):
            rate = resolve_agent_voice_rate(pace=pace)
            if rate:
                voice.rate = rate
                logger.debug(
                    "[Orchestrator] voice pace=%s rate=%s style=%s",
                    pace,
                    rate,
                    getattr(voice, "style", None),
                )

    def _qualify_instructions(self) -> str:
        """Qualify skill (+ open-thread resume so pauses don't lose the agenda)."""
        instructions = compose(self._fsm.state, self._ctx.facts())
        open_q = (self._open_question or "").strip()
        if open_q and self._fsm.state == "QUALIFY":
            instructions = (
                f"{instructions}\n\n"
                "OPEN THREAD (do not lose this):\n"
                f'The last real qualifying ask still unanswered was: "{open_q}"\n'
                "If the caller says they are still here / thinking / ready / continue / "
                "carry on / dive back in / asks what the last question was: briefly ack, "
                "then RE-ASK that question (paraphrase ok) in the SAME turn. "
                "Never answer with only \"whenever you're ready\" or "
                "\"let me know how you'd like to proceed.\" "
                "Silence check-ins are NOT the last question."
            )
        return instructions

    async def on_transcript_done(self, transcript: str) -> None:
        """Remember the last real qualify ask so silence/resume can re-ask it."""
        await super().on_transcript_done(transcript)
        text = (transcript or "").strip()
        if not text or self._fsm.state != "QUALIFY":
            return
        if self._silence_reprompting or self._silence_cue:
            return  # silence check-in — never treat as the open question
        self._open_question = text

    async def _silence_reprompt(self, index: int) -> None:
        if self._ctx.ended or getattr(self, "_finalizing", False):
            return
        cue = REPROMPT_CUES[min(index, len(REPROMPT_CUES) - 1)]
        logger.info(
            "[Orchestrator] silence reprompt #%s (quiet=%.1fs)",
            index + 1,
            self._silence_quiet_elapsed(),
        )
        self._silence_reprompting = True
        self._silence_cue = cue
        self._trust(
            "silence",
            status="checkin",
            checkinIndex=index + 1,
            checkinsDone=index + 1,
        )
        try:
            await self._update_session()
            # Caller may have spoken during session.update — do not create a check-in.
            if not self._silence_reprompting:
                logger.info("[Orchestrator] silence reprompt aborted before create")
                self._silence_cue = None
                return
            if self._ctx.ended or getattr(self, "_finalizing", False):
                self._silence_reprompting = False
                self._silence_cue = None
                return
            await self._create_response(cancel=True, think_pause=False)
        except Exception:
            self._silence_reprompting = False
            self._silence_cue = None
            raise

    async def _silence_close(self) -> None:
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        # Caller already speaking / transcript pending — never wrap over them.
        if self._silence_awaiting_transcript:
            logger.info("[Orchestrator] silence close skipped — caller speech pending")
            return

        token = self._silence_close_token
        self._silence_closing = True

        def _aborted() -> bool:
            return (
                token != self._silence_close_token
                or self._silence_awaiting_transcript
                or self._ctx.ended
            )

        # Already on DNC — wrap without overwriting do_not_call.
        if self._fsm.state == "DNC_CLOSE":
            if _aborted():
                self._silence_closing = False
                return
            quiet = self._silence_quiet_elapsed()
            checks = int(self._ctx.silence_count)
            logger.info(
                "[Orchestrator] silence -> DNC wrap without feedback (quiet=%.1fs)",
                quiet,
            )
            if self._dnc_awaiting_feedback:
                self._record_dnc_feedback_skip(reason="silence")
            elif self._dnc_awaiting_followup:
                self._finalize_dnc_intake_freeze(reason="silence during follow-up")
            else:
                self._finalize_dnc_intake_freeze(reason="silence")
            self._trust_receipt_line(
                f"silence timeout · {quiet:.0f}s quiet after {checks} check-in(s)",
                event_label=receipt_event_label("DNC_CLOSE"),
            )
            self._trust("silence", status="close", checkinsDone=checks)
            self._silence_task = None
            self._silence_anchor = None
            self._silence_paused_at = None
            self._silence_paused_total = 0.0
            self._silence_cue = None
            self._silence_reprompting = False
            self._silence_fired.add("close")
            self._open_question = None
            self._close_wait_since_audio = getattr(self, "_last_audio_delta_at", 0.0)
            self._hard_close_baseline_locked = True
            await self._update_session()
            if _aborted():
                self._silence_closing = False
                self._silence_fired.discard("close")
                return
            await self._create_response(cancel=True, think_pause=False)
            self._silence_closing = False
            self._schedule_hard_close()
            return

        if _aborted():
            self._silence_closing = False
            return

        logger.info(
            "[Orchestrator] silence -> NO_RESPONSE_CLOSE (quiet=%.1fs)",
            self._silence_quiet_elapsed(),
        )
        self._debug_silence_log(
            "silence_close_start",
            {"fired": sorted(self._silence_fired), "state": self._fsm.state},
            "H2_CLOSE",
        )
        execute_tool(
            "log_disposition",
            {"disposition": self._silence_policy.disposition},
            self._ctx,
            sink=self._sink,
        )
        quiet = self._silence_quiet_elapsed()
        checks = int(self._ctx.silence_count)
        self._trust_receipt_line(
            f"silence timeout · {quiet:.0f}s quiet after {checks} check-in(s)",
            event_label=receipt_event_label("NO_RESPONSE_CLOSE"),
        )
        disp = self._silence_policy.disposition
        self._trust_receipt_line(
            f"disposition set: {disp}",
            ok=True,
            once_key=f"disp:{disp}",
        )
        if _aborted():
            self._silence_closing = False
            return
        self._fsm.transition("NO_RESPONSE_CLOSE", reason="silence_timeout")
        self._trust("silence", status="close", checkinsDone=checks)
        self._silence_task = None
        self._silence_anchor = None
        self._silence_paused_at = None
        self._silence_paused_total = 0.0
        self._silence_cue = None
        self._silence_reprompting = False
        self._silence_fired.add("close")
        self._open_question = None
        self._close_wait_since_audio = getattr(self, "_last_audio_delta_at", 0.0)
        self._hard_close_baseline_locked = True
        await self._update_session()
        await self._create_response(cancel=True, think_pause=False)
        self._silence_closing = False
        self._schedule_hard_close()
        self._debug_silence_log(
            "silence_close_create_scheduled",
            {
                "state": self._fsm.state,
                "baseline": self._close_wait_since_audio,
                "hardCloseArmed": True,
            },
            "H4_AUDIO",
        )

    async def on_speech_started(self):
        # Caller spoke: abort any in-flight silence check-in so it cannot finish
        # after "Yes" and stack a second reply / invent answers.
        self._caller_speech_active = True
        self._cancel_phantom_rearm()
        aborting = self._silence_reprompting or self._silence_cue is not None
        if self._silence_closing or self._silence_close_pending():
            # Abort wrap so a real turn (decline / DNC / answer) wins over silence.
            self._abort_silence_close()
            self._pause_silence_timer_only()
            self._silence_awaiting_transcript = True
            self._silence_pending_arm = False
            if aborting:
                logger.info("[Orchestrator] silence check-in aborted by caller speech")
            await super().on_speech_started()
            return

        # Real caller speech: kill any "wait for playback then arm" settle loop —
        # otherwise silence arms while they are mid-sentence after a long intro.
        self._silence_arm_token += 1

        if aborting:
            self._cancel_silence_watch()
            self._silence_pending_arm = False
            self._silence_awaiting_transcript = True
            logger.info("[Orchestrator] silence check-in aborted by caller speech")
            await super().on_speech_started()
            return

        watch_active = (
            self._silence_anchor is not None
            or (self._silence_task is not None and not self._silence_task.done())
            or self._silence_pending_arm
        )
        if watch_active:
            # Stop the quiet countdown for the whole utterance. Phantom re-arm
            # waits until speech_stopped (+ ASR lag) — never from speech_started
            # alone (2s used to re-arm mid-sentence and fire check-in 2).
            self._pause_silence_timer_only()
            self._silence_pending_arm = False
            self._silence_awaiting_transcript = True
            await super().on_speech_started()
            return

        await super().on_speech_started()

    async def on_speech_stopped(self):
        """Caller finished this utterance — only NOW may a phantom silence re-arm."""
        self._caller_speech_active = False
        if self._ctx.ended:
            return
        if not self._silence_awaiting_transcript:
            return
        # Transcript often lags speech_stopped by 0.5–3s; wait before treating
        # this as a false VAD blip.
        self._schedule_phantom_rearm()

    async def on_audio_delta(self, audio_bytes: bytes):
        """Agent TTS chunk — silence must never count while she is still speaking."""
        await super().on_audio_delta(audio_bytes)
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        last = float(getattr(self, "_last_audio_delta_at", 0.0) or 0.0)
        # Still generating/streaming audio for a pending post-playback arm.
        if self._silence_pending_arm and last > self._silence_pending_peak:
            self._silence_pending_peak = last
        watching = (
            self._silence_anchor is not None
            or (self._silence_task is not None and not self._silence_task.done())
        )
        if watching:
            # Agent spoke during the quiet window — cancel the timer. The next
            # PlaybackFinished (after this speech) restarts the ladder from 5s.
            logger.info(
                "[Orchestrator] agent audio during quiet — cancel silence, wait playback"
            )
            self._cancel_silence_watch(reset=True)
            self._silence_pending_arm = True
            self._silence_pending_peak = last
            self._silence_pending_clear_fired = True
            await self._ask_client_playback_end()

    async def _ask_client_playback_end(self) -> None:
        """Ask the browser to report when buffered agent audio has finished playing."""
        try:
            await self.send_message(json.dumps({"Kind": "AwaitPlaybackEnd"}))
        except Exception:
            logger.debug(
                "[Orchestrator] AwaitPlaybackEnd send skipped", exc_info=True
            )

    async def on_playback_finished(self) -> None:
        """Client finished PLAYING agent audio — only then may the 5s quiet clock start."""
        await self._try_arm_silence_after_client_playback()

    async def _try_arm_silence_after_client_playback(self) -> None:
        if not self._silence_pending_arm:
            return
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            self._silence_pending_arm = False
            return
        # Caller mid-turn — keep pending until after their transcript / next reply.
        if self._caller_speech_active or self._silence_awaiting_transcript:
            logger.info(
                "[Orchestrator] silence pending held — caller speaking or awaiting ASR"
            )
            return
        peak = float(self._silence_pending_peak or 0.0)
        last = float(getattr(self, "_last_audio_delta_at", 0.0) or 0.0)
        drain = float(getattr(self, "_last_drain_at", 0.0) or 0.0)
        # More TTS arrived after generate-done / prior drain — wait for the next
        # PlaybackFinished once that audio has played.
        if last > peak + 0.02:
            self._silence_pending_peak = last
            logger.info(
                "[Orchestrator] silence pending — TTS still after prior peak; "
                "wait for next PlaybackFinished (not generate-done)"
            )
            await self._ask_client_playback_end()
            return
        if peak > 0 and drain < peak:
            return
        # Brief settle so a flicker-empty buffer doesn't arm mid-utterance.
        peak_snap = peak
        await asyncio.sleep(0.3)
        if not self._silence_pending_arm:
            return
        if self._caller_speech_active or self._silence_awaiting_transcript:
            return
        last2 = float(getattr(self, "_last_audio_delta_at", 0.0) or 0.0)
        if last2 > peak_snap + 0.02:
            self._silence_pending_peak = last2
            await self._ask_client_playback_end()
            return
        clear = bool(self._silence_pending_clear_fired)
        self._silence_pending_arm = False
        logger.info(
            "[Orchestrator] silence arm AFTER client playback finished "
            "(mark: never on generate-done)"
        )
        self._arm_silence_watch(clear_fired=clear)

    async def on_response_done(self, response) -> None:
        await super().on_response_done(response)
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        if self._silence_ignore_dones > 0:
            self._silence_ignore_dones -= 1
            logger.info("[Orchestrator] ignoring aborted silence response.done")
            return
        # GENERATE-DONE is NOT enough — do not start the 5s clock here.
        # Wait for the browser to finish *playing* the audio (PlaybackFinished).
        clear_fired = True
        if self._silence_reprompting:
            self._silence_reprompting = False
            self._silence_cue = None
            clear_fired = False  # keep ladder progress after check-in speech
        self._silence_arm_token += 1  # cancel any legacy settle loops
        self._silence_pending_arm = True
        self._silence_pending_peak = float(
            getattr(self, "_last_audio_delta_at", 0.0) or 0.0
        )
        self._silence_pending_clear_fired = clear_fired
        self._silence_settle_active = False
        logger.info(
            "[Orchestrator] generate-done — pending silence until PlaybackFinished "
            "(peak=%.3f clear_fired=%s)",
            self._silence_pending_peak,
            clear_fired,
        )
        await self._ask_client_playback_end()
        # If the client already drained (short utterance), try arming immediately.
        await self._try_arm_silence_after_client_playback()

    # --- session (behavior + tools per stage) ------------------------------

    def _terminal_close_instructions(self) -> str:
        """Stage skill + hard rules — no mood/reaction-first (those revive Q&A)."""
        text = compose(self._fsm.state, self._ctx.facts())
        if self._fsm.state == "DNC_CLOSE":
            if self._dnc_awaiting_feedback:
                rules = DNC_FEEDBACK_ASK_RULES
            elif self._dnc_awaiting_followup:
                rules = DNC_FEEDBACK_FOLLOWUP_RULES
            else:
                rules = DNC_CLOSE_RULES
            text = (
                f"{text}\n\n---\nTEMPORARY DNC CLOSE (this turn only):\n{rules}"
            )
            return text
        rules = {
            "DECLINE_CLOSE": DECLINE_CLOSE_RULES,
            "NO_RESPONSE_CLOSE": NO_RESPONSE_CLOSE_RULES,
        }.get(self._fsm.state)
        if rules:
            label = self._fsm.state.replace("_", " ")
            text = f"{text}\n\n---\nTEMPORARY {label} (this turn only):\n{rules}"
        return text

    def _finalize_dnc_intake_freeze(self, *, reason: str) -> None:
        """Freeze intake UI only after feedback phase ends (not at the opt-out gate)."""
        self._intake_frozen = True
        self._dnc_awaiting_feedback = False
        self._dnc_awaiting_followup = False
        freeze_at = time.strftime("%H:%M:%S")
        self._trust("freeze", frozen=True, at=freeze_at)
        self._trust_receipt_line(
            f"intake frozen after opt-out feedback · {reason}",
            ok=True,
            once_key="intake_frozen_done",
        )

    def _record_dnc_feedback(self, text: str) -> str:
        """Store opt-out feedback. Returns 'followup' | 'close' for the next beat."""
        raw = " ".join((text or "").split()).strip()
        lowered = raw.lower().rstrip(".!?")
        skipped = (not raw) or lowered in _DNC_FEEDBACK_SKIP
        if skipped:
            self._record_dnc_feedback_skip(reason="declined")
            return "close"
        quote = caller_quote(raw, max_len=96)
        execute_tool(
            "capture_borrower_field",
            {"field": "dnc_feedback", "value": raw, "confidence": 1.0},
            self._ctx,
            sink=self._sink,
        )
        self._trust_receipt_line(
            f'opt-out feedback · "{quote}"',
            ok=True,
            highlight=quote,
            once_key="dnc_feedback",
            event_label="do not call feedback",
        )
        # Stay open for one soft follow-up — freeze only after that (or skip).
        self._dnc_awaiting_feedback = False
        self._dnc_awaiting_followup = True
        self._mood = "hesitant"
        return "followup"

    def _record_dnc_followup(self, text: str) -> None:
        """Optional clarifying note after the main feedback; then freeze intake."""
        raw = " ".join((text or "").strip().split())
        lowered = raw.lower().rstrip(".!?")
        if raw and lowered not in _DNC_FEEDBACK_SKIP:
            quote = caller_quote(raw, max_len=96)
            execute_tool(
                "capture_borrower_field",
                {"field": "dnc_feedback_followup", "value": raw, "confidence": 1.0},
                self._ctx,
                sink=self._sink,
            )
            self._trust_receipt_line(
                f'opt-out feedback follow-up · "{quote}"',
                ok=True,
                highlight=quote,
                once_key="dnc_feedback_followup",
                event_label="do not call feedback",
            )
        self._finalize_dnc_intake_freeze(reason="feedback follow-up complete")

    def _record_dnc_feedback_skip(self, *, reason: str = "declined") -> None:
        self._trust_receipt_line(
            f"opt-out feedback skipped · {reason}",
            ok=True,
            once_key="dnc_feedback",
            event_label="do not call feedback",
        )
        self._finalize_dnc_intake_freeze(reason=f"feedback skipped ({reason})")

    def _session_config(self):
        session = super()._session_config()  # keeps transcription + tuning
        if self._silence_cue:
            # Brief silence check-in — works for Maya skills AND custom prompts.
            # No mood/reaction suite (would fight the one-line nudge).
            session.instructions = self._silence_checkin_instructions(self._silence_cue)
            session.tools = []
        elif self._fsm.state in _TERMINAL_CLOSE_STATES or self._fsm.state == "DNC_CLOSE":
            # Always use the close skill + hard rules — never the custom Settings
            # prompt or qualify history bias (was still asking buy/refi after silence).
            if self._fsm.state == "DNC_CLOSE":
                self._mood = "hesitant"
            session.instructions = self._terminal_close_instructions()
            session.tools = function_tools(tools_for(self._fsm.state))
            self._apply_mood_voice(session)
        elif self.system_prompt:
            # UI custom prompt: full wording override. Gates still run in code;
            # skills compose is skipped so the Settings text actually takes effect.
            # Mood + reaction-first still appended on conversational stages.
            text = self.system_prompt
            if self._fsm.state in _DELIVERY_STATES:
                text = f"{text}{self._delivery_suffix()}"
            session.instructions = text
            session.tools = function_tools(["end_call"])
            self._apply_mood_voice(session)
        else:
            text = (
                self._qualify_instructions()
                if self._fsm.state == "QUALIFY"
                else compose(self._fsm.state, self._ctx.facts())
            )
            if self._fsm.state in _DELIVERY_STATES:
                text = f"{text}{self._delivery_suffix()}"
            session.instructions = text
            session.tools = function_tools(tools_for(self._fsm.state))
            self._apply_mood_voice(session)
        # The orchestrator OWNS response creation: turn off the server's automatic
        # reply so each caller turn yields exactly one reply (ours) — no double and
        # no race between the server's auto-response and our create.create(). The
        # greeting and every turn's reply are created explicitly; barge-in is
        # unaffected (interrupt_response stays on).
        td = getattr(session, "turn_detection", None)
        if td is not None:
            td.create_response = False
        return session

    async def _update_session(self) -> None:
        if self._voicelive_connected and self.conn is not None:
            await self.conn.session.update(session=self._session_config())
        brief = briefing_for_state(self._fsm.state)
        self._trust_eng_log(
            f"session.update skills={brief.get('skills')} bytes={brief.get('bytes')}"
        )
        self._trust_snapshot(reason="session_update")

    async def _human_think_pause(self) -> None:
        """Short beat after the caller finishes — feels human (150–300ms)."""
        ms = resolve_agent_voice_lead_silence_ms()
        if ms <= 0:
            return
        await asyncio.sleep(ms / 1000.0)

    async def _create_response(
        self, *, cancel: bool = False, think_pause: bool | None = None
    ) -> None:
        # Call already ending (manual End Call / hard-close) — never speak again.
        if self._ctx.ended or getattr(self, "_finalizing", False):
            logger.info("[Orchestrator] skip response.create — call ending")
            return
        # Pause quiet accounting for any utterance Maya is about to speak.
        self._pause_silence_clock()
        if not (self._voicelive_connected and self.conn is not None):
            return
        if think_pause is None:
            # Default: pause after real caller turns; skip silence nudges.
            think_pause = not (self._silence_cue or self._silence_reprompting)
        if think_pause:
            await self._human_think_pause()
        # Re-check after think pause — manual End Call can land during the sleep.
        if self._ctx.ended or getattr(self, "_finalizing", False):
            logger.info("[Orchestrator] skip response.create after think-pause — call ending")
            return
        if cancel:
            await self._cancel_active_response_if_needed()
        if self._ctx.ended or getattr(self, "_finalizing", False):
            logger.info("[Orchestrator] skip response.create before Voice Live — call ending")
            return
        await self.conn.response.create()
        self._note_agent_turn_after_opt_out()
        # CALLBACK stays multi-turn: never hard-close just because a time is recorded
        # mid-negotiation — wait for disposition ``completed`` / end_call after the
        # confirmation goodbye has been spoken.
    # --- the gate, on every finalized caller turn --------------------------

    async def on_user_transcript_done(self, transcript: str):
        await super().on_user_transcript_done(transcript)  # UI transcript + existing behavior
        # Mirror any borrower fields the (model-agnostic) transcript extractor has
        # captured so far into the orchestrator's paper trail — independent of the
        # voice model calling capture_borrower_field.
        self._bridge_insights_to_ctx()
        text = (transcript or "").strip()
        if not text or self._ctx.ended:
            if self._silence_awaiting_transcript and self._silence_close_pending():
                self._silence_awaiting_transcript = False
                self._caller_speech_active = False
                self._cancel_phantom_rearm()
                self._arm_silence_watch(clear_fired=False)
            return

        if self._silence_close_pending() and not self._caller_turn_counts_during_silence_tail(
            text
        ):
            self._silence_awaiting_transcript = False
            self._caller_speech_active = False
            self._cancel_phantom_rearm()
            self._debug_silence_log(
                "silence_tail_junk_ignored",
                {"textHead": text[:48], "fired": sorted(self._silence_fired)},
                "H3_PHANTOM",
            )
            logger.info(
                "[Orchestrator] junk transcript during silence tail — re-arm close"
            )
            self._arm_silence_watch(clear_fired=False)
            return

        self._silence_awaiting_transcript = False
        self._caller_speech_active = False
        self._cancel_phantom_rearm()
        # Prior PlaybackFinished must not arm a quiet clock during this reply.
        self._silence_pending_arm = False

        # Mood from this turn — drives delivery cues + voice style/rate on session.update.
        self._last_user_text = text
        self._mood = detect_mood(text)
        if self._mood != "neutral":
            logger.info("[Orchestrator] mood=%s", self._mood)

        # Real caller turn — wipe silence progress; arm again after this reply settles.
        self._cancel_silence_watch()

        # The server no longer auto-responds (create_response is off for the
        # orchestrated session), so this handler creates exactly ONE reply per turn.
        decision = self._engine.handle_caller_turn(
            text, self._fsm, self._ctx, sink=self._sink
        )
        if decision is not None:
            # A gate fired — reply under the (possibly new) stage's skill + tools.
            logger.info(
                "[Orchestrator] gate=%s -> %s", decision.action, decision.state
            )
            if decision.action == "DNC_CLOSE":
                self._abort_pending_hard_close()
            self._notice_action(decision.action)
            await self._emit_gate_trust(decision.action, text)
            await self._update_session()
            await self._create_response(
                cancel=True,
                think_pause=decision.action not in _URGENT_NO_PAUSE,
            )
            # Single-turn goodbyes hang up after speech. DNC stays open for feedback.
            if self._fsm.state in _TERMINAL_CLOSE_STATES:
                self._schedule_hard_close()
            return

        # DNC multi-turn: feedback ask → optional soft follow-up → then freeze + close.
        if self._fsm.state == "DNC_CLOSE" and self._dnc_awaiting_feedback:
            logger.info("[Orchestrator] DNC feedback turn")
            next_beat = self._record_dnc_feedback(text)
            self._mood = "hesitant"
            await self._update_session()
            await self._create_response(cancel=True, think_pause=False)
            if next_beat == "close":
                self._schedule_hard_close()
            return

        if self._fsm.state == "DNC_CLOSE" and self._dnc_awaiting_followup:
            logger.info("[Orchestrator] DNC feedback follow-up turn")
            self._record_dnc_followup(text)
            self._mood = "hesitant"
            await self._update_session()
            await self._create_response(cancel=True, think_pause=False)
            self._schedule_hard_close()
            return

        # No gate. Greeting: semantic router (same plane as QUALIFY). Keyword refuse
        # stays as a fast TCPA floor; keyword affirm is offline fallback only.
        if self._fsm.state == "GREETING":
            if is_consent_refusal(text):
                logger.info("[Orchestrator] greeting consent refused — DECLINE_CLOSE")
                apply_action("DECLINE_CLOSE", self._fsm, self._ctx, sink=self._sink)
                execute_tool(
                    "log_disposition",
                    {"disposition": "no_tcpa_consent"},
                    self._ctx,
                    sink=self._sink,
                )
                self._notice_action("DECLINE_CLOSE")
                await self._emit_gate_trust("DECLINE_CLOSE", text)
                await self._update_session()
                await self._create_response(cancel=True, think_pause=False)
                self._schedule_hard_close()
                return
            if semantic_enabled():
                logger.info("[Orchestrator] greeting → semantic router")
                self._spawn_router()
                return
            # Semantic off / no endpoint: keyword affirm or stay on INTRO.
            if is_consent_affirm(text):
                self._fsm.transition("QUALIFY", reason="consent_affirmed")
                await self._update_session()
                await self._create_response(cancel=True)
                return
            logger.info("[Orchestrator] greeting turn ambiguous — staying in GREETING")
            await self._update_session()
            await self._create_response(cancel=True)
            return

        # Ordinary qualifying / mid-callback turn.
        if self._fsm.state == "QUALIFY":
            await self._update_session()
        if semantic_enabled() and self._fsm.state in ("QUALIFY", "CALLBACK_CLOSE"):
            self._spawn_router()
        else:
            await self._create_response()

    # --- tool calls from the model -----------------------------------------

    async def on_function_call(self, name, call_id, arguments):
        try:
            args = json.loads(arguments) if arguments else {}
        except (ValueError, TypeError):
            args = {}
        result = execute_tool(name, args, self._ctx, sink=self._sink)
        logger.info("[Orchestrator] tool %s -> %s", name, result)
        rid = (result or {}).get("record_id")
        if name == "add_to_do_not_call" and rid:
            self._trust_receipt_line(
                f"do not call record {rid} written",
                ok=True,
                record_id=str(rid),
                once_key=f"dnc:{rid}",
                event_label=receipt_event_label("DNC_CLOSE"),
            )
            self._trust_promise("we won't call again", str(rid))
        elif name == "schedule_callback" and rid:
            preferred = (args or {}).get("preferred_time") or "agreed window"
            self._trust_promise(f"callback · {preferred}", str(rid))
            self._trust_receipt_line(
                f"callback recorded · {preferred} {rid}",
                ok=True,
                record_id=str(rid),
                once_key=f"cb:{rid}",
                event_label=receipt_event_label("CALLBACK_CLOSE"),
                highlight=str(rid),
            )
            self._trust_receipt_line(
                "confirmation matches record",
                ok=True,
                once_key=f"cb_confirm:{rid}",
            )
        elif name == "log_disposition":
            disp = (result or {}).get("disposition") or args.get("disposition")
            if disp:
                self._trust_receipt_line(
                    f"disposition set: {disp}",
                    ok=True,
                    once_key=f"disp:{disp}",
                )
        elif name == "transfer_to_lo":
            self._trust_receipt_line(
                "transfer to loan officer recorded",
                ok=True,
                once_key="transfer_recorded",
                event_label=receipt_event_label("TRANSFER"),
            )

        if self._voicelive_connected and self.conn is not None and call_id:
            try:
                await self.conn.conversation.item.create(
                    item=FunctionCallOutputItem(call_id=call_id, output=json.dumps(result))
                )
            except Exception:
                logger.exception("[Orchestrator] failed to send function_call_output")

        target = _TOOL_STATE_CHANGE.get(name)
        if target and self._fsm.state not in ("ENDED", target):
            self._fsm.transition(target, reason=f"tool:{name}")
            await self._update_session()

        # Close the call only when the paper trail is done AND the dialogue is.
        # CALLBACK: schedule_callback records the time but does NOT end the call —
        # the agent still confirms, then log_disposition(completed) / end_call.
        close_now = False
        if self._ctx.ended:
            logger.info(
                "[Orchestrator] end_call accepted (disposition=%s)",
                self._ctx.disposition,
            )
            close_now = True
        elif (
            self._ctx.disposition in _END_DISPOSITIONS
            and self._fsm.state in _CLOSE_STATES
        ):
            logger.info(
                "[Orchestrator] terminal disposition '%s' in %s — scheduling close",
                self._ctx.disposition,
                self._fsm.state,
            )
            close_now = True
        elif name == "schedule_callback" and (result or {}).get("ok"):
            # Time on the record. Do not hard-close (still need confirm + completed)
            # and do not response.create (that was the double-goodbye). Next turn or
            # log_disposition(completed) finishes the call after speech settles.
            logger.info(
                "[Orchestrator] schedule_callback recorded — waiting for confirm + completed"
            )
            return

        if close_now:
            self._schedule_hard_close()
            return

        # Mid-call tools (e.g. capture_borrower_field) — let the model continue.
        await self._create_response(think_pause=False)


class OrchestratedWebHandler(OrchestratorMixin, WebMediaHandler):
    """WebMediaHandler with the orchestrator brain mixed in."""
