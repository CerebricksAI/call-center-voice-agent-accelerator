"""Azure Voice Live token usage → USD cost (configurable per 1M tokens)."""

from __future__ import annotations

import os
import re
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
    """Per-million-token USD rates (Azure gpt-4o-mini-transcribe).

    Official: audio input $1.25, text output $5.00 (text input unused — STT is
    audio-in -> text-out).
    """

    text_input: float = 1.25
    text_output: float = 5.0
    audio_input: float = 1.25


def _model_env_token(model: str | None) -> str:
    """ENV-safe token for a model name, e.g. ``gpt-realtime-mini`` -> ``GPT_REALTIME_MINI``."""
    return re.sub(r"[^A-Z0-9]+", "_", (model or "").upper()).strip("_")


def _rate(suffix: str, default: float, model_token: str) -> float:
    """Resolve a price: per-model override -> generic override -> default.

    Both Voice Live *Basic* models (gpt-4o-mini, gpt-realtime-mini) share the same
    tier rate by default. The per-model override (``VOICE_LIVE_PRICE_<MODEL>_<SUFFIX>``)
    lets a different-tier model (e.g. Pro ``gpt-realtime``) be priced separately
    without code changes.
    """
    if model_token:
        raw = os.getenv(f"VOICE_LIVE_PRICE_{model_token}_{suffix}")
        if raw and raw.strip():
            try:
                return float(raw)
            except ValueError:
                pass
    return _env_float(f"VOICE_LIVE_PRICE_{suffix}", default)


# Azure Voice Live per-1M-token USD rates by tier, effective 2025-07-01.
# Verified June 2026 (azure.microsoft.com/pricing/details/speech, learn.microsoft.com
# Voice Live, MS Q&A 5778584):
#   Basic: text-in $0.66, text-out $2.64 (4x in), audio-in $15.
#   Pro:   text-in $4.40, text-out $17.60, cached text-in $1.375, audio-in $17.
# Values marked (est) are not published on a fetchable page and are inferred from
# the in/out ratios — confirm in the Azure portal before relying on them, or set
# the matching VOICE_LIVE_PRICE_* env var. Both selectable models are Basic tier,
# so the cost difference between them comes from token TYPE (cascaded text vs
# native audio), not the rate table.
_TIER_DEFAULTS: dict[str, dict[str, float]] = {
    "basic": {
        "TEXT_INPUT_PER_1M": 0.66,
        "TEXT_CACHED_INPUT_PER_1M": 0.33,
        "TEXT_OUTPUT_PER_1M": 2.64,
        "AUDIO_INPUT_PER_1M": 15.0,
        "AUDIO_CACHED_INPUT_PER_1M": 0.33,   # (est)
        "AUDIO_OUTPUT_PER_1M": 33.0,         # (est)
    },
    "pro": {
        "TEXT_INPUT_PER_1M": 4.40,
        "TEXT_CACHED_INPUT_PER_1M": 1.375,
        "TEXT_OUTPUT_PER_1M": 17.60,
        "AUDIO_INPUT_PER_1M": 17.0,
        "AUDIO_CACHED_INPUT_PER_1M": 1.375,  # (est)
        "AUDIO_OUTPUT_PER_1M": 37.40,        # (est) ~2.2x audio-in, as in Basic
    },
}

# Exact per-model rates — override the tier defaults for a specific model.
# gpt-realtime-mini has its own published native-audio rates (per the Azure
# Voice Live billing sheet): text $0.60/$2.40, audio $10/$20 per 1M tokens.
_MODEL_DEFAULTS: dict[str, dict[str, float]] = {
    "gpt-realtime-mini": {
        "TEXT_INPUT_PER_1M": 0.60,
        "TEXT_CACHED_INPUT_PER_1M": 0.30,    # (est) cached not in the 4-component sheet
        "TEXT_OUTPUT_PER_1M": 2.40,
        "AUDIO_INPUT_PER_1M": 10.0,
        "AUDIO_CACHED_INPUT_PER_1M": 0.30,   # (est)
        "AUDIO_OUTPUT_PER_1M": 20.0,
    },
}

# Models billed at the Voice Live Pro tier; everything else defaults to Basic.
_PRO_MODELS = frozenset(
    {"gpt-realtime", "gpt-4o", "gpt-4.1", "gpt-5", "gpt-5-chat"}
)


def _tier_for_model(model: str | None) -> str:
    return "pro" if (model or "").strip().lower() in _PRO_MODELS else "basic"


def get_voice_live_rates(model: str | None = None) -> VoiceLiveRates:
    """Per-million-token rates for ``model``.

    Picks the tier defaults by model name (Basic for gpt-4o-mini / gpt-realtime-mini,
    Pro for gpt-realtime / gpt-4o / …), then applies any env overrides
    (``VOICE_LIVE_PRICE_<MODEL>_<SUFFIX>`` first, then ``VOICE_LIVE_PRICE_<SUFFIX>``).
    """
    t = _model_env_token(model)
    name = (model or "").strip().lower()
    # Exact per-model rates take precedence over the tier defaults.
    d = _MODEL_DEFAULTS.get(name) or _TIER_DEFAULTS[_tier_for_model(model)]
    return VoiceLiveRates(
        text_input=_rate("TEXT_INPUT_PER_1M", d["TEXT_INPUT_PER_1M"], t),
        text_cached_input=_rate("TEXT_CACHED_INPUT_PER_1M", d["TEXT_CACHED_INPUT_PER_1M"], t),
        text_output=_rate("TEXT_OUTPUT_PER_1M", d["TEXT_OUTPUT_PER_1M"], t),
        audio_input=_rate("AUDIO_INPUT_PER_1M", d["AUDIO_INPUT_PER_1M"], t),
        audio_cached_input=_rate("AUDIO_CACHED_INPUT_PER_1M", d["AUDIO_CACHED_INPUT_PER_1M"], t),
        audio_output=_rate("AUDIO_OUTPUT_PER_1M", d["AUDIO_OUTPUT_PER_1M"], t),
    )


def get_transcribe_rates() -> TranscribeRates:
    return TranscribeRates(
        text_input=_env_float("TRANSCRIBE_PRICE_TEXT_INPUT_PER_1M", 1.25),
        text_output=_env_float("TRANSCRIBE_PRICE_TEXT_OUTPUT_PER_1M", 5.0),
        audio_input=_env_float("TRANSCRIBE_PRICE_AUDIO_INPUT_PER_1M", 1.25),
    )


def get_text_analysis_rates() -> VoiceLiveRates:
    """Standard gpt-4o-mini TEXT rates for the side analysis sessions (lead
    extraction + call summary). These are plain text completions billed at the
    standard model price ($0.15 in / $0.60 out) — NOT the Voice Live voice rate.
    """
    return VoiceLiveRates(
        text_input=_env_float("TEXT_ANALYSIS_PRICE_INPUT_PER_1M", 0.15),
        text_cached_input=_env_float("TEXT_ANALYSIS_PRICE_CACHED_INPUT_PER_1M", 0.075),
        text_output=_env_float("TEXT_ANALYSIS_PRICE_OUTPUT_PER_1M", 0.60),
        audio_input=0.0,
        audio_cached_input=0.0,
        audio_output=0.0,
    )


def get_tts_rate_per_1m_chars() -> float:
    """Azure Speech TTS price per 1M characters synthesized (the agent voice).

    The spoken output is rendered by the Azure voice (en-US-Ava:DragonHDLatestNeural)
    and billed separately by Azure AI Speech, per character — for BOTH voice models.
    Default is the HD-neural rate; confirm the exact Dragon HD price in the Azure
    portal and override via TTS_PRICE_PER_1M_CHARS.
    """
    return _env_float("TTS_PRICE_PER_1M_CHARS", 30.0)


def compute_tts_cost_usd(char_count: int) -> dict | None:
    """USD cost to synthesize ``char_count`` characters of agent speech."""
    chars = _int(char_count)
    if chars <= 0:
        return None
    usd = chars * get_tts_rate_per_1m_chars() / 1_000_000
    return {"usd": round(usd, 6), "chars": chars}


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
    model: str | None = None,
) -> dict | None:
    """Return USD cost breakdown for a usage dict from normalize_usage().

    ``model`` selects per-model rates (see get_voice_live_rates) when ``rates``
    is not supplied explicitly.
    """
    if not usage:
        return None

    rates = rates or get_voice_live_rates(model)

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

    record_model = record.get("model")
    session_total = _cost_usd({"usd": record.get("callCostUsd")})
    row_voice = 0.0
    row_extract = 0.0

    for row in metrics:
        if not isinstance(row, dict):
            continue
        m = row.get("metrics")
        if isinstance(m, dict) and m.get("ttfaMs") is None:
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
            cost = compute_usage_cost_usd(tokens, text_only=False, model=record_model)
            if cost:
                row["cost"] = cost
        extract_tokens = row.get("extractTokens")
        if not row.get("extractCost") and isinstance(extract_tokens, dict) and extract_tokens:
            extract_cost = compute_usage_cost_usd(
                extract_tokens, text_only=True, rates=get_text_analysis_rates()
            )
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
    tts = _cost_usd({"usd": stored.get("ttsUsd")})

    parts_total = round(voice + transcribe + extract + summary + tts, 6)
    if session_total > 0 and summary <= 0 and session_total > parts_total:
        summary = round(session_total - voice - transcribe - extract - tts, 6)
        if summary < 0:
            summary = 0.0

    total = session_total if session_total > 0 else round(voice + transcribe + extract + summary + tts, 6)
    if total <= 0 and (voice > 0 or transcribe > 0 or extract > 0 or summary > 0 or tts > 0):
        total = round(voice + transcribe + extract + summary + tts, 6)

    record["costBreakdown"] = {
        "voiceUsd": round(voice, 6),
        "transcribeUsd": round(transcribe, 6),
        "extractUsd": round(extract, 6),
        "summaryUsd": round(summary, 6),
        "ttsUsd": round(tts, 6),
        "totalUsd": round(total, 6),
    }
    if total > 0:
        record["callCostUsd"] = round(total, 6)

    return record
