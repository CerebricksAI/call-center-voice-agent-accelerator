import asyncio
import logging
import os
import secrets
from datetime import timedelta
from urllib.parse import quote

from dotenv import load_dotenv
from quart import Quart, jsonify, redirect, request, session, websocket

from app.auth import (
    auth_config_payload,
    auth_mode,
    clear_session,
    demo_login_enabled,
    establish_session,
    is_allowed_email,
    is_public_path,
    is_session_valid,
    session_payload,
    touch_session,
    verify_credentials,
)
from app.msal_auth import (
    EntraAuthError,
    build_auth_url,
    build_logout_url,
    exchange_code,
    is_entra_configured,
    resolve_redirect_uri,
    user_from_token_result,
)

from app.auth_settings import secret_key, session_cookie_secure, session_timeout_minutes
from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.analytics import compute_analytics
from app.call_store import get_call, is_enabled as cosmos_enabled, list_calls
from app.config_validator import validate_config
from app.handler.web_media_handler import WebMediaHandler
from app.logging_config import configure_logging, new_correlation_id
from app.conversation_extractor import resolve_extract_model, resolve_summary_model
from app.usage_cost import enrich_call_record
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
app.config["SECRET_KEY"] = secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=session_timeout_minutes())
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if session_cookie_secure():
    app.config["SESSION_COOKIE_SECURE"] = True
app.config["AZURE_VOICE_LIVE_API_KEY"] = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = os.getenv("AZURE_VOICE_LIVE_ENDPOINT")
app.config["VOICE_LIVE_MODEL"] = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
app.config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"] = os.getenv(
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", ""
)
app.config["AMBIENT_PRESET"] = os.getenv("AMBIENT_PRESET", "none")

# Log ambient configuration on startup
ambient_preset = app.config["AMBIENT_PRESET"]
latency_mode = os.getenv("VOICE_LIVE_LATENCY_MODE", "default").strip().lower()
if ambient_preset and ambient_preset != "none":
    logger.info("Ambient scenes ENABLED: preset='%s'", ambient_preset)
else:
    logger.info("Ambient scenes DISABLED (preset=none)")
logger.info("Voice Live latency profile: %s", latency_mode)
logger.info(
    "Voice Live models: voice=%s extract=%s summary=%s transcribe=%s",
    os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip(),
    resolve_extract_model(),
    resolve_summary_model(),
    os.getenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1").strip(),
)


def _resolved_models() -> dict[str, str]:
    """Active model names for UI labels (matches handler / extractor defaults)."""
    voice = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()
    transcribe = os.getenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1").strip()
    extract = resolve_extract_model()
    summary = resolve_summary_model()
    return {
        "voiceModel": voice,
        "transcriptionModel": transcribe,
        "extractModel": extract,
        "summaryModel": summary,
        "serviceName": os.getenv("AZD_SERVICE_NAME", "app").strip() or "app",
        "containerAppName": os.getenv("CONTAINER_APP_NAME", "").strip(),
        "selectableModels": _selectable_models(),
        "defaultVoiceModel": voice,
    }


def _selectable_models() -> list[str]:
    """Voice models the user may pick in the UI (allow-list).

    From VOICE_LIVE_SELECTABLE_MODELS (comma-separated); defaults to the env
    voice model plus the two we support (gpt-4o-mini, gpt-realtime-mini).
    """
    raw = os.getenv("VOICE_LIVE_SELECTABLE_MODELS", "").strip()
    models: list[str] = []
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
    if not models:
        default = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()
        models = [default]
        for m in ("gpt-4o-mini", "gpt-realtime-mini"):
            if m not in models:
                models.append(m)
    return models


def _resolve_voice_model(requested: str | None) -> str:
    """Return the requested voice model if it is allow-listed, else the env default."""
    default = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini").strip()
    if requested and requested.strip():
        candidate = requested.strip()
        if candidate in _selectable_models():
            return candidate
        logger.warning(
            "Rejected unsupported voice model request %r — using default %r",
            candidate,
            default,
        )
    return default

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

logger.info("Auth mode: %s (entra configured=%s)", auth_mode(), is_entra_configured())


def _request_app_base() -> str:
    """Public URL base (HTTPS behind Container Apps ingress)."""
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host
    return f"{proto}://{host}".rstrip("/")


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


@app.route("/auth/config")
async def auth_config():
    """Tell the login page which sign-in method is active."""
    return jsonify(auth_config_payload()), 200


@app.route("/auth/microsoft")
async def auth_microsoft():
    """Start Microsoft Entra sign-in (authorization code redirect)."""
    if auth_mode() != "entra":
        return redirect("/login")
    if not is_entra_configured():
        return redirect("/login?error=entra_not_configured")

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    app_base = _request_app_base()
    callback_uri = resolve_redirect_uri(app_base)
    if not callback_uri:
        return redirect("/login?error=entra_not_configured")
    session["oauth_redirect_uri"] = callback_uri
    try:
        return redirect(build_auth_url(state, redirect_uri_value=callback_uri))
    except EntraAuthError as exc:
        logger.exception("[Auth] Failed to build Entra login URL")
        return redirect(f"/login?error={quote(str(exc))}")


@app.route("/auth/callback")
async def auth_callback():
    """Entra OAuth redirect target — exchange code and establish session."""
    oauth_error = request.args.get("error")
    if oauth_error:
        description = request.args.get("error_description") or oauth_error
        logger.warning("[Auth] Entra OAuth error: %s", description)
        return redirect(f"/login?error={quote(description)}")

    state = request.args.get("state", "")
    expected = session.pop("oauth_state", None)
    if not expected or state != expected:
        logger.warning("[Auth] Entra callback state mismatch")
        return redirect("/login?error=invalid_state")

    code = request.args.get("code", "")
    if not code:
        return redirect("/login?error=missing_code")

    try:
        callback_uri = session.pop("oauth_redirect_uri", None) or resolve_redirect_uri(
            _request_app_base()
        )
        token_result = exchange_code(code, redirect_uri_value=callback_uri)
        user = user_from_token_result(token_result)
    except EntraAuthError:
        logger.exception("[Auth] Entra token exchange failed")
        return redirect("/login?error=token_exchange_failed")

    email = user.get("email", "")
    if not email or not is_allowed_email(email):
        logger.warning("[Auth] Entra sign-in rejected for email domain: %s", email)
        return redirect("/login?error=domain_not_allowed")

    establish_session(
        session,
        email,
        name=user.get("name", ""),
        oid=user.get("oid", ""),
        provider="entra",
    )
    logger.info("[Auth] Entra user signed in: %s", session.get("email"))
    return redirect("/")


@app.route("/auth/login", methods=["POST"])
async def auth_login():
    """Validate demo credentials and start a session (works alongside Entra)."""
    if not demo_login_enabled():
        return jsonify({"error": "Email/password sign-in is disabled."}), 403

    payload = await request.get_json(silent=True) or {}
    email = payload.get("email") or (await request.form).get("email", "")
    password = payload.get("password") or (await request.form).get("password", "")

    if not verify_credentials(str(email), str(password)):
        return jsonify({"error": "Invalid email or password."}), 401

    establish_session(session, str(email), provider="demo")
    logger.info("[Auth] Demo user signed in: %s", session.get("email"))
    return jsonify(session_payload(session)), 200


@app.route("/auth/logout", methods=["POST"])
async def auth_logout():
    """End the current session."""
    email = session.get("email")
    provider = session.get("auth_provider")
    clear_session(session)
    if email:
        logger.info("[Auth] User signed out: %s", email)

    payload = {"authenticated": False}
    if provider == "entra":
        logout_url = build_logout_url(app_base=_request_app_base())
        if logout_url:
            payload["logoutUrl"] = logout_url
    return jsonify(payload), 200


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
    cid = new_correlation_id()
    logger.info("Incoming Web WebSocket connection")

    call_id = cid
    if not await call_manager.acquire(call_id, "web"):
        await websocket.close(4429, "Too Many Connections")
        return

    voice_model = _resolve_voice_model(websocket.args.get("model"))
    logger.info("Web call using voice model: %s", voice_model)
    handler = WebMediaHandler(app.config, voice_model=voice_model)
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


@app.route("/history")
async def history_page():
    """Past call history (loaded from Cosmos DB)."""
    response = await app.send_static_file("history.html")
    response.cache_control.no_store = True
    return response


@app.route("/analytics")
async def analytics_page():
    """Aggregate call analytics dashboards (business + AI performance)."""
    response = await app.send_static_file("analytics.html")
    response.cache_control.no_store = True
    return response


@app.route("/api/calls")
async def api_list_calls():
    """GET recent call records from Cosmos DB."""
    if not cosmos_enabled():
        return jsonify(
            {
                "enabled": False,
                "calls": [],
                "message": "Cosmos DB is not configured. Set COSMOS_ENDPOINT to enable call history.",
            }
        ), 200

    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "Invalid limit or offset."}), 400

    calls = await list_calls(limit=limit, offset=offset)
    for i, call in enumerate(calls):
        calls[i] = enrich_call_record(call, include_timeline=False)
    return jsonify({"enabled": True, "calls": calls, "limit": limit, "offset": offset}), 200


@app.route("/api/calls/<call_id>")
async def api_get_call(call_id: str):
    """GET one full call document from Cosmos DB."""
    if not cosmos_enabled():
        return jsonify(
            {
                "enabled": False,
                "error": "Cosmos DB is not configured. Set COSMOS_ENDPOINT to enable call history.",
            }
        ), 503

    record = await get_call(call_id)
    if record is None:
        return jsonify({"error": "Call not found."}), 404
    return jsonify(enrich_call_record(record)), 200


@app.route("/api/analytics")
async def api_analytics():
    """GET aggregate analytics computed from saved Cosmos call records."""
    if not cosmos_enabled():
        return jsonify({"enabled": False, "hasData": False}), 200
    range_key = request.args.get("range", "7d")
    try:
        payload = await compute_analytics(range_key)
    except Exception:
        logger.exception("[Analytics] aggregation failed")
        return jsonify({"enabled": True, "hasData": False, "error": "aggregation_failed"}), 200
    return jsonify(payload), 200


@app.route("/api/models")
async def api_models():
    """GET configured LLM model names for dashboard labels."""
    return jsonify(_resolved_models()), 200


@app.route("/health")
async def health():
    """Liveness/readiness probe endpoint."""
    return {"status": "healthy"}, 200


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "8000"))
    app.run(debug=_debug, host="0.0.0.0", port=_port)
