#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _urlopen_without_redirect(request: Request, *, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


urlopen = _urlopen_without_redirect

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GITHUB_ROUTER_LISTEN_PORT", "5681"))

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30
GITHUB_OWNER = os.environ.get("GITHUB_ROUTER_OWNER", "rhgo1749").strip()
GITHUB_OWNER_TYPE = os.environ.get("GITHUB_ROUTER_OWNER_TYPE", "personal").strip().casefold()
GITHUB_TOPIC = os.environ.get("GITHUB_ROUTER_TOPIC", "hermes-agent").strip()
PUBLIC_URL = os.environ.get("GITHUB_ROUTER_PUBLIC_URL", "").strip()
LEASE_BASE_URL = os.environ.get(
    "GITHUB_ROUTER_LEASE_BASE_URL",
    "http://127.0.0.1:5680",
).rstrip("/")
N8N_EDGE_SYNC_URL = os.environ.get(
    "GITHUB_ROUTER_N8N_EDGE_SYNC_URL",
    "http://127.0.0.1:5678/webhook/hermes-github-edge-sync",
).strip().rstrip("/")
N8N_EDGE_SYNC_TIMEOUT_SECONDS = float(
    os.environ.get("GITHUB_ROUTER_N8N_EDGE_SYNC_TIMEOUT_SECONDS", "130")
)
GITHUB_GET_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
N8N_EDGE_SYNC_MAX_RESPONSE_BYTES = 64 * 1024
WAIT_SECONDS = int(os.environ.get("GITHUB_ROUTER_WAIT_SECONDS", "75"))
SCOPE_TTL_SECONDS = int(
    os.environ.get(
        "GITHUB_ROUTER_SCOPE_TTL_SECONDS",
        str(max(WAIT_SECONDS + 120, 300)),
    )
)
SCOPE_CLAIM_LEASE_SECONDS = int(
    os.environ.get(
        "GITHUB_ROUTER_SCOPE_CLAIM_LEASE_SECONDS",
        str(max(SCOPE_TTL_SECONDS, 1200)),
    )
)
SCOPE_MAX_ATTEMPTS = int(
    os.environ.get("GITHUB_ROUTER_SCOPE_MAX_ATTEMPTS", "3")
)
SCOPE_MAX_PENDING = max(
    1,
    int(os.environ.get("GITHUB_ROUTER_SCOPE_MAX_PENDING", "4096")),
)
SCOPE_RETRY_BACKOFF_SECONDS = (
    1,
    5,
)
DELIVERY_TTL_SECONDS = int(
    os.environ.get("GITHUB_ROUTER_DELIVERY_TTL_SECONDS", "3600")
)
DELIVERY_MAX_ENTRIES = int(
    os.environ.get("GITHUB_ROUTER_DELIVERY_MAX_ENTRIES", "4096")
)
STATE_PATH = Path(
    os.environ.get("GITHUB_ROUTER_STATE_PATH", "/state/github-router.json")
)
GITHUB_TOKEN_FILE = Path(
    os.environ.get(
        "GITHUB_ROUTER_GITHUB_TOKEN_FILE",
        "/run/secrets/github-token",
    )
)
WEBHOOK_SECRET_FILE = Path(
    os.environ.get(
        "GITHUB_ROUTER_WEBHOOK_SECRET_FILE",
        "/run/secrets/github-webhook-secret",
    )
)
INTAKE_TOKEN_FILE = Path(
    os.environ.get(
        "GITHUB_ROUTER_INTAKE_TOKEN_FILE",
        "/run/secrets/hermes-intake-control-token",
    )
)
MAX_BODY_BYTES = 1024 * 1024
SUPPORTED_EVENTS = {
    "installation",
    "installation_repositories",
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "public",
    "repository",
}
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$"
)
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SCOPE_ID_RE = _DELIVERY_ID_RE
_CLAIM_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
_SCOPE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_SCOPE_REPOSITORIES = 100
_N8N_EDGE_SYNC_PATH = "/webhook/hermes-github-edge-sync"
_STATE_LOCK = threading.Lock()


def _safe_scope_reason(value: object) -> str:
    if isinstance(value, str) and _SCOPE_REASON_RE.fullmatch(value):
        return value
    return "scope_retryable"


class RouterError(RuntimeError):
    pass


def _load_registry_module():
    configured = os.environ.get("GITHUB_ROUTER_REGISTRY_SCRIPT", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / "scripts" / "repository_registry.py",
        Path("/app/repository_registry.py"),
    ]
    path = next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )
    if path is None:
        raise RuntimeError("repository_registry.py is unavailable")
    spec = importlib.util.spec_from_file_location(
        "github_router_repository_registry",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load repository_registry.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


registry = _load_registry_module()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_secret(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RouterError(f"{label} is unavailable") from exc
    if not value:
        raise RouterError(f"{label} is empty")
    return value


def _load_state_unlocked() -> dict[str, Any]:
    try:
        with STATE_PATH.open("rb") as handle:
            raw_bytes = handle.read(8 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RouterError(f"cannot read router state: {exc}") from exc
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise RouterError("router state is too large")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RouterError("router state is not UTF-8") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RouterError("router state is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RouterError("router state must be an object")
    return value


def _write_state_unlocked(value: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(
        f".{STATE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with tmp.open("wb") as fh:
            fh.write(_json_bytes(value) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _normalise_scope_item(
    raw: object,
    now: int,
    *,
    allow_expired: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    raw_scope_id = raw.get("id")
    if not isinstance(raw_scope_id, str):
        return None
    scope_id = raw_scope_id.strip()
    if scope_id != raw_scope_id or not _SCOPE_ID_RE.fullmatch(scope_id):
        return None
    fields = ("created_at", "expires_at", "attempts", "not_before")
    values = tuple(raw.get(field) for field in fields)
    if any(type(value) is not int for value in values):
        return None
    created_at = cast(int, values[0])
    expires_at = cast(int, values[1])
    attempts = cast(int, values[2])
    not_before = cast(int, values[3])
    if (
        created_at <= 0
        or expires_at <= created_at
        or attempts < 0
        or attempts > SCOPE_MAX_ATTEMPTS
        or not_before < 0
        or not_before > expires_at
    ):
        return None
    if not allow_expired and expires_at <= now:
        return None
    mode = raw.get("mode")
    if not isinstance(mode, str):
        return None
    if mode not in {"event", "full"}:
        return None
    raw_repositories = raw.get("repositories", [])
    if not isinstance(raw_repositories, list):
        return None
    repositories: list[str] = []
    seen: set[str] = set()
    for raw_repository in raw_repositories:
        if not isinstance(raw_repository, str):
            return None
        repository = raw_repository.strip()
        if (
            repository != raw_repository
            or len(repository) > 256
            or not _REPOSITORY_RE.fullmatch(repository)
            or repository.casefold() in seen
        ):
            return None
        seen.add(repository.casefold())
        repositories.append(repository)
    if len(repositories) > _MAX_SCOPE_REPOSITORIES:
        return None
    repositories.sort(key=str.casefold)
    if mode == "event" and not repositories:
        return None
    if mode == "full" and repositories:
        return None
    raw_delivery = raw.get("delivery")
    if raw_delivery is not None and (
        not isinstance(raw_delivery, str)
        or raw_delivery != raw_delivery.strip()
        or not _DELIVERY_ID_RE.fullmatch(raw_delivery)
    ):
        return None
    result = {
        "id": scope_id,
        "mode": mode,
        "repositories": repositories,
        "created_at": created_at,
        "expires_at": expires_at,
        "attempts": attempts,
        "not_before": not_before,
    }
    if raw_delivery is not None:
        result["delivery"] = raw_delivery
    return result


def _prune_pending_scopes(raw_pending: object, now: int | None = None) -> list[dict[str, Any]]:
    now = int(time.time()) if now is None else now
    if not isinstance(raw_pending, list):
        return []
    kept: list[dict[str, Any]] = []
    for raw in raw_pending:
        if not isinstance(raw, dict):
            continue
        item = _normalise_scope_item(raw, now, allow_expired=True)
        pending_at = raw.get("pending_at")
        reason = _safe_scope_reason(raw.get("reason"))
        if (
            item is None
            or type(pending_at) is not int
            or pending_at <= 0
        ):
            continue
        kept.append(
            {
                **item,
                "pending_at": pending_at,
                "reason": reason,
            }
        )
    return kept[-SCOPE_MAX_PENDING:]


def _append_pending_scope(
    state: dict[str, Any],
    item: dict[str, Any],
    *,
    now: int,
    reason: str,
) -> None:
    pending = _prune_pending_scopes(state.get("pending_scopes"), now)
    pending = [existing for existing in pending if existing.get("id") != item.get("id")]
    pending.append(
        {
            **item,
            "pending_at": now,
            "reason": _safe_scope_reason(reason),
        }
    )
    state["pending_scopes"] = pending[-SCOPE_MAX_PENDING:]


def _recover_queued_scopes_unlocked(
    state: dict[str, Any],
    now: int,
) -> None:
    """Keep expired queued work durable instead of silently dropping it."""
    raw_queue = state.get("scope_queue")
    if not isinstance(raw_queue, list):
        state["scope_queue"] = []
        return
    queue: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_queue:
        item = _normalise_scope_item(raw, now, allow_expired=True)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        if item["expires_at"] > now:
            queue.append(item)
            continue
        next_attempt = item["attempts"] + 1
        if next_attempt >= SCOPE_MAX_ATTEMPTS:
            _append_pending_scope(
                state,
                {**item, "attempts": next_attempt},
                now=now,
                reason="scope_expired",
            )
            continue
        queue.append(
            {
                **item,
                "attempts": next_attempt,
                "expires_at": now + SCOPE_TTL_SECONDS,
                "not_before": now,
            }
        )
    state["scope_queue"] = queue


def _recover_scope_claims_unlocked(
    state: dict[str, Any],
    now: int,
) -> None:
    """Move expired in-flight scopes back to durable retry state."""
    raw_claims = state.get("scope_claims")
    if not isinstance(raw_claims, dict):
        state["scope_claims"] = {}
        state["pending_scopes"] = _prune_pending_scopes(
            state.get("pending_scopes"), now
        )
        _recover_queued_scopes_unlocked(state, now)
        return
    claims: dict[str, dict[str, Any]] = {}
    state["pending_scopes"] = _prune_pending_scopes(state.get("pending_scopes"), now)
    _recover_queued_scopes_unlocked(state, now)
    queue = state["scope_queue"]
    for raw_id, raw_claim in raw_claims.items():
        if not isinstance(raw_id, str) or not _SCOPE_ID_RE.fullmatch(raw_id):
            continue
        scope_id = raw_id
        if not isinstance(raw_claim, dict):
            continue
        if raw_claim.get("id") != scope_id:
            continue
        claim_expires_at = raw_claim.get("claim_expires_at")
        if type(claim_expires_at) is not int or claim_expires_at <= 0:
            continue
        item = _normalise_scope_item(raw_claim, now, allow_expired=True)
        if item is None:
            continue
        raw_claim_token = raw_claim.get("claim_token")
        claim_token = (
            raw_claim_token
            if isinstance(raw_claim_token, str)
            and _CLAIM_TOKEN_RE.fullmatch(raw_claim_token)
            else uuid.uuid4().hex
        )
        queue = [queued for queued in queue if queued.get("id") != scope_id]
        if claim_expires_at > now:
            claims[scope_id] = {
                **item,
                "claim_expires_at": claim_expires_at,
                "claim_token": claim_token,
            }
            continue
        next_attempt = item["attempts"] + 1
        if next_attempt >= SCOPE_MAX_ATTEMPTS:
            _append_pending_scope(
                state,
                {**item, "attempts": next_attempt},
                now=now,
                reason="claim_lease_expired",
            )
            continue
        queue.append(
            {
                **item,
                "attempts": next_attempt,
                "expires_at": now + SCOPE_TTL_SECONDS,
                "not_before": now,
            }
        )
    state["scope_claims"] = claims
    state["scope_queue"] = queue


def _prune_queue(raw_queue: object, now: int | None = None) -> list[dict[str, Any]]:
    now = int(time.time()) if now is None else now
    if not isinstance(raw_queue, list):
        return []
    queue: list[dict[str, Any]] = []
    for raw in raw_queue:
        item = _normalise_scope_item(raw, now)
        if item is not None:
            queue.append(item)
    return queue


def _enqueue_scope(
    *,
    full: bool,
    repository: str | None = None,
    repositories: list[str] | tuple[str, ...] | None = None,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    if full:
        scoped_repositories: list[str] = []
    else:
        raw_repositories = (
            list(repositories)
            if repositories is not None
            else ([repository] if repository is not None else [])
        )
        scoped_repositories = []
        seen: set[str] = set()
        for raw_repository in raw_repositories:
            if not isinstance(raw_repository, str):
                raise RouterError("invalid repository identity")
            candidate = raw_repository.strip()
            if (
                candidate != raw_repository
                or len(candidate) > 256
                or not _REPOSITORY_RE.fullmatch(candidate)
            ):
                raise RouterError(f"invalid repository identity: {candidate!r}")
            key = candidate.casefold()
            if key not in seen:
                scoped_repositories.append(candidate)
                seen.add(key)
        if not scoped_repositories or len(scoped_repositories) > _MAX_SCOPE_REPOSITORIES:
            raise RouterError("invalid repository scope")
    if delivery_id is not None and (
        not isinstance(delivery_id, str)
        or delivery_id != delivery_id.strip()
        or not _DELIVERY_ID_RE.fullmatch(delivery_id)
    ):
        raise RouterError("invalid delivery id")
    item = {
        "id": str(uuid.uuid4()),
        "mode": "full" if full else "event",
        "repositories": scoped_repositories,
        "created_at": now,
        "expires_at": now + SCOPE_TTL_SECONDS,
        "attempts": 0,
        "not_before": now,
    }
    if delivery_id is not None:
        item["delivery"] = delivery_id
    with _STATE_LOCK:
        state = _load_state_unlocked()
        _recover_scope_claims_unlocked(state, now)
        if delivery_id is not None:
            queue = state["scope_queue"]
            for existing in queue:
                if isinstance(existing, dict) and existing.get("delivery") == delivery_id:
                    _write_state_unlocked(state)
                    return {**existing, "existing": True}
            pending = state.get("pending_scopes")
            if isinstance(pending, list):
                for existing in pending:
                    if isinstance(existing, dict) and existing.get("delivery") == delivery_id:
                        _write_state_unlocked(state)
                        return {**existing, "existing": True, "pending": True}
            claims = state.get("scope_claims")
            if isinstance(claims, dict):
                for existing in claims.values():
                    if (
                        isinstance(existing, dict)
                        and existing.get("delivery") == delivery_id
                    ):
                        _write_state_unlocked(state)
                        return {
                            key: value
                            for key, value in existing.items()
                            if key != "claim_token"
                        } | {"existing": True, "in_flight": True}
        queue = state["scope_queue"]
        queue.append(item)
        state["scope_queue"] = queue
        _write_state_unlocked(state)
    return item


def _claim_scope() -> dict[str, Any]:
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        _recover_scope_claims_unlocked(state, now)
        queue = state["scope_queue"]
        claim_index = next(
            (
                index
                for index, item in enumerate(queue)
                if int(item.get("not_before") or 0) <= now
            ),
            None,
        )
        if claim_index is not None:
            item = queue.pop(claim_index)
            claims = state.get("scope_claims")
            if not isinstance(claims, dict):
                claims = {}
            claim_token = uuid.uuid4().hex
            claims[item["id"]] = {
                **item,
                "claim_expires_at": now + SCOPE_CLAIM_LEASE_SECONDS,
                "claim_token": claim_token,
            }
            state["scope_queue"] = queue
            state["scope_claims"] = claims
            state["last_claimed_scope"] = {
                **item,
                "claim_token": claim_token,
            }
            _write_state_unlocked(state)
            return {
                **item,
                "claim_token": claim_token,
            }
        state["scope_queue"] = queue
        _write_state_unlocked(state)
    return {
        "id": "",
        "mode": "none",
        "repositories": [],
        "created_at": now,
        "expires_at": 0,
        "attempts": 0,
        "not_before": 0,
    }


def _claim_token_matches(claim: object, claim_token: object) -> bool:
    if not isinstance(claim, dict) or not isinstance(claim_token, str):
        return False
    stored = claim.get("claim_token")
    return (
        isinstance(stored, str)
        and _CLAIM_TOKEN_RE.fullmatch(stored) is not None
        and _CLAIM_TOKEN_RE.fullmatch(claim_token) is not None
        and hmac.compare_digest(stored, claim_token)
    )


def _ack_scope(scope_id: str, claim_token: str) -> dict[str, Any]:
    """Remove one in-flight scope after the intake worker completed it."""
    if not isinstance(scope_id, str):
        raise RouterError("invalid scope id")
    if not isinstance(claim_token, str) or not _CLAIM_TOKEN_RE.fullmatch(claim_token):
        raise RouterError("invalid scope claim token")
    raw_scope_id = scope_id
    scope_id = raw_scope_id.strip()
    if scope_id != raw_scope_id or not _SCOPE_ID_RE.fullmatch(scope_id):
        raise RouterError("invalid scope id")
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        _recover_scope_claims_unlocked(state, now)
        claims = state.get("scope_claims")
        if not isinstance(claims, dict):
            claims = {}
        claim = claims.get(scope_id)
        if claim is None:
            last = state.get("last_scope_transition")
            if isinstance(last, dict) and last.get("id") == scope_id:
                if (
                    last.get("status") == "acknowledged"
                    and _claim_token_matches(last, claim_token)
                ):
                    _write_state_unlocked(state)
                    return {"ok": True, "id": scope_id, "status": "already_acknowledged"}
                if (
                    last.get("status") in {"requeued", "pending"}
                    and _claim_token_matches(last, claim_token)
                ):
                    _write_state_unlocked(state)
                    return {"ok": True, "id": scope_id, "status": "already_requeued"}
            raise RouterError("scope claim token mismatch")
        if not _claim_token_matches(claim, claim_token):
            raise RouterError("scope claim token mismatch")
        del claims[scope_id]
        state["scope_claims"] = claims
        state["last_scope_transition"] = {
            "id": scope_id,
            "status": "acknowledged",
            "at": now,
            "claim_token": claim_token,
        }
        _write_state_unlocked(state)
    return {"ok": True, "id": scope_id, "status": "acknowledged"}


def _requeue_scope(
    scope_id: str,
    reason: str,
    claim_token: str,
) -> dict[str, Any]:
    """Requeue a failed scope or retain it in durable pending state."""
    if not isinstance(scope_id, str):
        raise RouterError("invalid scope id")
    if not isinstance(reason, str):
        raise RouterError("invalid scope reason")
    safe_reason = _safe_scope_reason(reason)
    if not isinstance(claim_token, str) or not _CLAIM_TOKEN_RE.fullmatch(claim_token):
        raise RouterError("invalid scope claim token")
    raw_scope_id = scope_id
    scope_id = raw_scope_id.strip()
    if scope_id != raw_scope_id or not _SCOPE_ID_RE.fullmatch(scope_id):
        raise RouterError("invalid scope id")
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        _recover_scope_claims_unlocked(state, now)
        claims = state.get("scope_claims")
        claim = claims.get(scope_id) if isinstance(claims, dict) else None
        if claim is None:
            last = state.get("last_scope_transition")
            if (
                isinstance(last, dict)
                and last.get("id") == scope_id
                and _claim_token_matches(last, claim_token)
            ):
                if last.get("status") == "requeued":
                    _write_state_unlocked(state)
                    return {
                        "ok": True,
                        "id": scope_id,
                        "status": "already_requeued",
                        "attempts": last.get("attempts", 0),
                    }
                if last.get("status") == "pending":
                    _write_state_unlocked(state)
                    return {
                        "ok": True,
                        "id": scope_id,
                        "status": "pending",
                        "attempts": last.get("attempts", 0),
                    }
            raise RouterError("scope claim token mismatch")
        if not _claim_token_matches(claim, claim_token):
            raise RouterError("scope claim token mismatch")
        assert isinstance(claims, dict)
        item = _normalise_scope_item(claim, now, allow_expired=True)
        if item is None:
            raise RouterError("scope claim is invalid")
        claims.pop(scope_id)
        next_attempt = item["attempts"] + 1
        if next_attempt >= SCOPE_MAX_ATTEMPTS:
            _append_pending_scope(
                state,
                {**item, "attempts": next_attempt},
                now=now,
                reason=safe_reason,
            )
            status = "pending"
        else:
            backoff_index = min(
                next_attempt - 1,
                len(SCOPE_RETRY_BACKOFF_SECONDS) - 1,
            )
            queue = state["scope_queue"]
            queue.append(
                {
                    **item,
                    "attempts": next_attempt,
                    "expires_at": now + SCOPE_TTL_SECONDS,
                    "not_before": now + SCOPE_RETRY_BACKOFF_SECONDS[backoff_index],
                }
            )
            state["scope_queue"] = queue
            status = "requeued"
        state["scope_claims"] = claims
        state["last_scope_transition"] = {
            "id": scope_id,
            "status": status,
            "at": now,
            "attempts": next_attempt,
            "reason": safe_reason,
            "claim_token": claim_token,
        }
        _write_state_unlocked(state)
    return {
        "ok": True,
        "id": scope_id,
        "status": status,
        "attempts": next_attempt,
    }


def _queue_status() -> dict[str, Any]:
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        _recover_scope_claims_unlocked(state, now)
        queue = state["scope_queue"]
        claims = state.get("scope_claims")
        pending = state.get("pending_scopes")
        in_flight_count = len(claims) if isinstance(claims, dict) else 0
        pending_count = len(pending) if isinstance(pending, list) else 0
        managed = [str(item) for item in state.get("managed_repositories", [])]
        state["scope_queue"] = queue
        _write_state_unlocked(state)
    return {
        "queued_scopes": len(queue),
        "in_flight_scopes": in_flight_count,
        "pending_scopes": pending_count,
        "managed_count": len(managed),
    }


def _prune_deliveries(raw_map: object, now: int | None = None) -> dict[str, dict[str, int]]:
    now = int(time.time()) if now is None else now
    if not isinstance(raw_map, dict):
        return {}
    kept: dict[str, dict[str, int]] = {}
    for raw_id, raw_entry in raw_map.items():
        delivery_id = str(raw_id).strip()
        if not _DELIVERY_ID_RE.fullmatch(delivery_id):
            continue
        if not isinstance(raw_entry, dict):
            continue
        try:
            expires_at = int(raw_entry.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if expires_at <= now:
            continue
        try:
            created_at = int(raw_entry.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        kept[delivery_id] = {"created_at": created_at, "expires_at": expires_at}
    return kept


def _cap_deliveries(
    kept: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    if len(kept) <= DELIVERY_MAX_ENTRIES:
        return kept
    oldest_first = sorted(kept.items(), key=lambda item: item[1]["created_at"])
    overflow = len(kept) - DELIVERY_MAX_ENTRIES
    evicted = {item[0] for item in oldest_first[:overflow]}
    for delivery_id in evicted:
        del kept[delivery_id]
    return kept


def _bounded_deliveries(
    raw_map: object, now: int | None = None
) -> dict[str, dict[str, int]]:
    return _cap_deliveries(_prune_deliveries(raw_map, now))


def _claim_delivery(
    delivery_id: str, now: int | None = None
) -> tuple[str, bool]:
    """Atomically claim a signed delivery ID for downstream dispatch.

    Returns ("duplicate", True) when the ID was already recorded inside its
    TTL (replay), or ("fresh", False) when this call owns the dispatch. The
    claim persists in the router state file so restarts keep deduplicating.
    The cap is applied after the insert so the stored set never exceeds
    DELIVERY_MAX_ENTRIES.
    """
    now = int(time.time()) if now is None else now
    with _STATE_LOCK:
        state = _load_state_unlocked()
        kept = _bounded_deliveries(state.get("delivery_dedupe"), now)
        if delivery_id in kept:
            state["delivery_dedupe"] = kept
            _write_state_unlocked(state)
            return "duplicate", True
        kept[delivery_id] = {
            "created_at": now,
            "expires_at": now + DELIVERY_TTL_SECONDS,
        }
        kept = _cap_deliveries(kept)
        state["delivery_dedupe"] = kept
        _write_state_unlocked(state)
        return "fresh", False


def _release_delivery(
    delivery_id: str, now: int | None = None
) -> None:
    """Drop a recorded delivery so a later retry can dispatch again.

    Used when dispatching failed (5xx): GitHub retries failed deliveries and
    the operator can resend, so a failed delivery must not be suppressed.
    """
    now = int(time.time()) if now is None else now
    with _STATE_LOCK:
        state = _load_state_unlocked()
        kept = _bounded_deliveries(state.get("delivery_dedupe"), now)
        if delivery_id in kept:
            del kept[delivery_id]
        state["delivery_dedupe"] = kept
        _write_state_unlocked(state)


def _service_authorized(header: str) -> bool:
    try:
        token = _read_secret(INTAKE_TOKEN_FILE, "intake control token")
    except RouterError:
        return False
    expected = f"Bearer {token}"
    return bool(header) and hmac.compare_digest(
        header.encode("utf-8"),
        expected.encode("utf-8"),
    )


def _github_signature_valid(body: bytes, signature: str) -> bool:
    try:
        secret = _read_secret(WEBHOOK_SECRET_FILE, "GitHub webhook secret")
    except RouterError:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return bool(signature) and hmac.compare_digest(
        signature.encode("ascii", errors="ignore"),
        expected.encode("ascii"),
    )


def _github_path(repository: str, suffix: str) -> str:
    owner, name = repository.split("/", 1)
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{suffix}"


def _github_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else _json_bytes(payload)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hermes-n8n-control-plane/github-router",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{GITHUB_API}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    attempts = 2 if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(GITHUB_GET_MAX_RESPONSE_BYTES + 1)
                if len(raw) > GITHUB_GET_MAX_RESPONSE_BYTES:
                    raise RouterError("GitHub API response is too large")
                if not raw:
                    return None
                return json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
        except HTTPError as exc:
            if method.upper() == "GET" and 500 <= exc.code < 600 and attempt == 0:
                exc.close()
                continue
            if method.upper() == "GET" and 500 <= exc.code < 600:
                exc.close()
                raise RouterError(
                    f"GitHub API {method} {path} retry exhausted"
                ) from exc
            # Never relay a provider response body: proxies can echo credentials
            # or credential-bearing URLs in an error payload.
            exc.close()
            raise RouterError(
                f"GitHub API {method} {path} returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if method.upper() == "GET" and attempt == 0:
                continue
            if method.upper() == "GET":
                raise RouterError(
                    f"GitHub API {method} {path} retry exhausted"
                ) from exc
            raise RouterError(
                f"GitHub API {method} {path} failed: {type(exc).__name__}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise RouterError(
                f"GitHub API {method} {path} returned invalid JSON"
            ) from exc
    raise RouterError(f"GitHub API {method} {path} retry exhausted")


def _validated_public_url() -> str:
    value = PUBLIC_URL.strip()
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise RouterError("GITHUB_ROUTER_PUBLIC_URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RouterError(
            "GITHUB_ROUTER_PUBLIC_URL must be an HTTPS URL without credentials/query/fragment"
        )
    if parsed.path.rstrip("/") != "/github/hermes-intake":
        raise RouterError(
            "GITHUB_ROUTER_PUBLIC_URL path must be /github/hermes-intake"
        )
    return value.rstrip("/")


def _list_hooks(repository: str, token: str) -> list[dict[str, Any]]:
    payload = _github_request(
        "GET",
        _github_path(repository, "/hooks?per_page=100"),
        token,
    )
    if not isinstance(payload, list):
        raise RouterError(f"GitHub hooks response is invalid for {repository}")
    return [hook for hook in payload if isinstance(hook, dict)]


def _hook_url(hook: dict[str, Any]) -> str:
    config = hook.get("config")
    if not isinstance(config, dict):
        return ""
    return str(config.get("url") or "").rstrip("/")


def _ensure_webhook(
    repository: str,
    token: str,
    public_url: str,
    webhook_secret: str,
) -> tuple[str, int]:
    hooks = _list_hooks(repository, token)
    matches = [
        hook
        for hook in hooks
        if _hook_url(hook) == public_url and isinstance(hook.get("id"), int)
    ]
    payload = {
        "active": True,
        "events": ["issues", "issue_comment", "pull_request", "pull_request_review"],
        "config": {
            "url": public_url,
            "content_type": "json",
            "secret": webhook_secret,
            "insecure_ssl": "0",
        },
    }
    duplicates_removed = 0
    if matches:
        matches.sort(key=lambda hook: int(hook["id"]))
        keep = matches[0]
        _github_request(
            "PATCH",
            _github_path(repository, f"/hooks/{int(keep['id'])}"),
            token,
            payload,
        )
        for duplicate in matches[1:]:
            _github_request(
                "DELETE",
                _github_path(repository, f"/hooks/{int(duplicate['id'])}"),
                token,
            )
            duplicates_removed += 1
        return "updated", duplicates_removed
    _github_request(
        "POST",
        _github_path(repository, "/hooks"),
        token,
        {"name": "web", **payload},
    )
    return "created", 0


def _delete_router_webhooks(repository: str, token: str, public_url: str) -> int:
    removed = 0
    for hook in _list_hooks(repository, token):
        hook_id = hook.get("id")
        if _hook_url(hook) == public_url and isinstance(hook_id, int):
            _github_request(
                "DELETE",
                _github_path(repository, f"/hooks/{hook_id}"),
                token,
            )
            removed += 1
    return removed


def _reconcile_webhooks() -> dict[str, Any]:
    token = _read_secret(GITHUB_TOKEN_FILE, "GitHub token")
    secret = _read_secret(WEBHOOK_SECRET_FILE, "GitHub webhook secret")
    public_url = _validated_public_url()
    if GITHUB_OWNER_TYPE == "personal":
        repositories = registry.discover_repositories(
            token,
            GITHUB_OWNER,
            GITHUB_TOPIC,
        )
    else:
        repositories = registry.discover_repositories(
            token,
            GITHUB_OWNER,
            GITHUB_TOPIC,
            GITHUB_OWNER_TYPE,
        )
    current = sorted(
        {
            str(repository.get("full_name") or "").strip()
            for repository in repositories
            if isinstance(repository, dict)
            and _REPOSITORY_RE.fullmatch(
                str(repository.get("full_name") or "").strip()
            )
        },
        key=str.casefold,
    )
    with _STATE_LOCK:
        state = _load_state_unlocked()
        previous = {
            str(repository)
            for repository in state.get("managed_repositories", [])
            if _REPOSITORY_RE.fullmatch(str(repository))
        }
    created = 0
    updated = 0
    deleted = 0
    for repository in current:
        action, duplicate_deletes = _ensure_webhook(
            repository,
            token,
            public_url,
            secret,
        )
        created += int(action == "created")
        updated += int(action == "updated")
        deleted += duplicate_deletes
    current_casefold = {repository.casefold() for repository in current}
    for repository in sorted(previous, key=str.casefold):
        if repository.casefold() in current_casefold:
            continue
        deleted += _delete_router_webhooks(repository, token, public_url)
    with _STATE_LOCK:
        state = _load_state_unlocked()
        state["managed_repositories"] = current
        state["last_reconciled_at"] = int(time.time())
        _write_state_unlocked(state)
    return {
        "ok": True,
        "managed": len(current),
        "created": created,
        "updated": updated,
        "deleted": deleted,
    }


def _validated_lease_base_url() -> str:
    value = LEASE_BASE_URL.strip()
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RouterError("lease-controller URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RouterError("lease-controller URL must be a loopback URL")
    if port is not None and not 1 <= port <= 65535:
        raise RouterError("lease-controller URL has an invalid port")
    return value.rstrip("/")


def _lease_request(path: str, token: str) -> tuple[int, dict[str, Any]]:
    if path != "/trigger" and not path.startswith("/pause?lease="):
        raise RouterError("lease-controller path is invalid")
    base_url = _validated_lease_base_url()
    request = Request(
        f"{base_url}{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        data=b"",
    )
    try:
        with urlopen(request, timeout=65) as response:
            raw = response.read(N8N_EDGE_SYNC_MAX_RESPONSE_BYTES + 1)
            if len(raw) > N8N_EDGE_SYNC_MAX_RESPONSE_BYTES:
                raise RouterError("lease-controller response is too large")
            try:
                payload = (
                    json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
                    if raw
                    else {}
                )
            except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                raise RouterError("lease-controller response is invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RouterError("lease-controller response is not an object")
            return int(response.status), payload
    except HTTPError as exc:
        exc.close()
        return int(exc.code), {}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RouterError(
            f"lease-controller request failed: {type(exc).__name__}"
        ) from exc


def _validated_n8n_edge_sync_url() -> str:
    value = N8N_EDGE_SYNC_URL
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RouterError("n8n edge-sync URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != _N8N_EDGE_SYNC_PATH
        or hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RouterError("n8n edge-sync URL must be a loopback Webhook URL")
    if port is not None and not 1 <= port <= 65535:
        raise RouterError("n8n edge-sync URL has an invalid port")
    return value


def _normalise_pull_request_event(
    payload: dict[str, Any],
    repository: str,
    delivery_id: str,
) -> dict[str, Any]:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise RouterError("pull_request_missing")

    raw_action = payload.get("action")
    action = raw_action.strip() if isinstance(raw_action, str) else ""
    if not _ACTION_RE.fullmatch(action):
        raise RouterError("pull_request_action_invalid")

    merged = pull_request.get("merged")
    if not isinstance(merged, bool):
        raise RouterError("pull_request_merged_invalid")

    label = payload.get("label")
    label_name = ""
    if label is not None:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise RouterError("pull_request_label_invalid")
        label_name = label["name"].strip()
        if len(label_name) > 128:
            raise RouterError("pull_request_label_invalid")

    return {
        "repository": repository,
        "event": "pull_request",
        "action": action,
        "merged": merged,
        "label": label_name,
        "delivery": delivery_id,
    }


def _n8n_edge_sync(event: dict[str, Any]) -> dict[str, Any]:
    """Forward one bounded event to the private, authenticated n8n hop."""
    url = _validated_n8n_edge_sync_url()
    token = _read_secret(INTAKE_TOKEN_FILE, "intake control token")
    request = Request(
        url,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Hermes-Event-Source": "github-router",
        },
        data=_json_bytes(event),
    )
    try:
        with urlopen(request, timeout=N8N_EDGE_SYNC_TIMEOUT_SECONDS) as response:
            raw = response.read(N8N_EDGE_SYNC_MAX_RESPONSE_BYTES + 1)
            if len(raw) > N8N_EDGE_SYNC_MAX_RESPONSE_BYTES:
                raise RouterError("n8n edge-sync response is too large")
            if not raw:
                raise RouterError("n8n edge-sync response is empty")
            try:
                body = json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_pairs,
                )
            except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                raise RouterError("n8n edge-sync response is invalid JSON") from exc
            if not isinstance(body, dict):
                raise RouterError("n8n edge-sync response is not an object")
            return {"status": int(response.status), "body": body}
    except HTTPError as exc:
        exc.close()
        raise RouterError(f"n8n edge-sync returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RouterError(
            f"n8n edge-sync request failed: {type(exc).__name__}"
        ) from exc


def _delayed_pause(lease: str, token: str) -> None:
    time.sleep(WAIT_SECONDS)
    try:
        status, body = _lease_request(
            f"/pause?lease={quote(lease, safe='')}",
            token,
        )
        print(
            "github-router delayed pause "
            f"status={status} paused={body.get('paused')} "
            f"reason={body.get('reason', '')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - delayed pause must not affect wake
        print(
            "github-router delayed pause warning: "
            f"{type(exc).__name__}",
            flush=True,
        )


def _wake() -> dict[str, Any]:
    token = _read_secret(INTAKE_TOKEN_FILE, "intake control token")
    status, payload = _lease_request("/trigger", token)
    if not 200 <= status < 300:
        raise RouterError(
            f"lease-controller trigger rejected with HTTP {status}"
        )
    raw_lease = payload.get("lease")
    if not isinstance(raw_lease, str) or raw_lease != raw_lease.strip() or not raw_lease:
        raise RouterError("lease-controller trigger returned no lease")
    lease = raw_lease
    threading.Thread(
        target=_delayed_pause,
        args=(lease, token),
        daemon=True,
        name=f"github-router-pause-{lease[:8]}",
    ).start()
    return {
        "lease": lease,
        "upstream_status": payload.get("upstream_status"),
    }


def _managed_repository(repository: str) -> bool:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        managed = state.get("managed_repositories") or []
    return repository.casefold() in {
        str(item).casefold() for item in managed
    }


def _installation_account(payload: dict[str, Any]) -> tuple[str, str]:
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return "", ""
    account = installation.get("account")
    if not isinstance(account, dict):
        return "", ""
    login = account.get("login")
    account_type = account.get("type")
    if not isinstance(login, str) or not isinstance(account_type, str):
        return "", ""
    if login != login.strip() or account_type != account_type.strip():
        return "", ""
    return login, account_type


def _owner_scope_matches(payload: dict[str, Any], repositories: list[str]) -> bool:
    configured_owner = GITHUB_OWNER.casefold()
    if not configured_owner or GITHUB_OWNER_TYPE not in {"personal", "organization"}:
        return False
    for repository in repositories:
        owner, _, _ = repository.partition("/")
        if owner.casefold() != configured_owner:
            return False
    account_login, account_type = _installation_account(payload)
    if "installation" in payload and (not account_login or not account_type):
        return False
    if account_login and account_login.casefold() != configured_owner:
        return False
    if account_type:
        expected_type = "User" if GITHUB_OWNER_TYPE == "personal" else "Organization"
        if account_type.casefold() != expected_type.casefold():
            return False
    return True


def _installation_context_matches(payload: dict[str, Any]) -> bool:
    account_login, account_type = _installation_account(payload)
    expected_type = "User" if GITHUB_OWNER_TYPE == "personal" else "Organization"
    return (
        bool(account_login)
        and account_login.casefold() == GITHUB_OWNER.casefold()
        and account_type.casefold() == expected_type.casefold()
    )


def _has_app_installation_context(payload: dict[str, Any]) -> bool:
    account_login, _ = _installation_account(payload)
    return bool(account_login) and account_login.casefold() == GITHUB_OWNER.casefold()


def _repository_from_event_item(item: object) -> str:
    if not isinstance(item, dict):
        raise RouterError("repository_missing")
    repository = item.get("full_name")
    if (
        not isinstance(repository, str)
        or repository != repository.strip()
        or not _REPOSITORY_RE.fullmatch(repository)
    ):
        raise RouterError("repository_missing")
    return repository


def _event_repositories(event: str, payload: dict[str, Any]) -> list[str]:
    """Extract a bounded repository set from App and repository deliveries."""
    if event == "installation":
        raw_items = payload.get("repositories")
        if raw_items is None:
            return []
    elif event == "installation_repositories":
        if str(payload.get("action") or "").strip().casefold() == "removed":
            return []
        raw_items = payload.get("repositories_added")
        if raw_items is None:
            return []
    else:
        raw_items = [payload.get("repository")]
    if not isinstance(raw_items, list):
        raise RouterError("repository_missing")
    if len(raw_items) > _MAX_SCOPE_REPOSITORIES:
        raise RouterError("repository_scope_too_large")
    repositories: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        repository = _repository_from_event_item(item)
        key = repository.casefold()
        if key not in seen:
            repositories.append(repository)
            seen.add(key)
    return repositories


def _ignored_event_response(
    *,
    reason: str,
    event: str,
    delivery_id: str,
    repositories: list[str] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "ignored": True,
        "reason": reason,
        "event": event,
        "delivery": delivery_id,
    }
    if repositories:
        response["repositories"] = repositories
        if len(repositories) == 1:
            response["repository"] = repositories[0]
    return response


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGitHubRouter/2"

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"{self.address_string()} [{self.log_date_time_string()}] {format % args}",
            flush=True,
        )

    def _send_json(self, status: int, value: object) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_scope_control_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise RouterError("invalid_scope_control_body")
        length = int(raw_length)
        if length <= 0 or length > 4096:
            raise RouterError("invalid_scope_control_body")
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise RouterError("invalid_scope_control_body")
            payload = json.loads(
                body,
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise RouterError("invalid_scope_control_body") from exc
        if not isinstance(payload, dict):
            raise RouterError("invalid_scope_control_body")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            # Fixed minimal liveness body. The funnel proxies public traffic
            # to this handler from a local source address, so client-address
            # checks cannot distinguish external callers; nothing beyond
            # liveness may be disclosed here. Queue depth, secret-file
            # presence, and URL configuration are served by the
            # Bearer-authenticated /debug/state endpoint instead.
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/debug/state":
            authorization = self.headers.get("Authorization", "").strip()
            if not _service_authorized(authorization):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "authorization_required"},
                )
                return
            try:
                status = _queue_status()
            except RouterError as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    **status,
                    "public_url_configured": bool(PUBLIC_URL),
                    "github_token_configured": GITHUB_TOKEN_FILE.is_file(),
                    "webhook_secret_configured": WEBHOOK_SECRET_FILE.is_file(),
                    "hermes_token_configured": INTAKE_TOKEN_FILE.is_file(),
                },
            )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found"},
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/github/hermes-intake":
            try:
                self._github_event()
            except RouterError as exc:
                try:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"ok": False, "error": str(exc)},
                    )
                except Exception:
                    self.close_connection = True
            except UnicodeEncodeError:
                # A header value (e.g. X-GitHub-Delivery) is not ASCII: the
                # signed-ingress surface must fail closed instead of killing
                # the request handler.
                try:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "invalid_header"},
                    )
                except Exception:
                    self.close_connection = True
            return
        authorization = self.headers.get("Authorization", "").strip()
        if not _service_authorized(authorization):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "authorization_required"},
            )
            return
        if parsed.path == "/scope/claim":
            try:
                item = _claim_scope()
            except RouterError as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, **item})
            return
        if parsed.path in {"/scope/ack", "/scope/requeue"}:
            try:
                payload = self._read_scope_control_body()
                scope_id = payload.get("id")
                if not isinstance(scope_id, str):
                    raise RouterError("invalid scope id")
                claim_token = payload.get("claim_token")
                if not isinstance(claim_token, str):
                    raise RouterError("invalid scope claim token")
                if parsed.path == "/scope/ack":
                    result = _ack_scope(scope_id, claim_token)
                else:
                    reason = payload.get("reason", "")
                    if not isinstance(reason, str):
                        raise RouterError("invalid scope reason")
                    result = _requeue_scope(scope_id, reason, claim_token)
            except RouterError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if "not in flight" in str(exc) or "claim token mismatch" in str(exc)
                    else HTTPStatus.BAD_REQUEST
                    if str(exc).startswith("invalid")
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._send_json(status, {"ok": False, "error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if parsed.path == "/reconcile":
            try:
                result = _reconcile_webhooks()
            except RouterError as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if parsed.path == "/fallback":
            reconcile_result: dict[str, Any] | None = None
            reconcile_warning = ""
            try:
                reconcile_result = _reconcile_webhooks()
            except RouterError as exc:
                reconcile_warning = str(exc)
            try:
                scope = _enqueue_scope(full=True)
                wake = _wake()
            except RouterError as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "mode": "fallback",
                    "scope": scope,
                    "wake": wake,
                    "reconcile_ok": reconcile_result is not None,
                    "reconcile": reconcile_result,
                    "reconcile_warning": reconcile_warning,
                },
            )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found"},
        )

    def _github_event(self) -> None:
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isascii() or not raw_length.isdigit():
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_body_length"},
            )
            return
        length = int(raw_length)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_body_length"},
            )
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "truncated_body"},
            )
            return
        signature = self.headers.get("X-Hub-Signature-256", "").strip()
        if not _github_signature_valid(body, signature):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "invalid_signature"},
            )
            return
        delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
        if not _DELIVERY_ID_RE.fullmatch(delivery_id):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_delivery_id"},
            )
            return
        try:
            _decision, is_duplicate = _claim_delivery(delivery_id)
        except RouterError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": str(exc)},
            )
            return
        if is_duplicate:
            print(
                "github-router duplicate/no-op delivery "
                f"delivery={delivery_id}",
                flush=True,
            )
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "ok": True,
                    "duplicate": True,
                    "reason": "duplicate_delivery",
                    "delivery": delivery_id,
                },
            )
            return
        event = self.headers.get("X-GitHub-Event", "").strip()
        if event == "ping":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "event": "ping", "delivery": delivery_id},
            )
            return
        if event not in SUPPORTED_EVENTS:
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "ok": True,
                    "ignored": True,
                    "reason": "unsupported_event",
                    "event": event,
                    "delivery": delivery_id,
                },
            )
            return
        try:
            payload = json.loads(body, object_pairs_hook=_reject_duplicate_pairs)
        except (json.JSONDecodeError, RecursionError, ValueError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_json"},
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "payload_invalid"},
            )
            return
        try:
            repositories = _event_repositories(event, payload)
        except RouterError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc)},
            )
            return
        if (
            event in {"installation", "installation_repositories"}
            and not _installation_context_matches(payload)
        ) or not _owner_scope_matches(payload, repositories):
            self._send_json(
                HTTPStatus.ACCEPTED,
                _ignored_event_response(
                    reason="owner_scope_mismatch",
                    event=event,
                    delivery_id=delivery_id,
                    repositories=repositories,
                ),
            )
            return
        if not repositories:
            reason = (
                "installation_no_repository_candidates"
                if event in {"installation", "installation_repositories"}
                else "repository_missing"
            )
            self._send_json(
                HTTPStatus.ACCEPTED,
                _ignored_event_response(
                    reason=reason,
                    event=event,
                    delivery_id=delivery_id,
                ),
            )
            return
        action = str(payload.get("action") or "").strip().casefold()
        if event == "repository" and action != "archived":
            self._send_json(
                HTTPStatus.ACCEPTED,
                _ignored_event_response(
                    reason="unsupported_repository_action",
                    event=event,
                    delivery_id=delivery_id,
                    repositories=repositories,
                ),
            )
            return
        unknown_repositories = [
            repository
            for repository in repositories
            if not _managed_repository(repository)
        ]
        if unknown_repositories and not _has_app_installation_context(payload):
            self._send_json(
                HTTPStatus.ACCEPTED,
                _ignored_event_response(
                    reason="repository_not_managed",
                    event=event,
                    delivery_id=delivery_id,
                    repositories=unknown_repositories,
                ),
            )
            return
        # Only already-managed repositories may use the low-latency PR edge
        # sync.  An App delivery for an unknown repository must take the
        # onboarding queue so checkout/contract/board gates run first.
        if event == "pull_request" and len(repositories) == 1 and not unknown_repositories:
            repository = repositories[0]
            try:
                normalized = _normalise_pull_request_event(
                    payload,
                    repository,
                    delivery_id,
                )
            except RouterError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
                return
            try:
                edge_sync = _n8n_edge_sync(normalized)
            except RouterError as exc:
                # A failed n8n/actuator hop must remain retryable. The
                # delivery claim is released so GitHub can redeliver it.
                _release_delivery(delivery_id)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "ok": True,
                    "edge_sync": True,
                    "repository": repository,
                    "event": event,
                    "delivery": delivery_id,
                    "upstream_status": edge_sync["status"],
                },
            )
            return
        try:
            scope = _enqueue_scope(
                full=False,
                repositories=repositories,
                delivery_id=delivery_id,
            )
            if scope.get("in_flight") or scope.get("pending"):
                wake = {
                    "skipped": True,
                    "reason": "scope_already_active_or_pending",
                }
            else:
                wake = _wake()
        except RouterError as exc:
            # A failed dispatch (lease-controller/GitHub error) must not
            # suppress the delivery: release the claim so the GitHub 5xx
            # retry or an operator resend can dispatch again.
            _release_delivery(delivery_id)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": str(exc)},
            )
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "queued": True,
                "repository": repositories[0] if len(repositories) == 1 else None,
                "repositories": repositories,
                "event": event,
                "delivery": delivery_id,
                "scope": scope,
                "wake": wake,
            },
        )
        return


def main() -> int:
    if (
        not GITHUB_OWNER
        or not GITHUB_TOPIC
        or GITHUB_OWNER_TYPE not in {"personal", "organization"}
    ):
        raise SystemExit(
            "GITHUB_ROUTER_OWNER/TOPIC are required and OWNER_TYPE must be "
            "personal or organization"
        )
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(
        f"github-router listening on http://{LISTEN_HOST}:{LISTEN_PORT}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
