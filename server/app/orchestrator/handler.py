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
from app.orchestrator.dialog import apply_action
from app.orchestrator.fsm import CallContext, CallStateMachine
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
    SILENCE_CHECKIN_RULES,
    SILENCE_WATCH_STATES,
    load_silence_policy,
    next_silence_step,
    quiet_elapsed,
)
from app.orchestrator.tools import execute_tool, function_tools, tools_for
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
_TERMINAL_CLOSE_STATES = {"DECLINE_CLOSE", "DNC_CLOSE", "NO_RESPONSE_CLOSE"}

# Mood + reaction-first cues apply on conversational stages (not hard closes).
_DELIVERY_STATES = frozenset(
    {"GREETING", "QUALIFY", "CALLBACK_CLOSE", "TRANSFER", "LANGUAGE_ROUTE"}
)

# Skip the human think-pause — TCPA opt-out must answer immediately.
_URGENT_NO_PAUSE = frozenset({"DNC_CLOSE"})

# Dispositions that mean the call has reached its outcome. Recording one of these
# (e.g. callback 'completed', logged as the last step of the callback flow) ends the
# call in code even if the model never calls end_call — the hard-close still waits
# for the goodbye to finish first, so nothing is cut off. This closes the loop for
# CALLBACK_CLOSE, which can't close on entry.
_END_DISPOSITIONS = {"completed", "do_not_call", "declined", "no_response"}
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
        self._mood: Mood = "neutral"
        self._last_user_text: str = ""
        logger.info("[Orchestrator] enabled — starting in %s", self._fsm.state)

    def set_call_context(self, call_id, channel="web"):
        super().set_call_context(call_id, channel)
        self._fsm.call_id = call_id
        self._ctx.call_id = call_id

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
            if preferred:
                execute_tool(
                    "schedule_callback",
                    {"preferred_time": preferred},
                    self._ctx,
                    sink=self._sink,
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
        self._hard_close_task = asyncio.create_task(self._hard_close_after_grace())

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
        _POST_SPEECH_GRACE_S -> finalize (summary + persist) -> close the socket.
        Nothing cuts the goodbye short.
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
        try:
            await self._wait_for_playback_end()
            await asyncio.sleep(self._POST_SPEECH_GRACE_S)
        except asyncio.CancelledError:
            raise
        # Finalize (summary + persist), then close the socket authoritatively.
        try:
            if not getattr(self, "_finalizing", False):
                await self.request_end_call(source="agent")
        except Exception:
            logger.debug("[Orchestrator] finalize on hard-close failed", exc_info=True)
        ws = getattr(self, "client_ws", None)
        if ws is None:
            return
        try:
            logger.info("[Orchestrator] hard-closing client WebSocket after goodbye")
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
        if action and self._fsm.state == "QUALIFY":
            logger.info("[Orchestrator] router -> %s (single reply)", action)
            apply_action(action, self._fsm, self._ctx, sink=self._sink)
            self._notice_action(action)
            await self._update_session()
            await self._create_response(
                cancel=True,
                think_pause=action not in _URGENT_NO_PAUSE,
            )
            if self._fsm.state in _TERMINAL_CLOSE_STATES:
                self._schedule_hard_close()
            return
        await self._create_response()

    # --- silence policy (T11): 8s / 16s quiet check-ins → no_response ~25s -----
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

    def _silence_close_pending(self) -> bool:
        """Both check-ins fired — next silence step is mandatory NO_RESPONSE close."""
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return False
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

    def _arm_silence_watch(self, *, clear_fired: bool = True) -> None:
        """Start a fresh quiet countdown after the agent finished speaking."""
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            self._cancel_silence_watch()
            return
        self._silence_anchor = time.monotonic()
        self._silence_paused_at = None
        self._silence_paused_total = 0.0
        self._silence_cue = None
        if clear_fired:
            self._silence_fired = set()
            self._ctx.silence_count = 0
        task = self._silence_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
        self._silence_task = asyncio.create_task(self._silence_watch_loop())

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
            self._silence_fired.add("close")
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
        self._fsm.transition("NO_RESPONSE_CLOSE", reason="silence_timeout")
        self._silence_task = None
        self._silence_anchor = None
        self._silence_paused_at = None
        self._silence_paused_total = 0.0
        self._silence_cue = None
        self._silence_reprompting = False
        self._silence_fired.add("close")
        # Lock pre-goodbye audio baseline BEFORE response.create so hard-close
        # does not treat the previous check-in as the goodbye — and so a slow
        # TTS start is not aborted by an early no-audio fallback.
        self._close_wait_since_audio = getattr(self, "_last_audio_delta_at", 0.0)
        self._hard_close_baseline_locked = True
        await self._update_session()
        await self._create_response(cancel=True, think_pause=False)
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
        aborting = self._silence_reprompting or self._silence_cue is not None
        if self._silence_close_pending():
            # Both check-ins done — do not wipe progress on a noise blip; wait for
            # a real finalized transcript (or re-arm close if junk).
            self._pause_silence_timer_only()
            self._silence_awaiting_transcript = True
            if aborting:
                logger.info("[Orchestrator] silence check-in aborted by caller speech")
            await super().on_speech_started()
            return
        self._cancel_silence_watch()
        if aborting:
            logger.info("[Orchestrator] silence check-in aborted by caller speech")
        await super().on_speech_started()

    async def _arm_silence_after_settle(self, *, clear_fired: bool, token: int) -> None:
        """Arm only after Maya's audio has settled — not while she is still talking."""
        settled = getattr(self, "_AUDIO_SETTLED_S", 0.4)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if token != self._silence_arm_token:
                return  # aborted (caller spoke / new turn)
            if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
                return
            last = getattr(self, "_last_audio_delta_at", 0.0)
            if last <= 0.0 or (time.perf_counter() - last) >= settled:
                break
            await asyncio.sleep(0.1)
        if token != self._silence_arm_token:
            return
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        self._arm_silence_watch(clear_fired=clear_fired)

    async def on_response_done(self, response) -> None:
        await super().on_response_done(response)
        if self._ctx.ended or self._fsm.state not in SILENCE_WATCH_STATES:
            return
        if self._silence_ignore_dones > 0:
            self._silence_ignore_dones -= 1
            logger.info("[Orchestrator] ignoring aborted silence response.done")
            return
        if self._silence_reprompting:
            # Check-in finished — fresh full gap until the NEXT step (keep fired stage).
            self._silence_reprompting = False
            self._silence_cue = None
            token = self._silence_arm_token
            asyncio.create_task(
                self._arm_silence_after_settle(clear_fired=False, token=token)
            )
            return
        # Normal qualify turn finished — brand-new silence episode.
        token = self._silence_arm_token
        asyncio.create_task(
            self._arm_silence_after_settle(clear_fired=True, token=token)
        )

    # --- session (behavior + tools per stage) ------------------------------

    def _session_config(self):
        session = super()._session_config()  # keeps transcription + tuning
        if self._silence_cue:
            # Brief silence check-in — works for Maya skills AND custom prompts.
            # No mood/reaction suite (would fight the one-line nudge).
            session.instructions = self._silence_checkin_instructions(self._silence_cue)
            session.tools = []
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
                self._arm_silence_watch(clear_fired=False)
            return

        if self._silence_close_pending() and not self._caller_turn_counts_during_silence_tail(
            text
        ):
            self._silence_awaiting_transcript = False
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

        # Mood from this turn — drives delivery cues + voice style/rate on session.update.
        self._last_user_text = text
        self._mood = detect_mood(text)
        if self._mood != "neutral":
            logger.info("[Orchestrator] mood=%s", self._mood)

        # Caller spoke — silence clock restarts after this turn's reply settles.
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
            self._notice_action(decision.action)
            await self._update_session()
            await self._create_response(
                cancel=True,
                think_pause=decision.action not in _URGENT_NO_PAUSE,
            )
            # For a single-turn goodbye, end the call after the goodbye plays even if
            # the model never calls end_call (disposition is already recorded).
            if self._fsm.state in _TERMINAL_CLOSE_STATES:
                self._schedule_hard_close()
            return

        # No gate. Advance greeting -> qualify on the caller's first real turn, then
        # reply under the QUALIFY skill (not a re-read of the greeting disclosure).
        if self._fsm.state == "GREETING":
            self._fsm.transition("QUALIFY", reason="consent_or_first_turn")
            await self._update_session()
            await self._create_response(cancel=True)
            return

        # Ordinary qualifying turn. Push OPEN THREAD (last ask) before the reply so
        # "I'm ready / still here" resumes the agenda instead of soft-stalling.
        if self._fsm.state == "QUALIFY":
            await self._update_session()
        if semantic_enabled() and self._fsm.state == "QUALIFY":
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

        # A terminal outcome was recorded (e.g. callback 'completed') but the model
        # may not call end_call. End in code — the hard-close waits for the goodbye
        # to finish playing, so nothing is cut off. Idempotent with the entry-time
        # schedule for decline/DNC.
        if (
            not self._ctx.ended
            and self._ctx.disposition in _END_DISPOSITIONS
            and self._fsm.state in _CLOSE_STATES
        ):
            logger.info(
                "[Orchestrator] terminal disposition '%s' in %s — scheduling close",
                self._ctx.disposition,
                self._fsm.state,
            )
            self._schedule_hard_close()

        if self._ctx.ended:
            logger.info("[Orchestrator] end_call accepted (disposition=%s)", self._ctx.disposition)
            # Orchestrator owns teardown: wait for the goodbye to finish, then close.
            self._schedule_hard_close()
        else:
            # Tool follow-up — already mid-turn; no post-caller think pause.
            await self._create_response(think_pause=False)


class OrchestratedWebHandler(OrchestratorMixin, WebMediaHandler):
    """WebMediaHandler with the orchestrator brain mixed in."""
