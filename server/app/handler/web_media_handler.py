"""Web browser client handler with live transcript support."""

import asyncio
import json
import logging
import os

from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    RequestSession,
)

from app.conversation_extractor import (
    extract_conversation_insights,
    extract_new_insights,
)

from .voicelive_media_handler import VoiceLiveMediaHandler

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
        self._extract_tasks: set[asyncio.Task] = set()
        self._extract_seq = 0
        self._emitted_insights: dict[str, str] = {}
        self._last_published_seq = 0
        self._extract_publish_lock = asyncio.Lock()
        self._final_extract_done = False
        self._finalizing = False

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

    def _turns_for_extract(self) -> list[dict]:
        if self._mirror_turns:
            return list(self._mirror_turns)
        return list(self._call_turns)

    def _record_final_turn(self, role: str, text: str) -> None:
        text = (text or "").strip()
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
            if (text or "").strip():
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
            self._user_partial,
            final=False,
            replace=True,
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
        await self._send_transcript(
            "assistant",
            self._assistant_partial,
            final=False,
            replace=True,
        )

    async def on_transcript_done(self, transcript: str) -> None:
        self._assistant_partial = ""
        await self._send_transcript("assistant", transcript, final=True)

    def _schedule_insight_extraction(self) -> None:
        """Kick off a parallel extraction for this transcript turn (non-blocking)."""
        if self._finalizing or self._final_extract_done:
            return
        self._extract_seq += 1
        seq = self._extract_seq
        task = asyncio.create_task(self._extract_turn_parallel(seq))
        self._extract_tasks.add(task)
        task.add_done_callback(self._extract_tasks.discard)

    async def _extract_turn_parallel(self, seq: int) -> None:
        """Extract new one-liner key details from the latest caller turn(s)."""
        try:
            turns = self._turns_for_extract()
            if not turns:
                return
            insights = await asyncio.to_thread(
                extract_new_insights, turns, self._emitted_insights
            )
            if not insights:
                logger.debug(
                    "[WebMediaHandler] No new insights (seq=%s, turns=%d)",
                    seq,
                    len(turns),
                )
                return
            async with self._extract_publish_lock:
                if seq < self._last_published_seq:
                    return
                self._last_published_seq = seq
            logger.info(
                "[WebMediaHandler] Sending %d new insight(s) to client (seq=%s)",
                len(insights),
                seq,
            )
            await self._send_insights(insights, turn_seq=seq, append=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[WebMediaHandler] Parallel insight extraction failed (seq=%s)", seq
            )

    async def _run_final_extraction(self) -> None:
        """Full LLM extraction after the Voice Live call session closes."""
        if self._final_extract_done:
            return
        turns = self._turns_for_extract()
        if not turns:
            return
        self._final_extract_done = True
        try:
            await self._send_insights([], loading=True)
            model = (os.getenv("EXTRACT_MODEL") or self.model).strip()
            insights = await extract_conversation_insights(
                turns,
                endpoint=self.endpoint,
                api_key=self.api_key,
                model=model,
                llm=True,
                emitted_keys=self._emitted_insights,
            )
            await self._send_insights(insights, turn_seq=self._extract_seq, append=True)
            logger.info(
                "[WebMediaHandler] Final extraction sent %d insight(s)",
                len(insights),
            )
        except Exception:
            logger.exception("[WebMediaHandler] Final insight extraction failed")
            await self._send_insights(
                [],
                error="Could not extract key details. Please try another call.",
            )

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

    async def on_agent_event(self, payload: dict) -> None:
        """Forward agent metrics + event-timeline payloads to the browser."""
        await self.send_message(json.dumps(payload))

    async def _finalize_call(self) -> None:
        """Close Voice Live, then run full LLM extraction while the browser ws stays open."""
        if self._finalizing:
            return
        self._finalizing = True
        for task in list(self._extract_tasks):
            if not task.done():
                task.cancel()
        if self._extract_tasks:
            await asyncio.gather(*self._extract_tasks, return_exceptions=True)
        if not self._final_extract_done:
            await super().cleanup()
            await self._run_final_extraction()

    async def _handle_control_message(self, text: str) -> bool:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False
        if payload.get("Kind") != "EndCall":
            return False
        logger.info("[WebMediaHandler] EndCall received — finalizing call")
        asyncio.create_task(self._finalize_call())
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
            if data is None and text is not None:
                data = text.encode("utf-8")
            if data is None:
                return
        else:
            data = msg
        await self.handle_audio(data)

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
        for task in list(self._extract_tasks):
            if not task.done():
                task.cancel()
        if self._extract_tasks:
            await asyncio.gather(*self._extract_tasks, return_exceptions=True)

        if not self._finalizing and not self._final_extract_done:
            await super().cleanup()
            await self._run_final_extraction()
        elif not self._finalizing:
            await super().cleanup()
