#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import http.client
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "github-router" / "router.py"
spec = importlib.util.spec_from_file_location("github_router", MODULE_PATH)
assert spec and spec.loader
router: Any = importlib.util.module_from_spec(spec)
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
        "HERMES_TOKEN_FILE": router.INTAKE_TOKEN_FILE,
        "PUBLIC_URL": router.PUBLIC_URL,
        "DELIVERY_TTL_SECONDS": router.DELIVERY_TTL_SECONDS,
        "DELIVERY_MAX_ENTRIES": router.DELIVERY_MAX_ENTRIES,
    }
    secret_dir = root / "secrets"
    secret_dir.mkdir()
    router.STATE_PATH = root / "state.json"
    router.GITHUB_TOKEN_FILE = secret_dir / "github-token"
    router.WEBHOOK_SECRET_FILE = secret_dir / "github-webhook-secret"
    router.INTAKE_TOKEN_FILE = secret_dir / "hermes-intake-control-token"
    router.PUBLIC_URL = "https://example.test/github/hermes-intake"
    router.GITHUB_TOKEN_FILE.write_text("github-token", encoding="utf-8")
    router.WEBHOOK_SECRET_FILE.write_text("webhook-secret", encoding="utf-8")
    router.INTAKE_TOKEN_FILE.write_text("hermes-token", encoding="utf-8")
    return original


def _restore(original: dict) -> None:
    for name, value in original.items():
        setattr(router, name, value)


def _signed_headers(
    body: bytes, *, event: str = "issue_comment", delivery: str | None = None
) -> dict[str, str]:
    secret = router.WEBHOOK_SECRET_FILE.read_text(encoding="utf-8").strip()
    signature = "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery if delivery is not None else str(uuid.uuid4()),
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
            router.__dict__["_wake"] = lambda: (_ for _ in ()).throw(
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
            router.__dict__["_wake"] = original_wake
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
            router.__dict__["_wake"] = lambda: {
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
            router.__dict__["_wake"] = original_wake
            _restore(original)


def _pull_request_body(
    *,
    action: str,
    merged: bool,
    label: str | None = None,
) -> bytes:
    payload = {
        "action": action,
        "repository": {"full_name": "rhgo1749/ctrl-hangul"},
        "pull_request": {"merged": merged},
    }
    if label is not None:
        payload["label"] = {"name": label}
    return json.dumps(payload).encode()


def test_merged_pull_request_routes_to_n8n_edge_sync_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_edge_sync = router._n8n_edge_sync
        original_wake = router._wake
        forwarded: list[dict] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_n8n_edge_sync"] = lambda event: (
                forwarded.append(event),
                {"status": 200, "body": {"ok": True}},
            )[1]
            router.__dict__["_wake"] = lambda: (_ for _ in ()).throw(
                AssertionError("PR edge sync must not wake legacy intake")
            )
            body = _pull_request_body(action="closed", merged=True)
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request",
                        delivery="delivery-merged",
                    ),
                )
                replay_status, replay = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request",
                        delivery="delivery-merged",
                    ),
                )
            assert status == 202
            assert payload["edge_sync"] is True
            assert replay_status == 202
            assert replay["duplicate"] is True
            assert forwarded == [
                {
                    "repository": "rhgo1749/ctrl-hangul",
                    "event": "pull_request",
                    "action": "closed",
                    "merged": True,
                    "label": "",
                    "delivery": "delivery-merged",
                }
            ]
            assert router._claim_scope()["mode"] == "none"
        finally:
            router.__dict__["_n8n_edge_sync"] = original_edge_sync
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_agent_rework_label_routes_bounded_event_to_n8n() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_edge_sync = router._n8n_edge_sync
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            forwarded: list[dict] = []
            router.__dict__["_n8n_edge_sync"] = lambda event: (
                forwarded.append(event),
                {"status": 200, "body": {"ok": True}},
            )[1]
            body = _pull_request_body(
                action="labeled",
                merged=False,
                label="agent-rework",
            )
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request",
                        delivery="delivery-rework",
                    ),
                )
            assert status == 202
            assert payload["edge_sync"] is True
            assert forwarded[0]["action"] == "labeled"
            assert forwarded[0]["label"] == "agent-rework"
            assert set(forwarded[0]) == {
                "repository",
                "event",
                "action",
                "merged",
                "label",
                "delivery",
            }
        finally:
            router.__dict__["_n8n_edge_sync"] = original_edge_sync
            _restore(original)


def test_malformed_pull_request_event_fails_closed_without_n8n() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_edge_sync = router._n8n_edge_sync
        calls = 0
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )

            def unexpected(event):
                nonlocal calls
                calls += 1
                raise AssertionError(event)

            router.__dict__["_n8n_edge_sync"] = unexpected
            body = json.dumps(
                {
                    "action": "closed",
                    "repository": {"full_name": "rhgo1749/ctrl-hangul"},
                }
            ).encode()
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request",
                        delivery="delivery-malformed-pr",
                    ),
                )
            assert status == 400
            assert payload["error"] == "pull_request_missing"
            assert calls == 0
            assert router._claim_scope()["mode"] == "none"
        finally:
            router.__dict__["_n8n_edge_sync"] = original_edge_sync
            _restore(original)


def test_n8n_edge_sync_failure_releases_delivery_for_retry() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_edge_sync = router._n8n_edge_sync
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_n8n_edge_sync"] = lambda event: (_ for _ in ()).throw(
                router.RouterError("n8n edge-sync unavailable")
            )
            body = _pull_request_body(action="closed", merged=True)
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request",
                        delivery="delivery-edge-retry",
                    ),
                )
            assert status == 502
            assert payload["error"] == "n8n edge-sync unavailable"
            assert router._load_state_unlocked().get("delivery_dedupe") in (
                None,
                {},
            )
        finally:
            router.__dict__["_n8n_edge_sync"] = original_edge_sync
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
            router.__dict__["_wake"] = lambda: {
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
            router.__dict__["_wake"] = original_wake
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


def _post_event(
    base_url: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[int, dict]:
    return _request(
        base_url,
        "POST",
        "/github/hermes-intake",
        body=body,
        headers=headers,
    )


def _event_body(repository: str = "rhgo1749/ctrl-hangul") -> bytes:
    return json.dumps({"repository": {"full_name": repository}}).encode()


def test_duplicate_delivery_is_noop_single_wake() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[dict] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append({"lease": "lease-dedupe"}),
                {"lease": "lease-dedupe", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-42"),
                )
                assert status == 202
                assert payload["queued"] is True
                assert payload["delivery"] == "delivery-42"
                assert "duplicate" not in payload
                status2, payload2 = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-42"),
                )
                assert status2 == 202
                assert payload2["duplicate"] is True
                assert payload2["reason"] == "duplicate_delivery"
                assert "queued" not in payload2
                assert len(wakes) == 1
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_duplicate_delivery_survives_restart() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-restart", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as first:
                status, _ = _post_event(
                    first.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-restart"),
                )
                assert status == 202
            assert len(wakes) == 1
            state = router._load_state_unlocked()
            assert "delivery-restart" in state["delivery_dedupe"]
            # Simulated restart: a fresh server process instance reads the
            # same persisted state file.
            with RunningServer() as second:
                status2, payload2 = _post_event(
                    second.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-restart"),
                )
                assert status2 == 202
                assert payload2["duplicate"] is True
            assert len(wakes) == 1
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_delivery_ttl_expiry_allows_reprocess() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-ttl", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as server:
                _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-ttl"),
                )
                # Expire the recorded claim without waiting in real time.
                state = router._load_state_unlocked()
                entry = state["delivery_dedupe"]["delivery-ttl"]
                state["delivery_dedupe"]["delivery-ttl"] = {
                    "created_at": entry["created_at"] - 7200,
                    "expires_at": entry["created_at"] - 3600,
                }
                router._write_state_unlocked(state)
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-ttl"),
                )
                assert status == 202
                assert payload["queued"] is True
                assert "duplicate" not in payload
                assert len(wakes) == 2
                # The expired entry was pruned and re-claimed: the record now
                # carries a fresh TTL window, not the expired one.
                state = router._load_state_unlocked()
                reentry = state["delivery_dedupe"]["delivery-ttl"]
                assert reentry["expires_at"] > int(time.time())
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_distinct_delivery_ids_dispatch_independently() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-distinct", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as server:
                for delivery_id in ("delivery-A", "delivery-B"):
                    status, payload = _post_event(
                        server.base_url,
                        body,
                        _signed_headers(body, delivery=delivery_id),
                    )
                    assert status == 202
                    assert payload["queued"] is True
                    assert payload["delivery"] == delivery_id
                assert len(wakes) == 2
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_invalid_signature_not_recorded_in_dedupe_store() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-sig", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-sig",
                        "X-Hub-Signature-256": "sha256=bad",
                    },
                )
                assert status == 401
                assert payload["error"] == "invalid_signature"
                state = router._load_state_unlocked()
                assert state.get("delivery_dedupe") in (None, {})
                assert wakes == []
                # The same delivery ID is processable once signed validly.
                status2, payload2 = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-sig"),
                )
                assert status2 == 202
                assert payload2["queued"] is True
                assert len(wakes) == 1
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_missing_delivery_id_rejected_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-missing", "upstream_status": 200},
            )[1]
            body = _event_body()
            headers = _signed_headers(body, delivery="delivery-missing")
            del headers["X-GitHub-Delivery"]
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    headers,
                )
                assert status == 400
                assert payload["error"] == "invalid_delivery_id"
                state = router._load_state_unlocked()
                assert state.get("delivery_dedupe") in (None, {})
                assert wakes == []
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_invalid_delivery_id_rejected_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-invalid-id", "upstream_status": 200},
            )[1]
            body = _event_body()
            with RunningServer() as server:
                for bad_id in ("bad id with space", "x" * 200):
                    status, payload = _post_event(
                        server.base_url,
                        body,
                        _signed_headers(body, delivery=bad_id),
                    )
                    assert status == 400
                    assert payload["error"] == "invalid_delivery_id"
                # A NUL byte in the delivery header is rejected fail-closed:
                # either the HTTP client refuses the raw header byte, or the
                # router's signed-ingress surface answers 400 without a wake.
                try:
                    status, payload = _post_event(
                        server.base_url,
                        body,
                        _signed_headers(body, delivery="bad\x00id"),
                    )
                except (HTTPError, URLError, http.client.HTTPException):
                    pass
                else:
                    assert status == 400
                    assert payload["error"] in {
                        "invalid_delivery_id",
                        "invalid_header",
                    }
                assert wakes == []
                state = router._load_state_unlocked()
                assert state.get("delivery_dedupe") in (None, {})
                # Surrounding whitespace is trimmed, the ID stays valid: the
                # delivery processes normally and dispatches exactly once.
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="  delivery-padded  "),
                )
                assert status == 202
                assert payload["queued"] is True
                assert payload["delivery"] == "delivery-padded"
                assert wakes == ["w"]
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_concurrent_duplicate_requests_dispatch_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wake_lock = threading.Lock()
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )

            def counting_wake() -> dict:
                time.sleep(0.05)
                with wake_lock:
                    wakes.append("w")
                return {"lease": "lease-concurrent", "upstream_status": 200}

            router.__dict__["_wake"] = counting_wake
            body = _event_body()
            barrier = threading.Barrier(2)

            def send() -> None:
                with RunningServer() as server:
                    barrier.wait()
                    _post_event(
                        server.base_url,
                        body,
                        _signed_headers(body, delivery="delivery-concurrent"),
                    )

            threads = [
                threading.Thread(target=send),
                threading.Thread(target=send),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert len(wakes) == 1
            state = router._load_state_unlocked()
            assert list(state["delivery_dedupe"]) == ["delivery-concurrent"]
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_dispatch_failure_releases_delivery_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes: list[str] = []
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = (
                lambda: (_ for _ in ()).throw(
                    router.RouterError("lease-controller unavailable")
                )
            )
            body = _event_body()
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-retry"),
                )
                assert status == 502
                assert "lease-controller unavailable" in payload["error"]
                state = router._load_state_unlocked()
                assert state.get("delivery_dedupe") in (None, {})
            # After the failure the same delivery can dispatch again: this is
            # the GitHub 5xx retry / operator resend recovery path.
            router.__dict__["_wake"] = lambda: (
                wakes.append("w"),
                {"lease": "lease-retry", "upstream_status": 200},
            )[1]
            with RunningServer() as server:
                status2, payload2 = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(body, delivery="delivery-retry"),
                )
                assert status2 == 202
                assert payload2["queued"] is True
                assert len(wakes) == 1
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_delivery_store_is_bounded_and_pruned() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            router.DELIVERY_MAX_ENTRIES = 3
            for index in range(4):
                router._claim_delivery(f"delivery-{index}")
            state = router._load_state_unlocked()
            entries = state["delivery_dedupe"]
            assert len(entries) == 3
            assert "delivery-0" not in entries
            assert {
                "delivery-1",
                "delivery-2",
                "delivery-3",
            } == set(entries)
            # An expired entry is pruned on the next production read path
            # (claim/release always bound the map before persisting it).
            entry = entries["delivery-1"]
            state["delivery_dedupe"]["delivery-1"] = {
                "created_at": entry["created_at"] - 7200,
                "expires_at": entry["created_at"] - 3600,
            }
            router._write_state_unlocked(state)
            router._claim_delivery("delivery-new")
            state = router._load_state_unlocked()
            assert "delivery-1" not in state["delivery_dedupe"]
            assert "delivery-new" in state["delivery_dedupe"]
        finally:
            _restore(original)



def test_pull_request_review_event_enqueues_repo_scope() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        try:
            router._write_state_unlocked(
                {"managed_repositories": ["rhgo1749/ctrl-hangul"]}
            )
            router.__dict__["_wake"] = lambda: {
                "lease": "lease-pr-review",
                "upstream_status": 200,
            }

            body = _event_body()
            with RunningServer() as server:
                status, payload = _post_event(
                    server.base_url,
                    body,
                    _signed_headers(
                        body,
                        event="pull_request_review",
                        delivery="delivery-pr-review",
                    ),
                )

            claim = router._claim_scope()

            assert status == 202
            assert payload["queued"] is True
            assert payload["event"] == "pull_request_review"
            assert payload["repository"] == "rhgo1749/ctrl-hangul"
            assert claim["mode"] == "event"
            assert claim["repositories"] == ["rhgo1749/ctrl-hangul"]
        finally:
            router.__dict__["_wake"] = original_wake
            _restore(original)


def test_webhook_subscription_includes_pull_request_review() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_list_hooks = router._list_hooks
        original_request = router._github_request
        captured = {}

        try:
            router._list_hooks = lambda repository, token: []

            def fake_request(method, path, token, payload=None):
                if method == "POST" and path.endswith("/hooks"):
                    captured["payload"] = payload
                    return {"id": 10}
                raise AssertionError(
                    f"unexpected GitHub request: {method} {path}"
                )

            router._github_request = fake_request

            action, duplicates_removed = router._ensure_webhook(
                "rhgo1749/ctrl-hangul",
                "github-token",
                router.PUBLIC_URL,
                "webhook-secret",
            )

            assert action == "created"
            assert duplicates_removed == 0
            assert captured["payload"]["events"] == [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
            ]
        finally:
            router._list_hooks = original_list_hooks
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

def test_healthz_is_always_minimal() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            with RunningServer() as server:
                status, payload = _request(server.base_url, "GET", "/healthz")
            assert status == 200
            # The funnel proxies public traffic from a local source address,
            # so healthz must be a fixed minimal body for every caller.
            assert payload == {"ok": True}
        finally:
            _restore(original)


def test_debug_state_requires_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            with RunningServer() as server:
                status, payload = _request(server.base_url, "GET", "/debug/state")
                assert status == 401
                assert payload["error"] == "authorization_required"

                status, payload = _request(
                    server.base_url,
                    "GET",
                    "/debug/state",
                    headers={"Authorization": "Bearer wrong-token"},
                )
                assert status == 401

                token = router.INTAKE_TOKEN_FILE.read_text(encoding="utf-8").strip()
                status, payload = _request(
                    server.base_url,
                    "GET",
                    "/debug/state",
                    headers={"Authorization": f"Bearer {token}"},
                )
            assert status == 200
            assert payload["ok"] is True
            assert "queued_scopes" in payload
            assert "public_url_configured" in payload
            assert "github_token_configured" in payload
            assert "webhook_secret_configured" in payload
            assert "hermes_token_configured" in payload
        finally:
            _restore(original)


def test_unknown_get_path_is_not_found() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        try:
            with RunningServer() as server:
                status, payload = _request(server.base_url, "GET", "/nope")
            assert status == 404
            assert payload["error"] == "not_found"
        finally:
            _restore(original)


def test_app_event_discovers_unknown_repository_and_wakes_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes = []
        try:
            router.GITHUB_OWNER = "rhgo1749"
            router.GITHUB_OWNER_TYPE = "personal"
            router._write_state_unlocked({"managed_repositories": []})
            router._wake = lambda: wakes.append(True)
            body = json.dumps(
                {
                    "action": "opened",
                    "installation": {
                        "account": {"login": "rhgo1749", "type": "User"}
                    },
                    "repository": {"full_name": "rhgo1749/new-agent"},
                }
            ).encode()

            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers=_signed_headers(body, event="issues", delivery="app-1"),
                )

            assert status == 202
            assert payload["queued"] is True
            assert payload["repositories"] == ["rhgo1749/new-agent"]
            assert wakes == [True]
            state = router._load_state_unlocked()
            assert state["scope_queue"][0]["repositories"] == ["rhgo1749/new-agent"]
        finally:
            router._wake = original_wake
            _restore(original)


def test_app_event_queues_unknown_pull_request_without_edge_sync() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        original_edge = router._n8n_edge_sync
        wakes = []
        try:
            router.GITHUB_OWNER = "rhgo1749"
            router.GITHUB_OWNER_TYPE = "personal"
            router._write_state_unlocked({"managed_repositories": []})
            router._wake = lambda: wakes.append(True)
            router._n8n_edge_sync = lambda event: (_ for _ in ()).throw(
                AssertionError("unknown repository must not edge-sync")
            )
            body = json.dumps(
                {
                    "action": "closed",
                    "installation": {
                        "account": {"login": "rhgo1749", "type": "User"}
                    },
                    "repository": {"full_name": "rhgo1749/new-agent"},
                    "pull_request": {"merged": False},
                }
            ).encode()

            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers=_signed_headers(
                        body,
                        event="pull_request",
                        delivery="app-pr-1",
                    ),
                )

            assert status == 202
            assert payload["queued"] is True
            assert wakes == [True]
        finally:
            router._wake = original_wake
            router._n8n_edge_sync = original_edge
            _restore(original)


def test_installation_repository_batch_is_deduplicated_and_queued() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes = []
        try:
            router.GITHUB_OWNER = "rhgo1749"
            router.GITHUB_OWNER_TYPE = "personal"
            router._write_state_unlocked({"managed_repositories": []})
            router._wake = lambda: wakes.append(True)
            body = json.dumps(
                {
                    "action": "created",
                    "installation": {
                        "account": {"login": "rhgo1749", "type": "User"}
                    },
                    "repositories": [
                        {"full_name": "rhgo1749/one"},
                        {"full_name": "rhgo1749/two"},
                        {"full_name": "rhgo1749/ONE"},
                    ],
                }
            ).encode()

            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers=_signed_headers(
                        body,
                        event="installation",
                        delivery="install-1",
                    ),
                )

            assert status == 202
            assert payload["repositories"] == [
                "rhgo1749/one",
                "rhgo1749/two",
            ]
            assert wakes == [True]
        finally:
            router._wake = original_wake
            _restore(original)


def test_foreign_app_installation_is_rejected_without_queueing() -> None:
    with tempfile.TemporaryDirectory() as td:
        original = _install_temp_paths(Path(td))
        original_wake = router._wake
        wakes = []
        try:
            router.GITHUB_OWNER = "rhgo1749"
            router.GITHUB_OWNER_TYPE = "personal"
            router._write_state_unlocked({"managed_repositories": []})
            router._wake = lambda: wakes.append(True)
            body = json.dumps(
                {
                    "installation": {
                        "account": {"login": "other-owner", "type": "User"}
                    },
                    "repository": {"full_name": "other-owner/private"},
                }
            ).encode()

            with RunningServer() as server:
                status, payload = _request(
                    server.base_url,
                    "POST",
                    "/github/hermes-intake",
                    body=body,
                    headers=_signed_headers(body, event="issues", delivery="foreign-1"),
                )

            assert status == 202
            assert payload["reason"] == "owner_scope_mismatch"
            assert wakes == []
            assert router._load_state_unlocked().get("scopes", []) == []
        finally:
            router._wake = original_wake
            _restore(original)
