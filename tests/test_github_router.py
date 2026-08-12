#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "github-router" / "router.py"
spec = importlib.util.spec_from_file_location("github_router", MODULE_PATH)
assert spec and spec.loader
router = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = router
spec.loader.exec_module(router)


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request = Request(
        base_url + path,
        method=method,
        data=body if method == "POST" else None,
        headers=headers or {},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


class RunningServer:
    def __init__(self) -> None:
        self.server = router.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            router.Handler,
        )
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _install_temp_paths(root: Path):
    original = {
        "STATE_PATH": router.STATE_PATH,
        "GITHUB_TOKEN_FILE": router.GITHUB_TOKEN_FILE,
        "WEBHOOK_SECRET_FILE": router.WEBHOOK_SECRET_FILE,
        "HERMES_TOKEN_FILE": router.HERMES_TOKEN_FILE,
        "PUBLIC_URL": router.PUBLIC_URL,
    }
    secret_dir = root / "secrets"
    secret_dir.mkdir()
    router.STATE_PATH = root / "state.json"
    router.GITHUB_TOKEN_FILE = secret_dir / "github-token"
    router.WEBHOOK_SECRET_FILE = secret_dir / "github-webhook-secret"
    router.HERMES_TOKEN_FILE = secret_dir / "hermes-cron-token"
    router.PUBLIC_URL = "https://example.test/github/hermes-intake"
    router.GITHUB_TOKEN_FILE.write_text("github-token", encoding="utf-8")
    router.WEBHOOK_SECRET_FILE.write_text("webhook-secret", encoding="utf-8")
    router.HERMES_TOKEN_FILE.write_text("hermes-token", encoding="utf-8")
    return original


def _restore(original: dict) -> None:
    for name, value in original.items():
        setattr(router, name, value)


def _signed_headers(body: bytes, *, event: str = "issue_comment") -> dict[str, str]:
    secret = router.WEBHOOK_SECRET_FILE.read_text(encoding="utf-8").strip()
    signature = "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "delivery-test",
        "X-Hub-Signature-256": signature,
    }


def test_invalid_signature_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router._wake = lambda: (_ for _ in ()).throw(
                AssertionError("invalid signature reached wake")
            )
            body = json.dumps(
                {"repository": {"full_name": "rhgo1749/ctrl-hangul"}}
            ).encode()
            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "issue_comment",
                        "X-Hub-Signature-256": "sha256=bad",
                    },
                )
            assert status == 401
            assert payload["error"] == "invalid_signature"
        finally:
            router._wake = original_wake
            _restore(original)


def test_scope_claim_requires_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            router._enqueue_scope(
                full=False,
                repository="rhgo1749/ctrl-hangul",
            )
            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/scope/claim",
                )
                auth_status, claim = _request(
                    server.base_url,
                    "POST",
                    "/scope/claim",
                    headers={"Authorization": "Bearer hermes-token"},
                )
            assert status == 401
            assert payload["error"] == "authorization_required"
            assert auth_status == 200
            assert claim["mode"] == "event"
            assert claim["repositories"] == ["rhgo1749/ctrl-hangul"]
        finally:
            _restore(original)


def test_valid_event_enqueues_one_repo_scope() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router._wake = lambda: {
                "lease": "lease-test",
                "upstream_status": 200,
            }
            body = json.dumps(
                {"repository": {"full_name": "rhgo1749/ctrl-hangul"}}
            ).encode()
            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers=_signed_headers(body),
                )
                claim_status, claim = _request(
                    server.base_url,
                    "POST",
                    "/scope/claim",
                    headers={"Authorization": "Bearer hermes-token"},
                )
            assert status == 202
            assert payload["queued"] is True
            assert claim_status == 200
            assert claim["mode"] == "event"
            assert claim["repositories"] == ["rhgo1749/ctrl-hangul"]
        finally:
            router._wake = original_wake
            _restore(original)


def test_fifo_scope_queue_keeps_fallback_and_event_distinct() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            router._enqueue_scope(full=True)
            router._enqueue_scope(
                full=False,
                repository="rhgo1749/ctrl-hangul",
            )
            first = router._claim_scope()
            second = router._claim_scope()
            third = router._claim_scope()
            assert first["mode"] == "full"
            assert first["repositories"] == []
            assert second["mode"] == "event"
            assert second["repositories"] == ["rhgo1749/ctrl-hangul"]
            assert third["mode"] == "none"
        finally:
            _restore(original)


def test_fallback_survives_reconcile_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_reconcile = router._reconcile_webhooks
        original_wake = router._wake
        try:
            router._reconcile_webhooks = (
                lambda: (_ for _ in ()).throw(
                    router.RouterError("reconcile unavailable")
                )
            )
            router._wake = lambda: {
                "lease": "lease-fallback",
                "upstream_status": 200,
            }
            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/fallback",
                    headers={"Authorization": "Bearer hermes-token"},
                )
                _, claim = _request(
                    server.base_url,
                    "POST",
                    "/scope/claim",
                    headers={"Authorization": "Bearer hermes-token"},
                )
            assert status == 200
            assert payload["reconcile_ok"] is False
            assert "reconcile unavailable" in payload["reconcile_warning"]
            assert claim["mode"] == "full"
        finally:
            router._reconcile_webhooks = original_reconcile
            router._wake = original_wake
            _restore(original)


def test_reconcile_creates_and_removes_only_router_webhooks() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_discover = router.registry.discover_repositories
        original_request = router._github_request
        calls: list[tuple[str, str]] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/old-repo"]}
            )
            router.registry.discover_repositories = (
                lambda token, owner, topic: [
                    {
                        "full_name": "rhgo1749/new-repo",
                        "id": 1,
                        "default_branch": "main",
                        "owner": {"login": "rhgo1749"},
                    }
                ]
            )

            def fake_request(method, path, token, payload=None):
                calls.append((method, path))
                if method == "GET" and "new-repo/hooks" in path:
                    return []
                if method == "POST" and "new-repo/hooks" in path:
                    return {"id": 10}
                if method == "GET" and "old-repo/hooks" in path:
                    return [
                        {
                            "id": 7,
                            "config": {"url": router.PUBLIC_URL},
                        },
                        {
                            "id": 8,
                            "config": {"url": "https://other.example/hook"},
                        },
                    ]
                if method == "DELETE" and "old-repo/hooks/7" in path:
                    return None
                raise AssertionError(f"unexpected GitHub request: {method} {path}")

            router._github_request = fake_request
            result = router._reconcile_webhooks()
            assert result["managed"] == 1
            assert result["created"] == 1
            assert result["deleted"] == 1
            state = router._load_state_unlocked()
            assert state["managed_repositories"] == ["rhgo1749/new-repo"]
            assert not any("hooks/8" in path for _, path in calls)
        finally:
            router.registry.discover_repositories = original_discover
            router._github_request = original_request
            _restore(original)


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
