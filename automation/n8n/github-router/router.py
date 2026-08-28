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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GITHUB_ROUTER_LISTEN_PORT", "5681"))

GITHUB_API = "https://api.github.com"
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
N8N_EDGE_SYNC_MAX_RESPONSE_BYTES = 64 * 1024
WAIT_SECONDS = int(os.environ.get("GITHUB_ROUTER_WAIT_SECONDS", "75"))
SCOPE_TTL_SECONDS = int(
    os.environ.get(
        "GITHUB_ROUTER_SCOPE_TTL_SECONDS",
        str(max(WAIT_SECONDS + 120, 300)),
    )
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
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_SCOPE_REPOSITORIES = 100
_N8N_EDGE_SYNC_PATH = "/webhook/hermes-github-edge-sync"
_STATE_LOCK = threading.Lock()


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
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RouterError(f"cannot read router state: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
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


def _prune_queue(raw_queue: object, now: int | None = None) -> list[dict[str, Any]]:
    now = int(time.time()) if now is None else now
    if not isinstance(raw_queue, list):
        return []
    queue: list[dict[str, Any]] = []
    for raw in raw_queue:
        if not isinstance(raw, dict):
            continue
        try:
            expires_at = int(raw.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if expires_at <= now:
            continue
        mode = str(raw.get("mode") or "")
        if mode not in {"event", "full"}:
            continue
        raw_repositories = raw.get("repositories", [])
        if not isinstance(raw_repositories, list):
            continue
        repositories = sorted(
            {
                str(repository).strip()
                for repository in raw_repositories
                if _REPOSITORY_RE.match(str(repository).strip())
            },
            key=str.casefold,
        )[:_MAX_SCOPE_REPOSITORIES]
        if mode == "event" and not repositories:
            continue
        queue.append(
            {
                "id": str(raw.get("id") or uuid.uuid4()),
                "mode": mode,
                "repositories": repositories,
                "created_at": int(raw.get("created_at") or now),
                "expires_at": expires_at,
            }
        )
    return queue


def _enqueue_scope(
    *,
    full: bool,
    repository: str | None = None,
    repositories: list[str] | tuple[str, ...] | None = None,
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
            candidate = str(raw_repository).strip()
            if not _REPOSITORY_RE.match(candidate):
                raise RouterError(f"invalid repository identity: {candidate!r}")
            key = candidate.casefold()
            if key not in seen:
                scoped_repositories.append(candidate)
                seen.add(key)
        if not scoped_repositories or len(scoped_repositories) > _MAX_SCOPE_REPOSITORIES:
            raise RouterError("invalid repository scope")
    item = {
        "id": str(uuid.uuid4()),
        "mode": "full" if full else "event",
        "repositories": scoped_repositories,
        "created_at": now,
        "expires_at": now + SCOPE_TTL_SECONDS,
    }
    with _STATE_LOCK:
        state = _load_state_unlocked()
        queue = _prune_queue(state.get("scope_queue"), now)
        queue.append(item)
        state["scope_queue"] = queue
        _write_state_unlocked(state)
    return item


def _claim_scope() -> dict[str, Any]:
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        queue = _prune_queue(state.get("scope_queue"), now)
        if queue:
            item = queue.pop(0)
            state["scope_queue"] = queue
            state["last_claimed_scope"] = item
            _write_state_unlocked(state)
            return item
        state["scope_queue"] = []
        _write_state_unlocked(state)
    return {
        "id": "",
        "mode": "none",
        "repositories": [],
        "created_at": now,
        "expires_at": 0,
    }


def _queue_status() -> dict[str, Any]:
    now = int(time.time())
    with _STATE_LOCK:
        state = _load_state_unlocked()
        queue = _prune_queue(state.get("scope_queue"), now)
        if queue != state.get("scope_queue"):
            state["scope_queue"] = queue
            _write_state_unlocked(state)
        managed = [str(item) for item in state.get("managed_repositories", [])]
    return {
        "queued_scopes": len(queue),
        "managed_count": len(managed),
    }


def _prune_deliveries(raw_map: object, now: int | None = None) -> dict[str, dict[str, int]]:
    now = int(time.time()) if now is None else now
    if not isinstance(raw_map, dict):
        return {}
    kept: dict[str, dict[str, int]] = {}
    for raw_id, raw_entry in raw_map.items():
        delivery_id = str(raw_id).strip()
        if not _DELIVERY_ID_RE.match(delivery_id):
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
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw)
    except HTTPError as exc:
        # Never relay a provider response body: proxies can echo credentials
        # or credential-bearing URLs in an error payload.
        raise RouterError(
            f"GitHub API {method} {path} returned HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RouterError(
            f"GitHub API {method} {path} failed: {type(exc).__name__}"
        ) from exc


def _validated_public_url() -> str:
    value = PUBLIC_URL.strip()
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
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
            and _REPOSITORY_RE.match(
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
            if _REPOSITORY_RE.match(str(repository))
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


def _lease_request(path: str, token: str) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"{LEASE_BASE_URL}{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        data=b"",
    )
    try:
        with urlopen(request, timeout=65) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return int(response.status), payload
    except HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        return int(exc.code), payload
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RouterError(
            f"lease-controller request failed: {type(exc).__name__}"
        ) from exc


def _validated_n8n_edge_sync_url() -> str:
    value = N8N_EDGE_SYNC_URL
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != _N8N_EDGE_SYNC_PATH
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RouterError("n8n edge-sync URL must be a loopback Webhook URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RouterError("n8n edge-sync URL has an invalid port") from exc
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
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RouterError("n8n edge-sync response is invalid JSON") from exc
            if not isinstance(body, dict):
                raise RouterError("n8n edge-sync response is not an object")
            return {"status": int(response.status), "body": body}
    except HTTPError as exc:
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
    except Exception as exc:
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
    lease = str(payload.get("lease") or "").strip()
    if not lease:
        raise RouterError("lease-controller trigger returned no lease")
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
    return (
        str(account.get("login") or "").strip(),
        str(account.get("type") or "").strip(),
    )


def _owner_scope_matches(payload: dict[str, Any], repositories: list[str]) -> bool:
    configured_owner = GITHUB_OWNER.casefold()
    if not configured_owner or GITHUB_OWNER_TYPE not in {"personal", "organization"}:
        return False
    for repository in repositories:
        owner, _, _ = repository.partition("/")
        if owner.casefold() != configured_owner:
            return False
    account_login, account_type = _installation_account(payload)
    if account_login and account_login.casefold() != configured_owner:
        return False
    if account_type:
        expected_type = "User" if GITHUB_OWNER_TYPE == "personal" else "Organization"
        if account_type.casefold() != expected_type.casefold():
            return False
    return True


def _has_app_installation_context(payload: dict[str, Any]) -> bool:
    account_login, _ = _installation_account(payload)
    return bool(account_login) and account_login.casefold() == GITHUB_OWNER.casefold()


def _repository_from_event_item(item: object) -> str:
    if not isinstance(item, dict):
        raise RouterError("repository_missing")
    repository = str(item.get("full_name") or "").strip()
    if not _REPOSITORY_RE.fullmatch(repository):
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
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_body_length"},
            )
            return
        body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256", "").strip()
        if not _github_signature_valid(body, signature):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "invalid_signature"},
            )
            return
        delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
        if not _DELIVERY_ID_RE.match(delivery_id):
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
            payload = json.loads(body)
        except json.JSONDecodeError:
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
        if not _owner_scope_matches(payload, repositories):
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
            scope = _enqueue_scope(full=False, repositories=repositories)
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
