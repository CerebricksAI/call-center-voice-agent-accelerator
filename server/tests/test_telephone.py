"""Unit tests for the isolated telephone (Twilio) integration."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/ on path

from telephone.config import TelephoneConfig, get_telephone_config  # noqa: E402
from telephone.handlers import (  # noqa: E402
    _handler_config,
    handle_telephone_voice,
    handle_telephone_ws,
)


@pytest.fixture(autouse=True)
def _clear_telephone_env(monkeypatch):
    """Keep telephone env isolated from the developer's real .env."""
    monkeypatch.delenv("TELEPHONE_TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TELEPHONE_TWILIO_PHONE_NUMBER", raising=False)


def test_get_telephone_config_unset_returns_none():
    assert get_telephone_config() is None


def test_get_telephone_config_placeholder_returns_none(monkeypatch):
    monkeypatch.setenv(
        "TELEPHONE_TWILIO_AUTH_TOKEN",
        "<Twilio Auth Token for the phone number>",
    )
    assert get_telephone_config() is None


def test_get_telephone_config_blank_returns_none(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "   ")
    assert get_telephone_config() is None


def test_get_telephone_config_set_returns_dataclass(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("TELEPHONE_TWILIO_PHONE_NUMBER", "+15715209316")
    cfg = get_telephone_config()
    assert isinstance(cfg, TelephoneConfig)
    assert cfg.auth_token == "secret-token"
    assert cfg.phone_number == "+15715209316"


def test_get_telephone_config_phone_optional(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")
    cfg = get_telephone_config()
    assert cfg is not None
    assert cfg.phone_number is None


def test_handler_config_uses_local_dict_key_not_env(monkeypatch):
    """Dict key TWILIO_AUTH_TOKEN must not require that env var to be set."""
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    cfg = _handler_config("from-telephone-env", {"AZURE_VOICE_LIVE_API_KEY": "k"})
    assert cfg["TWILIO_AUTH_TOKEN"] == "from-telephone-env"
    assert cfg["AZURE_VOICE_LIVE_API_KEY"] == "k"
    assert os.getenv("TWILIO_AUTH_TOKEN") in (None, "")


def test_telephone_env_does_not_set_provider_detect_key(monkeypatch):
    """TELEPHONE_TWILIO_AUTH_TOKEN alone must not look like TWILIO_AUTH_TOKEN."""
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    assert get_telephone_config() is not None
    assert not os.getenv("TWILIO_AUTH_TOKEN")


class _AwaitableMapping:
    """Minimal awaitable that behaves like Quart's ``request.form``."""

    def __init__(self, data: dict):
        self._data = data

    def __await__(self):
        async def _coro():
            return self._data

        return _coro().__await__()


def _mock_request(*, method="POST", url="https://example.test/telephone/voice", form=None):
    req = MagicMock()
    req.method = method
    req.url = url
    req.host_url = "https://example.test/"
    req.headers = {"X-Twilio-Signature": "sig"}
    req.form = _AwaitableMapping(form or {"CallSid": "CA123"})
    return req


def test_handle_telephone_voice_unavailable_without_config():
    body, status, headers = asyncio.run(handle_telephone_voice(_mock_request()))
    assert status == 503
    assert body == "Service Unavailable"
    assert headers == {}


def test_handle_telephone_voice_forbidden_on_bad_signature(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    mock_handler = MagicMock()
    mock_handler.validate_request.return_value = False

    with patch(
        "app.providers.twilio.event_handler.TwilioEventHandler",
        return_value=mock_handler,
    ):
        body, status, headers = asyncio.run(handle_telephone_voice(_mock_request()))

    assert status == 403
    assert body == "Forbidden"
    mock_handler.validate_request.assert_called_once()


def test_handle_telephone_voice_503_when_validation_unavailable(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    mock_handler = MagicMock()
    mock_handler.validate_request.return_value = None

    with patch(
        "app.providers.twilio.event_handler.TwilioEventHandler",
        return_value=mock_handler,
    ):
        body, status, _headers = asyncio.run(handle_telephone_voice(_mock_request()))

    assert status == 503
    assert body == "Service Unavailable"


def test_handle_telephone_voice_returns_twiml_with_telephone_ws(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    mock_handler = MagicMock()
    mock_handler.validate_request.return_value = True
    mock_handler.generate_stream_twiml.return_value = (
        '<?xml version="1.0"?><Response><Connect><Stream '
        'url="wss://example.test/telephone/ws"/></Connect></Response>'
    )

    with patch(
        "app.providers.twilio.event_handler.TwilioEventHandler",
        return_value=mock_handler,
    ):
        body, status, headers = asyncio.run(handle_telephone_voice(_mock_request()))

    assert status == 200
    assert headers == {"Content-Type": "text/xml"}
    assert "/telephone/ws" in body
    mock_handler.generate_stream_twiml.assert_called_once_with(
        "wss://example.test/telephone/ws"
    )


def test_handle_telephone_voice_rewrites_http_host_to_wss(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    mock_handler = MagicMock()
    mock_handler.validate_request.return_value = True
    mock_handler.generate_stream_twiml.return_value = "<Response/>"

    req = _mock_request()
    req.host_url = "http://example.test/"

    with patch(
        "app.providers.twilio.event_handler.TwilioEventHandler",
        return_value=mock_handler,
    ):
        asyncio.run(handle_telephone_voice(req))

    mock_handler.generate_stream_twiml.assert_called_once_with(
        "wss://example.test/telephone/ws"
    )


def test_handle_telephone_ws_closes_when_unconfigured():
    ws = AsyncMock()
    call_manager = AsyncMock()

    asyncio.run(handle_telephone_ws(ws, call_manager))

    ws.close.assert_awaited_once_with(4503, "Service Unavailable")
    call_manager.acquire.assert_not_called()


def test_handle_telephone_ws_releases_on_success(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    ws = AsyncMock()
    call_manager = AsyncMock()
    call_manager.acquire = AsyncMock(return_value=True)
    call_manager.release = AsyncMock()

    mock_media = MagicMock()
    mock_media.authenticate_and_start = AsyncMock(return_value=True)
    mock_media.stream_sid = "MZ_stream"
    mock_media.cleanup = AsyncMock()

    from quart import Quart

    app = Quart(__name__)
    app.config["AZURE_VOICE_LIVE_API_KEY"] = "k"

    async def _run():
        async with app.app_context():
            with (
                patch(
                    "app.providers.twilio.media_handler.TwilioMediaHandler",
                    return_value=mock_media,
                ),
                patch(
                    "telephone.handlers.run_call_loop", new_callable=AsyncMock
                ) as run_loop,
                patch("telephone.handlers.new_correlation_id", return_value="cid-1"),
            ):
                await handle_telephone_ws(ws, call_manager)
                return run_loop

    run_loop = asyncio.run(_run())

    call_manager.acquire.assert_awaited_once_with("MZ_stream", "telephone")
    run_loop.assert_awaited_once()
    call_manager.release.assert_awaited_once_with("MZ_stream")
    mock_media.cleanup.assert_awaited_once()


def test_handle_telephone_ws_too_many_connections(monkeypatch):
    monkeypatch.setenv("TELEPHONE_TWILIO_AUTH_TOKEN", "secret-token")

    ws = AsyncMock()
    call_manager = AsyncMock()
    call_manager.acquire = AsyncMock(return_value=False)

    mock_media = MagicMock()
    mock_media.authenticate_and_start = AsyncMock(return_value=True)
    mock_media.stream_sid = "MZ_stream"
    mock_media.cleanup = AsyncMock()

    from quart import Quart

    app = Quart(__name__)

    async def _run():
        async with app.app_context():
            with (
                patch(
                    "app.providers.twilio.media_handler.TwilioMediaHandler",
                    return_value=mock_media,
                ),
                patch(
                    "telephone.handlers.run_call_loop", new_callable=AsyncMock
                ) as run_loop,
            ):
                await handle_telephone_ws(ws, call_manager)
                return run_loop

    run_loop = asyncio.run(_run())

    ws.close.assert_awaited_once_with(4429, "Too Many Connections")
    run_loop.assert_not_called()
    call_manager.release.assert_not_called()
