#!/usr/bin/env python3
"""Small event-routing overlay for the GitHub router.

The canonical router intentionally owns signature verification, delivery dedupe,
owner/managed-repository admission and generic intake scope persistence.  This
entrypoint leaves those boundaries unchanged and adds one low-latency *wake
hint* for a PR conversation ``issue_comment(created)`` whose first non-empty
line is exactly ``AGENT_REWORK_COMPLETE``.

The comment is never treated as completion authority here.  The edge reconciler
still fresh-reads GitHub and validates trusted actor, task/request binding, live
PR head, validation marker, timing and current-round run provenance before any
Kanban or label transition.  The router only asks the already-existing edge
owner to look now instead of waiting for a later generic intake wake.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

_COMPLETION_MARKER = "AGENT_REWORK_COMPLETE"
_HINT = threading.local()


def _load_core() -> ModuleType:
    path = Path(__file__).with_name("router.py")
    spec = importlib.util.spec_from_file_location("github_router_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load GitHub router core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _first_nonempty_line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _is_pr_completion_comment(event: str, payload: dict[str, Any]) -> bool:
    if event != "issue_comment":
        return False
    action = payload.get("action")
    if not isinstance(action, str) or action.strip().casefold() != "created":
        return False
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
        return False
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return False
    return _first_nonempty_line(comment.get("body")) == _COMPLETION_MARKER


def install(core: ModuleType) -> ModuleType:
    """Install the bounded completion-comment wake overlay once."""
    if getattr(core, "_completion_comment_wake_installed", False):
        return core

    original_event_repositories = core._event_repositories
    original_enqueue_scope = core._enqueue_scope

    def event_repositories(event: str, payload: dict[str, Any]) -> list[str]:
        # ThreadingHTTPServer uses one request thread per delivery.  Always
        # clear any prior hint before parsing the current signed event.
        _HINT.completion_comment = False
        repositories = original_event_repositories(event, payload)
        if len(repositories) == 1 and _is_pr_completion_comment(event, payload):
            _HINT.completion_comment = True
        return repositories

    def enqueue_scope(
        *,
        full: bool,
        repository: str | None = None,
        repositories: list[str] | tuple[str, ...] | None = None,
        delivery_id: str | None = None,
    ) -> dict[str, Any]:
        result = original_enqueue_scope(
            full=full,
            repository=repository,
            repositories=repositories,
            delivery_id=delivery_id,
        )
        try:
            if full or not bool(getattr(_HINT, "completion_comment", False)):
                return result
            scoped = (
                list(repositories)
                if repositories is not None
                else ([repository] if repository is not None else [])
            )
            if len(scoped) != 1 or not delivery_id:
                return result
            target = scoped[0]
            # Unknown/App-first-discovery repositories must finish onboarding
            # before they can use the managed-repository low-latency lane.
            if not core._managed_repository(target):
                return result

            # /v1/edge-sync currently exposes a deliberately tiny wake
            # allowlist.  Reuse its existing rework wake envelope internally;
            # this payload is a control-plane trigger only and is never used as
            # PR state evidence.  The edge immediately fresh-reads GitHub.
            wake = core._n8n_edge_sync(
                {
                    "repository": target,
                    "event": "pull_request",
                    "action": "labeled",
                    "merged": False,
                    "label": "agent-rework",
                    "delivery": delivery_id,
                }
            )
            return {
                **result,
                "completion_edge_sync": True,
                "completion_edge_sync_status": wake.get("status"),
            }
        finally:
            _HINT.completion_comment = False

    core._event_repositories = event_repositories
    core._enqueue_scope = enqueue_scope
    core._completion_comment_wake_installed = True
    return core


_core = install(_load_core())


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    return int(_core.main())


if __name__ == "__main__":
    raise SystemExit(main())
