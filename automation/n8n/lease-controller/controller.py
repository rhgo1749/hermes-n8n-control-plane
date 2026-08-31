#!/usr/bin/env python3
from __future__ import annotations

import hmac
import json
import os
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("LEASE_LISTEN_PORT", "5680"))

ACTUATOR_BASE_URL = os.environ.get(
    "LEASE_ACTUATOR_BASE_URL",
    "http://127.0.0.1:5682",
).strip().rstrip("/")

ACTUATOR_TIMEOUT_SECONDS = float(
    os.environ.get("LEASE_ACTUATOR_TIMEOUT_SECONDS", "920")
)
ACTUATOR_MAX_RESPONSE_BYTES = 64 * 1024

TOKEN_FILE = Path(
    os.environ.get(
        "LEASE_TOKEN_FILE",
        "/state/secrets/hermes-intake-control-token",
    )
)

STATE_PATH = Path(
    os.environ.get(
        "LEASE_STATE_PATH",
        "/state/hermes-intake-lease.json",
    )
)

_REQUEST_LOCK = threading.Lock()
_TRIGGER_LOCK = threading.Lock()


def _validated_actuator_base_url() -> str:
    try:
        parsed = urlparse(ACTUATOR_BASE_URL)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("LEASE_ACTUATOR_BASE_URL is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("LEASE_ACTUATOR_BASE_URL must be a loopback URL")
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError("LEASE_ACTUATOR_BASE_URL has an invalid port")
    return ACTUATOR_BASE_URL.rstrip("/")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_state() -> dict[str, object]:
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"cannot read lease state: {exc}") from exc

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("lease state is invalid JSON") from exc

    if not isinstance(value, dict):
        raise RuntimeError("lease state must be a JSON object")
    return value


def _write_state(value: dict[str, object]) -> None:
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


def _read_token() -> str:
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("intake control token unavailable") from exc

    if not value:
        raise RuntimeError("intake control token empty")
    return value


def _authorized(header: str) -> bool:
    if not header.startswith("Bearer "):
        return False

    supplied = header[7:].strip()

    try:
        expected = _read_token()
    except RuntimeError:
        return False

    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _call_actuator(authorization: str) -> tuple[int, bytes]:
    base_url = _validated_actuator_base_url()
    request = Request(
        f"{base_url}/v1/intake",
        method="POST",
        headers={
            "Authorization": authorization,
            "Accept": "application/json",
        },
        data=b"",
    )

    try:
        with urlopen(
            request,
            timeout=ACTUATOR_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(ACTUATOR_MAX_RESPONSE_BYTES + 1)
            if len(raw) > ACTUATOR_MAX_RESPONSE_BYTES:
                raise RuntimeError("intake actuator response is too large")
            return response.status, raw
    except HTTPError as exc:
        exc.close()
        return exc.code, b""
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"intake actuator request failed: {exc}"
        ) from exc


def _trigger_intake_in_background(
    lease: str,
    authorization: str,
) -> None:
    try:
        with _TRIGGER_LOCK:
            status, _body = _call_actuator(authorization)
    except RuntimeError as exc:
        with _REQUEST_LOCK:
            state = _load_state()
            if str(state.get("lease", "")) == lease:
                state["trigger_error"] = str(exc)
                _write_state(state)

        print(
            "lease-controller actuator outcome is ambiguous "
            f"lease={lease}: {exc}",
            flush=True,
        )
        return

    with _REQUEST_LOCK:
        state = _load_state()

        if str(state.get("lease", "")) != lease:
            return

        if not 200 <= status < 300:
            _write_state(
                {
                    "lease": lease,
                    "status": "failed",
                    "upstream_status": status,
                }
            )
            return

        if state.get("pause_requested"):
            _write_state(
                {
                    "lease": lease,
                    "status": "paused",
                    "upstream_status": status,
                }
            )
            return

        _write_state(
            {
                "lease": lease,
                "status": "active",
                "upstream_status": status,
            }
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesIntakeLeaseController/3"

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"{self.address_string()} "
            f"[{self.log_date_time_string()}] "
            f"{format % args}",
            flush=True,
        )

    def _send_json(self, status: int, value: object) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
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
            state = _load_state()
            status = state.get("status", "none")
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": str(exc)},
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "lease_status": status},
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        authorization = self.headers.get(
            "Authorization",
            "",
        ).strip()

        if not _authorized(authorization):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "authorization_required"},
            )
            return

        if parsed.path == "/trigger":
            try:
                self._trigger(authorization)
            except RuntimeError as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
            return

        if parsed.path == "/pause":
            lease = parse_qs(parsed.query).get(
                "lease",
                [""],
            )[0].strip()

            if not lease:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "lease_required"},
                )
                return

            try:
                self._pause(lease)
            except RuntimeError as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": str(exc)},
                )
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found"},
        )

    def do_OPTIONS(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/trigger":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
            )
            return

        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "POST, OPTIONS")
        self.end_headers()

    def _trigger(self, authorization: str) -> None:
        lease = str(uuid.uuid4())

        with _REQUEST_LOCK:
            _load_state()
            _write_state(
                {
                    "lease": lease,
                    "status": "pending",
                }
            )

        threading.Thread(
            target=_trigger_intake_in_background,
            args=(lease, authorization),
            daemon=True,
            name=f"hermes-intake-trigger-{lease[:8]}",
        ).start()

        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "accepted": True,
                "lease": lease,
            },
        )

    def _pause(self, lease: str) -> None:
        with _REQUEST_LOCK:
            state = _load_state()
            current = str(state.get("lease", ""))
            current_status = str(state.get("status", ""))

            if not current or lease != current:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "paused": False,
                        "reason": "superseded",
                    },
                )
                return

            if current_status == "paused":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "paused": True,
                        "reason": "already_paused",
                    },
                )
                return

            if current_status == "pending":
                state["pause_requested"] = True
                _write_state(state)

                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        "ok": True,
                        "paused": False,
                        "reason": "pause_queued",
                    },
                )
                return

            if current_status != "active":
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "paused": False,
                        "reason": "lease_not_active",
                    },
                )
                return

            # Direct actuator has no persistent schedule to pause. The delayed
            # router cleanup now closes only this lease locally.
            _write_state(
                {
                    "lease": lease,
                    "status": "paused",
                }
            )

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "paused": True,
            },
        )


def main() -> int:
    if not ACTUATOR_BASE_URL:
        raise SystemExit("LEASE_ACTUATOR_BASE_URL is required")
    try:
        _validated_actuator_base_url()
        _read_token()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    server = ThreadingHTTPServer(
        (LISTEN_HOST, LISTEN_PORT),
        Handler,
    )

    print(
        f"lease-controller listening on "
        f"http://{LISTEN_HOST}:{LISTEN_PORT}; "
        f"actuator={ACTUATOR_BASE_URL}",
        flush=True,
    )

    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
