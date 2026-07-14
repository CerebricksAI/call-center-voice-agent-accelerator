"""Voice Live session tuning from environment (latency profiles + overrides).

Profiles control end-of-utterance detection, response token caps, audio
pre-processing, and TTS playback buffering. Individual env vars override the
active profile when set.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureSemanticDetection,
    AzureSemanticVad,
    AzureStandardVoice,
    EouThresholdLevel,
    InputAudioFormat,
    Modality,
    OpenAIVoice,
    OutputAudioFormat,
    RequestSession,
)

from app.agent_persona import (
    describe_effective_voice,
    resolve_agent_voice_name,
    resolve_agent_voice_pitch,
    resolve_agent_voice_rate,
    resolve_agent_voice_style,
    resolve_agent_voice_temperature,
    resolve_agent_voice_volume,
    resolve_lead_qualification_instructions,
    voice_name_is_openai,
)

logger = logging.getLogger(__name__)

_EOU_LEVELS: dict[str, EouThresholdLevel] = {
    "default": EouThresholdLevel.DEFAULT,
    "low": EouThresholdLevel.LOW,
    "medium": EouThresholdLevel.MEDIUM,
    "high": EouThresholdLevel.HIGH,
}

# default = patient / human-paced (wait through thinking pauses).
# balanced / aggressive = snappier TTFA (can feel jumpy on phone).
# Barge-in flush stays in the handler — these knobs only affect when a turn ends.
_LATENCY_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "eou": "low",
        "timeout_ms": 1500,
        "silence_duration_ms": 600,
        "max_tokens": 400,
        "noise_reduction": True,
        "echo_cancellation": True,
        "tts_buffer_ms": 100,
    },
    "balanced": {
        "eou": "low",
        "timeout_ms": 700,
        "silence_duration_ms": 400,
        "max_tokens": 200,
        "noise_reduction": True,
        "echo_cancellation": True,
        "tts_buffer_ms": 100,
    },
    "aggressive": {
        "eou": "low",
        "timeout_ms": 600,
        "silence_duration_ms": 300,
        "max_tokens": 150,
        "noise_reduction": False,
        "echo_cancellation": True,
        "tts_buffer_ms": 50,
    },
}


@dataclass(frozen=True)
class VoiceLiveSessionOptions:
    latency_mode: str
    eou_threshold: EouThresholdLevel
    eou_timeout_ms: int | None
    silence_duration_ms: int
    max_response_output_tokens: int | None
    noise_reduction: bool
    echo_cancellation: bool
    tts_playback_buffer_ms: int

    @property
    def tts_playback_buffer_bytes(self) -> int:
        """PCM16 mono 24 kHz: bytes needed for ``tts_playback_buffer_ms``."""
        return max(0, int(self.tts_playback_buffer_ms * 48))


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(key: str, default: int | None) -> int | None:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r — using default", key, raw)
        return default


def _resolve_eou_level(raw: str) -> EouThresholdLevel:
    level = _EOU_LEVELS.get(raw.strip().lower())
    if level is None:
        logger.warning("Unknown EOU threshold %r — using medium", raw)
        return EouThresholdLevel.MEDIUM
    return level


def _eou_level_to_str(level: EouThresholdLevel) -> str:
    mapping = {
        EouThresholdLevel.LOW: "low",
        EouThresholdLevel.MEDIUM: "medium",
        EouThresholdLevel.HIGH: "high",
        EouThresholdLevel.DEFAULT: "default",
    }
    return mapping.get(level, "medium")


def _is_native_realtime_model(model: str) -> bool:
    """Native audio I/O models (e.g. gpt-realtime-mini) reject text-based EOU."""
    return "realtime" in (model or "").strip().lower()


def _resolve_eou_mode(model: str) -> str:
    """auto | text | smart | vad — see VOICE_LIVE_EOU_MODE env."""
    mode = os.getenv("VOICE_LIVE_EOU_MODE", "auto").strip().lower()
    if mode == "auto":
        return "smart" if _is_native_realtime_model(model) else "text"
    return mode


def _build_turn_detection(
    opts: VoiceLiveSessionOptions, *, model: str
) -> AzureSemanticVad:
    """Build turn detection compatible with the deployed Voice Live model."""
    mode = _resolve_eou_mode(model)
    # remove_filler_words: "umm"/"uh" should not end the caller's turn early.
    # interrupt_response + auto_truncate: Story-3 barge-in (server-side halt).
    common: dict[str, Any] = {
        "interrupt_response": True,
        "auto_truncate": True,
        "remove_filler_words": True,
        "prefix_padding_ms": 200,
    }

    if mode == "text":
        eou_kwargs: dict[str, Any] = {"threshold_level": opts.eou_threshold}
        if opts.eou_timeout_ms is not None:
            eou_kwargs["timeout_ms"] = opts.eou_timeout_ms
        return AzureSemanticVad(
            end_of_utterance_detection=AzureSemanticDetection(**eou_kwargs),
            silence_duration_ms=opts.silence_duration_ms,
            **common,
        )

    if mode == "smart":
        # Audio-based EOU — required for native realtime models (gpt-realtime-mini).
        eou: dict[str, Any] = {
            "model": "smart_end_of_turn_detection",
            "threshold_level": _eou_level_to_str(opts.eou_threshold),
        }
        if opts.eou_timeout_ms is not None:
            eou["timeout_ms"] = opts.eou_timeout_ms
        return AzureSemanticVad(
            end_of_utterance_detection=eou,
            silence_duration_ms=opts.silence_duration_ms,
            **common,
        )

    if mode != "vad":
        logger.warning("Unknown VOICE_LIVE_EOU_MODE=%r — using vad", mode)

    # Semantic VAD only (no nested EOU) — fallback if smart EOU is unavailable.
    return AzureSemanticVad(
        silence_duration_ms=opts.silence_duration_ms,
        **common,
    )


def resolve_session_options() -> VoiceLiveSessionOptions:
    """Build session tuning options from profile + optional env overrides."""
    mode = os.getenv("VOICE_LIVE_LATENCY_MODE", "default").strip().lower()
    preset = _LATENCY_PRESETS.get(mode)
    if preset is None:
        logger.warning(
            "Unknown VOICE_LIVE_LATENCY_MODE=%r — using default profile", mode
        )
        mode = "default"
        preset = _LATENCY_PRESETS["default"]

    eou_raw = os.getenv("VOICE_LIVE_EOU_THRESHOLD", "").strip().lower()
    eou = _resolve_eou_level(eou_raw or preset["eou"])

    timeout_ms = _env_int("VOICE_LIVE_EOU_TIMEOUT_MS", preset["timeout_ms"])
    max_tokens = _env_int(
        "VOICE_LIVE_MAX_RESPONSE_TOKENS", preset["max_tokens"]
    )
    noise = _env_bool("VOICE_LIVE_NOISE_REDUCTION", preset["noise_reduction"])
    echo = _env_bool("VOICE_LIVE_ECHO_CANCELLATION", preset["echo_cancellation"])
    buffer_ms = _env_int("TTS_PLAYBACK_BUFFER_MS", preset["tts_buffer_ms"]) or 0
    silence_ms = _env_int(
        "VOICE_LIVE_SILENCE_DURATION_MS", preset["silence_duration_ms"]
    )
    if silence_ms is None:
        silence_ms = preset["silence_duration_ms"]

    return VoiceLiveSessionOptions(
        latency_mode=mode,
        eou_threshold=eou,
        eou_timeout_ms=timeout_ms,
        silence_duration_ms=max(0, silence_ms),
        max_response_output_tokens=max_tokens,
        noise_reduction=noise,
        echo_cancellation=echo,
        tts_playback_buffer_ms=max(0, buffer_ms),
    )


def resolve_model_temperature() -> float:
    """Model sampling temperature (``VOICE_LIVE_MODEL_TEMPERATURE``, default 0.6).

    This is the LLM's *output* randomness, and is distinct from the voice/TTS
    temperature on ``AzureStandardVoice``. Voice Live's default is 0.7, which is
    hot enough that a small model invents plausible-but-false answers when the
    caller's input is short or garbled. Lowering it to 0.6 is the single biggest
    lever against fabrication. ``RequestSession.temperature`` accepts 0.0–1.0.
    """
    raw = os.getenv("VOICE_LIVE_MODEL_TEMPERATURE", "").strip()
    if not raw:
        return 0.6
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid VOICE_LIVE_MODEL_TEMPERATURE=%r — using 0.6", raw
        )
        return 0.6
    return max(0.0, min(1.0, value))


def build_request_session(
    options: VoiceLiveSessionOptions | None = None,
    *,
    model: str | None = None,
    instructions: str | None = None,
) -> RequestSession:
    """Return a Voice Live ``RequestSession`` using env-driven tuning.

    ``instructions`` overrides the default persona/system prompt when a non-empty
    string is supplied (the per-session custom prompt from the UI); otherwise the
    built-in lead-qualification persona is used.
    """
    opts = options or resolve_session_options()
    voice_model = (model or os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")).strip()
    system_prompt = (
        instructions.strip()
        if isinstance(instructions, str) and instructions.strip()
        else resolve_lead_qualification_instructions()
    )

    voice_kwargs: dict[str, Any] = {
        "name": resolve_agent_voice_name(),
        "temperature": resolve_agent_voice_temperature(),
    }
    style = resolve_agent_voice_style()
    rate = resolve_agent_voice_rate()
    pitch = resolve_agent_voice_pitch()
    volume = resolve_agent_voice_volume()
    if style:
        voice_kwargs["style"] = style
    if rate:
        voice_kwargs["rate"] = rate
    if pitch:
        voice_kwargs["pitch"] = pitch
    if volume:
        voice_kwargs["volume"] = volume

    # OpenAI realtime voices (alloy/coral/…) vs Azure neural/DragonHD.
    # VOICE_NAME selects which; DragonHD ignores style/rate for the most part.
    voice_name = resolve_agent_voice_name()
    if voice_name_is_openai(voice_name):
        session_voice: Any = OpenAIVoice(name=voice_name.lower())
    else:
        session_voice = AzureStandardVoice(**voice_kwargs)

    session_kwargs: dict[str, Any] = {
        "modalities": [Modality.TEXT, Modality.AUDIO],
        "instructions": system_prompt,
        "turn_detection": _build_turn_detection(opts, model=voice_model),
        "input_audio_format": InputAudioFormat.PCM16,
        "output_audio_format": OutputAudioFormat.PCM16,
        "voice": session_voice,
        # Model sampling temperature (anti-hallucination) — NOT the voice temp.
        "temperature": resolve_model_temperature(),
    }

    if opts.noise_reduction:
        session_kwargs["input_audio_noise_reduction"] = AudioNoiseReduction(
            type="azure_deep_noise_suppression"
        )
    if opts.echo_cancellation:
        session_kwargs["input_audio_echo_cancellation"] = AudioEchoCancellation()
    if opts.max_response_output_tokens is not None:
        session_kwargs["max_response_output_tokens"] = opts.max_response_output_tokens

    return RequestSession(**session_kwargs)


def log_session_options(options: VoiceLiveSessionOptions, *, model: str) -> None:
    """Log the active latency profile + effective voice once per call connect."""
    voice = describe_effective_voice()
    logger.info(
        "[VoiceLive] Latency profile=%s model=%s eou_mode=%s eou=%s "
        "eou_timeout_ms=%s silence_duration_ms=%s max_response_tokens=%s "
        "noise_reduction=%s echo_cancellation=%s tts_buffer_ms=%s "
        "model_temp=%s prompt_chars=%s",
        options.latency_mode,
        model.strip(),
        _resolve_eou_mode(model),
        options.eou_threshold,
        options.eou_timeout_ms,
        options.silence_duration_ms,
        options.max_response_output_tokens,
        options.noise_reduction,
        options.echo_cancellation,
        options.tts_playback_buffer_ms,
        resolve_model_temperature(),
        len(resolve_lead_qualification_instructions()),
    )
    logger.info(
        "[VoiceLive] EFFECTIVE VOICE name=%s source=%s openai=%s "
        "temp=%s rate=%s pitch=%s volume=%s style=%s "
        "(DragonHD mostly ignores style/rate — change VOICE_NAME to hear a new timbre)",
        voice["name"],
        voice["source"],
        voice["openai"],
        voice["temperature"],
        voice["rate"],
        voice["pitch"],
        voice["volume"],
        voice["style"],
    )
