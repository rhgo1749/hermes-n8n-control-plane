#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
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
                assert status == 200
                lease_a = first["lease"]

                status, second = _request(server.base_url, "POST", "/trigger")
                assert status == 200
                lease_b = second["lease"]

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

                persisted = json.loads(
                    controller.STATE_PATH.read_text(encoding="utf-8")
                )
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
                assert status == 200
                lease_a = first["lease"]

                status, failed = _request(server.base_url, "POST", "/trigger")
                assert status == 502
                assert "ambiguous upstream timeout" in failed["error"]

                persisted = json.loads(
                    controller.STATE_PATH.read_text(encoding="utf-8")
                )

                lease_b = persisted["lease"]
                assert lease_b != lease_a
                assert persisted == {
                    "lease": lease_b,
                    "status": "pending",
                }

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
                assert status == 200
                lease = triggered["lease"]

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

                # Even B itself cannot pause while its trigger outcome is
                # ambiguous. No pause may reach Hermes from pending state.
                status, pending = _request(
                    restarted_server.base_url,
                    "POST",
                    f"/pause?lease={lease_b}",
                )
                assert status == 409
                assert pending == {
                    "ok": False,
                    "paused": False,
                    "reason": "lease_not_active",
                }

                assert calls == []

                # The next fallback/event wake supersedes B and restores the
                # controller to a known active state.
                status, recovered = _request(
                    restarted_server.base_url,
                    "POST",
                    "/trigger",
                )
                assert status == 200

                lease_c = recovered["lease"]
                assert lease_c not in {lease_a, lease_b}
                assert calls == ["trigger"]

                persisted = json.loads(
                    controller.STATE_PATH.read_text(encoding="utf-8")
                )
                assert persisted == {
                    "lease": lease_c,
                    "status": "active",
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
