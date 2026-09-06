#!/usr/bin/env python3
"""Small event-routing overlay for the GitHub router.

The canonical router intentionally owns signature verification, delivery dedupe,
owner/managed-repository admission and generic intake scope persistence. This
entrypoint leaves those boundaries unchanged and adds two bounded wake hints:

* a low-latency edge wake for a PR conversation ``issue_comment(created)`` whose
  first non-empty line is exactly ``AGENT_REWORK_COMPLETE``;
* a low-frequency full-intake safety wake that reuses the canonical durable
  scope queue so a missed ``agent-ready`` webhook cannot strand an Issue
  indefinitely.

Neither hint is lifecycle authority. The completion-comment path only asks the
existing edge owner to fresh-read GitHub. The periodic path only enqueues a
canonical ``full`` intake scope and wakes the preserved Hermes intake job; it
never reads or writes Issue/Kanban lifecycle state directly and it never runs
webhook reconciliation.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

_COMPLETION_MARKER = "AGENT_REWORK_COMPLETE"
_HINT = threading.local()
_PERIODIC_FULL_INTAKE_DELIVERY = "router-periodic-full-intake"
_DEFAULT_FALLBACK_INTERVAL_SECONDS = 3600
_MIN_FALLBACK_INTERVAL_SECONDS = 300


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
        # ThreadingHTTPServer uses one request thread per delivery. Always
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
            # allowlist. Reuse its existing rework wake envelope internally;
            # this payload is a control-plane trigger only and is never used as
            # PR state evidence. The edge immediately fresh-reads GitHub.
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


def _fallback_interval_seconds() -> int:
    raw = os.environ.get(
        "GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS",
        str(_DEFAULT_FALLBACK_INTERVAL_SECONDS),
    )
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS must be an integer") from exc
    if interval < _MIN_FALLBACK_INTERVAL_SECONDS:
        raise RuntimeError(
            "GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS must be at least "
            f"{_MIN_FALLBACK_INTERVAL_SECONDS}"
        )
    return interval


def _periodic_full_intake_once(core: ModuleType) -> dict[str, Any]:
    """Enqueue one idempotent full-intake safety scope and wake canonical intake.

    A stable synthetic delivery identity deduplicates only while the same safety
    scope is queued, in-flight, or pending. Once an acknowledged scope leaves
    those stores, the next interval may create a fresh full scan. If a prior
    wake failed after enqueue, the next tick sees the queued scope and re-wakes
    it rather than adding a duplicate.
    """
    scope = core._enqueue_scope(
        full=True,
        delivery_id=_PERIODIC_FULL_INTAKE_DELIVERY,
    )
    if scope.get("in_flight") or scope.get("pending"):
        return {
            "scope": scope,
            "wake": {
                "skipped": True,
                "reason": "scope_already_active_or_pending",
            },
        }
    return {"scope": scope, "wake": core._wake()}


def _periodic_full_intake_loop(
    core: ModuleType,
    interval_seconds: int,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the safety wake at a bounded cadence, never immediately at startup."""
    stop = stop_event if stop_event is not None else threading.Event()
    while not stop.wait(interval_seconds):
        try:
            result = _periodic_full_intake_once(core)
            scope = result.get("scope") or {}
            wake = result.get("wake") or {}
            print(
                "github-router periodic full intake "
                f"scope={scope.get('id', '')} "
                f"existing={bool(scope.get('existing'))} "
                f"wake_skipped={bool(wake.get('skipped'))} "
                f"upstream_status={wake.get('upstream_status', '')}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - next interval is the bounded retry
            print(
                "github-router periodic full intake warning: "
                f"{type(exc).__name__}",
                flush=True,
            )


_core = install(_load_core())


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    interval = _fallback_interval_seconds()
    threading.Thread(
        target=_periodic_full_intake_loop,
        args=(_core, interval),
        daemon=True,
        name="github-router-periodic-full-intake",
    ).start()
    print(
        "github-router periodic full intake enabled "
        f"interval_seconds={interval}",
        flush=True,
    )
    return int(_core.main())


if __name__ == "__main__":
    raise SystemExit(main())
