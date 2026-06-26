"""Base handler for Azure Voice Live API connections using the official SDK.

Provides the shared Voice Live connection, event processing, web client
audio handling with ambient mixing, and cleanup logic. Telephony subclasses
override on_message() and hook methods to implement protocol-specific behavior.
"""

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Union

import numpy as np
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import ManagedIdentityCredential
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    ServerEventType,
)

from .ambient_mixer import AmbientMixer
from app import call_store
from app.agent_persona import BROKERAGE_NAME
from app.voice_live_session import (
    build_request_session,
    log_session_options,
    resolve_session_options,
)
from app.usage_cost import (
    compute_transcribe_cost_usd,
    compute_tts_cost_usd,
    compute_usage_cost_usd,
    estimate_transcribe_usage_from_speech_ms,
    get_text_analysis_rates,
    normalize_transcription_usage,
    normalize_usage,
)

# Data type for WebSocket messages (str or bytes) sent to client
Data = Union[str, bytes]

logger = logging.getLogger(__name__)

# Default chunk size in bytes (100ms of audio at 24kHz, 16-bit mono)
DEFAULT_CHUNK_SIZE = 4800  # 24000 samples/sec * 0.1 sec * 2 bytes


def _coerce_pcm_bytes(data) -> bytes | None:
    """Normalize inbound client audio to raw bytes."""
    if data is None:
        return None
    if isinstance(data, memoryview):
        return bytes(data)
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return data.encode("latin-1")
    return None


class VoiceLiveMediaHandler:
    """Handles the connection to Azure Voice Live API and web clients.

    Uses the azure-ai-voicelive SDK for typed session config, event handling,
    and audio streaming. Provides web client audio handling (raw PCM + ambient
    mixing) by default. Telephony subclasses override on_message() and hooks
    for their specific protocols.
    """

    def __init__(self, config, voice_model=None, system_prompt=None):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        # Per-session model (validated UI selection) overrides the env default.
        # Note: only the VOICE model is per-session — extract/summary stay on their
        # own (gpt-4o-mini) resolution, independent of this choice.
        self.model = (voice_model or config["VOICE_LIVE_MODEL"] or "").strip()
        # Optional per-session system prompt from the UI. When set, it overrides
        # the default persona instructions for this call only; blank = default.
        self.system_prompt = (system_prompt or "").strip() or None
        self.api_key = config["AZURE_VOICE_LIVE_API_KEY"]
        self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.conn = None
        self._conn_ctx = None  # async context manager from SDK connect()
        self._credential = None  # kept alive for token refresh
        self._receiver_task = None
        self._voicelive_connected = False  # True while Voice Live WS is healthy

        # Client WebSocket
        self.client_ws = None

        # TTS output buffering for continuous ambient mixing
        self._tts_output_buffer = bytearray()
        self._tts_buffer_lock = asyncio.Lock()
        self._max_buffer_size = 480000  # 10 seconds of audio
        self._buffer_warning_logged = False
        self._tts_playback_started = False
        self._session_options = resolve_session_options()
        self._min_buffer_to_start = self._session_options.tts_playback_buffer_bytes

        # Ambient mixer initialization
        self._ambient_mixer: Optional[AmbientMixer] = None
        ambient_preset = config.get("AMBIENT_PRESET", "none")
        if ambient_preset and ambient_preset != "none":
            try:
                self._ambient_mixer = AmbientMixer(preset=ambient_preset)
            except Exception as e:
                logger.error(f"Failed to initialize AmbientMixer: {e}")

        # Agent metrics / event timeline (surfaced by the web client)
        self._metrics_t0 = None         # perf_counter origin for the session clock
        self._turn_index = 0            # increments on each user turn
        self._turn_ts = {}              # event name -> perf_counter for the active user turn
        # Response-side timing is tracked per response (keyed off response.created),
        # isolated from user turns so a stale audio chunk from a barged-over reply
        # can't be mis-attributed to the next turn's "first audio".
        self._active_response = None

        # Per-call persistence (one Cosmos document per call)
        self.call_id = None
        self.channel = "web"
        self._call_started_at = None
        self._call_turns = []     # [{role, text, atMs}]
        # Index in _call_turns where the in-flight user turn's transcript belongs.
        # Captured at speech start so a late transcript (native-realtime models reply
        # before their side-channel STT finishes) is inserted in conversational order
        # rather than appended after the agent's reply.
        self._user_turn_insert_index = None
        self._call_metrics = []   # [{turn, atMs, metrics, tokens, tokensPerSec}]
        self._call_events = []    # [{event, turn, atMs, ...}]
        self._metrics_seq = 0     # sequential response # for the UI table
        self._call_cost_usd = 0.0
        self._voice_cost_usd = 0.0
        self._transcribe_cost_usd = 0.0
        self._extract_cost_usd = 0.0
        self._summary_cost_usd = 0.0
        self._tts_cost_usd = 0.0

    def _session_config(self):
        """Return the typed session configuration for Voice Live."""
        return build_request_session(
            self._session_options, model=self.model, instructions=self.system_prompt
        )

    # ------------------------------------------------------------------
    # Voice Live connection
    # ------------------------------------------------------------------

    async def connect_voicelive(self):
        """Connect to Azure Voice Live API using the SDK."""
        t0 = time.perf_counter()
        self._call_started_at = datetime.now(timezone.utc)
        # Anchor the event timeline to call connect so elapsed times match durationSec.
        self._metrics_t0 = t0
        if not any(e.get("event") == "call_started" for e in self._call_events):
            self._call_events.append({"event": "call_started", "turn": None, "atMs": 0})

        if self.client_id:
            self._credential = ManagedIdentityCredential(client_id=self.client_id)
            credential = self._credential
        else:
            credential = AzureKeyCredential(self.api_key)

        t1 = time.perf_counter()
        logger.info("[VoiceLive] Credential prepared in %.2fs", t1 - t0)

        self._conn_ctx = voicelive_connect(
            endpoint=self.endpoint,
            credential=credential,
            model=self.model.strip(),
        )
        self.conn = await self._conn_ctx.__aenter__()

        t2 = time.perf_counter()
        logger.info("[VoiceLive] SDK connected in %.2fs (total %.2fs)", t2 - t1, t2 - t0)
        self._voicelive_connected = True
        log_session_options(self._session_options, model=self.model)

        await self.conn.session.update(session=self._session_config())
        await self.conn.response.create()

        self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def send_audio(self, audio_b64: str):
        """Send PCM 24kHz 16-bit mono audio (base64) to Voice Live."""
        if not self._voicelive_connected:
            return
        await self.conn.input_audio_buffer.append(audio=audio_b64)

    def _is_native_realtime_model(self) -> bool:
        return "realtime" in (self.model or "").strip().lower()

    async def _cancel_active_response_if_needed(self) -> None:
        """Cancel a stale in-flight response when the caller starts speaking again."""
        if self._active_response is None or not self._voicelive_connected:
            return
        if not self._is_native_realtime_model():
            return
        rid = self._active_response.get("id")
        try:
            if rid:
                await self.conn.response.cancel(response_id=rid)
            else:
                await self.conn.response.cancel()
            logger.debug(
                "[VoiceLive] Cancelled in-flight response (id=%s) on new speech",
                rid,
            )
        except Exception as exc:
            logger.debug("[VoiceLive] Response cancel skipped: %s", exc)
        self._active_response = None

    async def _receiver_loop(self):
        """Receives typed events from Voice Live and dispatches to hook methods."""
        cancelled = False
        try:
            async for event in self.conn:
                event_type = event.type

                match event_type:
                    case ServerEventType.SESSION_CREATED:
                        session_id = event.session.id if hasattr(event, "session") else None
                        logger.info("[VoiceLive] Session ID: %s", session_id)
                        await self._mark("session_created")

                    case ServerEventType.SESSION_UPDATED:
                        session = getattr(event, "session", None)
                        transcription = (
                            getattr(session, "input_audio_transcription", None)
                            if session
                            else None
                        )
                        logger.info(
                            "[VoiceLive] Session updated (input_transcription=%s)",
                            transcription,
                        )

                    case ServerEventType.CONVERSATION_ITEM_CREATED:
                        item = getattr(event, "item", None)
                        if item:
                            await self.on_conversation_item_created(item)

                    case ServerEventType.INPUT_AUDIO_BUFFER_CLEARED:
                        logger.debug("[VoiceLive] Input audio buffer cleared")

                    case ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        logger.info(
                            "[VoiceLive] Speech started at %s ms",
                            event.audio_start_ms,
                        )
                        await self._cancel_active_response_if_needed()
                        # New user turn — reset per-turn timing state.
                        self._turn_index += 1
                        self._turn_ts = {}
                        # Reserve this turn's slot in the transcript now, before the
                        # agent's reply gets appended (see _user_turn_insert_index).
                        self._user_turn_insert_index = len(self._call_turns)
                        await self._mark(
                            "speech_started", audioStartMs=event.audio_start_ms
                        )
                        await self.on_speech_started()

                    case ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                        logger.info("[VoiceLive] Speech stopped")
                        await self._mark("speech_stopped")

                    case ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA:
                        delta = getattr(event, "delta", None)
                        if delta:
                            if "stt_first" not in self._turn_ts:
                                await self._mark("stt_first")
                            await self.on_user_transcript_delta(delta)

                    case ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                        transcript = event.transcript
                        logger.info("[VoiceLive] User transcript: %s", transcript)
                        now = time.perf_counter()
                        await self._mark(
                            "stt_done",
                            ts=now,
                            sttMs=self._delta_ms(self._turn_ts.get("speech_stopped"), now),
                        )
                        await self._record_transcribe_usage(event, transcript)
                        if transcript:
                            stopped = self._turn_ts.get("speech_stopped")
                            turn = {
                                "role": "user",
                                "text": transcript,
                                # When the caller actually spoke, not when STT finished.
                                "atMs": self._clock_ms(stopped if stopped is not None else None),
                            }
                            idx = self._user_turn_insert_index
                            if idx is not None and 0 <= idx <= len(self._call_turns):
                                self._call_turns.insert(idx, turn)
                            else:
                                self._call_turns.append(turn)
                            self._user_turn_insert_index = None
                            await self.on_user_transcript_done(transcript)

                    case ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                        error = event.error if hasattr(event, "error") else "unknown"
                        logger.warning(
                            "[VoiceLive] User transcription failed: %s", error
                        )
                        await self._mark("stt_failed", message=str(error))

                    case ServerEventType.RESPONSE_CREATED:
                        created = await self._mark("response_created")
                        # Start an isolated timing record for THIS response and
                        # snapshot the user-turn timing that triggered it.
                        self._active_response = {
                            "id": getattr(getattr(event, "response", None), "id", None),
                            "turn": self._turn_index,
                            "created": created,
                            "first_audio": None,
                            "audio_done": None,
                            "done": None,
                            "user": dict(self._turn_ts),
                        }

                    case ServerEventType.RESPONSE_AUDIO_DELTA:
                        delta = event.delta
                        if delta:
                            ar = self._active_response
                            rid = getattr(event, "response_id", None)
                            if (
                                ar is not None
                                and ar["first_audio"] is None
                                and (rid is None or rid == ar["id"])
                            ):
                                now = time.perf_counter()
                                ar["first_audio"] = now
                                await self._mark(
                                    "first_audio",
                                    ts=now,
                                    ttfaMs=self._delta_ms(ar.get("created"), now),
                                )
                            await self.on_audio_delta(delta)

                    case ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
                        delta = getattr(event, "delta", None)
                        if delta:
                            await self.on_assistant_transcript_delta(delta)

                    case ServerEventType.RESPONSE_AUDIO_DONE:
                        now = await self._mark("audio_done")
                        if self._active_response is not None:
                            self._active_response["audio_done"] = now

                    case ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                        transcript = event.transcript
                        logger.debug("[VoiceLive] AI: %s", transcript)
                        if transcript:
                            self._call_turns.append(
                                {"role": "assistant", "text": transcript, "atMs": self._clock_ms()}
                            )
                            # Azure TTS (the agent voice) is billed per synthesized
                            # character, separate from the model tokens — both models
                            # render output through the Azure voice.
                            tts_cost = self._record_tts_cost(transcript)
                            if self._active_response is not None and tts_cost:
                                self._active_response["tts"] = tts_cost
                            await self.on_transcript_done(transcript)

                    case ServerEventType.RESPONSE_TEXT_DELTA:
                        delta = getattr(event, "delta", None)
                        if delta:
                            await self.on_response_text_delta(delta)

                    case ServerEventType.RESPONSE_TEXT_DONE:
                        text = getattr(event, "text", None)
                        await self.on_response_text_done(text)

                    case ServerEventType.RESPONSE_DONE:
                        response = getattr(event, "response", None)
                        response_id = getattr(response, "id", None)
                        logger.info("[VoiceLive] Response done: id=%s", response_id)
                        now = await self._mark("response_done")
                        ar = self._active_response
                        skip_metrics = await self.should_skip_response_metrics(response)
                        if (
                            not skip_metrics
                            and ar is not None
                            and ar.get("created") is not None
                        ):
                            ar["done"] = now
                            await self._emit_turn_metrics(ar, self._extract_usage(response))
                        await self.on_response_done(response)
                        self._active_response = None

                    case ServerEventType.ERROR:
                        logger.error("[VoiceLive] Error: %s", event.error)
                        err = getattr(event, "error", None)
                        await self._mark(
                            "error", message=str(getattr(err, "message", err))
                        )

                    case _:
                        logger.debug("[VoiceLive] Event: %s", event_type)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("[VoiceLive] Receiver loop error")
        finally:
            self._voicelive_connected = False
            if not cancelled:
                try:
                    await self.on_voicelive_disconnected(cancelled=False)
                except Exception:
                    logger.exception("[VoiceLive] on_voicelive_disconnected hook failed")
            # Keep the client WebSocket open while finalize/summary runs (web client).
            if not cancelled and self.client_ws and not getattr(self, "_finalizing", False):
                try:
                    logger.warning("[VoiceLive] Voice Live disconnected — closing client WebSocket")
                    await self.client_ws.close(1001)  # Going Away
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Client WebSocket
    # ------------------------------------------------------------------

    async def init_websocket(self, socket):
        """Sets up the client WebSocket."""
        self.client_ws = socket

    def set_call_context(self, call_id, channel="web"):
        """Record identifying context used when persisting the call record."""
        self.call_id = call_id
        self.channel = channel

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        try:
            await self.client_ws.send(message)
        except Exception:
            logger.exception("[VoiceLive] Failed to send message to client")

    # ------------------------------------------------------------------
    # Hooks — web client implementations (override in telephony subclasses)
    # ------------------------------------------------------------------

    async def on_voicelive_disconnected(self, *, cancelled: bool = False) -> None:
        """Hook: Voice Live session ended (override in web handler to auto-finalize)."""

    async def on_speech_started(self):
        """Barge-in: send StopAudio to client and clear TTS buffer."""
        stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))

        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def on_audio_delta(self, audio_bytes: bytes):
        """Handle audio from Voice Live — buffer for ambient or send directly."""
        if self._ambient_mixer is not None and self._ambient_mixer.is_enabled():
            async with self._tts_buffer_lock:
                self._tts_output_buffer.extend(audio_bytes)
                if len(self._tts_output_buffer) > self._max_buffer_size:
                    if not self._buffer_warning_logged:
                        logger.warning(
                            f"TTS buffer large: {len(self._tts_output_buffer)} bytes. "
                            "Speech may be delayed but will not be cut."
                        )
                        self._buffer_warning_logged = True
                elif self._buffer_warning_logged and len(self._tts_output_buffer) < self._max_buffer_size // 2:
                    self._buffer_warning_logged = False
        else:
            await self._send_audio_to_client(audio_bytes)

    async def on_user_transcript_delta(self, transcript: str):
        """Hook: partial user speech transcript (web client only)."""

    async def on_user_transcript_done(self, transcript: str):
        """Hook: final user speech transcript (web client only)."""

    async def on_assistant_transcript_delta(self, transcript: str):
        """Hook: partial assistant speech transcript (web client only)."""

    async def on_conversation_item_created(self, item):
        """Hook: conversation item created (web client may extract user transcript)."""

    async def on_transcript_done(self, transcript: str):
        """Hook: final assistant speech transcript (web client only)."""

    async def on_response_done(self, response) -> None:
        """Hook: Voice Live response finished (web client may capture extract output)."""

    async def on_response_text_delta(self, delta: str):
        """Hook: text-only response delta (e.g. live key-detail extraction)."""

    async def on_response_text_done(self, text: str | None):
        """Hook: text-only response complete."""

    async def should_skip_response_metrics(self, response) -> bool:
        """Hook: skip per-turn metrics for non-conversation responses (e.g. extraction)."""
        return False

    async def on_agent_event(self, payload: dict):
        """Hook: agent metrics + event-timeline payload (web client forwards to UI)."""

    # ------------------------------------------------------------------
    # Agent metrics + event timeline
    # ------------------------------------------------------------------

    def _clock_ms(self, t=None):
        """Milliseconds since call connect (None until the session clock is set)."""
        if self._metrics_t0 is None:
            return None
        end = t if t is not None else time.perf_counter()
        return round((end - self._metrics_t0) * 1000)

    def _call_count_fields(self, transcript: list) -> dict:
        """Explicit counts so UI can distinguish messages vs user speech rounds."""
        messages = len(transcript)
        return {
            "turnCount": messages,
            "messageCount": messages,
            "userTurnCount": self._turn_index,
            "agentResponseCount": len(self._call_metrics),
        }

    def _events_for_persist(self, started, ended) -> list:
        """Return timeline events bookended with call_started / call_ended at durationSec."""
        events = [e for e in self._call_events if e.get("event") != "call_ended"]
        if not any(e.get("event") == "call_started" for e in events):
            events.insert(0, {"event": "call_started", "turn": None, "atMs": 0})
        if started and ended:
            duration_ms = round((ended - started).total_seconds() * 1000)
            events.append({"event": "call_ended", "turn": None, "atMs": duration_ms})
        return events

    async def _record_session_event(self, name, *, ts=None, emit=False, **info):
        """Record a call-level timeline marker (not tied to a user turn)."""
        now = ts if ts is not None else time.perf_counter()
        if self._metrics_t0 is None:
            self._metrics_t0 = now
        at_ms = self._clock_ms(now)
        self._call_events.append({"event": name, "turn": None, "atMs": at_ms, **info})
        if emit:
            payload = {
                "Kind": "AgentEvent",
                "kind": "event",
                "event": name,
                "turn": None,
                "atMs": at_ms,
            }
            payload.update(info)
            await self.on_agent_event(payload)
        return now

    @staticmethod
    def _delta_ms(t_start, t_end):
        """Milliseconds between two perf_counter readings.

        Returns None if either reading is missing or the interval is negative —
        a negative interval only arises from cross-turn / barge-in artifacts, so
        it is surfaced as "—" rather than a bogus number.
        """
        if t_start is None or t_end is None:
            return None
        ms = round((t_end - t_start) * 1000)
        return ms if ms >= 0 else None

    async def _mark(self, name, *, ts=None, emit=True, **info):
        """Record a per-turn timestamp and emit a timeline event to the metrics hook."""
        now = ts if ts is not None else time.perf_counter()
        if self._metrics_t0 is None:
            self._metrics_t0 = now
        self._turn_ts[name] = now
        self._call_events.append(
            {"event": name, "turn": self._turn_index, "atMs": self._clock_ms(now), **info}
        )
        if emit:
            payload = {
                "Kind": "AgentEvent",
                "kind": "event",
                "event": name,
                "turn": self._turn_index,
                "atMs": self._clock_ms(now),
            }
            payload.update(info)
            await self.on_agent_event(payload)
        return now

    @staticmethod
    def _extract_usage(response):
        """Pull token counts off a RESPONSE_DONE response.usage (None if absent)."""
        return normalize_usage(response)

    def _record_usage_cost(
        self,
        usage,
        *,
        text_only: bool = False,
        category: str = "voice",
        model: str | None = None,
    ) -> dict | None:
        # Voice turns: per-session model rates. Extract/summary are standalone
        # gpt-4o-mini TEXT completions billed at the standard $0.15/$0.60 rate.
        if category in ("extract", "summary"):
            cost = compute_usage_cost_usd(
                usage, text_only=True, rates=get_text_analysis_rates()
            )
        else:
            cost = compute_usage_cost_usd(usage, text_only=text_only, model=model)
        if cost:
            usd = cost["usd"]
            self._call_cost_usd = round(self._call_cost_usd + usd, 6)
            if category == "extract":
                self._extract_cost_usd = round(self._extract_cost_usd + usd, 6)
            elif category == "summary":
                self._summary_cost_usd = round(self._summary_cost_usd + usd, 6)
            else:
                self._voice_cost_usd = round(self._voice_cost_usd + usd, 6)
        return cost

    def _record_tts_cost(self, text: str | None) -> dict | None:
        """Bill Azure TTS for the agent's spoken characters (separate per-char charge)."""
        cost = compute_tts_cost_usd(len(text or ""))
        if cost:
            usd = cost["usd"]
            self._call_cost_usd = round(self._call_cost_usd + usd, 6)
            self._tts_cost_usd = round(self._tts_cost_usd + usd, 6)
        return cost

    async def _record_transcribe_usage(self, event, transcript: str | None) -> None:
        usage = normalize_transcription_usage(event)
        if not usage:
            speech_ms = self._delta_ms(
                self._turn_ts.get("speech_started"),
                self._turn_ts.get("speech_stopped"),
            )
            if speech_ms and speech_ms > 0:
                usage = estimate_transcribe_usage_from_speech_ms(
                    speech_ms, transcript=transcript
                )
        if not usage:
            return
        cost = compute_transcribe_cost_usd(usage)
        if not cost:
            return
        usd = cost["usd"]
        self._call_cost_usd = round(self._call_cost_usd + usd, 6)
        self._transcribe_cost_usd = round(self._transcribe_cost_usd + usd, 6)
        await self.on_agent_event(
            {
                "Kind": "AgentEvent",
                "kind": "usage",
                "source": "transcribe",
                "cost": cost,
                "tokens": usage,
                "callCostUsd": self._call_cost_usd,
                "costBreakdown": self._cost_breakdown(),
            }
        )

    def _cost_breakdown(self) -> dict:
        total = round(self._call_cost_usd, 6)
        return {
            "voiceUsd": round(self._voice_cost_usd, 6),
            "transcribeUsd": round(self._transcribe_cost_usd, 6),
            "extractUsd": round(self._extract_cost_usd, 6),
            "summaryUsd": round(self._summary_cost_usd, 6),
            "ttsUsd": round(self._tts_cost_usd, 6),
            "totalUsd": total,
        }

    async def _emit_turn_metrics(self, ar, usage):
        """Compute one response's latencies + tokens and emit them.

        Timing comes from the response's own record (``ar``) plus the snapshot of
        the user turn that triggered it, so overlapping / barged-over responses
        never contaminate each other. Negative intervals are clamped to None.
        """
        u = ar["user"]
        speech_started = u.get("speech_started")
        speech_stopped = u.get("speech_stopped")
        created = ar.get("created")
        first_audio = ar.get("first_audio")
        audio_done = ar.get("audio_done")
        done = ar.get("done")
        stt_done = u.get("stt_done")
        metrics = {
            "userSpeechMs": self._delta_ms(speech_started, speech_stopped),
            "sttFirstMs": self._delta_ms(speech_stopped, u.get("stt_first")),
            "sttMs": self._delta_ms(speech_stopped, stt_done),
            # Model planning: caller stop → response.created (excludes side STT).
            "thinkMs": self._delta_ms(speech_stopped, created),
            "ttfaMs": self._delta_ms(created, first_audio),
            "firstAudioMs": self._delta_ms(speech_stopped, first_audio),
            "ttsStartMs": self._delta_ms(created, first_audio),
            "ttsMs": self._delta_ms(first_audio, audio_done),
            "responseMs": self._delta_ms(created, done),
            "turnMs": self._delta_ms(speech_started or created, done),
        }
        # Caller-centric totals (match analytics / Call History formulas).
        fa = metrics.get("firstAudioMs")
        tts = metrics.get("ttsMs")
        if fa is not None and tts is not None:
            metrics["e2eResponseMs"] = fa + tts
        else:
            e2e_stop = self._delta_ms(speech_stopped, audio_done or done)
            if e2e_stop is not None:
                metrics["e2eResponseMs"] = e2e_stop
        self._metrics_seq += 1
        payload = {
            "Kind": "AgentEvent",
            "kind": "metrics",
            "turn": ar.get("turn", self._turn_index),
            "seq": self._metrics_seq,
            "atMs": self._clock_ms(),
            "metrics": metrics,
        }
        if usage:
            payload["tokens"] = usage
            cost = self._record_usage_cost(usage, text_only=False, model=self.model)
            if cost is not None:
                payload["cost"] = cost
            payload["callCostUsd"] = self._call_cost_usd
            payload["costBreakdown"] = self._cost_breakdown()
            output_tokens = usage.get("output")
            response_ms = metrics.get("responseMs")
            if output_tokens and response_ms:
                payload["tokensPerSec"] = round(output_tokens / (response_ms / 1000), 3)
        # Per-turn Azure TTS (agent voice) cost, recorded when the spoken transcript
        # completed. Independent of the model's token usage.
        tts_cost = ar.get("tts")
        if tts_cost is not None:
            payload["ttsCost"] = tts_cost
            payload["costBreakdown"] = self._cost_breakdown()
        self._call_metrics.append(
            {
                "turn": payload["turn"],
                "seq": payload["seq"],
                "atMs": payload.get("atMs"),
                "metrics": payload.get("metrics"),
                "tokens": payload.get("tokens"),
                "tokensPerSec": payload.get("tokensPerSec"),
                "cost": payload.get("cost"),
                "ttsCost": payload.get("ttsCost"),
            }
        )
        await self.on_agent_event(payload)

    # ------------------------------------------------------------------
    # Audio output to client
    # ------------------------------------------------------------------

    async def _send_audio_to_client(self, audio_bytes: bytes):
        """Send audio bytes to the client. Override in subclasses for wrapping."""
        await self.send_message(audio_bytes)

    # ------------------------------------------------------------------
    # Inbound audio from client
    # ------------------------------------------------------------------

    def _receive_audio_from_client(self, data) -> tuple:
        """Convert client audio to PCM 24kHz. Override for format conversion.

        Returns (pcm_bytes | None, chunk_size). Return None for silent frames.
        """
        pcm = _coerce_pcm_bytes(data)
        if pcm is None:
            return None, DEFAULT_CHUNK_SIZE
        return pcm, len(pcm)

    async def on_message(self, msg):
        """Process one incoming WebSocket message. Override in subclasses for protocol handling."""
        pcm = _coerce_pcm_bytes(msg)
        if pcm is None:
            return
        await self.handle_audio(pcm)

    async def handle_audio(self, data):
        """Process inbound audio: convert, mix ambient, forward to Voice Live."""
        pcm_bytes = _coerce_pcm_bytes(data)
        if not pcm_bytes:
            return
        pcm_bytes, chunk_size = self._receive_audio_from_client(pcm_bytes)
        await self._send_continuous_audio(chunk_size)
        if pcm_bytes:
            audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
            await self.send_audio(audio_b64)

    # ------------------------------------------------------------------
    # Ambient mixing
    # ------------------------------------------------------------------

    async def _send_continuous_audio(self, chunk_size: int) -> None:
        """Send continuous audio (ambient + TTS if available) back to client."""
        if self._ambient_mixer is None or not self._ambient_mixer.is_enabled():
            return

        try:
            async with self._tts_buffer_lock:
                buffer_len = len(self._tts_output_buffer)
                ambient_bytes = self._ambient_mixer.get_ambient_only_chunk(chunk_size)

                should_play_tts = False
                if self._tts_playback_started:
                    if buffer_len >= chunk_size:
                        should_play_tts = True
                    elif buffer_len > 0:
                        should_play_tts = True
                    else:
                        self._tts_playback_started = False
                else:
                    if buffer_len >= self._min_buffer_to_start:
                        self._tts_playback_started = True
                        should_play_tts = True

                if should_play_tts and buffer_len >= chunk_size:
                    tts_chunk = bytes(self._tts_output_buffer[:chunk_size])
                    del self._tts_output_buffer[:chunk_size]

                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mixed = np.clip(ambient + tts, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()

                elif should_play_tts and buffer_len > 0:
                    tts_chunk = bytes(self._tts_output_buffer[:])
                    self._tts_output_buffer.clear()
                    self._tts_playback_started = False

                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts_samples = len(tts_chunk) // 2
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    ambient[:tts_samples] += tts
                    mixed = np.clip(ambient, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()

                else:
                    output_bytes = ambient_bytes

            await self._send_audio_to_client(output_bytes)

        except Exception:
            logger.exception("[VoiceLive] Error in _send_continuous_audio")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _persist_call_record(self) -> None:
        """Write one document for this call to Cosmos (no-op if disabled)."""
        if not call_store.is_enabled():
            return
        if not (self._call_turns or self._call_metrics):
            return  # nothing meaningful happened on this call
        try:
            call_id = self.call_id or uuid.uuid4().hex
            ended = datetime.now(timezone.utc)
            started = self._call_started_at
            transcript = list(self._call_turns)
            record = {
                "id": call_id,
                "callId": call_id,
                "channel": self.channel,
                "model": self.model,
                "customPrompt": self.system_prompt is not None,
                "brokerage": BROKERAGE_NAME,
                "persona": "Maya — mortgage pre-qualification",
                "startedAt": started.isoformat() if started else None,
                "endedAt": ended.isoformat(),
                "durationSec": round((ended - started).total_seconds(), 3) if started else None,
                "transcript": transcript,
                "metrics": self._call_metrics,
                "events": self._events_for_persist(started, ended),
                **self._call_count_fields(transcript),
            }
            await call_store.save_call(record)
        except Exception:
            logger.exception("[VoiceLive] Error building call record for persistence")

    async def cleanup(self, *, persist: bool = True):
        """Persist the call record, then cancel tasks and close the connection."""
        if persist:
            await self._persist_call_record()
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except (asyncio.CancelledError, Exception):
                pass
            self._receiver_task = None
        if self._conn_ctx:
            try:
                await self._conn_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._conn_ctx = None
            self.conn = None
        if self._credential:
            try:
                await self._credential.close()
            except Exception:
                pass
            self._credential = None
        logger.info("[VoiceLive] Cleaned up")
