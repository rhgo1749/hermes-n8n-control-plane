#!/usr/bin/env python3
from __future__ import annotations

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


# This edge controller is intentionally loopback-only. Do not make the
# listen address runtime-configurable: n8n reaches it through host networking.
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("LEASE_LISTEN_PORT", "5680"))

# Host-specific Hermes addresses belong in deployment configuration, never
# in the tracked controller source.
HERMES_BASE_URL = os.environ.get(
    "LEASE_HERMES_BASE_URL",
    "",
).strip().rstrip("/")
HERMES_JOB_ID = os.environ.get(
    "LEASE_HERMES_JOB_ID",
    "bf431b2a6ba6",
)
HERMES_PROFILE = os.environ.get(
    "LEASE_HERMES_PROFILE",
    "default",
)
HERMES_TRIGGER_TIMEOUT_SECONDS = float(
    os.environ.get("LEASE_HERMES_TRIGGER_TIMEOUT_SECONDS", "900")
)
HERMES_PAUSE_TIMEOUT_SECONDS = float(
    os.environ.get("LEASE_HERMES_PAUSE_TIMEOUT_SECONDS", "60")
)

STATE_PATH = Path(
    os.environ.get(
        "LEASE_STATE_PATH",
        "/state/hermes-intake-lease.json",
    )
)

_REQUEST_LOCK = threading.Lock()
_TRIGGER_LOCK = threading.Lock()


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
    data = _json_bytes(value) + b"\n"

    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _upstream_url(action: str) -> str:
    return (
        f"{HERMES_BASE_URL}/api/cron/jobs/"
        f"{HERMES_JOB_ID}/{action}?profile={HERMES_PROFILE}"
    )


def _call_hermes(action: str, authorization: str) -> tuple[int, bytes]:
    request = Request(
        _upstream_url(action),
        method="POST",
        headers={
            "Authorization": authorization,
            "Accept": "application/json",
        },
        data=b"",
    )
    timeout = (
        HERMES_TRIGGER_TIMEOUT_SECONDS
        if action == "trigger"
        else HERMES_PAUSE_TIMEOUT_SECONDS
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Hermes {action} request failed: {exc}") from exc


def _trigger_hermes_in_background(
    lease: str,
    authorization: str,
) -> None:
    """Run Hermes' synchronous dashboard trigger without blocking the caller.

    Hermes' dashboard ``/trigger`` executes a cron job end-to-end and may take
    minutes. The lease-controller therefore acknowledges the wake immediately
    and waits for Hermes in a daemon thread. A dedicated trigger lock preserves
    the previous controller behavior of serializing upstream trigger calls.
    """

    try:
        with _TRIGGER_LOCK:
            status, _body = _call_hermes("trigger", authorization)
    except RuntimeError as exc:
        # A timeout is ambiguous: Hermes may have accepted the trigger and kept
        # running after this client disconnected. Keep the current lease pending
        # rather than falsely declaring the wake failed or resurrecting an older
        # lease.
        with _REQUEST_LOCK:
            state = _load_state()
            if str(state.get("lease", "")) == lease:
                state["trigger_error"] = str(exc)
                _write_state(state)
        print(
            "lease-controller Hermes trigger outcome is ambiguous "
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
            print(
                "lease-controller Hermes trigger rejected "
                f"lease={lease} status={status}",
                flush=True,
            )
            return

        if state.get("pause_requested"):
            try:
                pause_status, _body = _call_hermes("pause", authorization)
            except RuntimeError as exc:
                _write_state(
                    {
                        "lease": lease,
                        "status": "active",
                        "pause_error": str(exc),
                    }
                )
                print(
                    "lease-controller queued pause failed "
                    f"lease={lease}: {exc}",
                    flush=True,
                )
                return

            if 200 <= pause_status < 300:
                _write_state(
                    {
                        "lease": lease,
                        "status": "paused",
                        "upstream_status": pause_status,
                    }
                )
            else:
                _write_state(
                    {
                        "lease": lease,
                        "status": "active",
                        "pause_error": "hermes_pause_rejected",
                        "upstream_status": pause_status,
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
    server_version = "HermesIntakeLeaseController/2"

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"{self.address_string()} "
            f"[{self.log_date_time_string()}] "
            f"{fmt % args}",
            flush=True,
        )

    def _send_json(self, status: int, value: object) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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

        authorization = self.headers.get("Authorization", "").strip()
        if not authorization:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "authorization_required"},
            )
            return

        if parsed.path == "/trigger":
            self._trigger(authorization)
            return

        if parsed.path == "/pause":
            lease = parse_qs(parsed.query).get("lease", [""])[0].strip()
            if not lease:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "lease_required"},
                )
                return
            self._pause(authorization, lease)
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found"},
        )

    def _trigger(self, authorization: str) -> None:
        lease = str(uuid.uuid4())

        with _REQUEST_LOCK:
            # Validate any persisted state before replacing it. Once the new
            # lease is persisted, never resurrect an older lease.
            _load_state()

            # Persist first so the accepted wake immediately supersedes every
            # older delayed pause, even across controller restarts.
            _write_state(
                {
                    "lease": lease,
                    "status": "pending",
                }
            )

        threading.Thread(
            target=_trigger_hermes_in_background,
            args=(lease, authorization),
            daemon=True,
            name=f"hermes-intake-trigger-{lease[:8]}",
        ).start()

        # Hermes' dashboard trigger is synchronous and executes the cron job
        # end-to-end. Acknowledge only admission here; completion is reflected
        # later through the persisted lease state.
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "accepted": True,
                "lease": lease,
            },
        )

    def _pause(self, authorization: str, lease: str) -> None:
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

            try:
                status, _body = _call_hermes("pause", authorization)
            except RuntimeError as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"ok": False, "error": str(exc)},
                )
                return

            if not 200 <= status < 300:
                self._send_json(
                    status,
                    {
                        "ok": False,
                        "paused": False,
                        "error": "hermes_pause_rejected",
                        "upstream_status": status,
                    },
                )
                return

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
                "upstream_status": status,
            },
        )


def main() -> int:
    if not HERMES_BASE_URL:
        raise SystemExit("LEASE_HERMES_BASE_URL is required")

    server = ThreadingHTTPServer(
        (LISTEN_HOST, LISTEN_PORT),
        Handler,
    )
    print(
        f"lease-controller listening on "
        f"http://{LISTEN_HOST}:{LISTEN_PORT}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
