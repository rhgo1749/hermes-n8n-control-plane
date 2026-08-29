from __future__ import annotations

import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Self, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


intake = _load_module(
    "github_redirect_test_intake",
    ROOT / "automation" / "hermes" / "scripts" / "github-agent-ready-kanban-intake.py",
)
registry = _load_module(
    "github_redirect_test_registry",
    ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py",
)
router = _load_module(
    "github_redirect_test_router",
    ROOT / "automation" / "n8n" / "github-router" / "router.py",
)


class _RedirectServer(ThreadingHTTPServer):
    role: str
    location: str
    authorization: list[str | None]
    bodies: list[bytes]


class _RedirectHandler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        server = cast(_RedirectServer, self.server)
        server.authorization.append(self.headers.get("Authorization"))
        server.bodies.append(body)
        if server.role == "source":
            self.send_response(307)
            self.send_header("Location", server.location)
            self.end_headers()
            return
        encoded = json.dumps({"redirected": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        return None


class _RedirectPair:
    source: _RedirectServer
    target: _RedirectServer
    threads: list[Thread]
    source_base: str

    def __enter__(self) -> Self:
        self.target = _RedirectServer(("127.0.0.1", 0), _RedirectHandler)
        self.target.role = "target"
        self.target.location = ""
        self.target.authorization = []
        self.target.bodies = []
        self.source = _RedirectServer(("127.0.0.1", 0), _RedirectHandler)
        self.source.role = "source"
        self.source.location = (
            f"http://127.0.0.1:{self.target.server_port}/redirect-target"
        )
        self.source.authorization = []
        self.source.bodies = []
        self.threads = [
            Thread(target=server.serve_forever, daemon=True)
            for server in (self.source, self.target)
        ]
        for thread in self.threads:
            thread.start()
        self.source_base = f"http://127.0.0.1:{self.source.server_port}"
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        for server in (self.source, self.target):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)


def test_intake_get_does_not_forward_bearer_to_redirect_target(monkeypatch):
    with _RedirectPair() as pair:
        monkeypatch.setattr(intake, "GITHUB_API", pair.source_base)
        with pytest.raises(intake.IntakeError, match="GitHub API 307"):
            intake._github_get_json(
                "redirect-secret",
                "/repos/example/project",
                user_agent="test",
            )
        assert pair.source.authorization == ["Bearer redirect-secret"]
        assert pair.target.authorization == []
        assert pair.target.bodies == []


def test_intake_patch_does_not_forward_bearer_to_redirect_target(monkeypatch):
    with _RedirectPair() as pair:
        monkeypatch.setattr(intake, "GITHUB_API", pair.source_base)
        status, body = intake._github_patch_json(
            "redirect-secret",
            "/repos/example/project/issues/1",
            {"labels": ["agent-ready"]},
        )
        assert (status, body) == (307, None)
        assert pair.source.authorization == ["Bearer redirect-secret"]
        assert pair.target.authorization == []
        assert pair.target.bodies == []


def test_registry_get_does_not_forward_bearer_to_redirect_target(monkeypatch):
    with _RedirectPair() as pair:
        monkeypatch.setattr(registry, "GITHUB_API", pair.source_base)
        with pytest.raises(registry.RegistryError, match="HTTP 307"):
            registry._github_json("redirect-secret", "/repos/example/project")
        assert pair.source.authorization == ["Bearer redirect-secret"]
        assert pair.target.authorization == []
        assert pair.target.bodies == []


def test_router_get_does_not_forward_bearer_to_redirect_target(monkeypatch):
    with _RedirectPair() as pair:
        monkeypatch.setattr(router, "GITHUB_API", pair.source_base)
        with pytest.raises(router.RouterError, match="returned HTTP 307"):
            router._github_request(
                "GET",
                "/repos/example/project",
                "redirect-secret",
            )
        assert pair.source.authorization == ["Bearer redirect-secret"]
        assert pair.target.authorization == []
        assert pair.target.bodies == []


def test_router_patch_does_not_forward_bearer_to_redirect_target(monkeypatch):
    with _RedirectPair() as pair:
        monkeypatch.setattr(router, "GITHUB_API", pair.source_base)
        with pytest.raises(router.RouterError, match="returned HTTP 307"):
            router._github_request(
                "PATCH",
                "/repos/example/project/hooks/1",
                "redirect-secret",
                {"active": True},
            )
        assert pair.source.authorization == ["Bearer redirect-secret"]
        assert pair.source.bodies == [b'{"active":true}']
        assert pair.target.authorization == []
        assert pair.target.bodies == []
