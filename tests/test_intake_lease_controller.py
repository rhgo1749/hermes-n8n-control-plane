#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "n8n"
    / "lease-controller"
    / "controller.py"
)

spec = importlib.util.spec_from_file_location("intake_lease_controller", MODULE_PATH)
assert spec and spec.loader
controller = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = controller
spec.loader.exec_module(controller)


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    authorization: str | None = "Bearer test-token",
) -> tuple[int, dict]:
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization

    request = Request(
        base_url + path,
        method=method,
        headers=headers,
        data=b"" if method == "POST" else None,
    )

    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for asynchronous lease state")


def _read_state() -> dict:
    try:
        return json.loads(controller.STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


class RunningServer:
    def __init__(self) -> None:
        self.server = controller.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            controller.Handler,
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


def test_trigger_returns_before_slow_hermes_completion() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        started = threading.Event()
        release = threading.Event()

        def fake_call(action: str, authorization: str):
            assert action == "trigger"
            assert authorization == "Bearer test-token"
            started.set()
            assert release.wait(timeout=2)
            return 200, b"{}"

        controller._call_hermes = fake_call

        try:
            with RunningServer() as server:
                begin = time.monotonic()
                status, body = _request(server.base_url, "POST", "/trigger")
                elapsed = time.monotonic() - begin

                assert status == 202
                assert body["accepted"] is True
                assert body["lease"]
                assert elapsed < 0.5
                assert started.wait(timeout=1)
                assert _read_state()["status"] == "pending"

                release.set()
                _wait_for(lambda: _read_state().get("status") == "active")
        finally:
            release.set()
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


def test_pause_queues_while_trigger_pending_and_applies_after_completion() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_call(action: str, authorization: str):
            calls.append(action)
            if action == "trigger":
                started.set()
                assert release.wait(timeout=2)
                return 200, b"{}"
            assert action == "pause"
            return 200, b"{}"

        controller._call_hermes = fake_call

        try:
            with RunningServer() as server:
                status, triggered = _request(server.base_url, "POST", "/trigger")
                assert status == 202
                lease = triggered["lease"]
                assert started.wait(timeout=1)

                status, queued = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease}",
                )
                assert status == 202
                assert queued == {
                    "ok": True,
                    "paused": False,
                    "reason": "pause_queued",
                }
                assert _read_state()["pause_requested"] is True

                release.set()
                _wait_for(lambda: _read_state().get("status") == "paused")
                assert calls == ["trigger", "pause"]
        finally:
            release.set()
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


def test_stale_pause_is_superseded() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        calls: list[str] = []

        def fake_call(action: str, authorization: str):
            assert authorization == "Bearer test-token"
            calls.append(action)
            return 200, b'{"ok":true}'

        controller._call_hermes = fake_call

        try:
            with RunningServer() as server:
                status, first = _request(server.base_url, "POST", "/trigger")
                assert status == 202
                lease_a = first["lease"]
                _wait_for(
                    lambda: _read_state().get("lease") == lease_a
                    and _read_state().get("status") == "active"
                )

                status, second = _request(server.base_url, "POST", "/trigger")
                assert status == 202
                lease_b = second["lease"]
                _wait_for(
                    lambda: _read_state().get("lease") == lease_b
                    and _read_state().get("status") == "active"
                )

                assert lease_a != lease_b
                assert calls == ["trigger", "trigger"]

                status, stale = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease_a}",
                )
                assert status == 200
                assert stale == {
                    "ok": True,
                    "paused": False,
                    "reason": "superseded",
                }

                # Critical invariant: stale A must not reach Hermes pause.
                assert calls == ["trigger", "trigger"]

                status, latest = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease_b}",
                )
                assert status == 200
                assert latest["ok"] is True
                assert latest["paused"] is True

                assert calls == ["trigger", "trigger", "pause"]

                persisted = _read_state()
                assert persisted == {
                    "lease": lease_b,
                    "status": "paused",
                }
        finally:
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


def test_trigger_failure_never_resurrects_previous_lease() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        calls = 0

        def fake_call(action: str, authorization: str):
            nonlocal calls
            assert action == "trigger"
            calls += 1
            if calls == 1:
                return 200, b"{}"
            raise RuntimeError("ambiguous upstream timeout")

        controller._call_hermes = fake_call

        try:
            with RunningServer() as server:
                status, first = _request(server.base_url, "POST", "/trigger")
                assert status == 202
                lease_a = first["lease"]
                _wait_for(
                    lambda: _read_state().get("lease") == lease_a
                    and _read_state().get("status") == "active"
                )

                status, accepted = _request(server.base_url, "POST", "/trigger")
                assert status == 202
                lease_b = accepted["lease"]
                _wait_for(lambda: "trigger_error" in _read_state())

                persisted = _read_state()
                assert lease_b != lease_a
                assert persisted["lease"] == lease_b
                assert persisted["status"] == "pending"
                assert "ambiguous upstream timeout" in persisted["trigger_error"]

                # A must remain permanently superseded even though the
                # outcome of B's upstream trigger call is unknown.
                status, stale = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease_a}",
                )
                assert status == 200
                assert stale == {
                    "ok": True,
                    "paused": False,
                    "reason": "superseded",
                }
        finally:
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


def test_missing_authorization_fails_closed() -> None:
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                    authorization=None,
                )
                assert status == 401
                assert body["error"] == "authorization_required"
                assert not controller.STATE_PATH.exists()
        finally:
            controller.STATE_PATH = original_state


def test_health_survives_persisted_state() -> None:
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        controller._write_state(
            {
                "lease": "persisted-lease",
                "status": "active",
            }
        )

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "GET",
                    "/healthz",
                    authorization=None,
                )
                assert status == 200
                assert body == {
                    "ok": True,
                    "lease_status": "active",
                }
        finally:
            controller.STATE_PATH = original_state


def test_active_lease_survives_controller_restart() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"
        calls: list[str] = []

        def fake_call(action: str, authorization: str):
            calls.append(action)
            return 200, b"{}"

        controller._call_hermes = fake_call

        try:
            with RunningServer() as first_server:
                status, triggered = _request(
                    first_server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease = triggered["lease"]
                _wait_for(lambda: _read_state().get("status") == "active")

            # New HTTP server instance, same persisted lease state.
            with RunningServer() as restarted_server:
                status, paused = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease}",
                )
                assert status == 200
                assert paused["paused"] is True

            assert calls == ["trigger", "pause"]
        finally:
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


def test_pending_state_survives_restart_and_next_trigger_recovers() -> None:
    original_call = controller._call_hermes
    original_state = controller.STATE_PATH

    with tempfile.TemporaryDirectory() as td:
        controller.STATE_PATH = Path(td) / "lease.json"

        # Simulate a controller crash after B was durably persisted as
        # pending but before its upstream trigger outcome became known.
        lease_a = "lease-a-old"
        lease_b = "lease-b-pending"
        controller._write_state(
            {
                "lease": lease_b,
                "status": "pending",
            }
        )

        calls: list[str] = []

        def fake_call(action: str, authorization: str):
            calls.append(action)
            return 200, b"{}"

        controller._call_hermes = fake_call

        try:
            # A new HTTP server instance represents controller restart.
            with RunningServer() as restarted_server:
                status, stale = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease_a}",
                )
                assert status == 200
                assert stale == {
                    "ok": True,
                    "paused": False,
                    "reason": "superseded",
                }

                # A matching delayed pause can be durably queued while the
                # trigger outcome is still pending. No pause reaches Hermes
                # until a live trigger worker observes it.
                status, pending = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease_b}",
                )
                assert status == 202
                assert pending == {
                    "ok": True,
                    "paused": False,
                    "reason": "pause_queued",
                }

                assert calls == []

                # The next fallback/event wake supersedes B and restores the
                # controller to a known active state.
                status, recovered = _request(
                    restarted_server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202

                lease_c = recovered["lease"]
                assert lease_c not in {lease_a, lease_b}
                _wait_for(
                    lambda: _read_state().get("lease") == lease_c
                    and _read_state().get("status") == "active"
                )
                assert calls == ["trigger"]

                persisted = _read_state()
                assert persisted == {
                    "lease": lease_c,
                    "status": "active",
                    "upstream_status": 200,
                }

                status, paused = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease_c}",
                )
                assert status == 200
                assert paused["paused"] is True
                assert calls == ["trigger", "pause"]
        finally:
            controller._call_hermes = original_call
            controller.STATE_PATH = original_state


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
