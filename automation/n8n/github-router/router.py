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
GITHUB_TOPIC = os.environ.get("GITHUB_ROUTER_TOPIC", "hermes-agent").strip()
PUBLIC_URL = os.environ.get("GITHUB_ROUTER_PUBLIC_URL", "").strip()
LEASE_BASE_URL = os.environ.get(
    "GITHUB_ROUTER_LEASE_BASE_URL",
    "http://127.0.0.1:5680",
).rstrip("/")
WAIT_SECONDS = int(os.environ.get("GITHUB_ROUTER_WAIT_SECONDS", "75"))
SCOPE_TTL_SECONDS = int(
    os.environ.get(
        "GITHUB_ROUTER_SCOPE_TTL_SECONDS",
        str(max(WAIT_SECONDS + 120, 300)),
    )
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
HERMES_TOKEN_FILE = Path(
    os.environ.get(
        "GITHUB_ROUTER_HERMES_TOKEN_FILE",
        "/run/secrets/hermes-cron-token",
    )
)
MAX_BODY_BYTES = 1024 * 1024
SUPPORTED_EVENTS = {"issues", "issue_comment", "pull_request"}
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
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
        repositories = sorted(
            {
                str(repository).strip()
                for repository in raw.get("repositories", [])
                if _REPOSITORY_RE.match(str(repository).strip())
            },
            key=str.casefold,
        )
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


def _enqueue_scope(*, full: bool, repository: str | None = None) -> dict[str, Any]:
    now = int(time.time())
    if repository is not None:
        repository = repository.strip()
        if not _REPOSITORY_RE.match(repository):
            raise RouterError(f"invalid repository identity: {repository!r}")
    item = {
        "id": str(uuid.uuid4()),
        "mode": "full" if full else "event",
        "repositories": [] if full else [repository],
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


def _service_authorized(header: str) -> bool:
    try:
        token = _read_secret(HERMES_TOKEN_FILE, "Hermes cron token")
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
        raw = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RouterError(
            f"GitHub API {method} {path} returned HTTP {exc.code}: {raw}"
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
        "events": ["issues", "issue_comment", "pull_request"],
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
    repositories = registry.discover_repositories(token, GITHUB_OWNER, GITHUB_TOPIC)
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
    token = _read_secret(HERMES_TOKEN_FILE, "Hermes cron token")
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


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGitHubRouter/2"

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}",
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
        if parsed.path != "/healthz":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
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
                "hermes_token_configured": HERMES_TOKEN_FILE.is_file(),
            },
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/github/hermes-intake":
            self._github_event()
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
        authorization = self.headers.get("Authorization", "").strip()
        if not _service_authorized(authorization):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "authorization_required"},
            )
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
        event = self.headers.get("X-GitHub-Event", "").strip()
        if event == "ping":
            self._send_json(HTTPStatus.OK, {"ok": True, "event": "ping"})
            return
        if event not in SUPPORTED_EVENTS:
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "ok": True,
                    "ignored": True,
                    "reason": "unsupported_event",
                    "event": event,
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
        repository = str(
            ((payload.get("repository") or {}).get("full_name")) or ""
        ).strip()
        if not _REPOSITORY_RE.match(repository):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "repository_missing"},
            )
            return
        try:
            if not _managed_repository(repository):
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        "ok": True,
                        "ignored": True,
                        "reason": "repository_not_managed",
                        "repository": repository,
                    },
                )
                return
            scope = _enqueue_scope(full=False, repository=repository)
            wake = _wake()
        except RouterError as exc:
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
                "repository": repository,
                "event": event,
                "scope": scope,
                "wake": wake,
            },
        )


def main() -> int:
    if not GITHUB_OWNER or not GITHUB_TOPIC:
        raise SystemExit(
            "GITHUB_ROUTER_OWNER and GITHUB_ROUTER_TOPIC are required"
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
