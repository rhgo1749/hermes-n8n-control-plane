"""H4V3 Overview user plugin (dashboard extension).

This plugin only ships a dashboard extension (``dashboard/manifest.json`` +
``plugin_api.py`` + static bundle).  The no-op ``register`` below satisfies
the Hermes agent plugin loader's directory-plugin contract so the plugin can
be discovered and enabled through ``hermes plugins enable``; the dashboard
backend/UI are loaded by the dashboard plugin system instead.
"""
from __future__ import annotations


def register(ctx) -> None:  # type: ignore[no-untyped-def]
    """No agent-side hooks; the dashboard extension is self-contained."""
    del ctx
