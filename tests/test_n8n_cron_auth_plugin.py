#!/usr/bin/env python3
"""Focused runtime test for the user plugin without touching live Hermes state."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "hermes-plugin" / "n8n-cron-auth" / "__init__.py"
HERMES_SOURCE = Path("/ws/hermes-agent")


class Context:
    def __init__(self) -> None:
        self.providers = []

    def register_dashboard_auth_provider(self, provider) -> None:
        self.providers.append(provider)


def load_plugin():
    spec = importlib.util.spec_from_file_location("test_n8n_cron_auth", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not HERMES_SOURCE.is_dir():
        raise SystemExit(f"Hermes source is unavailable: {HERMES_SOURCE}")
    sys.path.insert(0, str(HERMES_SOURCE))
    from hermes_cli.dashboard_auth.token_auth import clear_token_routes, is_token_route

    with tempfile.TemporaryDirectory(prefix="n8n-cron-auth-") as raw:
        root = Path(raw)
        token_path = root / "token"
        token = secrets.token_urlsafe(32)
        token_path.write_text(token + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        previous = os.environ.get("HERMES_N8N_CRON_TOKEN_FILE")
        os.environ["HERMES_N8N_CRON_TOKEN_FILE"] = str(token_path)
        try:
            clear_token_routes()
            plugin = load_plugin()
            context = Context()
            plugin.register(context)
            assert len(context.providers) == 1
            provider = context.providers[0]
            assert provider.verify_token(token=token) is not None
            assert provider.verify_token(token=token + "x") is None
            assert all(is_token_route(path) for path in plugin.TOKEN_ROUTE_PATHS)
            assert len(plugin.TOKEN_ROUTE_PATHS) == len(plugin.ALLOWED_JOBS) * 2

            clear_token_routes()
            original_providers = plugin.list_token_providers
            setattr(plugin, "list_token_providers", lambda: [object()])
            conflict = Context()
            try:
                plugin.register(conflict)
            finally:
                setattr(plugin, "list_token_providers", original_providers)
            assert not conflict.providers
            assert plugin.LAST_SKIP_REASON == "another non-interactive dashboard token provider is already registered"

            clear_token_routes()
            token_auth = importlib.import_module("hermes_cli.dashboard_auth.token_auth")

            def fail_register_route(_path: str) -> None:
                raise RuntimeError("test failure")

            original_register_route = getattr(token_auth, "register_token_route")
            setattr(token_auth, "register_token_route", fail_register_route)
            route_failure = Context()
            try:
                plugin.register(route_failure)
            finally:
                setattr(token_auth, "register_token_route", original_register_route)
            assert not route_failure.providers
            assert plugin.LAST_SKIP_REASON == "token route registration failed: RuntimeError"

            clear_token_routes()
            token_path.chmod(0o644)
            rejected = Context()
            plugin.register(rejected)
            assert not rejected.providers
            assert plugin.LAST_SKIP_REASON == "token file permissions are broader than 0600"
        finally:
            clear_token_routes()
            if previous is None:
                os.environ.pop("HERMES_N8N_CRON_TOKEN_FILE", None)
            else:
                os.environ["HERMES_N8N_CRON_TOKEN_FILE"] = previous

    print(json.dumps({"ok": True, "route_count": 10}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
