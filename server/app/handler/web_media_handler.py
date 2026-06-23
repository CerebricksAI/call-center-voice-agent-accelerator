"""Web browser client handler with live transcript support."""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    RequestSession,
)

from app.conversation_extractor import llm_extract_insights, llm_generate_call_summary
from app.transcript_sanitize import sanitize_assistant_transcript
from app import call_store
from app.agent_persona import BROKERAGE_NAME

from .voicelive_media_handler import VoiceLiveMediaHandler, _coerce_pcm_bytes

logger = logging.getLogger(__name__)


class WebMediaHandler(VoiceLiveMediaHandler):
    """Voice Live handler for the browser web client.

    Enables input audio transcription and forwards live/final transcripts
    to the browser over the WebSocket. Telephony providers use other handlers.
    """

    def __init__(self, config):
        super().__init__(config)
        self._last_user_transcript = ""
        self._assistant_partial = ""
        self._user_partial = ""
        self._mirror_turns: list[dict] = []
        self._extract_seq = 0
        self._emitted_insights: dict[str, str] = {}
        self._last_published_seq = 0
        self._extract_publish_lock = asyncio.Lock()
        self._extract_run_lock = asyncio.Lock()
        self._extract_worker: asyncio.Task | None = None
        self._finalizing = False
        self._summary_sent = False
        self._summary_loading_sent = False
        self._voicelive_cleaned = False
        self._persist_done = False
        self._persisted_call_id: str | None = None
        self._call_saved_notified = False
        self._call_summary_text: str | None = None
        self._finalize_task: asyncio.Task | None = None

    def _session_config(self) -> RequestSession:
        session = super()._session_config()
        transcription_model = os.getenv(
            "INPUT_TRANSCRIPTION_MODEL", "whisper-1"
        )
        session.input_audio_transcription = AudioInputTranscriptionOptions(
            model=transcription_model,
            language="en-US",
        )
        logger.info(
            "Web client input transcription enabled: model=%s",
            transcription_model,
        )
        return session

    async def connect_voicelive(self):
        await self.notify_call_started()
        await super().connect_voicelive()

    def _turns_for_extract(self) -> list[dict]:
        if self._mirror_turns:
            return list(self._mirror_turns)
        return list(self._call_turns)

    def _record_final_turn(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if role == "assistant":
            text = sanitize_assistant_transcript(text)
        if not text:
            return
        last = self._mirror_turns[-1] if self._mirror_turns else None
        if last and last.get("role") == role and last.get("text") == text:
            return
        self._mirror_turns.append({"role": role, "text": text})

    async def _send_transcript(
        self, role: str, text: str, final: bool, *, replace: bool = False
    ) -> None:
        if final:
            self._record_final_turn(role, text)
            if role == "user" and (text or "").strip():
                self._schedule_insight_extraction()
        await self.send_message(
            json.dumps(
                {
                    "Kind": "Transcript",
                    "Role": role,
                    "Text": text,
                    "Final": final,
                    "Replace": replace,
                }
            )
        )

    async def on_speech_started(self):
        """Reset partial transcript buffers when the user starts speaking."""
        self._user_partial = ""
        self._assistant_partial = ""
        await super().on_speech_started()

    async def on_user_transcript_delta(self, transcript: str) -> None:
        self._user_partial += transcript
        await self._send_transcript(
            "user",
            transcript,
            final=False,
            replace=False,
        )

    async def on_user_transcript_done(self, transcript: str) -> None:
        transcript = transcript.strip()
        self._user_partial = ""
        if not transcript or transcript == self._last_user_transcript:
            return
        self._last_user_transcript = transcript
        await self._send_transcript("user", transcript, final=True)

    async def on_assistant_transcript_delta(self, transcript: str) -> None:
        self._assistant_partial += transcript
        display = sanitize_assistant_transcript(self._assistant_partial)
        await self._send_transcript(
            "assistant",
            display,
            final=False,
            replace=True,
        )

    async def on_transcript_done(self, transcript: str) -> None:
        transcript = sanitize_assistant_transcript(transcript)
        if self._call_turns and self._call_turns[-1].get("role") == "assistant":
            if transcript:
                self._call_turns[-1]["text"] = transcript
            else:
                self._call_turns.pop()
        self._assistant_partial = ""
        if not transcript:
            return
        await self._send_transcript("assistant", transcript, final=True)

    def _schedule_insight_extraction(self) -> None:
        """Queue LLM extraction — one session at a time, coalesces rapid turns."""
        if self._finalizing:
            return
        self._extract_seq += 1
        if self._extract_worker is None or self._extract_worker.done():
            self._extract_worker = asyncio.create_task(self._extract_worker_loop())

    async def _extract_worker_loop(self) -> None:
        """Serialize LLM passes so the voice session is not starved."""
        async with self._extract_run_lock:
            try:
                while not self._finalizing:
                    seq = self._extract_seq
                    turns = self._turns_for_extract()
                    if not turns:
                        return

                    await self._send_insights([], turn_seq=seq, loading=True, append=True)

                    try:
                        insights, usage = await llm_extract_insights(
                            turns, self._emitted_insights
                        )
                    except Exception:
                        logger.exception(
                            "[WebMediaHandler] LLM extract failed (seq=%s)", seq
                        )
                        await self._send_insights(
                            [],
                            turn_seq=seq,
                            loading=False,
                            error="Insight extraction failed",
                            append=True,
                        )
                        return

                    if usage:
                        cost = self._record_usage_cost(usage, text_only=True)
                        await self.on_agent_event(
                            {
                                "Kind": "AgentEvent",
                                "kind": "usage",
                                "source": "extract",
                                "extractSeq": seq,
                                "tokens": usage,
                                "cost": cost,
                                "callCostUsd": self._call_cost_usd,
                            }
                        )

                    async with self._extract_publish_lock:
                        if seq < self._last_published_seq:
                            return
                        self._last_published_seq = seq

                    await self._send_insights(
                        insights,
                        turn_seq=seq,
                        loading=False,
                        append=True,
                    )
                    if insights:
                        logger.info(
                            "[WebMediaHandler] LLM extract sent %d insight(s) (seq=%s)",
                            len(insights),
                            seq,
                        )

                    if self._extract_seq == seq:
                        return
            except asyncio.CancelledError:
                raise

    async def _send_insights(
        self,
        insights: list[dict],
        *,
        error: str | None = None,
        turn_seq: int | None = None,
        loading: bool = False,
        append: bool = True,
    ) -> None:
        payload = {
            "Kind": "ExtractedInsights",
            "loading": loading,
            "insights": insights,
            "error": error,
            "append": append,
        }
        if turn_seq is not None:
            payload["turnSeq"] = turn_seq
        await self.send_message(json.dumps(payload))

    async def notify_call_started(self) -> None:
        """Tell the browser which call id will be used for Cosmos persistence."""
        if not self.call_id:
            return
        await self.send_message(
            json.dumps({"Kind": "CallStarted", "callId": self.call_id})
        )

    async def _emit_call_saved(self, call_id: str) -> None:
        await self.send_message(
            json.dumps({"Kind": "CallSaved", "callId": call_id})
        )

    async def _emit_call_summary(
        self,
        *,
        loading: bool,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = {
            "Kind": "CallSummary",
            "loading": loading,
            "summary": summary,
            "error": error,
        }
        if self.call_id:
            payload["callId"] = self.call_id
        await self.send_message(json.dumps(payload))

    def _turns_for_summary(self) -> list[dict]:
        turns = self._turns_for_extract()
        cleaned: list[dict] = []
        for turn in turns:
            role = turn.get("role", "")
            text = (turn.get("text") or "").strip()
            if role == "assistant":
                text = sanitize_assistant_transcript(text)
            elif role == "user":
                text = text.strip()
            if text:
                cleaned.append({"role": role, "text": text})
        return cleaned

    async def _send_call_summary(self) -> None:
        """Analyze the full transcript and send a summary to the browser."""
        if self._summary_sent:
            return

        if not self._summary_loading_sent:
            try:
                await self._emit_call_summary(loading=True)
                self._summary_loading_sent = True
            except Exception:
                logger.exception(
                    "[WebMediaHandler] Failed to send call summary loading state"
                )

        self._summary_sent = True
        turns = self._turns_for_summary()

        if not turns:
            msg = "No conversation to summarize."
            self._call_summary_text = msg
            await self._emit_call_summary(loading=False, summary=msg)
            return

        try:
            summary, usage = await llm_generate_call_summary(
                turns,
                key_facts=self._emitted_insights or None,
            )
            if usage:
                cost = self._record_usage_cost(usage, text_only=True)
                await self.on_agent_event(
                    {
                        "Kind": "AgentEvent",
                        "kind": "usage",
                        "source": "summary",
                        "tokens": usage,
                        "cost": cost,
                        "callCostUsd": self._call_cost_usd,
                    }
                )
            if not (summary or "").strip():
                await self._emit_call_summary(
                    loading=False,
                    error="Call summary could not be generated",
                )
                return
            self._call_summary_text = summary
            await self._emit_call_summary(loading=False, summary=summary)
            logger.info("[WebMediaHandler] Call summary sent (%d chars)", len(summary))
        except Exception:
            logger.exception("[WebMediaHandler] Call summary failed")
            await self._emit_call_summary(
                loading=False,
                error="Call summary failed",
            )

    async def on_agent_event(self, payload: dict) -> None:
        """Forward agent metrics + event-timeline payloads to the browser."""
        await self.send_message(json.dumps(payload))

    async def _cancel_extract_worker(self) -> None:
        if self._extract_worker is None or self._extract_worker.done():
            self._extract_worker = None
            return
        self._extract_worker.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(self._extract_worker, return_exceptions=True),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            logger.info(
                "[WebMediaHandler] Extract worker still stopping; continuing finalize"
            )
        self._extract_worker = None

    async def _ensure_voicelive_cleanup(self, *, persist: bool = True) -> None:
        if self._voicelive_cleaned:
            return
        self._voicelive_cleaned = True
        await super().cleanup(persist=persist)

    def _turns_for_persist(self) -> list[dict]:
        if self._call_turns:
            return list(self._call_turns)
        return [
            {"role": t.get("role"), "text": t.get("text"), "atMs": t.get("atMs")}
            for t in self._mirror_turns
            if (t.get("text") or "").strip()
        ]

    async def _persist_call_record(self) -> str | None:
        """Write call document to Cosmos. Returns call id when saved."""
        if not call_store.is_enabled():
            return None

        transcript = self._turns_for_persist()
        if not (transcript or self._call_metrics or self._call_summary_text):
            logger.info(
                "[WebMediaHandler] Skipping Cosmos persist for call %s — no data yet",
                self.call_id or "?",
            )
            return None

        try:
            call_id = self.call_id or uuid.uuid4().hex
            ended = datetime.now(timezone.utc)
            started = self._call_started_at
            record = {
                "id": call_id,
                "callId": call_id,
                "channel": self.channel,
                "brokerage": BROKERAGE_NAME,
                "persona": "Maya — mortgage pre-qualification",
                "startedAt": started.isoformat() if started else None,
                "endedAt": ended.isoformat(),
                "durationSec": round((ended - started).total_seconds(), 1)
                if started
                else None,
                "turnCount": len(transcript),
                "transcript": transcript,
                "metrics": self._call_metrics,
                "events": self._call_events,
                "callSummary": self._call_summary_text,
                "keyDetails": [
                    {"key": k, "value": v}
                    for k, v in (self._emitted_insights or {}).items()
                ],
            }
            await call_store.save_call(record)
            return call_id
        except Exception:
            logger.exception("[WebMediaHandler] Error building call record for persistence")
            raise

    async def _persist_call_record_if_needed(self) -> str | None:
        if self._persist_done:
            return self._persisted_call_id
        try:
            self._persisted_call_id = await self._persist_call_record()
        finally:
            self._persist_done = True
        return self._persisted_call_id

    async def _await_persist_call_record(self) -> str | None:
        return await self._persist_call_record_if_needed()

    async def _try_notify_call_saved(self, saved_id: str | None) -> None:
        if not saved_id or self._call_saved_notified:
            return
        try:
            await self._emit_call_saved(saved_id)
            self._call_saved_notified = True
        except Exception:
            logger.exception(
                "[WebMediaHandler] Failed to notify client call %s was saved",
                saved_id,
            )

    async def _finalize_call(self) -> None:
        """End the Voice Live session and generate a call summary."""
        if self._finalizing and self._finalize_task is not asyncio.current_task():
            if self._finalize_task is not None:
                await asyncio.gather(self._finalize_task, return_exceptions=True)
            return
        self._finalizing = True
        try:
            cancel_task = asyncio.create_task(self._cancel_extract_worker())
            await asyncio.gather(
                self._send_call_summary(),
                self._ensure_voicelive_cleanup(persist=False),
                cancel_task,
                return_exceptions=True,
            )
            saved_id = await self._await_persist_call_record()
            await self._try_notify_call_saved(saved_id)
        except Exception:
            logger.exception("[WebMediaHandler] Finalize call failed")

    async def _handle_control_message(self, text: str) -> bool:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False
        if payload.get("Kind") != "EndCall":
            return False
        logger.info("[WebMediaHandler] EndCall received — closing session")
        self._finalizing = True
        if not self._summary_sent and not self._summary_loading_sent:
            try:
                await self._emit_call_summary(loading=True)
                self._summary_loading_sent = True
            except Exception:
                logger.exception(
                    "[WebMediaHandler] Failed to send call summary loading on EndCall"
                )
        if self._finalize_task is None or self._finalize_task.done():
            self._finalize_task = asyncio.create_task(self._finalize_call())
        return True

    async def on_message(self, msg):
        """Unwrap Quart ASGI websocket frames before forwarding audio."""
        if isinstance(msg, dict):
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            if text and await self._handle_control_message(text):
                return
            data = msg.get("bytes")
            if data is None:
                return
        else:
            data = msg
        pcm = _coerce_pcm_bytes(data)
        if pcm is None:
            return
        await self.handle_audio(pcm)

    async def on_conversation_item_created(self, item) -> None:
        """Extract user transcript from conversation items when ASR events attach it."""
        role = getattr(item, "role", None)
        if role != "user":
            return
        for part in getattr(item, "content", None) or []:
            transcript = getattr(part, "transcript", None)
            if transcript:
                await self.on_user_transcript_done(transcript)
                return
            text = getattr(part, "text", None)
            if text:
                await self.on_user_transcript_done(text)
                return

    async def cleanup(self):
        await self._cancel_extract_worker()
        if not self._finalizing:
            self._finalizing = True
        if self._finalize_task is not None and not self._finalize_task.done():
            await asyncio.gather(self._finalize_task, return_exceptions=True)
        elif not self._summary_sent:
            await self._send_call_summary()
        if not self._voicelive_cleaned:
            await self._ensure_voicelive_cleanup(persist=False)
        saved_id = await self._await_persist_call_record()
        await self._try_notify_call_saved(saved_id)
