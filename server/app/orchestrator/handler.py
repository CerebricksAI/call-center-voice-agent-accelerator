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

    # --- end-of-call is the orchestrator's alone --------------------------

    def _schedule_auto_end_call(self) -> None:
        """Fully suppress the base handler's auto-end.

        WebMediaHandler ends the call on a fixed timer / transcript heuristic, which
        (a) bypasses the disposition gate and (b) fires CallEnded early — the client
        then stops audio playback and cuts off the agent's goodbye. The orchestrator
        ends the call itself via ``_schedule_hard_close`` once end_call is accepted,
        AFTER the goodbye has finished. So this base path is a no-op here.
        """
        logger.debug("[Orchestrator] base auto-end suppressed; orchestrator owns end")

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

    # Seconds to wait after the goodbye finishes generating, to let the client
    # finish PLAYING the buffered audio before the socket is closed.
    _POST_SPEECH_GRACE_S = 3.0

    async def _hard_close_after_grace(self) -> None:
        """End the call only after the agent's final speech is fully delivered.

        Timeline: end_call fires -> wait for the goodbye response to START, then to
        FINISH generating (the agent's speech is complete) -> wait a fixed buffer for
        the client to finish playing it -> finalize (summary + persist) -> close the
        socket. Nothing cuts the goodbye short.
        """
        try:
            # 1. Let the goodbye response start (it may be scheduled just before the
            #    response is created).
            for _ in range(20):  # up to ~2s
                if getattr(self, "_active_response", None) is not None:
                    break
                await asyncio.sleep(0.1)
            # 2. Wait for it to finish generating — agent's speech complete (capped).
            waited = 0.0
            while getattr(self, "_active_response", None) is not None and waited < 20.0:
                await asyncio.sleep(0.5)
                waited += 0.5
            # 3. Buffer for client-side audio playback to finish.
            await asyncio.sleep(self._POST_SPEECH_GRACE_S)
        except asyncio.CancelledError:
            raise
        # 4. Finalize (summary + persist), then close the socket authoritatively.
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

        if self._ctx.ended:
            logger.info("[Orchestrator] end_call accepted (disposition=%s)", self._ctx.disposition)
            # Orchestrator owns teardown: wait for the goodbye to finish, then close.
            self._schedule_hard_close()
        else:
            await self._create_response()


class OrchestratedWebHandler(OrchestratorMixin, WebMediaHandler):
    """WebMediaHandler with the orchestrator brain mixed in."""
