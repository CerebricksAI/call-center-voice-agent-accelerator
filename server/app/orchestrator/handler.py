"""Live wiring — mix the orchestrator into the Voice Live web handler.

One brain, wired at three seams:
  * _session_config() — compose skills for the current FSM state + register that
    state's tools (reuses WebMediaHandler's transcription setup via super()).
  * on_user_transcript_done() — run the gate on every finalized caller turn; when
    it fires, swap the model's briefing and re-drive the response.
  * on_function_call() — execute the model's tool call, record it, reply with a
    FunctionCallOutputItem, and advance the FSM for state-changing tools.

Enabled only when ORCHESTRATOR_ENABLED is truthy (server.py picks the handler).
Compliance (opt-out etc.) is enforced in code here, never in a prompt.
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
from app.orchestrator.dialog import apply_action
from app.orchestrator.fsm import CallContext, CallStateMachine
from app.orchestrator.semantic import semantic_enabled
from app.orchestrator.tools import execute_tool, function_tools, tools_for
from skills.loader import compose

logger = logging.getLogger(__name__)


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
_TERMINAL_CLOSE_STATES = {"DECLINE_CLOSE", "DNC_CLOSE"}

# Dispositions that mean the call has reached its outcome. Recording one of these
# (e.g. callback 'completed', logged as the last step of the callback flow) ends the
# call in code even if the model never calls end_call — the hard-close still waits
# for the goodbye to finish first, so nothing is cut off. This closes the loop for
# CALLBACK_CLOSE, which can't close on entry.
_END_DISPOSITIONS = {"completed", "do_not_call", "declined"}
_CLOSE_STATES = {"DECLINE_CLOSE", "DNC_CLOSE", "CALLBACK_CLOSE"}


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
        self._hard_close_task = asyncio.create_task(self._hard_close_after_grace())

    # End-of-call timing. The call ends only after the agent's goodbye has FULLY
    # played on the client, then a short pause — never mid-sentence.
    _POST_SPEECH_GRACE_S = 2.0      # pause after speech finishes, before ending
    _PLAYBACK_START_CAP_S = 3.0     # max wait for the goodbye to start speaking
    _PLAYBACK_END_CAP_S = 25.0      # overall cap waiting for playback to finish
    _AUDIO_SETTLED_S = 0.4          # no new audio for this long => stream settled
    _SETTLED_FALLBACK_S = 3.0       # if the client never signals, close after this quiet

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
        reported its playback buffer emptied). Playback is done when the audio stream
        has SETTLED and the client drained AFTER the final chunk — which also handles
        a mid-goodbye buffer underrun (more audio arrives, so we keep waiting) and a
        silent model (no audio, returns promptly). If the client never sends the
        signal (e.g. an un-updated page), fall back to a quiet-audio window so the
        call still ends. Capped so a stuck client can never hang the call.
        """
        loop = asyncio.get_event_loop()
        # 1. Wait for the goodbye to start producing audio.
        start_deadline = loop.time() + self._PLAYBACK_START_CAP_S
        while loop.time() < start_deadline:
            last = getattr(self, "_last_audio_delta_at", 0.0)
            if last > 0.0 and (time.perf_counter() - last) < 1.0:
                break  # audio is actively flowing
            await asyncio.sleep(0.1)
        # 2. Wait for the stream to settle and the client's final drain.
        end_deadline = loop.time() + self._PLAYBACK_END_CAP_S
        while loop.time() < end_deadline:
            now = time.perf_counter()
            last_audio = getattr(self, "_last_audio_delta_at", 0.0)
            last_drain = getattr(self, "_last_drain_at", 0.0)
            quiet = now - last_audio
            if quiet >= self._AUDIO_SETTLED_S and (
                last_drain >= last_audio               # client played it all out
                or quiet >= self._SETTLED_FALLBACK_S    # no client signal — fall back
            ):
                return
            await asyncio.sleep(0.1)
        logger.info("[Orchestrator] playback-end wait capped; closing anyway")

    # --- semantic router (whole conversation; only when the keyword gate is silent) --

    def _spawn_router(self) -> None:
        task = getattr(self, "_router_task", None)
        if task is not None and not task.done():
            task.cancel()  # supersede any in-flight pass with the latest turn
        self._router_task = asyncio.create_task(self._route_conversation())

    async def _route_conversation(self) -> None:
        """Whole-conversation router: decide the intent, then create the SINGLE reply
        for this turn — the routed close/hand-off, or a normal qualifying reply.

        The reply is created HERE (not before), so a routing turn yields exactly one
        reply instead of a normal reply plus an override. We re-check state after the
        LLM call so a slow result can't act on a call that already moved on.
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
            logger.info("[Orchestrator] router -> %s (route)", action)
            apply_action(action, self._fsm, self._ctx, sink=self._sink)
            self._notice_action(action)
            await self._update_session()
            await self._create_response(cancel=True)
            if self._fsm.state in _TERMINAL_CLOSE_STATES:
                self._schedule_hard_close()
        else:
            # Not a hand-off after all — the single normal qualifying reply.
            await self._create_response()

    # --- session (behavior + tools per stage) ------------------------------

    def _session_config(self):
        session = super()._session_config()  # keeps transcription + tuning
        session.instructions = compose(self._fsm.state, self._ctx.facts())
        session.tools = function_tools(tools_for(self._fsm.state))
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

    async def _create_response(self, *, cancel: bool = False) -> None:
        if not (self._voicelive_connected and self.conn is not None):
            return
        if cancel:
            await self._cancel_active_response_if_needed()
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
            return

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
            await self._create_response(cancel=True)
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

        # Ordinary qualifying turn. Hand the whole conversation to the semantic router
        # (no keyword pre-filter — the model decides intent every turn). The router
        # creates the SINGLE reply for this turn: a routed close/hand-off, or a normal
        # qualifying reply. Creating the reply in the router (not before) is what keeps
        # it to one reply. Note: this adds ~1-2s per qualifying turn (the model call).
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
            await self._create_response()


class OrchestratedWebHandler(OrchestratorMixin, WebMediaHandler):
    """WebMediaHandler with the orchestrator brain mixed in."""
