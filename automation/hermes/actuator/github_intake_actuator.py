#!/usr/bin/env python3
"""Loopback-only fixed-command actuator for GitHub -> Hermes Kanban intake."""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _load_edge_sync_timeout_contract():
    candidates = (
        Path(__file__).with_name("edge_sync_timeout.py"),
        Path(__file__).resolve().parents[1] / "edge_sync_timeout.py",
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "hermes_edge_sync_timeout_contract_actuator",
            candidate,
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    raise RuntimeError("edge_sync_timeout_contract_unavailable")


_TIMEOUT_CONTRACT = _load_edge_sync_timeout_contract()

HOST = "127.0.0.1"
PORT = 5682

TOKEN_FILE = Path(
    os.environ.get(
        "HERMES_INTAKE_ACTUATOR_TOKEN_FILE",
        "/home/hermes/.hermes/.control-plane/github-intake-control-token",
    )
)
PYTHON_BIN = Path("/opt/venv/bin/python3")
INTAKE_SCRIPT = Path(
    "/home/hermes/.hermes/scripts/github-agent-ready-kanban-intake.py"
)
EDGE_SYNC_SCRIPT = Path(
    "/home/hermes/.hermes/scripts/kanban-github-sync.py"
)
REGISTRY_SCRIPT = Path(
    "/home/hermes/.hermes/scripts/repository_registry.py"
)
KANBAN_BOARDS_ROOT = Path(
    os.environ.get(
        "HERMES_KANBAN_BOARDS_ROOT",
        "/home/hermes/.hermes/kanban/boards",
    )
)
TIMEOUT_SECONDS = float(
    os.environ.get("HERMES_INTAKE_ACTUATOR_TIMEOUT_SECONDS", "900")
)
MAX_EDGE_SYNC_BODY_BYTES = 16 * 1024
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ACTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DELIVERY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
BOARD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Compatibility:
# - legacy main: secrets.token_urlsafe(32) -> 43 URL-safe characters
# - new installs: secrets.token_hex(32) -> 64 lowercase hex characters
TOKEN_RE = re.compile(r"(?:[A-Za-z0-9_-]{43}|[0-9a-f]{64})\Z")
_RUN_LOCK = threading.Lock()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_token() -> str:
    info = TOKEN_FILE.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("token_not_regular")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("token_permissions")
    if info.st_size <= 0 or info.st_size > 256:
        raise ValueError("token_size")

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("token_format")
    return token


def _authorized(header: str) -> bool:
    if not header.startswith("Bearer "):
        return False

    supplied = header[7:].strip()
    if not TOKEN_RE.fullmatch(supplied):
        return False

    try:
        expected = _read_token()
    except (OSError, UnicodeError, ValueError):
        return False

    return hmac.compare_digest(supplied, expected)


def _runtime_ready() -> bool:
    return (
        PYTHON_BIN.is_file()
        and os.access(PYTHON_BIN, os.X_OK)
        and INTAKE_SCRIPT.is_file()
    )


def _edge_runtime_ready() -> bool:
    return (
        PYTHON_BIN.is_file()
        and os.access(PYTHON_BIN, os.X_OK)
        and EDGE_SYNC_SCRIPT.is_file()
        and REGISTRY_SCRIPT.is_file()
    )


def _env_file_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{key}="
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or not value.startswith(prefix):
            continue
        value = value[len(prefix) :].strip().split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


def _github_token() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "HERMES_GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    hermes_home = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
    for path in (hermes_home / ".env", Path("/home/hermes/.hermes/.env")):
        value = _env_file_value(path, "GITHUB_TOKEN")
        if value:
            return value
    raise RuntimeError("github_token_unavailable")


def _load_registry_module():
    if not REGISTRY_SCRIPT.is_file():
        raise RuntimeError("repository_registry_unavailable")
    spec = importlib.util.spec_from_file_location(
        "hermes_github_intake_actuator_registry",
        REGISTRY_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("repository_registry_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resolve_board(repository: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository_invalid")
    try:
        registry = _load_registry_module()
        evidence = registry._kanban_board_repository_evidence(KANBAN_BOARDS_ROOT)
        board, _status = registry._resolve_board(repository, evidence)
    except Exception as exc:
        raise RuntimeError("board_resolution_failed") from exc
    if not board or not BOARD_RE.fullmatch(str(board)):
        raise ValueError("board_unresolved")
    try:
        root = KANBAN_BOARDS_ROOT.resolve(strict=False)
        board_dir = (KANBAN_BOARDS_ROOT / str(board)).resolve(strict=False)
    except OSError as exc:
        raise RuntimeError("board_resolution_failed") from exc
    if board_dir.parent != root or not (board_dir / "kanban.db").is_file():
        raise ValueError("board_unresolved")
    return str(board)


def _run_intake() -> int:
    if not _runtime_ready():
        raise RuntimeError("intake_runtime_unavailable")

    env = {
        **os.environ,
        "HOME": "/home/hermes",
        "HERMES_HOME": "/home/hermes/.hermes",
        "PATH": (
            "/opt/venv/bin:"
            "/home/hermes/.local/bin:"
            "/usr/local/bin:/usr/bin:/bin"
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Resolve the GitHub credential once at the trusted actuator
        # boundary; never place it in the command or URL.
        "GITHUB_TOKEN": _github_token(),
        # Keep repository-scoped wake claims independent of the retired cron
        # auth plugin.
        "HERMES_INTAKE_SCOPE_TOKEN_FILE": str(TOKEN_FILE),
    }

    completed = subprocess.run(
        [str(PYTHON_BIN), str(INTAKE_SCRIPT)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        shell=False,
        env=env,
    )
    return int(completed.returncode)


def _edge_sync_timeout_seconds() -> float:
    try:
        return _TIMEOUT_CONTRACT.parse_edge_sync_timeout()
    except _TIMEOUT_CONTRACT.TimeoutConfigurationError as exc:
        raise RuntimeError(exc.code) from exc


def _stop_edge_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _run_edge_sync(board: str) -> list[dict[str, Any]]:
    timeout_seconds = _edge_sync_timeout_seconds()
    if not _edge_runtime_ready():
        raise RuntimeError("edge_sync_runtime_unavailable")

    env = {
        **os.environ,
        "HOME": "/home/hermes",
        "HERMES_HOME": "/home/hermes/.hermes",
        "PATH": (
            "/opt/venv/bin:"
            "/home/hermes/.local/bin:"
            "/usr/local/bin:/usr/bin:/bin"
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GITHUB_TOKEN": _github_token(),
        # The edge-owned rework respawn lane is the canonical event-driven
        # path for managed ``agent-rework`` deliveries: a successful label
        # event flows router -> actuator -> this sync, and the sync must
        # claim/spawn the rework task in the same run instead of leaving
        # it waiting for a later intake tick.  The lane itself stays
        # strictly scoped to tasks whose governing transition is a
        # consumed agent-rework (see ``_dispatch_pending_rework``), so
        # enabling it here does not bypass the core dispatcher's
        # active-PR duplicate-spawn protection for any other task.
        "HERMES_KANBAN_REWORK_DISPATCH": "1",
    }
    command = [
        str(PYTHON_BIN),
        str(EDGE_SYNC_SCRIPT),
        "--board",
        board,
        "--json",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            env=env,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("edge_sync_spawn_failed") from exc

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[bytes] = []
    output_bytes = 0
    deadline = time.monotonic() + timeout_seconds
    stream_closed = False
    try:
        while not stream_closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_edge_process(process)
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            events = selector.select(remaining)
            if not events:
                _stop_edge_process(process)
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _ in events:
                data = os.read(key.fd, 4096)
                if not data:
                    stream_closed = True
                    selector.unregister(key.fileobj)
                    break
                if (
                    output_bytes + len(data)
                    > _TIMEOUT_CONTRACT.EDGE_SYNC_OUTPUT_LIMIT_BYTES
                ):
                    _stop_edge_process(process)
                    raise RuntimeError("edge_sync_output_limit")
                output.append(data)
                output_bytes += len(data)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_edge_process(process)
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        returncode = process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        _stop_edge_process(process)
        raise
    finally:
        try:
            selector.close()
        finally:
            process.stdout.close()

    if returncode != 0:
        raise RuntimeError("edge_sync_command_failed")
    raw_output = b"".join(output)
    try:
        payload = json.loads(raw_output or b"")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("edge_sync_output_invalid") from exc
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise RuntimeError("edge_sync_output_invalid")
    return payload


def _edge_sync_error_code(exc: RuntimeError) -> str:
    code = str(exc)
    if code in {"edge_sync_output_limit", "edge_timeout_invalid"}:
        return code
    return "edge_sync_execution_failed"


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGitHubIntakeActuator/1"

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"[github-intake-actuator] "
            f"{self.client_address[0]} {format % args}",
            flush=True,
        )

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
            )
            return

        try:
            token_ready = bool(_read_token())
        except (OSError, UnicodeError, ValueError):
            token_ready = False

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "hermes-github-intake-actuator",
                "token_ready": token_ready,
                "runtime_ready": _runtime_ready(),
                "edge_sync_runtime_ready": _edge_runtime_ready(),
                "busy": _RUN_LOCK.locked(),
            },
        )

    def do_POST(self) -> None:
        if self.path not in {"/v1/intake", "/v1/edge-sync"}:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
            )
            return

        if not _authorized(self.headers.get("Authorization", "").strip()):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "unauthorized"},
            )
            return

        if self.path == "/v1/intake":
            self._handle_intake()
            return
        self._handle_edge_sync()

    def _handle_intake(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1

        if content_length != 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request_body_not_allowed"},
            )
            return

        if not _RUN_LOCK.acquire(blocking=False):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": "intake_busy"},
            )
            return

        try:
            returncode = _run_intake()
        except subprocess.TimeoutExpired:
            self._send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"ok": False, "error": "intake_timeout"},
            )
            return
        except (RuntimeError, OSError, subprocess.SubprocessError):
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": "intake_execution_failed"},
            )
            return
        finally:
            _RUN_LOCK.release()

        if returncode != 0:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error": "intake_failed",
                    "returncode": returncode,
                },
            )
            return

        self._send_json(HTTPStatus.OK, {"ok": True, "returncode": 0})

    def _handle_edge_sync(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length <= 0 or content_length > MAX_EDGE_SYNC_BODY_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_request_body"},
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_json"},
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request_must_be_object"},
            )
            return

        expected_keys = {
            "repository",
            "event",
            "action",
            "merged",
            "label",
            "delivery",
        }
        if set(payload) != expected_keys:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request_schema_invalid"},
            )
            return

        repository = payload["repository"]
        event = payload["event"]
        action = payload["action"]
        label = payload["label"]
        delivery = payload["delivery"]
        merged = payload["merged"]
        if (
            not isinstance(repository, str)
            or not REPOSITORY_RE.fullmatch(repository)
            or event != "pull_request"
            or not isinstance(action, str)
            or not ACTION_RE.fullmatch(action)
            or not isinstance(merged, bool)
            or not isinstance(label, str)
            or len(label) > 128
            or not isinstance(delivery, str)
            or not DELIVERY_RE.fullmatch(delivery)
        ):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request_schema_invalid"},
            )
            return

        allowed = (
            action == "closed" and merged is True
        ) or (
            action == "labeled" and label == "agent-rework"
        )
        if not allowed:
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "ok": True,
                    "ignored": True,
                    "reason": "unsupported_pull_request_action",
                },
            )
            return

        try:
            board = _resolve_board(repository)
        except ValueError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": str(exc)},
            )
            return
        except RuntimeError:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": "board_resolution_failed"},
            )
            return

        if not _RUN_LOCK.acquire(blocking=False):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": "edge_sync_busy"},
            )
            return
        try:
            results = _run_edge_sync(board)
        except subprocess.TimeoutExpired:
            self._send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"ok": False, "error": "edge_sync_timeout"},
            )
            return
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": _edge_sync_error_code(exc)},
            )
            return
        except (OSError, subprocess.SubprocessError):
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": "edge_sync_execution_failed"},
            )
            return
        finally:
            _RUN_LOCK.release()

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "board": board,
                "returncode": 0,
                "results": results,
            },
        )


def main() -> int:
    if HOST != "127.0.0.1" or PORT != 5682:
        raise SystemExit("actuator must remain fixed to loopback:5682")

    _read_token()
    if not _runtime_ready():
        raise SystemExit("intake runtime is unavailable")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"Hermes GitHub intake actuator listening on {HOST}:{PORT}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
