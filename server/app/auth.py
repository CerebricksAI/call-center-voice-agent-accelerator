"""Web client authentication — demo gate or Microsoft Entra ID (MSAL)."""

import time

from app.auth_settings import (
    allowed_email_domain,
    auth_demo_password,
    auth_mode as configured_auth_mode,
    demo_login_enabled as configured_demo_login_enabled,
    session_timeout_minutes,
)
from app.msal_auth import is_entra_configured

PUBLIC_EXACT_PATHS = frozenset({"/health", "/login", "/favicon.ico"})
PUBLIC_STATIC_PATHS = frozenset({"/static/sales-agent-logo.png", "/static/quadrant-logo.png"})
PUBLIC_PREFIX_PATHS = (
    "/auth/",
    "/acs/",
    "/infobip/",
    "/genesys",
    "/voice",
)


def auth_mode() -> str:
    """Return ``demo`` or ``entra``."""
    mode = configured_auth_mode()
    if mode == "demo":
        return "demo"
    return "entra" if is_entra_configured() else "demo"


def session_timeout_seconds() -> int:
    return session_timeout_minutes() * 60


def demo_login_enabled() -> bool:
    return configured_demo_login_enabled()


def demo_password() -> str:
    return auth_demo_password()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_allowed_email(email: str) -> bool:
    normalized = normalize_email(email)
    domain = allowed_email_domain()
    if not normalized or "@" not in normalized:
        return False
    if not domain:
        return True
    return normalized.endswith(f"@{domain}")


def verify_credentials(email: str, password: str) -> bool:
    return is_allowed_email(email) and password == demo_password()


def is_public_path(path: str) -> bool:
    if path in PUBLIC_EXACT_PATHS:
        return True
    if path in PUBLIC_STATIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIX_PATHS)


def is_session_valid(session) -> bool:
    if not session.get("authenticated"):
        return False
    expires_at = session.get("expires_at")
    if expires_at is None:
        return False
    return time.time() < float(expires_at)


def establish_session(
    session,
    email: str,
    *,
    name: str = "",
    oid: str = "",
    provider: str = "demo",
) -> None:
    session["authenticated"] = True
    session["email"] = normalize_email(email)
    session["name"] = (name or "").strip()
    session["oid"] = (oid or "").strip()
    session["auth_provider"] = provider
    session["expires_at"] = time.time() + session_timeout_seconds()
    session.permanent = True


def touch_session(session) -> None:
    if session.get("authenticated"):
        session["expires_at"] = time.time() + session_timeout_seconds()


def clear_session(session) -> None:
    session.clear()


def session_payload(session) -> dict:
    return {
        "authenticated": is_session_valid(session),
        "email": session.get("email"),
        "name": session.get("name"),
        "authProvider": session.get("auth_provider"),
        "expiresAt": session.get("expires_at"),
        "timeoutMinutes": session_timeout_seconds() // 60,
        "authMode": auth_mode(),
    }


def auth_config_payload() -> dict:
    return {
        "mode": auth_mode(),
        "allowedEmailDomain": allowed_email_domain(),
        "entraConfigured": is_entra_configured(),
        "demoLoginEnabled": demo_login_enabled(),
    }
