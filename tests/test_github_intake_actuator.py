#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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

actuator: Any = importlib.util.module_from_spec(spec)
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
        actuator._github_token,
    )

    actuator.TOKEN_FILE = token
    actuator.PYTHON_BIN = python_bin
    actuator.INTAKE_SCRIPT = intake
    actuator._github_token = lambda: "github-token-for-test"

    return originals


def _restore_runtime(originals) -> None:
    (
        actuator.TOKEN_FILE,
        actuator.PYTHON_BIN,
        actuator.INTAKE_SCRIPT,
    ) = originals[:3]
    actuator._github_token = originals[3]


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
            assert env["GITHUB_TOKEN"] == "github-token-for-test"
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


def _edge_payload(
    *,
    action: str = "closed",
    merged: bool = True,
    label: str = "",
) -> bytes:
    return json.dumps(
        {
            "repository": "rhgo1749/ctrl-hangul",
            "event": "pull_request",
            "action": action,
            "merged": merged,
            "label": label,
            "delivery": "delivery-actuator-test",
        }
    ).encode()


def test_edge_sync_rejects_unknown_fields_without_execution() -> None:
    original_resolve = actuator._resolve_board
    original_run = actuator._run_edge_sync
    calls = 0

    def unexpected(board):
        nonlocal calls
        calls += 1
        raise AssertionError(board)

    actuator.__dict__["_resolve_board"] = lambda repository: "ctrlhangul"
    actuator.__dict__["_run_edge_sync"] = unexpected
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        try:
            payload = json.loads(_edge_payload())
            payload["unexpected"] = "reject-me"
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=json.dumps(payload).encode(),
                )
            assert status == 400
            assert body["error"] == "request_schema_invalid"
            assert calls == 0
        finally:
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_run_edge_sync"] = original_run
            _restore_runtime(originals)


def test_edge_sync_unsupported_action_is_explicit_noop() -> None:
    original_resolve = actuator._resolve_board
    original_run = actuator._run_edge_sync
    actuator.__dict__["_resolve_board"] = lambda repository: (
        (_ for _ in ()).throw(AssertionError("unsupported action resolved board"))
    )
    actuator.__dict__["_run_edge_sync"] = lambda board: (
        (_ for _ in ()).throw(AssertionError("unsupported action executed"))
    )
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=_edge_payload(action="opened", merged=False),
                )
            assert status == 202
            assert body == {
                "ok": True,
                "ignored": True,
                "reason": "unsupported_pull_request_action",
            }
        finally:
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_run_edge_sync"] = original_run
            _restore_runtime(originals)


def test_edge_sync_resolves_board_and_reads_back_results() -> None:
    original_resolve = actuator._resolve_board
    original_run = actuator._run_edge_sync
    calls: list[str] = []
    actuator.__dict__["_resolve_board"] = lambda repository: (
        calls.append(repository),
        "ctrlhangul",
    )[1]
    actuator.__dict__["_run_edge_sync"] = lambda board: [
        {"task_id": "task-1", "status": "done", "changed": True}
    ]
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=_edge_payload(),
                )
            assert status == 200
            assert body == {
                "ok": True,
                "board": "ctrlhangul",
                "returncode": 0,
                "results": [
                    {"task_id": "task-1", "status": "done", "changed": True}
                ],
            }
            assert calls == ["rhgo1749/ctrl-hangul"]
        finally:
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_run_edge_sync"] = original_run
            _restore_runtime(originals)


def test_edge_sync_command_failure_is_not_reported_as_success() -> None:
    original_resolve = actuator._resolve_board
    original_run = actuator._run_edge_sync
    actuator.__dict__["_resolve_board"] = lambda repository: "ctrlhangul"
    actuator.__dict__["_run_edge_sync"] = lambda board: (
        (_ for _ in ()).throw(RuntimeError("edge_sync_command_failed"))
    )
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=_edge_payload(),
                )
            assert status == 502
            assert body == {
                "ok": False,
                "error": "edge_sync_execution_failed",
            }
        finally:
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_run_edge_sync"] = original_run
            _restore_runtime(originals)


def test_edge_sync_busy_fails_closed() -> None:
    original_resolve = actuator._resolve_board
    original_run = actuator._run_edge_sync
    actuator.__dict__["_resolve_board"] = lambda repository: "ctrlhangul"
    actuator.__dict__["_run_edge_sync"] = lambda board: (
        (_ for _ in ()).throw(AssertionError("busy actuator executed"))
    )
    actuator._RUN_LOCK.acquire()
    with tempfile.TemporaryDirectory() as td:
        originals = _configure_runtime(td)
        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=_edge_payload(),
                )
            assert status == 409
            assert body["error"] == "edge_sync_busy"
        finally:
            actuator._RUN_LOCK.release()
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_run_edge_sync"] = original_run
            _restore_runtime(originals)


def test_board_resolution_uses_authoritative_task_provenance() -> None:
    original_registry = actuator.REGISTRY_SCRIPT
    original_boards_root = actuator.KANBAN_BOARDS_ROOT
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        boards_root = root / "boards"
        board_dir = boards_root / "ctrlhangul"
        board_dir.mkdir(parents=True)
        db = board_dir / "kanban.db"
        with sqlite3.connect(db) as connection:
            connection.execute(
                "CREATE TABLE tasks (idempotency_key TEXT)"
            )
            connection.execute(
                "INSERT INTO tasks (idempotency_key) VALUES (?)",
                ("github:rhgo1749/ctrl-hangul:issue:58",),
            )
            connection.commit()
        actuator.__dict__["REGISTRY_SCRIPT"] = (
            ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py"
        )
        actuator.__dict__["KANBAN_BOARDS_ROOT"] = boards_root
        try:
            assert actuator._resolve_board("rhgo1749/ctrl-hangul") == "ctrlhangul"
            try:
                actuator._resolve_board("rhgo1749/unknown")
            except ValueError as exc:
                assert str(exc) == "board_unresolved"
            else:
                raise AssertionError("unknown repository must fail closed")
        finally:
            actuator.__dict__["REGISTRY_SCRIPT"] = original_registry
            actuator.__dict__["KANBAN_BOARDS_ROOT"] = original_boards_root


def test_edge_sync_fixed_command_is_bounded_and_shell_free() -> None:
    original_edge = actuator.EDGE_SYNC_SCRIPT
    original_registry = actuator.REGISTRY_SCRIPT
    original_python = actuator.PYTHON_BIN
    original_github_token = actuator._github_token
    original_popen = actuator.subprocess.Popen
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        edge = root / "kanban-github-sync.py"
        registry = root / "repository_registry.py"
        edge.write_text(
            "import json\n"
            "import os\n"
            "import sys\n"
            "assert sys.argv[1:] == ['--board', 'ctrlhangul', '--json']\n"
            f"assert os.environ['GITHUB_TOKEN'] == {TEST_TOKEN!r}\n"
            "print(json.dumps([{'task_id': 'task-1'}]))\n",
            encoding="utf-8",
        )
        registry.write_text("# registry\n", encoding="utf-8")
        actuator.__dict__["PYTHON_BIN"] = Path(sys.executable)
        actuator.__dict__["EDGE_SYNC_SCRIPT"] = edge
        actuator.__dict__["REGISTRY_SCRIPT"] = registry
        actuator.__dict__["_github_token"] = lambda: TEST_TOKEN
        captured: dict[str, object] = {}

        def recording_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return original_popen(argv, **kwargs)

        actuator.subprocess.Popen = recording_popen
        try:
            assert actuator._run_edge_sync("ctrlhangul") == [
                {"task_id": "task-1"}
            ]
            assert captured["argv"] == [
                str(actuator.PYTHON_BIN),
                str(edge),
                "--board",
                "ctrlhangul",
                "--json",
            ]
            assert captured["shell"] is False
        finally:
            actuator.subprocess.Popen = original_popen
            actuator.__dict__["PYTHON_BIN"] = original_python
            actuator.__dict__["EDGE_SYNC_SCRIPT"] = original_edge
            actuator.__dict__["REGISTRY_SCRIPT"] = original_registry
            actuator.__dict__["_github_token"] = original_github_token


def test_edge_sync_oversized_output_fails_closed_and_terminates_child() -> None:
    original_edge = actuator.EDGE_SYNC_SCRIPT
    original_registry = actuator.REGISTRY_SCRIPT
    original_python = actuator.PYTHON_BIN
    original_github_token = actuator._github_token
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "child-marker"
        edge = root / "kanban-github-sync.py"
        registry = root / "repository_registry.py"
        edge.write_text(
            "import json\n"
            "import time\n"
            "from pathlib import Path\n"
            f"marker = Path({str(marker)!r})\n"
            "marker.write_text('started', encoding='utf-8')\n"
            "print(json.dumps([{'payload': 'x' * 2000001}]))\n"
            "time.sleep(5)\n"
            "marker.write_text('survived', encoding='utf-8')\n",
            encoding="utf-8",
        )
        registry.write_text("# registry\n", encoding="utf-8")
        actuator.__dict__["PYTHON_BIN"] = Path(sys.executable)
        actuator.__dict__["EDGE_SYNC_SCRIPT"] = edge
        actuator.__dict__["REGISTRY_SCRIPT"] = registry
        actuator.__dict__["_github_token"] = lambda: TEST_TOKEN
        try:
            try:
                actuator._run_edge_sync("ctrlhangul")
            except RuntimeError as exc:
                assert str(exc) == "edge_sync_output_limit", str(exc)
                assert actuator._edge_sync_error_code(exc) == "edge_sync_output_limit"
            else:
                raise AssertionError("oversized edge output must fail closed")
            time.sleep(0.1)
            assert marker.read_text(encoding="utf-8") == "started"
        finally:
            actuator.__dict__["PYTHON_BIN"] = original_python
            actuator.__dict__["EDGE_SYNC_SCRIPT"] = original_edge
            actuator.__dict__["REGISTRY_SCRIPT"] = original_registry
            actuator.__dict__["_github_token"] = original_github_token


def test_edge_sync_invalid_timeout_values_fail_closed_before_spawn() -> None:
    original_edge = actuator.EDGE_SYNC_SCRIPT
    original_registry = actuator.REGISTRY_SCRIPT
    original_python = actuator.PYTHON_BIN
    original_github_token = actuator._github_token
    previous_timeout = os.environ.get("HERMES_EDGE_SYNC_TIMEOUT_SECONDS")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "spawned"
        edge = root / "kanban-github-sync.py"
        registry = root / "repository_registry.py"
        edge.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('spawned', encoding='utf-8')\n"
            "print('[]')\n",
            encoding="utf-8",
        )
        registry.write_text("# registry\n", encoding="utf-8")
        actuator.__dict__["PYTHON_BIN"] = Path(sys.executable)
        actuator.__dict__["EDGE_SYNC_SCRIPT"] = edge
        actuator.__dict__["REGISTRY_SCRIPT"] = registry
        actuator.__dict__["_github_token"] = lambda: TEST_TOKEN
        try:
            for raw in ("nan", "inf", "0", "-1", "not-a-number", "3600.1"):
                os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = raw
                try:
                    actuator._run_edge_sync("ctrlhangul")
                except RuntimeError as exc:
                    assert str(exc) == "edge_timeout_invalid", str(exc)
                else:
                    raise AssertionError(f"invalid timeout must fail closed: {raw!r}")
                assert not marker.exists(), raw

            os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = "0.05"
            assert actuator._run_edge_sync("ctrlhangul") == []
            assert marker.read_text(encoding="utf-8") == "spawned"
        finally:
            if previous_timeout is None:
                os.environ.pop("HERMES_EDGE_SYNC_TIMEOUT_SECONDS", None)
            else:
                os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = previous_timeout
            actuator.__dict__["PYTHON_BIN"] = original_python
            actuator.__dict__["EDGE_SYNC_SCRIPT"] = original_edge
            actuator.__dict__["REGISTRY_SCRIPT"] = original_registry
            actuator.__dict__["_github_token"] = original_github_token


def test_edge_sync_invalid_timeout_http_response_is_stable() -> None:
    original_edge = actuator.EDGE_SYNC_SCRIPT
    original_registry = actuator.REGISTRY_SCRIPT
    original_python = actuator.PYTHON_BIN
    original_token_file = actuator.TOKEN_FILE
    original_resolve = actuator._resolve_board
    original_github_token = actuator._github_token
    previous_timeout = os.environ.get("HERMES_EDGE_SYNC_TIMEOUT_SECONDS")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "spawned"
        edge = root / "kanban-github-sync.py"
        registry = root / "repository_registry.py"
        token = root / "token"
        token.write_text(TEST_TOKEN + "\n", encoding="utf-8")
        token.chmod(0o600)
        edge.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('spawned', encoding='utf-8')\n"
            "print('[]')\n",
            encoding="utf-8",
        )
        registry.write_text("# registry\n", encoding="utf-8")
        actuator.__dict__["PYTHON_BIN"] = Path(sys.executable)
        actuator.__dict__["TOKEN_FILE"] = token
        actuator.__dict__["EDGE_SYNC_SCRIPT"] = edge
        actuator.__dict__["REGISTRY_SCRIPT"] = registry
        actuator.__dict__["_resolve_board"] = lambda repository: "ctrlhangul"
        actuator.__dict__["_github_token"] = lambda: TEST_TOKEN
        os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = "nan"
        try:
            with RunningServer() as server:
                status, body = _request(
                    server.base_url,
                    "POST",
                    "/v1/edge-sync",
                    authorization=f"Bearer {TEST_TOKEN}",
                    data=_edge_payload(),
                )
            assert status == 502
            assert body == {"ok": False, "error": "edge_timeout_invalid"}
            assert not marker.exists()
        finally:
            if previous_timeout is None:
                os.environ.pop("HERMES_EDGE_SYNC_TIMEOUT_SECONDS", None)
            else:
                os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = previous_timeout
            actuator.__dict__["PYTHON_BIN"] = original_python
            actuator.__dict__["TOKEN_FILE"] = original_token_file
            actuator.__dict__["EDGE_SYNC_SCRIPT"] = original_edge
            actuator.__dict__["REGISTRY_SCRIPT"] = original_registry
            actuator.__dict__["_resolve_board"] = original_resolve
            actuator.__dict__["_github_token"] = original_github_token


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
