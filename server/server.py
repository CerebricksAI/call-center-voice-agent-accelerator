import asyncio
import logging
import os
from datetime import timedelta

from dotenv import load_dotenv
from quart import Quart, jsonify, redirect, request, session, websocket

from app.auth import (
    clear_session,
    establish_session,
    is_public_path,
    is_session_valid,
    session_payload,
    touch_session,
    verify_credentials,
)

from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.config_validator import validate_config
from app.handler.web_media_handler import WebMediaHandler
from app.logging_config import configure_logging, new_correlation_id
from app.provider_registry import detect_provider, get_configured_providers, get_provider

load_dotenv()

# ---------------------------------------------------------------------------
# Structured logging (with correlation ID support)
# ---------------------------------------------------------------------------

_debug = os.getenv("DEBUG_MODE", "false").lower() == "true"
configure_logging(level=logging.DEBUG if _debug else logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Quart(__name__)
app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "dev-only-change-me-in-production"
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    minutes=int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["AZURE_VOICE_LIVE_API_KEY"] = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = os.getenv("AZURE_VOICE_LIVE_ENDPOINT")
app.config["VOICE_LIVE_MODEL"] = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
app.config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"] = os.getenv(
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", ""
)
app.config["AMBIENT_PRESET"] = os.getenv("AMBIENT_PRESET", "none")

# Log ambient configuration on startup
ambient_preset = app.config["AMBIENT_PRESET"]
if ambient_preset and ambient_preset != "none":
    logger.info("Ambient scenes ENABLED: preset='%s'", ambient_preset)
else:
    logger.info("Ambient scenes DISABLED (preset=none)")

# ---------------------------------------------------------------------------
# Call manager (concurrency limits + timeout enforcement)
# ---------------------------------------------------------------------------

call_manager = CallManager(
    max_concurrent=int(os.getenv("MAX_CONCURRENT_CALLS", "50")),
    max_duration=int(os.getenv("MAX_CALL_DURATION", "3600")),
    idle_timeout=int(os.getenv("CALL_IDLE_TIMEOUT", "120")),
)

# ---------------------------------------------------------------------------
# Telephony provider detection and validation
# ---------------------------------------------------------------------------

# Import all provider packages to populate the detection registry.
# Only lightweight registration happens here (no heavy SDK imports until
# register_routes is called — each provider defers handler imports).
import importlib
import pkgutil

_providers_pkg = importlib.import_module("app.providers")
for _finder, _mod_name, _ispkg in pkgutil.iter_modules(_providers_pkg.__path__):
    if _ispkg:  # each provider is a package (folder with __init__.py)
        try:
            importlib.import_module(f"app.providers.{_mod_name}")
        except ImportError as e:
            logger.debug("Skipping provider %s: %s", _mod_name, e)

_telephony_client = detect_provider()

# Warn if multiple telephony credentials are set — only one can be active
_configured_providers = get_configured_providers()
if len(_configured_providers) > 1:
    logger.warning(
        "Multiple telephony credentials detected: %s. Using '%s' (first match). "
        "Remove unused credentials to avoid confusion.",
        ", ".join(_configured_providers),
        _telephony_client,
    )

if _telephony_client:
    logger.info("Telephony provider: %s", _telephony_client)
else:
    logger.info("No telephony provider credentials found")

# Validate configuration — returns False if telephony provider is misconfigured
_provider_ready = validate_config(app.config, _telephony_client)

# Register telephony routes only if the provider is fully configured
_provider_info = get_provider(_telephony_client) if _telephony_client else None
if _provider_ready and _provider_info:
    _provider_info.register_routes(app, call_manager)
    logger.info("Registered routes for provider: %s", _provider_info.display_name)
else:
    logger.warning("Telephony routes not registered — only web client available")

# ---------------------------------------------------------------------------
# Auth middleware (web client + static assets; telephony webhooks stay public)
# ---------------------------------------------------------------------------


@app.before_request
async def require_login():
    path = request.path
    if is_public_path(path):
        return None
    if not is_session_valid(session):
        clear_session(session)
        if path.startswith("/auth/"):
            return None
        accept = request.headers.get("Accept", "")
        if "application/json" in accept or path.startswith("/web/"):
            return jsonify({"error": "Session expired. Please sign in again."}), 401
        return redirect("/login")
    touch_session(session)
    return None


# ---------------------------------------------------------------------------
# Routes: Web client (always available)
# ---------------------------------------------------------------------------


@app.route("/login")
async def login_page():
    """Serve the sign-in page."""
    response = await app.send_static_file("login.html")
    response.cache_control.no_store = True
    return response


@app.route("/auth/login", methods=["POST"])
async def auth_login():
    """Validate demo credentials and start a session."""
    payload = await request.get_json(silent=True) or {}
    email = payload.get("email") or (await request.form).get("email", "")
    password = payload.get("password") or (await request.form).get("password", "")

    if not verify_credentials(str(email), str(password)):
        return jsonify({"error": "Invalid email or password."}), 401

    establish_session(session, str(email))
    logger.info("[Auth] User signed in: %s", session.get("email"))
    return jsonify(session_payload(session)), 200


@app.route("/auth/logout", methods=["POST"])
async def auth_logout():
    """End the current session."""
    email = session.get("email")
    clear_session(session)
    if email:
        logger.info("[Auth] User signed out: %s", email)
    return jsonify({"authenticated": False}), 200


@app.route("/auth/session")
async def auth_session():
    """Return current session status (used by the SPA and login redirect)."""
    if not is_session_valid(session):
        clear_session(session)
        return jsonify({"authenticated": False}), 401
    touch_session(session)
    return jsonify(session_payload(session)), 200


@app.websocket("/web/ws")
async def web_ws():
    """WebSocket endpoint for web clients to send audio to Voice Live."""
    if not is_session_valid(session):
        await websocket.close(4401, "Unauthorized")
        return
    touch_session(session)
    cid = new_correlation_id()
    logger.info("Incoming Web WebSocket connection")

    call_id = cid
    if not await call_manager.acquire(call_id, "web"):
        await websocket.close(4429, "Too Many Connections")
        return

    handler = WebMediaHandler(app.config)
    await handler.init_websocket(websocket)
    handler.set_call_context(call_id, "web")
    try:
        await run_call_loop(
            call_manager=call_manager,
            call_id=call_id,
            ws=websocket,
            handler=handler,
        )
    except asyncio.CancelledError:
        logger.info("Web WebSocket cancelled")
    except Exception:
        logger.exception("Web WebSocket connection closed")
    finally:
        await call_manager.release(call_id)
        await handler.cleanup()


@app.route("/")
async def index():
    """Serves the static index page."""
    response = await app.send_static_file("index.html")
    response.cache_control.no_store = True
    return response


@app.route("/health")
async def health():
    """Liveness/readiness probe endpoint."""
    return {"status": "healthy"}, 200


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "8000"))
    app.run(debug=_debug, host="0.0.0.0", port=_port)
