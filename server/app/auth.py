"""Demo login gate for the web client (@quadranttechnologies.com emails)."""

import os
import time

ALLOWED_EMAIL_DOMAIN = "quadranttechnologies.com"
DEFAULT_DEMO_PASSWORD = "Demo@123"

PUBLIC_EXACT_PATHS = frozenset({"/health", "/login", "/favicon.ico"})
PUBLIC_STATIC_PATHS = frozenset({"/static/quadrant-logo.png"})
PUBLIC_PREFIX_PATHS = (
    "/auth/",
    "/acs/",
    "/infobip/",
    "/genesys",
    "/voice",
)


def session_timeout_seconds() -> int:
    minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    return max(1, minutes) * 60


def demo_password() -> str:
    return os.getenv("AUTH_DEMO_PASSWORD", DEFAULT_DEMO_PASSWORD)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_allowed_email(email: str) -> bool:
    normalized = normalize_email(email)
    return normalized.endswith(f"@{ALLOWED_EMAIL_DOMAIN}") and "@" in normalized


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


def establish_session(session, email: str) -> None:
    session["authenticated"] = True
    session["email"] = normalize_email(email)
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
        "expiresAt": session.get("expires_at"),
        "timeoutMinutes": session_timeout_seconds() // 60,
    }
