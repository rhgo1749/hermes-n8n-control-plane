"""Token-auth adapter for a fixed allowlist of existing Hermes cron routes.

This plugin does not create an endpoint, dispatch workers, change Kanban, or
alter Hermes cron execution.  It only allows n8n to authenticate to the already
existing dashboard ``trigger``/``pause`` routes for the five migrated jobs.
"""
from __future__ import annotations

import hmac
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
    list_token_providers,
)

logger = logging.getLogger(__name__)

TOKEN_FILE_NAME = ".n8n-cron-token"
TOKEN_FILE_ENV = "HERMES_N8N_CRON_TOKEN_FILE"
MIN_TOKEN_CHARS = 43  # secrets.token_urlsafe(32): approximately 256 bits
MIN_DISTINCT_CHARS = 16
SCOPE = "n8n-cron-trigger"

# Profile name is metadata for auditability and is deliberately not caller
# supplied. The route allowlist prevents the token from listing, editing,
# creating, deleting, or triggering any job outside this migration.
ALLOWED_JOBS: dict[str, str] = {
    "168bd63461e7": "default",
    "e432a90c1361": "default",
    "df360bfa297d": "default",
    "bf431b2a6ba6": "default",
    "27f6725028ff": "dj-broadcast",
}
TOKEN_ROUTE_PATHS: tuple[str, ...] = tuple(
    f"/api/cron/jobs/{job_id}/{action}"
    for job_id in ALLOWED_JOBS
    for action in ("trigger", "pause")
)

LAST_SKIP_REASON = ""


def _token_path() -> Path:
    configured = os.environ.get(TOKEN_FILE_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path(__file__).with_name(TOKEN_FILE_NAME)


def _read_token() -> tuple[Optional[str], str]:
    path = _token_path()
    try:
        info = path.stat()
    except OSError:
        return None, "token file is unavailable"
    if not stat.S_ISREG(info.st_mode):
        return None, "token path is not a regular file"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return None, "token file permissions are broader than 0600"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, "token file cannot be read"
    if len(token) < MIN_TOKEN_CHARS:
        return None, "token is shorter than the 256-bit minimum"
    if len(set(token)) < MIN_DISTINCT_CHARS:
        return None, "token appears low-entropy"
    return token, ""


class N8nCronTokenProvider(DashboardAuthProvider):
    """Non-interactive bearer credential restricted by registered route paths."""

    name = "n8n-cron-token"
    display_name = "n8n cron control (service credential)"
    supports_token = True
    supports_session = False

    def __init__(self, token: str) -> None:
        self._token = token

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token and hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            return TokenPrincipal(
                principal="n8n-cron-control",
                provider=self.name,
                scopes=(SCOPE,),
            )
        return None

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("n8n-cron-token is not an interactive provider")

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError("n8n-cron-token is not an interactive provider")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("n8n-cron-token is not an interactive provider")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


def register(ctx) -> None:
    """Register a service token and only the fixed trigger/pause route allowlist."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    token, reason = _read_token()
    if token is None:
        LAST_SKIP_REASON = reason
        logger.warning("hermes-n8n-cron-auth: disabled: %s", reason)
        return

    # token_auth_middleware intentionally tries every registered service-token
    # provider for every opted-in route. This migration must not accidentally
    # make a pre-existing service credential (for example a drain credential)
    # valid for cron control, so coexistence is refused before any path is
    # registered. Bundled providers load before user plugins.
    if list_token_providers():
        LAST_SKIP_REASON = "another non-interactive dashboard token provider is already registered"
        logger.error("hermes-n8n-cron-auth: disabled: %s", LAST_SKIP_REASON)
        return

    try:
        from hermes_cli.dashboard_auth.token_auth import register_token_route

        for route in TOKEN_ROUTE_PATHS:
            register_token_route(route)
    except Exception as exc:
        # Register routes first. If this fails, the provider never enters the
        # process-wide token stack; any partially registered path has no
        # provider and therefore returns the seam's fail-closed 401.
        LAST_SKIP_REASON = f"token route registration failed: {type(exc).__name__}"
        logger.error("hermes-n8n-cron-auth: %s", LAST_SKIP_REASON)
        return

    try:
        ctx.register_dashboard_auth_provider(N8nCronTokenProvider(token))
    except Exception as exc:
        # A route with no provider remains fail closed. Do not let a failed
        # plugin registration turn into a globally usable service credential.
        LAST_SKIP_REASON = f"token provider registration failed: {type(exc).__name__}"
        logger.error("hermes-n8n-cron-auth: %s", LAST_SKIP_REASON)
        return

    logger.info(
        "hermes-n8n-cron-auth: registered %d least-privilege cron token routes",
        len(TOKEN_ROUTE_PATHS),
    )
