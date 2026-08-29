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

spec = importlib.util.spec_from_file_location(
    "intake_lease_controller",
    MODULE_PATH,
)
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
        return json.loads(
            controller.STATE_PATH.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}


def _prepare_runtime(td: str) -> tuple[Path, Path]:
    original_state = controller.STATE_PATH
    original_token = controller.TOKEN_FILE

    root = Path(td)
    controller.STATE_PATH = root / "lease.json"
    controller.TOKEN_FILE = root / "intake-token"
    controller.TOKEN_FILE.write_text(
        "test-token\n",
        encoding="utf-8",
    )

    return original_state, original_token


def _restore_runtime(original_state: Path, original_token: Path) -> None:
    controller.STATE_PATH = original_state
    controller.TOKEN_FILE = original_token


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


def test_trigger_returns_before_slow_actuator_completion() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)
        started = threading.Event()
        release = threading.Event()

        def fake_call(authorization: str):
            assert authorization == "Bearer test-token"
            started.set()
            assert release.wait(timeout=2)
            return 200, b"{}"

        controller._call_actuator = fake_call

        try:
            with RunningServer() as server:
                begin = time.monotonic()
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                elapsed = time.monotonic() - begin

                assert status == 202
                assert body["accepted"] is True
                assert body["lease"]
                assert elapsed < 0.5
                assert started.wait(timeout=1)
                assert _read_state()["status"] == "pending"

                release.set()
                _wait_for(
                    lambda: _read_state().get("status") == "active"
                )
        finally:
            release.set()
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


def test_trigger_options_contract_is_read_only() -> None:
    original_call = controller._call_actuator
    calls = 0

    def unexpected_call(authorization: str) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        raise AssertionError("OPTIONS must not trigger the actuator")

    controller.__dict__["_call_actuator"] = unexpected_call
    try:
        with RunningServer() as server:
            request = Request(
                f"{server.base_url}/trigger?profile=default",
                method="OPTIONS",
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 204
                assert response.headers["Allow"] == "POST, OPTIONS"
    finally:
        assert calls == 0
        controller.__dict__["_call_actuator"] = original_call


def test_pause_queues_while_actuator_pending() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def fake_call(authorization: str):
            nonlocal calls
            assert authorization == "Bearer test-token"
            calls += 1
            started.set()
            assert release.wait(timeout=2)
            return 200, b"{}"

        controller._call_actuator = fake_call

        try:
            with RunningServer() as server:
                status, triggered = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
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

                _wait_for(
                    lambda: _read_state().get("status") == "paused"
                )

                # Critical direct-actuator invariant:
                # pause never causes a second upstream call.
                assert calls == 1
        finally:
            release.set()
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


def test_stale_pause_is_superseded_and_latest_pause_is_local() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)
        calls = 0

        def fake_call(authorization: str):
            nonlocal calls
            assert authorization == "Bearer test-token"
            calls += 1
            return 200, b'{"ok":true}'

        controller._call_actuator = fake_call

        try:
            with RunningServer() as server:
                status, first = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease_a = first["lease"]

                _wait_for(
                    lambda: _read_state().get("lease") == lease_a
                    and _read_state().get("status") == "active"
                )

                status, second = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease_b = second["lease"]

                _wait_for(
                    lambda: _read_state().get("lease") == lease_b
                    and _read_state().get("status") == "active"
                )

                assert lease_a != lease_b
                assert calls == 2

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
                assert calls == 2

                status, latest = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease_b}",
                )

                assert status == 200
                assert latest == {
                    "ok": True,
                    "paused": True,
                }

                # Latest pause is also local-only.
                assert calls == 2

                assert _read_state() == {
                    "lease": lease_b,
                    "status": "paused",
                }
        finally:
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


def test_ambiguous_actuator_failure_never_resurrects_old_lease() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)
        calls = 0

        def fake_call(authorization: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 200, b"{}"
            raise RuntimeError("ambiguous actuator timeout")

        controller._call_actuator = fake_call

        try:
            with RunningServer() as server:
                status, first = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease_a = first["lease"]

                _wait_for(
                    lambda: _read_state().get("lease") == lease_a
                    and _read_state().get("status") == "active"
                )

                status, second = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease_b = second["lease"]

                _wait_for(
                    lambda: "trigger_error" in _read_state()
                )

                state = _read_state()

                assert lease_a != lease_b
                assert state["lease"] == lease_b
                assert state["status"] == "pending"
                assert "ambiguous actuator timeout" in state["trigger_error"]

                status, stale = _request(
                    server.base_url,
                    "POST",
                    f"/pause?lease={lease_a}",
                )

                assert status == 200
                assert stale["reason"] == "superseded"
        finally:
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


def test_actuator_rejection_marks_lease_failed() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)

        def fake_call(authorization: str):
            return 502, b'{"ok":false}'

        controller._call_actuator = fake_call

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease = body["lease"]

                _wait_for(
                    lambda: _read_state().get("status") == "failed"
                )

                assert _read_state() == {
                    "lease": lease,
                    "status": "failed",
                    "upstream_status": 502,
                }
        finally:
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


def test_missing_or_wrong_authorization_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)

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

                status, body = _request(
                    server.base_url,
                    "POST",
                    "/trigger",
                    authorization="Bearer wrong-token",
                )

                assert status == 401
                assert body["error"] == "authorization_required"
                assert not controller.STATE_PATH.exists()
        finally:
            _restore_runtime(original_state, original_token)


def test_health_survives_persisted_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)

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
            _restore_runtime(original_state, original_token)


def test_active_lease_restart_pause_remains_local() -> None:
    original_call = controller._call_actuator

    with tempfile.TemporaryDirectory() as td:
        original_state, original_token = _prepare_runtime(td)
        calls = 0

        def fake_call(authorization: str):
            nonlocal calls
            calls += 1
            return 200, b"{}"

        controller._call_actuator = fake_call

        try:
            with RunningServer() as first_server:
                status, triggered = _request(
                    first_server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 202
                lease = triggered["lease"]

                _wait_for(
                    lambda: _read_state().get("status") == "active"
                )

            with RunningServer() as restarted_server:
                status, paused = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease}",
                )

                assert status == 200
                assert paused["paused"] is True

            assert calls == 1
            assert _read_state() == {
                "lease": lease,
                "status": "paused",
            }
        finally:
            controller._call_actuator = original_call
            _restore_runtime(original_state, original_token)


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
