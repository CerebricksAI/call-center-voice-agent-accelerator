"""Configuration for the isolated telephone (Twilio) integration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TelephoneConfig:
    """Settings loaded from TELEPHONE_TWILIO_* env vars (not TWILIO_AUTH_TOKEN)."""

    auth_token: str
    phone_number: str | None = None


def _env_value(name: str) -> str | None:
    """Return a real env value, or None if missing/blank/placeholder like <...>."""
    raw = os.getenv(name, "").strip()
    if not raw or (raw.startswith("<") and raw.endswith(">")):
        return None
    return raw


def get_telephone_config() -> TelephoneConfig | None:
    """Return config if TELEPHONE_TWILIO_AUTH_TOKEN is set; otherwise None (feature off)."""
    auth_token = _env_value("TELEPHONE_TWILIO_AUTH_TOKEN")
    if auth_token is None:
        return None
    return TelephoneConfig(
        auth_token=auth_token,
        phone_number=_env_value("TELEPHONE_TWILIO_PHONE_NUMBER"),
    )
