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

    text_input: float = 0.66
    text_cached_input: float = 0.33
    text_output: float = 2.64
    audio_input: float = 15.0
    audio_cached_input: float = 0.33
    audio_output: float = 33.0


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


def normalize_usage(response) -> dict | None:
    """Pull token counts off a RESPONSE_DONE response.usage."""
    usage_model = getattr(response, "usage", None) if response else None
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
