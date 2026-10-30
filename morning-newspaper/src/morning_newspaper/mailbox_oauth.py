"""Microsoft OAuth2 (XOAUTH2) support for IMAP collection.

Outlook.com personal accounts refuse Basic authentication on IMAP/POP3
(``AUTH=XOAUTH2 LOGINDISABLED``), so the only machine-to-machine path left
is OAuth2 with a device-code grant. This module owns the token lifecycle:

1. ``request_device_code`` / ``poll_for_token`` -- interactive sign-in that
   produces a long-lived refresh token.
2. ``refresh_access_token`` -- turn that refresh token into a short-lived
   access token (and keep it fresh).
3. ``build_xoauth2_payload`` -- the SASL string ``imaplib.authenticate`` wants.

Only ``requests`` is required, which the project already depends on.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any, Dict, Optional, Tuple

import requests


# Tenant endpoints. "common" serves both work/school and personal Microsoft accounts;
# "consumers" serves personal Microsoft accounts only. Apps registered for personal
# accounts ONLY reject "common" with AADSTS9002346 ("Please use the /consumers endpoint"),
# so the endpoint must be switchable rather than hardcoded.
TENANT_COMMON = "common"
TENANT_CONSUMERS = "consumers"
AUTH_BASE_URL = "https://login.microsoftonline.com"

REFRESH_TOKEN_ENV = "MS_OAUTH_REFRESH_TOKEN"
CLIENT_ID_ENV = "MS_OAUTH_CLIENT_ID"
SCOPE_ENV = "MS_OAUTH_SCOPE"
TENANT_ENV = "MS_OAUTH_TENANT"

# src/morning_newspaper/mailbox_oauth.py -> project root
PROJECT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# The Azure CLI public client id is a Microsoft *first-party* application, so users
# are not allowed to consent to it ("invalid_request: users are not permitted to
# consent to first party applications"). A self-registered Azure AD app is required.
# Keep the Azure CLI id only for reference.
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
DEFAULT_CLIENT_ID = ""
DEFAULT_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# MailFlow's registered public client for personal Microsoft accounts. Third-party
# (not first-party), so users CAN consent to it. Registered for Microsoft accounts
# only, which is why it must be paired with the /consumers endpoint.
MAILFLOW_MS_CLIENT_ID = "60d3c6f7-c801-4f86-907d-fcd3c3a1507d"


def _client_id(client_id: str = "") -> str:
    resolved = (client_id or os.getenv(CLIENT_ID_ENV) or DEFAULT_CLIENT_ID).strip()
    if not resolved:
        raise RuntimeError(
            "no OAuth client id configured. Pass --client-id or set "
            f"{CLIENT_ID_ENV}. See scripts/microsoft_oauth_login.py --help."
        )
    return resolved


def _scope(scope: str = "") -> str:
    return (scope or os.getenv(SCOPE_ENV) or DEFAULT_SCOPE).strip()


def _tenant(tenant: str = "") -> str:
    resolved = (tenant or os.getenv(TENANT_ENV) or TENANT_COMMON).strip().lower()
    if resolved not in (TENANT_COMMON, TENANT_CONSUMERS):
        raise RuntimeError(f"unsupported tenant endpoint: {resolved!r}")
    return resolved


def _device_code_url(tenant: str = "") -> str:
    return f"{AUTH_BASE_URL}/{_tenant(tenant)}/oauth2/v2.0/devicecode"


def _token_url(tenant: str = "") -> str:
    return f"{AUTH_BASE_URL}/{_tenant(tenant)}/oauth2/v2.0/token"


def request_device_code(
    *,
    client_id: str = "",
    scope: str = "",
    tenant: str = "",
    timeout: int = 30,
) -> Dict[str, Any]:
    """Start a device-code grant. Returns the payload with user_code/verification_uri."""
    response = requests.post(
        _device_code_url(tenant),
        data={"client_id": _client_id(client_id), "scope": _scope(scope)},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def poll_for_token(
    device_code: str,
    *,
    client_id: str = "",
    scope: str = "",
    tenant: str = "",
    timeout_seconds: int = 600,
    poll_interval: int = 5,
) -> Dict[str, Any]:
    """Wait until the user finishes the browser sign-in, then return the token payload."""
    token_url = _token_url(tenant)
    deadline = time.monotonic() + max(30, timeout_seconds)
    while time.monotonic() < deadline:
        response = requests.post(
            token_url,
            data={
                "client_id": _client_id(client_id),
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "scope": _scope(scope),
            },
            timeout=30,
        )
        payload = _safe_json(response)
        if response.status_code == 200:
            return payload

        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            time.sleep(poll_interval)
            continue
        if error == "slow_down":
            poll_interval += 5
            time.sleep(poll_interval)
            continue
        if error in ("expired_token", "access_denied", "invalid_grant"):
            raise RuntimeError(f"oauth device flow failed: {error} - {payload.get('error_description', '')}")
        raise RuntimeError(f"oauth device flow failed: {error or response.status_code} - {payload.get('error_description', '')}")
    raise TimeoutError("oauth device flow timed out before the user completed sign-in")


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = "",
    scope: str = "",
    tenant: str = "",
    timeout: int = 30,
) -> Dict[str, Any]:
    """Exchange a refresh token for a fresh access token."""
    token = (refresh_token or "").strip()
    if not token:
        raise ValueError("refresh token is empty")
    response = requests.post(
        _token_url(tenant),
        data={
            "client_id": _client_id(client_id),
            "grant_type": "refresh_token",
            "refresh_token": token,
            "scope": _scope(scope),
        },
        timeout=timeout,
    )
    payload = _safe_json(response)
    if response.status_code != 200:
        raise RuntimeError(
            f"oauth refresh failed: {payload.get('error', response.status_code)} - {payload.get('error_description', '')}"
        )
    return payload


def build_xoauth2_payload(username: str, access_token: str) -> bytes:
    """Build the raw SASL XOAUTH2 response.

    NOTE: return the *unencoded* bytes. imaplib.authenticate() wraps the callback
    in _Authenticator, which base64-encodes whatever we return. Pre-encoding here
    would double-encode and the server rejects it with
    "BAD Command Argument Error. 12".
    """
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def access_token_from_env() -> Optional[str]:
    """Return a usable access token, refreshing it when only a refresh token is stored."""
    direct = (os.getenv("MS_OAUTH_ACCESS_TOKEN") or "").strip()
    if direct:
        return direct

    refresh_token = (os.getenv(REFRESH_TOKEN_ENV) or "").strip()
    if not refresh_token:
        return None

    payload = refresh_access_token(refresh_token)
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("oauth refresh returned no access_token")
    # Keep the freshest token around so repeat calls skip the refresh round-trip.
    os.environ["MS_OAUTH_ACCESS_TOKEN"] = token
    new_refresh = str(payload.get("refresh_token") or "").strip()
    if new_refresh and new_refresh != refresh_token:
        os.environ[REFRESH_TOKEN_ENV] = new_refresh
        # Microsoft rotates the refresh token on every refresh and invalidates the
        # previous one, so the new value MUST be persisted. Keeping it in os.environ
        # only would silently break the next scheduled run.
        _persist_refresh_token(new_refresh)
    return token


def _persist_refresh_token(value: str) -> None:
    """Write a rotated refresh token back to the project .env file."""
    path = PROJECT_ENV_PATH
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{REFRESH_TOKEN_ENV}="):
            lines[index] = f"{REFRESH_TOKEN_ENV}={value}"
            break
    else:
        lines.append(f"{REFRESH_TOKEN_ENV}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def device_code_instructions(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Return (user_code, verification_uri) for the human to complete sign-in."""
    user_code = str(payload.get("user_code") or "").strip()
    uri = str(payload.get("verification_uri") or payload.get("verification_uri_complete") or "").strip()
    if not user_code or not uri:
        raise RuntimeError(f"unexpected device code payload: {payload}")
    return user_code, uri


def _safe_json(response: Any) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"error": "invalid_json", "error_description": (response.text or "")[:200]}
    return payload if isinstance(payload, dict) else {"error": "unexpected_payload"}
