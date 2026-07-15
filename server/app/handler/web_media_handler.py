"""Web browser client handler with live transcript support."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    RequestSession,
)

from app.conversation_extractor import (
    insight_detail_rows,
    llm_extract_insights,
    llm_generate_call_summary,
    resolve_extract_model,
    resolve_summary_model,
)
from app.transcript_sanitize import (
    sanitize_assistant_transcript,
    transcript_has_farewell,
    transcript_requests_end_call,
)
from app import call_store
from app.agent_persona import BROKERAGE_NAME

from .voicelive_media_handler import VoiceLiveMediaHandler, _coerce_pcm_bytes

logger = logging.getLogger(__name__)


class WebMediaHandler(VoiceLiveMediaHandler):
    """Voice Live handler for the browser web client.

    Enables input audio transcription and forwards live/final transcripts
    to the browser over the WebSocket. Telephony providers use other handlers.
    """

    def __init__(self, config, voice_model=None, system_prompt=None):
        super().__init__(config, voice_model=voice_model, system_prompt=system_prompt)
        self._last_user_transcript = ""
        self._assistant_partial = ""
        self._user_partial = ""
        self._mirror_turns: list[dict] = []
        # perf_counter of the browser's last "playback drained" report (see
        # _handle_control_message). Same clock as _last_audio_delta_at so the
        # orchestrator can tell the goodbye finished playing before ending a call.
        self._last_drain_at = 0.0
        self._extract_seq = 0
        self._emitted_insights: dict[str, dict] = {}
        self._last_published_seq = 0
        self._extract_publish_lock = asyncio.Lock()
        self._extract_run_lock = asyncio.Lock()
        self._extract_worker: asyncio.Task | None = None
        self._final_extract_done = False
        self._finalizing = False
        self._summary_sent = False
        self._summary_loading_sent = False
        self._voicelive_cleaned = False
        self._persist_done = False
        self._persisted_call_id: str | None = None
        self._call_saved_notified = False
        self._call_summary_text: str | None = None
        self._summary_task: asyncio.Task | None = None
        self._finalize_task: asyncio.Task | None = None
        self._auto_end_task: asyncio.Task | None = None
        self._awaiting_close_ack = False
        self._awaiting_agent_goodbye = False
        self._closing_phase = False

    def _recent_assistant_text(self, n: int = 3) -> str:
        texts: list[str] = []
        turns = self._mirror_turns or self._call_turns
        for turn in reversed(turns):
            if turn.get("role") == "assistant":
                chunk = (turn.get("text") or "").strip()
                if chunk:
                    texts.append(chunk)
                if len(texts) >= n:
                    break
        return " ".join(reversed(texts))

    def _assistant_end_detect_context(self, current: str = "") -> str:
        recent = self._recent_assistant_text(3)
        current = (current or "").strip()
        if not current:
            return recent
        if recent and current in recent:
            return recent
        if recent:
            return f"{recent} {current}"
        return current

    def _last_assistant_text(self) -> str:
        for turn in reversed(self._mirror_turns or self._call_turns):
            if turn.get("role") == "assistant":
                return (turn.get("text") or "").strip()
        return ""

    def _user_declined_more(self, transcript: str) -> bool:
        text = (transcript or "").strip().lower()
        if not text:
            return False
        patterns = (
            r"^no(?:[,.!\s]|$)",
            r"^nothing(?:[,.!\s]|$)",
            r"^nope(?:[,.!\s]|$)",
            r"^that(?:'s| is) all",
            r"^we(?:'re| are) good",
            r"^thank you\.?$",
            r"^thanks\.?$",
            r"^no,?\s+we can",
        )
        return any(re.match(p, text) for p in patterns)

    def _summary_prefetch_enabled(self) -> bool:
        raw = os.getenv("SUMMARY_PREFETCH_ON_CLOSE", "true").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def _start_call_summary_task(self) -> None:
        """Start summary generation in the background (safe to call multiple times)."""
        if self._summary_task is not None and not self._summary_task.done():
            return
        if self._summary_sent:
            return
        self._summary_task = asyncio.create_task(self._send_call_summary())

    async def _await_call_summary(self) -> None:
        """Ensure summary generation finished (starts it if not already running)."""
        if not self._summary_loading_sent:
            try:
                await self._emit_call_summary(loading=True)
                self._summary_loading_sent = True
            except Exception:
                logger.exception(
                    "[WebMediaHandler] Failed to send call summary loading state"
                )
        self._start_call_summary_task()
        if self._summary_task is not None:
            await asyncio.gather(self._summary_task, return_exceptions=True)

    def _prefetch_call_summary(self) -> None:
        """Begin summary during auto-end delay so it may finish before finalize."""
        if not self._summary_prefetch_enabled() or self._finalizing:
            return
        # Do not notify the browser yet — avoids "Generating summary" banner mid-call.
        self._start_call_summary_task()

    async def _emit_call_summary_loading(self) -> None:
        try:
            await self._emit_call_summary(loading=True)
            self._summary_loading_sent = True
        except Exception:
            logger.exception(
                "[WebMediaHandler] Failed to send call summary prefetch loading state"
            )

    def _auto_end_call_delay_s(self) -> float:
        try:
            return max(0.0, float(os.getenv("AUTO_END_CALL_DELAY_S", "5")))
        except ValueError:
            return 5.0

    def _schedule_auto_end_call(self) -> None:
        """Wait briefly after the agent closes the interview, then finalize."""
        if self._finalizing:
            return
        self._prefetch_call_summary()
        if self._auto_end_task is not None and not self._auto_end_task.done():
            self._auto_end_task.cancel()
        self._auto_end_task = asyncio.create_task(self._auto_end_after_delay())

    async def _auto_end_after_delay(self) -> None:
        delay = self._auto_end_call_delay_s()
        try:
            if delay > 0:
                logger.info(
                    "[WebMediaHandler] Agent finished interview — auto end-call in %.1fs",
                    delay,
                )
                await asyncio.sleep(delay)
            logger.info(
                "[WebMediaHandler] auto-end timer elapsed (finalizing=%s) — ending call",
                self._finalizing,
            )
            if not self._finalizing:
                await self.request_end_call(source="agent")
        except asyncio.CancelledError:
            logger.debug("[WebMediaHandler] Auto end-call cancelled")
            raise

    def _cancel_auto_end_task(self) -> None:
        if self._auto_end_task is not None and not self._auto_end_task.done():
            self._auto_end_task.cancel()
        self._auto_end_task = None

    def _session_config(self) -> RequestSession:
        session = super()._session_config()
        transcription_model = os.getenv(
            "INPUT_TRANSCRIPTION_MODEL", "azure-speech"
        ).strip() or "azure-speech"
        # gpt-4o-transcribe / whisper hallucinate short mortgage turns on quiet web
        # mics (repro evidence: "Sheep for something else", "have a handy"). Prefer
        # azure-speech + phrase_list unless explicitly opted in.
        allow_gpt_stt = os.getenv(
            "INPUT_TRANSCRIPTION_ALLOW_GPT_STT", ""
        ).strip().lower() in {"1", "true", "yes"}
        openai_stt = {
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "whisper-1",
        }
        if not allow_gpt_stt and transcription_model.lower() in openai_stt:
            logger.warning(
                "Remapping STT model %s -> azure-speech for accuracy "
                "(set INPUT_TRANSCRIPTION_ALLOW_GPT_STT=true to keep OpenAI STT)",
                transcription_model,
            )
            transcription_model = "azure-speech"
        # phrase_list is ONLY valid for azure-speech-family models. Sending it with
        # gpt-4o-transcribe / whisper-1 rejects the session.update and kills the call.
        phrase_supported = transcription_model.lower() in {
            "azure-speech",
            "azure-fast-transcription",
            "azure-mrs",
            "mai-transcribe",
            "mai-transcribe-1.5",
        }
        kwargs: dict = {
            # ISO-639-1 ("en"), not a BCP-47 locale ("en-US"): whisper-1 / gpt-4o-transcribe
            # only honor ISO-639-1, and an unrecognized locale silently falls back to
            # language auto-detection — which mis-transcribes short English speech into
            # other scripts (e.g. Urdu) on the native-realtime model's side-channel STT.
            "model": transcription_model,
            "language": "en",
        }
        if phrase_supported:
            phrase_list = [
                "refinance",
                "cash out",
                "cash-out",
                "HELOC",
                "home equity",
                "escrow",
                "mortgage",
                "purchase",
                "single-family",
                "condo",
                "credit score",
                "researching",
                "do not call",
                "take me off",
                "call me back",
                "twenty thousand",
                "yes",
                "no",
                "Georgia",
                "Texas",
                "California",
                "morning",
                "afternoon",
                "evening",
            ]
            raw_phrases = os.getenv("INPUT_TRANSCRIPTION_PHRASE_LIST", "").strip()
            if raw_phrases:
                phrase_list = [p.strip() for p in raw_phrases.split(",") if p.strip()]
            kwargs["phrase_list"] = phrase_list
        session.input_audio_transcription = AudioInputTranscriptionOptions(**kwargs)
        logger.info(
            "Web client input transcription enabled: model=%s phrases=%s",
            transcription_model,
            len(kwargs.get("phrase_list") or []),
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
        # Native realtime models may emit multiple assistant finals per user turn.
        if role == "assistant" and last and last.get("role") == "assistant":
            prev = (last.get("text") or "").strip()
            if prev and (text.startswith(prev) or prev in text):
                last["text"] = text
                return
            if prev and len(text) < len(prev) * 0.5:
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
        if self._finalizing:
            return
        if self._awaiting_close_ack and self._user_declined_more(transcript):
            self._awaiting_close_ack = False
            self._awaiting_agent_goodbye = True
            self._closing_phase = True
            logger.info(
                "[WebMediaHandler] Caller declined follow-up — waiting for agent goodbye"
            )
        agent_ctx = self._assistant_end_detect_context()
        if agent_ctx and transcript_requests_end_call(agent_ctx):
            logger.info(
                "[WebMediaHandler] Caller acknowledged agent goodbye — scheduling auto end"
            )
            self._awaiting_agent_goodbye = False
            self._schedule_auto_end_call()
        elif self._awaiting_agent_goodbye and self._user_declined_more(transcript):
            if agent_ctx and (
                transcript_has_farewell(agent_ctx)
                or transcript_requests_end_call(agent_ctx)
            ):
                logger.info(
                    "[WebMediaHandler] Caller ack after agent close — scheduling auto end"
                )
                self._awaiting_agent_goodbye = False
                self._schedule_auto_end_call()

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
        raw = transcript or ""
        if re.search(r"anything else", raw, re.I):
            self._awaiting_close_ack = True
            self._closing_phase = True
        agent_ctx = self._assistant_end_detect_context(raw)
        if not self._finalizing:
            if transcript_requests_end_call(agent_ctx):
                self._closing_phase = True
                self._awaiting_agent_goodbye = False
                logger.info(
                    "[WebMediaHandler] Agent closed interview — scheduling auto end"
                )
                self._schedule_auto_end_call()
            elif self._awaiting_agent_goodbye and transcript_has_farewell(agent_ctx):
                self._awaiting_agent_goodbye = False
                logger.info(
                    "[WebMediaHandler] Agent farewell after caller declined — scheduling auto end"
                )
                self._schedule_auto_end_call()
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

    async def on_response_text_done(self, text: str | None) -> None:
        if not text or self._finalizing:
            return
        agent_ctx = self._assistant_end_detect_context(text)
        if transcript_requests_end_call(agent_ctx):
            self._closing_phase = True
            self._awaiting_agent_goodbye = False
            logger.info(
                "[WebMediaHandler] Agent closed interview (text) — scheduling auto end"
            )
            self._schedule_auto_end_call()

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
                while True:
                    if self._finalizing and self._extract_seq <= self._last_published_seq:
                        break
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

                    extract_error: str | None = None
                    if usage is None and not insights:
                        extract_error = (
                            "Key-details extraction unavailable "
                            "(Voice Live text session failed or timed out)"
                        )
                        logger.warning(
                            "[WebMediaHandler] Extract returned no data (seq=%s, turns=%d)",
                            seq,
                            len(turns),
                        )

                    if usage:
                        cost = self._record_usage_cost(
                            usage,
                            text_only=True,
                            category="extract",
                            model=resolve_extract_model(),
                        )
                        await self.on_agent_event(
                            {
                                "Kind": "AgentEvent",
                                "kind": "usage",
                                "source": "extract",
                                "extractSeq": seq,
                                "tokens": usage,
                                "cost": cost,
                                "callCostUsd": self._call_cost_usd,
                                "costBreakdown": self._cost_breakdown(),
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
                        error=extract_error,
                        append=True,
                    )
                    if insights:
                        logger.info(
                            "[WebMediaHandler] LLM extract sent %d insight(s) (seq=%s)",
                            len(insights),
                            seq,
                        )

                    if self._extract_seq == seq:
                        if self._finalizing:
                            break
                        return
            except asyncio.CancelledError:
                raise
            finally:
                try:
                    await self._send_insights([], loading=False, append=True)
                except Exception:
                    pass

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
            if (self._call_summary_text or "").strip():
                await self._emit_call_summary(
                    loading=False, summary=self._call_summary_text
                )
            return

        turns = self._turns_for_summary()

        if not turns:
            msg = "No conversation to summarize."
            self._call_summary_text = msg
            await self._record_session_event("summary_done", summaryMs=0)
            await self._emit_call_summary(loading=False, summary=msg)
            self._summary_sent = True
            return

        t_summary = time.perf_counter()
        await self._record_session_event("summary_started")
        try:
            summary, usage = await llm_generate_call_summary(
                turns,
                key_facts=self._emitted_insights or None,
            )
            await self._record_session_event(
                "summary_done",
                summaryMs=self._delta_ms(t_summary, time.perf_counter()),
            )
            if usage:
                cost = self._record_usage_cost(
                    usage,
                    text_only=True,
                    category="summary",
                    model=resolve_summary_model(),
                )
                await self.on_agent_event(
                    {
                        "Kind": "AgentEvent",
                        "kind": "usage",
                        "source": "summary",
                        "tokens": usage,
                        "cost": cost,
                        "callCostUsd": self._call_cost_usd,
                        "costBreakdown": self._cost_breakdown(),
                    }
                )
            if not (summary or "").strip():
                await self._emit_call_summary(
                    loading=False,
                    error="Call summary could not be generated",
                )
                self._summary_sent = True
                return
            self._call_summary_text = summary
            await self._emit_call_summary(loading=False, summary=summary)
            logger.info("[WebMediaHandler] Call summary sent (%d chars)", len(summary))
        except Exception:
            logger.exception("[WebMediaHandler] Call summary failed")
            await self._record_session_event(
                "summary_done",
                summaryMs=self._delta_ms(t_summary, time.perf_counter()),
                failed=True,
            )
            await self._emit_call_summary(
                loading=False,
                error="Call summary failed",
            )
        finally:
            self._summary_sent = True

    def _attach_extract_cost_to_metrics(self, payload: dict) -> None:
        """Merge key-details extraction cost onto the matching voice turn row."""
        seq = payload.get("extractSeq")
        if seq is None:
            return
        cost = payload.get("cost")
        if not cost:
            return
        for row in reversed(self._call_metrics):
            if row.get("seq") == seq or row.get("turn") == seq:
                row["extractCost"] = cost
                tokens = payload.get("tokens")
                if isinstance(tokens, dict) and tokens:
                    row["extractTokens"] = tokens
                return
        if self._call_metrics:
            row = self._call_metrics[-1]
            row["extractCost"] = cost
            tokens = payload.get("tokens")
            if isinstance(tokens, dict) and tokens:
                row["extractTokens"] = tokens

    async def on_agent_event(self, payload: dict) -> None:
        """Forward agent metrics + event-timeline payloads to the browser."""
        if payload.get("kind") == "usage" and payload.get("source") == "extract":
            self._attach_extract_cost_to_metrics(payload)
        if not payload.get("costBreakdown"):
            payload["costBreakdown"] = self._cost_breakdown()
        await self.send_message(json.dumps(payload))

    async def _await_extract_worker(self) -> None:
        """Let in-flight extraction finish before persisting (do not cancel mid-pass)."""
        if self._extract_worker is None or self._extract_worker.done():
            await self._run_final_extract()
            return
        timeout = float(os.getenv("EXTRACT_TIMEOUT_S", "30")) + 10.0
        try:
            await asyncio.wait_for(
                asyncio.shield(self._extract_worker),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[WebMediaHandler] Extract worker timed out after %.0fs during finalize",
                timeout,
            )
            self._extract_worker.cancel()
            try:
                await asyncio.gather(self._extract_worker, return_exceptions=True)
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        finally:
            self._extract_worker = None
        await self._run_final_extract()

    async def _run_final_extract(self) -> None:
        """One last extract pass at end-of-call so key details are not lost."""
        if self._final_extract_done:
            return
        turns = self._turns_for_extract()
        if not turns:
            return
        self._final_extract_done = True
        try:
            insights, usage = await llm_extract_insights(
                turns, self._emitted_insights
            )
        except Exception:
            logger.exception("[WebMediaHandler] Final extract failed")
            try:
                await self._send_insights(
                    [],
                    loading=False,
                    error="Insight extraction failed at end of call",
                    append=True,
                )
            except Exception:
                pass
            return
        if usage:
            cost = self._record_usage_cost(
                usage,
                text_only=True,
                category="extract",
                model=resolve_extract_model(),
            )
            await self.on_agent_event(
                {
                    "Kind": "AgentEvent",
                    "kind": "usage",
                    "source": "extract",
                    "tokens": usage,
                    "cost": cost,
                    "callCostUsd": self._call_cost_usd,
                    "costBreakdown": self._cost_breakdown(),
                }
            )
        if insights:
            await self._send_insights(insights, loading=False, append=True)
            logger.info(
                "[WebMediaHandler] Final extract sent %d insight(s)", len(insights)
            )
        else:
            try:
                await self._send_insights([], loading=False, append=True)
            except Exception:
                pass

    async def _cancel_extract_worker(self) -> None:
        await self._await_extract_worker()

    async def _ensure_voicelive_cleanup(self, *, persist: bool = True) -> None:
        if self._voicelive_cleaned:
            return
        self._voicelive_cleaned = True
        await self._record_session_event("session_closing")
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
                "model": self.model,
                "customPrompt": self.system_prompt is not None,
                "brokerage": BROKERAGE_NAME,
                "persona": "Maya — mortgage pre-qualification",
                "startedAt": started.isoformat() if started else None,
                "endedAt": ended.isoformat(),
                "durationSec": round((ended - started).total_seconds(), 3)
                if started
                else None,
                "transcript": transcript,
                "metrics": self._call_metrics,
                "events": self._events_for_persist(started, ended),
                **self._call_count_fields(transcript),
                "callSummary": self._call_summary_text,
                "keyDetails": insight_detail_rows(self._emitted_insights),
                "callCostUsd": round(self._call_cost_usd, 6) if self._call_cost_usd else None,
                "costBreakdown": self._cost_breakdown(),
                "voiceLiveModel": self.model,
                "extractModel": resolve_extract_model(),
                "summaryModel": resolve_summary_model(),
                "transcriptionModel": os.getenv(
                    "INPUT_TRANSCRIPTION_MODEL", "azure-speech"
                ).strip(),
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

    async def _emit_call_ended(self, *, source: str) -> None:
        payload = {"Kind": "CallEnded", "callId": self.call_id, "source": source}
        await self.send_message(json.dumps(payload))

    async def request_end_call(self, *, source: str = "client") -> None:
        """Start the same finalize flow as the End Call button."""
        logger.info(
            "[WebMediaHandler] request_end_call(source=%s, finalizing=%s)",
            source,
            self._finalizing,
        )
        if self._finalizing:
            return
        self._cancel_auto_end_task()
        self._finalizing = True
        event_name = "end_call_requested" if source == "client" else "call_ended"
        await self._record_session_event(event_name, source=source)
        try:
            await self._emit_call_ended(source=source)
        except Exception:
            logger.exception("[WebMediaHandler] Failed to notify client call ended")
        if not self._summary_loading_sent:
            try:
                await self._emit_call_summary(loading=True)
                self._summary_loading_sent = True
            except Exception:
                logger.exception(
                    "[WebMediaHandler] Failed to send call summary loading on end call"
                )
        self._start_call_summary_task()
        if self._finalize_task is None or self._finalize_task.done():
            self._finalize_task = asyncio.create_task(self._finalize_call())

    async def on_voicelive_disconnected(self, *, cancelled: bool = False) -> None:
        if not cancelled:
            logger.info("[WebMediaHandler] Voice Live disconnected — auto-ending call")
            await self.request_end_call(source="voicelive_disconnect")

    async def _finalize_call(self) -> None:
        """End the Voice Live session and generate a call summary."""
        if self._finalizing and self._finalize_task is not asyncio.current_task():
            if self._finalize_task is not None:
                await asyncio.gather(self._finalize_task, return_exceptions=True)
            return
        self._finalizing = True
        try:
            # Summary first while the client WebSocket is still open, then tear down
            # Voice Live. Parallel cleanup previously raced the HTTP summary call and
            # could leave the UI stuck on "Generating…" if the socket dropped early.
            await self._await_call_summary()
            cancel_task = asyncio.create_task(self._cancel_extract_worker())
            await asyncio.gather(
                self._ensure_voicelive_cleanup(persist=False),
                cancel_task,
                return_exceptions=True,
            )
            saved_id = await self._await_persist_call_record()
            await self._try_notify_call_saved(saved_id)
        except Exception:
            logger.exception("[WebMediaHandler] Finalize call failed")
            try:
                if not self._summary_sent:
                    await self._emit_call_summary(
                        loading=False,
                        error="Call summary failed during finalize",
                    )
                    self._summary_sent = True
            except Exception:
                logger.debug("[WebMediaHandler] summary error emit skipped", exc_info=True)

    async def await_finalize_complete(self) -> None:
        """Block until summary + persist have finished (hard-close / disconnect).

        ``request_end_call`` starts finalize as a background task so speech teardown
        stays responsive; callers that must keep the client socket open until the
        summary is delivered should await this before ``ws.close``.
        """
        task = self._finalize_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        if not self._finalizing:
            self._finalizing = True
        if not self._summary_sent:
            await self._await_call_summary()
        saved_id = await self._await_persist_call_record()
        await self._try_notify_call_saved(saved_id)
    async def _handle_control_message(self, text: str) -> bool:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False
        kind = payload.get("Kind")
        if kind == "PlaybackFinished":
            # The browser finished playing all buffered agent audio — hard-close
            # and silence both wait on this (never arm silence on generate-done).
            self._last_drain_at = time.perf_counter()
            await self.on_playback_finished()
            return True
        if kind != "EndCall":
            return False
        logger.info("[WebMediaHandler] EndCall received — closing session")
        await self.request_end_call(source="client")
        return True

    async def on_playback_finished(self) -> None:
        """Hook: browser finished playing buffered agent audio (PlaybackFinished)."""

    async def on_message(self, msg):
        """Handle browser control text and PCM audio from Quart receive().

        Quart's ``Websocket.receive()`` yields a raw ``str`` / ``bytes`` payload
        (not an ASGI dict). Text frames must be routed to control handling —
        otherwise EndCall / PlaybackFinished are latin-1-"encoded" into fake PCM
        and never hang up the call.
        """
        if isinstance(msg, dict):
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            if text and await self._handle_control_message(text):
                return
            data = msg.get("bytes")
            if data is None:
                return
        elif isinstance(msg, str):
            if await self._handle_control_message(msg):
                return
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
        self._cancel_auto_end_task()
        await self._cancel_extract_worker()
        if not self._finalizing:
            self._finalizing = True
        if self._finalize_task is not None and not self._finalize_task.done():
            await asyncio.gather(self._finalize_task, return_exceptions=True)
        elif not self._summary_sent:
            await self._await_call_summary()
        if not self._voicelive_cleaned:
            await self._ensure_voicelive_cleanup(persist=False)
        saved_id = await self._await_persist_call_record()
        await self._try_notify_call_saved(saved_id)
