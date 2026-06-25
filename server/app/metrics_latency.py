"""Caller-aligned latency helpers (borrower stop → first audio / response done).

Persisted on new turns as ``firstAudioMs`` and ``e2eResponseMs``. Older Cosmos
records are backfilled from stage fields (``sttMs``, ``ttfaMs``, etc.).
"""

from __future__ import annotations


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def derive_first_audio_ms(metrics: dict | None) -> float | None:
    """Borrower stops speaking → agent first audible sound.

    Formula: ``sttMs + thinkMs + ttsStartMs``
    """
    if not isinstance(metrics, dict):
        return None
    direct = _num(metrics.get("firstAudioMs"))
    if direct is not None:
        return direct
    stt = _num(metrics.get("sttMs"))
    think = _num(metrics.get("thinkMs"))
    tts_start = _num(metrics.get("ttsStartMs"))
    if stt is not None and think is not None and tts_start is not None:
        return stt + think + tts_start
    # Legacy fallbacks when stage fields are incomplete.
    ttfa = _num(metrics.get("ttfaMs"))
    if stt is not None and ttfa is not None:
        return stt + ttfa
    return ttfa


def derive_e2e_response_ms(metrics: dict | None) -> float | None:
    """Borrower stops speaking → agent finishes spoken reply.

    Formula: ``derive_first_audio_ms + ttsMs`` (equivalently STT + Think + TTS start + TTS).
    """
    if not isinstance(metrics, dict):
        return None
    direct = _num(metrics.get("e2eResponseMs"))
    if direct is not None:
        return direct
    first_audio = derive_first_audio_ms(metrics)
    tts = _num(metrics.get("ttsMs"))
    if first_audio is not None and tts is not None:
        return first_audio + tts
    stt = _num(metrics.get("sttMs"))
    think = _num(metrics.get("thinkMs"))
    tts_start = _num(metrics.get("ttsStartMs"))
    if all(v is not None for v in (stt, think, tts_start, tts)):
        return stt + think + tts_start + tts
    # Legacy: STT + think + server response window (created → done).
    resp = _num(metrics.get("responseMs"))
    if stt is not None and think is not None and resp is not None:
        return stt + think + resp
    if stt is not None and resp is not None:
        return stt + resp
    return resp
