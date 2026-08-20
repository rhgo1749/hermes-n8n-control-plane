#!/usr/bin/env python3
"""Loopback-only fixed-command actuator for GitHub -> Hermes Kanban intake."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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
TIMEOUT_SECONDS = float(
    os.environ.get("HERMES_INTAKE_ACTUATOR_TIMEOUT_SECONDS", "900")
)

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
        # Keep repository-scoped wake claims independent of the retired cron
        # auth plugin.
        "HERMES_INTAKE_SCOPE_TOKEN_FILE": str(TOKEN_FILE),
    }

    completed = subprocess.run(
        [str(PYTHON_BIN), str(INTAKE_SCRIPT)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        shell=False,
        env=env,
    )
    return int(completed.returncode)


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGitHubIntakeActuator/1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"[github-intake-actuator] "
            f"{self.client_address[0]} {fmt % args}",
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
                "busy": _RUN_LOCK.locked(),
            },
        )

    def do_POST(self) -> None:
        if self.path != "/v1/intake":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
            )
            return

        if not _authorized(
            self.headers.get("Authorization", "").strip()
        ):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "unauthorized"},
            )
            return

        try:
            content_length = int(
                self.headers.get("Content-Length", "0")
            )
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

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "returncode": 0},
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
