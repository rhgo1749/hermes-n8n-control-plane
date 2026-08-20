#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "actuator"
    / "github_intake_actuator.py"
)

spec = importlib.util.spec_from_file_location(
    "github_intake_actuator",
    MODULE_PATH,
)
assert spec and spec.loader

actuator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = actuator
spec.loader.exec_module(actuator)


TEST_TOKEN = "a" * 64


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    authorization: str | None = None,
    data: bytes | None = None,
) -> tuple[int, dict]:
    headers = {}

    if authorization is not None:
        headers["Authorization"] = authorization

    request = Request(
        base_url + path,
        method=method,
        headers=headers,
        data=data,
    )

    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


class RunningServer:
    def __init__(self) -> None:
        self.server = actuator.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            actuator.Handler,
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


def _configure_runtime(td: str):
    root = Path(td)

    token = root / "token"
    token.write_text(TEST_TOKEN + "\n", encoding="utf-8")
    token.chmod(0o600)

    python_bin = root / "python3"
    python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    python_bin.chmod(0o755)

    intake = root / "intake.py"
    intake.write_text("# test intake\n", encoding="utf-8")

    originals = (
        actuator.TOKEN_FILE,
        actuator.PYTHON_BIN,
        actuator.INTAKE_SCRIPT,
    )

    actuator.TOKEN_FILE = token
    actuator.PYTHON_BIN = python_bin
    actuator.INTAKE_SCRIPT = intake

    return originals


def _restore_runtime(originals) -> None:
    (
        actuator.TOKEN_FILE,
        actuator.PYTHON_BIN,
        actuator.INTAKE_SCRIPT,
    ) = originals


def test_health_reports_runtime_and_token_ready() -> None:
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "GET",
                    "/healthz",
                )

                assert status == 200
                assert body["ok"] is True
                assert (
                    body["service"]
                    == "hermes-github-intake-actuator"
                )
                assert body["token_ready"] is True
                assert body["runtime_ready"] is True
        finally:
            _restore_runtime(originals)


def test_unauthorized_request_never_executes() -> None:
    original_run = actuator.subprocess.run

    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        calls = 0

        def fake_run(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("must not execute")

        actuator.subprocess.run = fake_run

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/intake",
                    data=b"",
                )

                assert status == 401
                assert body["error"] == "unauthorized"

                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/intake",
                    authorization="Bearer " + ("b" * 64),
                    data=b"",
                )

                assert status == 401
                assert body["error"] == "unauthorized"
                assert calls == 0
        finally:
            actuator.subprocess.run = original_run
            _restore_runtime(originals)


def test_request_body_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/intake",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=b"{}",
                )

                assert status == 400
                assert (
                    body["error"]
                    == "request_body_not_allowed"
                )
        finally:
            _restore_runtime(originals)


def test_busy_actuator_rejects_second_execution() -> None:
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)

        actuator._RUN_LOCK.acquire()

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/intake",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=b"",
                )

                assert status == 409
                assert body["error"] == "intake_busy"
        finally:
            actuator._RUN_LOCK.release()
            _restore_runtime(originals)


def test_authorized_request_uses_fixed_command_without_shell() -> None:
    original_run = actuator.subprocess.run

    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)

            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="",
            )

        actuator.subprocess.run = fake_run

        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/intake",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=b"",
                )

                assert status == 200
                assert body == {
                    "ok": True,
                    "returncode": 0,
                }

            assert captured["argv"] == [
                str(actuator.PYTHON_BIN),
                str(actuator.INTAKE_SCRIPT),
            ]

            assert captured["shell"] is False
            assert captured["check"] is False
            assert captured["stdin"] is actuator.subprocess.DEVNULL

            env = captured["env"]

            assert env["HOME"] == "/home/hermes"
            assert env["HERMES_HOME"] == "/home/hermes/.hermes"
            assert (
                env["HERMES_INTAKE_SCOPE_TOKEN_FILE"]
                == str(actuator.TOKEN_FILE)
            )
        finally:
            actuator.subprocess.run = original_run
            _restore_runtime(originals)


def test_legacy_main_urlsafe_token_is_accepted() -> None:
    legacy_token = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcde"
    assert len(legacy_token) == 43

    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)

        actuator.TOKEN_FILE.write_text(
            legacy_token + "\n",
            encoding="utf-8",
        )
        actuator.TOKEN_FILE.chmod(0o600)

        try:
            assert actuator._authorized(
                f"Bearer {legacy_token}"
            ) is True

            # Case must remain significant for URL-safe legacy credentials.
            assert actuator._authorized(
                f"Bearer {legacy_token.swapcase()}"
            ) is False
        finally:
            _restore_runtime(originals)


def test_token_permissions_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)

        actuator.TOKEN_FILE.chmod(0o644)

        try:
            assert actuator._authorized(
                f"Bearer {TEST_TOKEN}"
            ) is False
        finally:
            _restore_runtime(originals)


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    for test in tests:
        test()
        print(f"PASS {test.__name__}")

    print(
        json.dumps(
            {
                "ok": True,
                "tests": len(tests),
            }
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
