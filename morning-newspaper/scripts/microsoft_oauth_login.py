"""Interactive Microsoft OAuth2 sign-in for Outlook IMAP collection.

Outlook.com refuses Basic authentication on IMAP, so the mailbox source needs
an OAuth2 access token. This script runs the device-code flow: it prints a
short user code, the user completes sign-in in a browser, and the resulting
refresh token is written to the project .env file.

Usage:
    python scripts/microsoft_oauth_login.py
    python scripts/microsoft_oauth_login.py --timeout 900
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from morning_newspaper.common import load_env_file
from morning_newspaper.mailbox_oauth import (
    CLIENT_ID_ENV,
    MAILFLOW_MS_CLIENT_ID,
    REFRESH_TOKEN_ENV,
    TENANT_COMMON,
    TENANT_CONSUMERS,
    TENANT_ENV,
    device_code_instructions,
    poll_for_token,
    request_device_code,
)


ENV_PATH = PROJECT_ROOT / ".env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sign in to Microsoft and store an IMAP refresh token.")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for browser sign-in.")
    parser.add_argument("--env", default=str(ENV_PATH), help="Path to the .env file to update.")
    parser.add_argument(
        "--client-id",
        default=os.getenv(CLIENT_ID_ENV, ""),
        help=(
            "Azure AD application (client) id of a public-client app. "
            f"Default: MailFlow's third-party personal-account client ({MAILFLOW_MS_CLIENT_ID}). "
            "First-party ids (Azure CLI / MS Office) are rejected: users cannot consent "
            "to first-party applications."
        ),
    )
    parser.add_argument(
        "--tenant",
        default=os.getenv(TENANT_ENV, TENANT_CONSUMERS),
        choices=[TENANT_COMMON, TENANT_CONSUMERS],
        help=(
            "Microsoft identity endpoint. Use 'consumers' for personal Microsoft accounts "
            "(outlook.com/hotmail.com); apps registered for personal accounts only reject "
            "'common' with AADSTS9002346."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_path = Path(args.env)
    load_env_file(env_path)

    client_id = args.client_id.strip() or MAILFLOW_MS_CLIENT_ID

    print("requesting device code ...", flush=True)
    print(f"  client_id = {client_id}", flush=True)
    print(f"  tenant    = {args.tenant}", flush=True)
    payload = request_device_code(client_id=client_id, tenant=args.tenant)
    user_code, verification_uri = device_code_instructions(payload)

    print()
    print("=" * 68)
    print("  请在浏览器完成微软账号登录授权")
    print("=" * 68)
    print(f"  1. 打开 : {verification_uri}")
    print(f"  2. 输入 : {user_code}")
    print()
    print(f"  授权完成后本脚本会自动继续（最长等待 {args.timeout} 秒）")
    print("=" * 68)
    print()

    result = poll_for_token(
        str(payload.get("device_code") or ""),
        client_id=client_id,
        tenant=args.tenant,
        timeout_seconds=args.timeout,
    )
    refresh_token = str(result.get("refresh_token") or "").strip()
    if not refresh_token:
        print("ERROR: token response contained no refresh_token", file=sys.stderr)
        raise SystemExit(1)

    _write_env(env_path, REFRESH_TOKEN_ENV, refresh_token)
    _write_env(env_path, CLIENT_ID_ENV, client_id)
    _write_env(env_path, TENANT_ENV, args.tenant)

    expires_in = result.get("expires_in")
    print("sign-in complete.")
    print(f"  refresh_token written to {env_path} ({REFRESH_TOKEN_ENV})")
    print(f"  access token expires_in: {expires_in}s")
    print("  the access token is refreshed from the refresh token on every run,")
    print("  so it is deliberately NOT persisted (a stale one would break IMAP).")


def _write_env(path: Path, key: str, value: str) -> None:
    """Set key=value in the .env file, replacing an existing entry in place."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    main()
