"""Web browser client handler with live transcript support."""

import json
import logging
import os

from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    RequestSession,
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

    async def _send_transcript(
        self, role: str, text: str, final: bool, *, replace: bool = False
    ) -> None:
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

    async def on_agent_event(self, payload: dict) -> None:
        """Forward agent metrics + event-timeline payloads to the browser."""
        await self.send_message(json.dumps(payload))

    async def on_message(self, msg):
        """Unwrap Quart ASGI websocket frames before forwarding audio."""
        if isinstance(msg, dict):
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is None and msg.get("text") is not None:
                data = msg["text"].encode("utf-8")
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
