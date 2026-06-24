"""Azure Voice Live token usage → USD cost (configurable per 1M tokens)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class VoiceLiveRates:
    """Per-million-token USD rates (Azure Voice Live Basic + Azure Speech Standard)."""

    # Official Azure Voice Live "Standard/Basic" rates (gpt-4o-mini family).
    # See https://azure.microsoft.com/en-us/pricing/details/speech/
    text_input: float = 0.66
    text_cached_input: float = 0.33
    text_output: float = 2.64
    audio_input: float = 15.0
    audio_cached_input: float = 0.33
    audio_output: float = 33.0


@dataclass(frozen=True)
class TranscribeRates:
    """Per-million-token USD rates (Azure gpt-4o-mini-transcribe)."""

    text_input: float = 1.25
    text_output: float = 5.0
    audio_input: float = 3.0


def get_voice_live_rates() -> VoiceLiveRates:
    return VoiceLiveRates(
        text_input=_env_float("VOICE_LIVE_PRICE_TEXT_INPUT_PER_1M", 0.66),
        text_cached_input=_env_float(
            "VOICE_LIVE_PRICE_TEXT_CACHED_INPUT_PER_1M", 0.33
        ),
        text_output=_env_float("VOICE_LIVE_PRICE_TEXT_OUTPUT_PER_1M", 2.64),
        audio_input=_env_float("VOICE_LIVE_PRICE_AUDIO_INPUT_PER_1M", 15.0),
        audio_cached_input=_env_float(
            "VOICE_LIVE_PRICE_AUDIO_CACHED_INPUT_PER_1M", 0.33
        ),
        audio_output=_env_float("VOICE_LIVE_PRICE_AUDIO_OUTPUT_PER_1M", 33.0),
    )


def get_transcribe_rates() -> TranscribeRates:
    return TranscribeRates(
        text_input=_env_float("TRANSCRIBE_PRICE_TEXT_INPUT_PER_1M", 1.25),
        text_output=_env_float("TRANSCRIBE_PRICE_TEXT_OUTPUT_PER_1M", 5.0),
        audio_input=_env_float("TRANSCRIBE_PRICE_AUDIO_INPUT_PER_1M", 3.0),
    )


def _normalize_usage_model(usage_model) -> dict | None:
    if not usage_model:
        return None

    usage: dict = {
        "total": getattr(usage_model, "total_tokens", None),
        "input": getattr(usage_model, "input_tokens", None),
        "output": getattr(usage_model, "output_tokens", None),
    }
    out_details = getattr(usage_model, "output_token_details", None)
    if out_details is not None:
        usage["outputText"] = getattr(out_details, "text_tokens", None)
        usage["outputAudio"] = getattr(out_details, "audio_tokens", None)
    in_details = getattr(usage_model, "input_token_details", None)
    if in_details is not None:
        usage["inputText"] = getattr(in_details, "text_tokens", None)
        usage["inputAudio"] = getattr(in_details, "audio_tokens", None)
        usage["inputCached"] = getattr(in_details, "cached_tokens", None)
        cached_details = getattr(in_details, "cached_tokens_details", None)
        if cached_details is not None:
            usage["inputCachedText"] = getattr(cached_details, "text_tokens", None)
            usage["inputCachedAudio"] = getattr(cached_details, "audio_tokens", None)
    return usage


def normalize_usage(response) -> dict | None:
    """Pull token counts off a RESPONSE_DONE response.usage."""
    usage_model = getattr(response, "usage", None) if response else None
    return _normalize_usage_model(usage_model)


def normalize_transcription_usage(event) -> dict | None:
    """Pull token counts off an input-audio transcription completed event."""
    usage_model = getattr(event, "usage", None) if event else None
    return _normalize_usage_model(usage_model)


def estimate_transcribe_usage_from_speech_ms(
    speech_ms: int,
    *,
    transcript: str | None = None,
) -> dict:
    """Estimate transcribe usage when the API does not return token counts."""
    audio_tokens = max(1, round(speech_ms / 1000 * 10))
    out_text = max(1, round(len(transcript or "") / 4)) if transcript else 0
    usage: dict = {
        "inputAudio": audio_tokens,
        "input": audio_tokens,
    }
    if out_text:
        usage["outputText"] = out_text
        usage["output"] = out_text
    return usage


def _int(v) -> int:
    if v is None:
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _split_cached(
    cached_total: int, text_tokens: int, audio_tokens: int
) -> tuple[int, int]:
    if cached_total <= 0:
        return 0, 0
    denom = text_tokens + audio_tokens
    if denom <= 0:
        return cached_total, 0
    cached_text = int(round(cached_total * (text_tokens / denom)))
    cached_text = min(cached_text, text_tokens, cached_total)
    cached_audio = min(cached_total - cached_text, audio_tokens)
    return cached_text, cached_audio


def compute_usage_cost_usd(
    usage: dict | None,
    *,
    text_only: bool = False,
    rates: VoiceLiveRates | None = None,
) -> dict | None:
    """Return USD cost breakdown for a usage dict from normalize_usage()."""
    if not usage:
        return None

    rates = rates or get_voice_live_rates()

    in_text = _int(usage.get("inputText"))
    in_audio = 0 if text_only else _int(usage.get("inputAudio"))
    out_text = _int(usage.get("outputText"))
    out_audio = 0 if text_only else _int(usage.get("outputAudio"))

    if in_text == 0 and usage.get("input") is not None and text_only:
        in_text = _int(usage.get("input"))
    if out_text == 0 and usage.get("output") is not None and text_only:
        out_text = _int(usage.get("output"))

    if not text_only:
        total_in = _int(usage.get("input"))
        total_out = _int(usage.get("output"))
        if in_text + in_audio == 0 and total_in > 0:
            in_audio = total_in
        if out_text + out_audio == 0 and total_out > 0:
            out_audio = total_out

    cached_total = _int(usage.get("inputCached"))
    cached_text = _int(usage.get("inputCachedText"))
    cached_audio = _int(usage.get("inputCachedAudio"))
    if cached_total and not cached_text and not cached_audio:
        cached_text, cached_audio = _split_cached(
            cached_total, in_text, in_audio
        )

    bill_in_text = max(0, in_text - cached_text)
    bill_in_audio = max(0, in_audio - cached_audio)

    text_in_cost = bill_in_text * rates.text_input / 1_000_000
    text_cached_cost = cached_text * rates.text_cached_input / 1_000_000
    audio_in_cost = bill_in_audio * rates.audio_input / 1_000_000
    audio_cached_cost = cached_audio * rates.audio_cached_input / 1_000_000
    text_out_cost = out_text * rates.text_output / 1_000_000
    audio_out_cost = out_audio * rates.audio_output / 1_000_000

    usd = (
        text_in_cost
        + text_cached_cost
        + audio_in_cost
        + audio_cached_cost
        + text_out_cost
        + audio_out_cost
    )

    return {
        "usd": round(usd, 6),
        "inputTokens": in_text + in_audio,
        "outputTokens": out_text + out_audio,
        "breakdown": {
            "textInputUsd": round(text_in_cost + text_cached_cost, 6),
            "audioInputUsd": round(audio_in_cost + audio_cached_cost, 6),
            "textOutputUsd": round(text_out_cost, 6),
            "audioOutputUsd": round(audio_out_cost, 6),
        },
    }


def compute_transcribe_cost_usd(
    usage: dict | None,
    *,
    rates: TranscribeRates | None = None,
) -> dict | None:
    """Return USD cost for caller STT (gpt-4o-mini-transcribe)."""
    if not usage:
        return None

    rates = rates or get_transcribe_rates()

    in_text = _int(usage.get("inputText"))
    in_audio = _int(usage.get("inputAudio"))
    if in_audio == 0:
        in_audio = _int(usage.get("input"))
    out_text = _int(usage.get("outputText"))
    if out_text == 0:
        out_text = _int(usage.get("output"))

    text_in_cost = in_text * rates.text_input / 1_000_000
    audio_in_cost = in_audio * rates.audio_input / 1_000_000
    text_out_cost = out_text * rates.text_output / 1_000_000

    usd = text_in_cost + audio_in_cost + text_out_cost

    return {
        "usd": round(usd, 6),
        "inputTokens": in_text + in_audio,
        "outputTokens": out_text,
        "breakdown": {
            "textInputUsd": round(text_in_cost, 6),
            "audioInputUsd": round(audio_in_cost, 6),
            "textOutputUsd": round(text_out_cost, 6),
        },
    }


def _estimate_transcribe_usd_from_metrics(metrics: list) -> float:
    total_speech_ms = 0
    for row in metrics:
        if not isinstance(row, dict):
            continue
        m = row.get("metrics")
        if isinstance(m, dict):
            total_speech_ms += _int(m.get("userSpeechMs"))
    if total_speech_ms <= 0:
        return 0.0
    cost = compute_transcribe_cost_usd(
        estimate_transcribe_usage_from_speech_ms(total_speech_ms)
    )
    return _cost_usd(cost)


def _cost_usd(cost: dict | None) -> float:
    if not isinstance(cost, dict):
        return 0.0
    try:
        return max(0.0, float(cost.get("usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def enrich_call_record(record: dict, *, include_timeline: bool = True) -> dict:
    """Backfill missing per-row and session USD costs from saved token usage."""
    if not isinstance(record, dict):
        return record

    metrics = record.get("metrics")
    if not isinstance(metrics, list):
        metrics = []

    transcript = record.get("transcript")
    if not isinstance(transcript, list):
        transcript = []

    if not record.get("messageCount"):
        record["messageCount"] = len(transcript) or record.get("turnCount")
    if record.get("turnCount") is None and record.get("messageCount") is not None:
        record["turnCount"] = record["messageCount"]
    if not record.get("agentResponseCount"):
        record["agentResponseCount"] = len(metrics)
    if not record.get("userTurnCount"):
        events = record.get("events") if isinstance(record.get("events"), list) else []
        user_turns: list[int] = []
        for e in events:
            if not isinstance(e, dict) or e.get("turn") is None:
                continue
            try:
                user_turns.append(int(e["turn"]))
            except (TypeError, ValueError):
                continue
        if user_turns:
            record["userTurnCount"] = max(user_turns)

    if include_timeline:
        events = record.get("events") if isinstance(record.get("events"), list) else []
        duration_ms = None
        if record.get("durationSec") is not None:
            try:
                duration_ms = round(float(record["durationSec"]) * 1000)
            except (TypeError, ValueError):
                duration_ms = None
        has_started = any(isinstance(e, dict) and e.get("event") == "call_started" for e in events)
        enriched_events = list(events)
        if not has_started:
            enriched_events.insert(0, {"event": "call_started", "turn": None, "atMs": 0})
        if duration_ms is not None:
            enriched_events = [
                e
                for e in enriched_events
                if not (isinstance(e, dict) and e.get("event") == "call_ended")
            ]
            enriched_events.append({"event": "call_ended", "turn": None, "atMs": duration_ms})
        record["events"] = enriched_events

    session_total = _cost_usd({"usd": record.get("callCostUsd")})
    row_voice = 0.0
    row_extract = 0.0

    for row in metrics:
        if not isinstance(row, dict):
            continue
        m = row.get("metrics")
        if isinstance(m, dict):
            think = m.get("thinkMs")
            tts_start = m.get("ttsStartMs")
            if think is not None and tts_start is not None:
                combined = think + tts_start
                if combined >= 0:
                    m["ttfaMs"] = combined
            elif tts_start is not None:
                m["ttfaMs"] = tts_start
        tokens = row.get("tokens")
        if not row.get("cost") and isinstance(tokens, dict) and tokens:
            cost = compute_usage_cost_usd(tokens, text_only=False)
            if cost:
                row["cost"] = cost
        extract_tokens = row.get("extractTokens")
        if not row.get("extractCost") and isinstance(extract_tokens, dict) and extract_tokens:
            extract_cost = compute_usage_cost_usd(extract_tokens, text_only=True)
            if extract_cost:
                row["extractCost"] = extract_cost
        row_voice += _cost_usd(row.get("cost"))
        row_extract += _cost_usd(row.get("extractCost"))

    stored = record.get("costBreakdown") if isinstance(record.get("costBreakdown"), dict) else {}
    voice = _cost_usd({"usd": stored.get("voiceUsd")}) or row_voice
    transcribe = _cost_usd({"usd": stored.get("transcribeUsd")})
    if transcribe <= 0:
        transcribe = _estimate_transcribe_usd_from_metrics(metrics)
    extract = _cost_usd({"usd": stored.get("extractUsd")}) or row_extract
    summary = _cost_usd({"usd": stored.get("summaryUsd")})

    parts_total = round(voice + transcribe + extract + summary, 6)
    if session_total > 0 and summary <= 0 and session_total > parts_total:
        summary = round(session_total - voice - transcribe - extract, 6)
        if summary < 0:
            summary = 0.0

    total = session_total if session_total > 0 else round(voice + transcribe + extract + summary, 6)
    if total <= 0 and (voice > 0 or transcribe > 0 or extract > 0 or summary > 0):
        total = round(voice + transcribe + extract + summary, 6)

    record["costBreakdown"] = {
        "voiceUsd": round(voice, 6),
        "transcribeUsd": round(transcribe, 6),
        "extractUsd": round(extract, 6),
        "summaryUsd": round(summary, 6),
        "totalUsd": round(total, 6),
    }
    if total > 0:
        record["callCostUsd"] = round(total, 6)

    return record
