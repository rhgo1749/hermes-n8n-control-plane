from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "github-router" / "router.py"
spec = importlib.util.spec_from_file_location("github_router_startup", MODULE_PATH)
assert spec and spec.loader
router: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = router
spec.loader.exec_module(router)


def test_main_allows_missing_installation_id_for_managed_webhook_mode(monkeypatch) -> None:
    served: list[tuple[tuple[str, int], object]] = []

    class FakeServer:
        def __init__(self, address, handler):
            served.append((address, handler))

        def serve_forever(self) -> None:
            return None

    monkeypatch.setattr(router, "GITHUB_INSTALLATION_ID", None)
    monkeypatch.setattr(router, "ThreadingHTTPServer", FakeServer)

    assert router.main() == 0
    assert served == [((router.LISTEN_HOST, router.LISTEN_PORT), router.Handler)]
