"""Microsoft Entra ID login via MSAL (authorization code flow, confidential client)."""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any

import msal

from app.auth_settings import (
    azure_ad_client_id,
    azure_ad_client_secret,
    azure_ad_tenant_id,
)

logger = logging.getLogger(__name__)

_SCOPES = ["User.Read"]


class EntraAuthError(Exception):
    """Raised when Entra token exchange or configuration fails."""


def is_entra_configured() -> bool:
    return bool(azure_ad_client_id() and azure_ad_tenant_id() and azure_ad_client_secret())


def authority() -> str:
    return f"https://login.microsoftonline.com/{azure_ad_tenant_id()}"


def resolve_redirect_uri(app_base: str = "") -> str:
    """Callback URL — env override, else derived from the request base URL."""
    explicit = os.getenv("AZURE_AD_REDIRECT_URI", "").strip().rstrip("/")
    if explicit:
        return explicit
    base = (app_base or os.getenv("APP_BASE_URL", "")).strip().rstrip("/")
    if base:
        return f"{base}/auth/callback"
    return ""


def resolve_post_logout_redirect_uri(app_base: str = "") -> str:
    explicit = os.getenv("AZURE_AD_POST_LOGOUT_REDIRECT_URI", "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = (app_base or os.getenv("APP_BASE_URL", "")).strip().rstrip("/")
    if base:
        return f"{base}/login"
    callback = resolve_redirect_uri(app_base)
    if callback.endswith("/auth/callback"):
        return callback[: -len("/auth/callback")] + "/login"
    return "/login"


def _client() -> msal.ConfidentialClientApplication:
    client_id = azure_ad_client_id()
    secret = azure_ad_client_secret()
    if not client_id or not secret:
        raise EntraAuthError("Entra client credentials are not configured.")
    return msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=secret,
        authority=authority(),
    )


def build_auth_url(state: str, *, redirect_uri_value: str) -> str:
    uri = (redirect_uri_value or "").strip().rstrip("/")
    if not uri:
        raise EntraAuthError("Redirect URI is not configured.")
    return _client().get_authorization_request_url(
        scopes=_SCOPES,
        state=state,
        redirect_uri=uri,
        prompt="select_account",
    )


def exchange_code(code: str, *, redirect_uri_value: str) -> dict[str, Any]:
    uri = (redirect_uri_value or "").strip().rstrip("/")
    if not uri:
        raise EntraAuthError("Redirect URI is not configured.")
    result = _client().acquire_token_by_authorization_code(
        code,
        scopes=_SCOPES,
        redirect_uri=uri,
    )
    if not result or "error" in result:
        message = (result or {}).get("error_description") or (result or {}).get(
            "error", "token_exchange_failed"
        )
        logger.warning("[Auth] Entra token exchange failed: %s", message)
        raise EntraAuthError(str(message))
    return result


def user_from_token_result(result: dict[str, Any]) -> dict[str, str]:
    claims = result.get("id_token_claims") or {}
    email = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    )
    return {
        "email": str(email).strip(),
        "name": str(claims.get("name") or "").strip(),
        "oid": str(claims.get("oid") or claims.get("sub") or "").strip(),
    }


def build_logout_url(*, app_base: str = "") -> str | None:
    if not is_entra_configured():
        return None
    post_logout = resolve_post_logout_redirect_uri(app_base)
    params = urllib.parse.urlencode({"post_logout_redirect_uri": post_logout})
    return f"{authority()}/oauth2/v2.0/logout?{params}"
