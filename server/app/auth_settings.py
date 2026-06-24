"""Deployed web-login settings (hardcoded — env vars override for local dev only).

Production needs no Container App auth env vars. Redirect URIs are derived from
the incoming request host (HTTPS on Azure Container Apps).

Update this file when Entra credentials rotate. Do not commit if your org blocks
secrets in git — use env overrides locally instead.
"""

import os

# --- Microsoft Entra ID (MSAL) -----------------------------------------------
_AZURE_AD_TENANT_ID = "0eadb77e-42dc-47f8-bbe3-ec2395e0712c"
_AZURE_AD_CLIENT_ID = "b18779bb-e5ae-4571-9662-10b99df457e1"
_AZURE_AD_CLIENT_SECRET = "oXs8Q~yO-OX7VBTab_o52beZvzxbcyUTWMOkMbrt"

# --- Session + demo login ----------------------------------------------------
_SECRET_KEY = "qt-voice-agent-session-signing-key-v1"
_AUTH_DEMO_PASSWORD = "Demo@123"
_ALLOWED_EMAIL_DOMAIN = "quadranttechnologies.com"
_AUTH_MODE = "entra"
_AUTH_DEMO_LOGIN_ENABLED = True
_SESSION_TIMEOUT_MINUTES = 30
# Secure cookies on HTTPS (Container Apps). Set SESSION_COOKIE_SECURE=false in .env for local http.
_SESSION_COOKIE_SECURE = True


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


def azure_ad_tenant_id() -> str:
    return _env_str("AZURE_AD_TENANT_ID", _AZURE_AD_TENANT_ID)


def azure_ad_client_id() -> str:
    return _env_str("AZURE_AD_CLIENT_ID", _AZURE_AD_CLIENT_ID)


def azure_ad_client_secret() -> str:
    return _env_str("AZURE_AD_CLIENT_SECRET", _AZURE_AD_CLIENT_SECRET)


def secret_key() -> str:
    return _env_str("SECRET_KEY", _SECRET_KEY)


def auth_demo_password() -> str:
    return _env_str("AUTH_DEMO_PASSWORD", _AUTH_DEMO_PASSWORD)


def allowed_email_domain() -> str:
    return _env_str("ALLOWED_EMAIL_DOMAIN", _ALLOWED_EMAIL_DOMAIN).lower()


def auth_mode() -> str:
    mode = _env_str("AUTH_MODE", _AUTH_MODE).lower()
    return mode if mode in ("demo", "entra") else _AUTH_MODE


def demo_login_enabled() -> bool:
    return _env_bool("AUTH_DEMO_LOGIN_ENABLED", _AUTH_DEMO_LOGIN_ENABLED)


def session_timeout_minutes() -> int:
    raw = os.getenv("SESSION_TIMEOUT_MINUTES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _SESSION_TIMEOUT_MINUTES


def session_cookie_secure() -> bool:
    return _env_bool("SESSION_COOKIE_SECURE", _SESSION_COOKIE_SECURE)
