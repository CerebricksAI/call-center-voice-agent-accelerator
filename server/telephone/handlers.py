"""HTTP and WebSocket handlers for the isolated telephone integration."""

from __future__ import annotations

import asyncio
import logging

from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.logging_config import new_correlation_id
from telephone.config import get_telephone_config

logger = logging.getLogger(__name__)


def _handler_config(auth_token: str, base_config: dict | None = None) -> dict:
    """Build the config mapping Twilio handlers expect (local key, not an env var)."""
    config = dict(base_config or {})
    config["TWILIO_AUTH_TOKEN"] = auth_token
    return config


async def handle_telephone_voice(request) -> tuple[str, int, dict]:
    """Twilio voice webhook → TwiML that streams audio to /telephone/ws."""
    cfg = get_telephone_config()
    if cfg is None:
        return "Service Unavailable", 503, {}

    # Deferred import — same pattern as app.providers.twilio (SDK optional until used).
    from app.providers.twilio.event_handler import TwilioEventHandler

    if cfg.phone_number:
        logger.info("Telephone /voice webhook (number=%s)", cfg.phone_number)
    else:
        logger.info("Telephone /voice webhook called")

    handler = TwilioEventHandler(_handler_config(cfg.auth_token))
    signature = request.headers.get("X-Twilio-Signature", "")
    params = dict(await request.form) if request.method == "POST" else {}
    valid = handler.validate_request(request.url, params, signature)
    if valid is None:
        return "Service Unavailable", 503, {}
    if not valid:
        return "Forbidden", 403, {}

    host_url = request.host_url.replace("http://", "https://", 1).rstrip("/")
    ws_url = host_url.replace("https://", "wss://") + "/telephone/ws"
    twiml = handler.generate_stream_twiml(ws_url)
    return twiml, 200, {"Content-Type": "text/xml"}


async def handle_telephone_ws(websocket, call_manager: CallManager) -> None:
    """Twilio Media Stream WebSocket → Voice Live via run_call_loop."""
    cfg = get_telephone_config()
    if cfg is None:
        await websocket.close(4503, "Service Unavailable")
        return

    # Deferred import — same pattern as app.providers.twilio (SDK optional until used).
    from app.providers.twilio.media_handler import TwilioMediaHandler
    from quart import current_app

    cid = new_correlation_id()
    logger.info("Incoming telephone Media Stream WebSocket connection")

    handler = TwilioMediaHandler(_handler_config(cfg.auth_token, current_app.config))
    handler.twilio_ws = websocket
    handler.correlation_id = cid

    if not await handler.authenticate_and_start():
        return

    call_id = handler.stream_sid or cid
    if not await call_manager.acquire(call_id, "telephone"):
        await websocket.close(4429, "Too Many Connections")
        return

    try:
        await run_call_loop(
            call_manager=call_manager,
            call_id=call_id,
            ws=websocket,
            handler=handler,
        )
    except asyncio.CancelledError:
        logger.info("Telephone WebSocket cancelled")
    except Exception:
        logger.exception("Telephone WebSocket connection closed")
    finally:
        await call_manager.release(call_id)
        await handler.cleanup()
