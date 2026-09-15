#!/usr/bin/env python3
"""Routing overlay plus bounded liveness recovery for the GitHub router.

The canonical router keeps ownership of signature verification, delivery
dedupe, repository admission and durable scope persistence. This entrypoint
adds only wake hints/recovery around those existing boundaries:

* completion-comment low-latency edge wake;
* durable-scope chain wake after a committed scope transition;
* durable defer when a supported direct PR edge wake is temporarily unavailable;
* bounded startup recovery for already-queued eligible work; and
* the existing low-frequency full-intake safety wake.

None of these paths decides GitHub/Kanban lifecycle state. Canonical intake and
edge reconciliation always fresh-read authoritative state.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

_COMPLETION_MARKER = "AGENT_REWORK_COMPLETE"
_HINT = threading.local()
_PERIODIC_FULL_INTAKE_DELIVERY = "router-periodic-full-intake"
_DEFAULT_FALLBACK_INTERVAL_SECONDS = 3600
_MIN_FALLBACK_INTERVAL_SECONDS = 300
_STARTUP_RECOVERY_ATTEMPTS = 3
_STARTUP_RECOVERY_DELAY_SECONDS = 1.0


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


def _supported_direct_edge_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    return (
        event.get("event") == "pull_request"
        and (
            (
                event.get("action") == "closed"
                and event.get("merged") is True
            )
            or (
                event.get("action") == "labeled"
                and event.get("label") == "agent-rework"
            )
        )
    )


def _eligible_scope_waiting(core: ModuleType, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else int(now)
    with core._STATE_LOCK:
        state = core._load_state_unlocked()
        core._recover_scope_claims_unlocked(state, current)
        queue = state.get("scope_queue")
        if not isinstance(queue, list):
            queue = []
        state["scope_queue"] = queue
        eligible = any(
            isinstance(item, dict)
            and int(item.get("not_before") or 0) <= current
            for item in queue
        )
        core._write_state_unlocked(state)
    return eligible


def _chain_scope_wake(core: ModuleType, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") not in {"acknowledged", "requeued", "pending"}:
        return result
    try:
        eligible = _eligible_scope_waiting(core)
    except Exception as exc:  # noqa: BLE001 - transition is already durable
        print(
            "github-router scope queue read-back warning: "
            f"{type(exc).__name__}",
            flush=True,
        )
        return {
            **result,
            "next_wake": {
                "accepted": False,
                "reason": "queue_check_failed",
            },
        }
    if not eligible:
        return {
            **result,
            "next_wake": {
                "accepted": False,
                "reason": "no_eligible_scope",
            },
        }
    try:
        wake = core._wake()
    except Exception as exc:  # noqa: BLE001 - transition is already durable
        print(
            "github-router scope chain wake warning: "
            f"{type(exc).__name__}",
            flush=True,
        )
        return {
            **result,
            "next_wake": {
                "accepted": False,
                "reason": "wake_failed",
            },
        }
    return {
        **result,
        "next_wake": {
            "accepted": True,
            "upstream_status": wake.get("upstream_status"),
        },
    }


def _install_liveness_recovery(core: ModuleType) -> None:
    if getattr(core, "_scope_liveness_recovery_installed", False):
        return
    required = ("_ack_scope", "_requeue_scope", "_n8n_edge_sync", "_wake")
    if not all(callable(getattr(core, name, None)) for name in required):
        return

    original_ack_scope = core._ack_scope
    original_requeue_scope = core._requeue_scope
    original_edge_sync = core._n8n_edge_sync

    def ack_scope(scope_id: str, claim_token: str) -> dict[str, Any]:
        return _chain_scope_wake(
            core,
            original_ack_scope(scope_id, claim_token),
        )

    def requeue_scope(
        scope_id: str,
        reason: str,
        claim_token: str,
    ) -> dict[str, Any]:
        return _chain_scope_wake(
            core,
            original_requeue_scope(scope_id, reason, claim_token),
        )

    def edge_sync(event: dict[str, Any]) -> dict[str, Any]:
        try:
            return original_edge_sync(event)
        except core.RouterError as edge_error:
            if not _supported_direct_edge_event(event):
                raise
            repository = event.get("repository")
            delivery_id = event.get("delivery")
            if not isinstance(repository, str) or not isinstance(delivery_id, str):
                raise
            prior_completion_hint = bool(
                getattr(_HINT, "completion_comment", False)
            )
            _HINT.completion_comment = False
            try:
                scope = core._enqueue_scope(
                    full=False,
                    repository=repository,
                    delivery_id=delivery_id,
                )
                if scope.get("in_flight") or scope.get("pending"):
                    wake = {
                        "skipped": True,
                        "reason": "scope_already_active_or_pending",
                    }
                else:
                    wake = core._wake()
            except Exception as defer_error:  # noqa: BLE001 - preserve retryability
                raise core.RouterError("edge_sync_defer_failed") from defer_error
            finally:
                _HINT.completion_comment = prior_completion_hint
            print(
                "github-router direct edge wake deferred to durable scope "
                f"repository={repository} "
                f"scope={scope.get('id', '')} "
                f"wake_skipped={bool(wake.get('skipped'))}",
                flush=True,
            )
            return {
                "status": 202,
                "body": {
                    "ok": True,
                    "deferred": True,
                    "reason": "edge_sync_deferred",
                    "scope_id": scope.get("id", ""),
                    "direct_error": type(edge_error).__name__,
                },
            }

    core._ack_scope = ack_scope
    core._requeue_scope = requeue_scope
    core._n8n_edge_sync = edge_sync
    core._scope_liveness_recovery_installed = True


def install(core: ModuleType) -> ModuleType:
    """Install the bounded event-routing and liveness overlays once."""
    if not getattr(core, "_completion_comment_wake_installed", False):
        original_event_repositories = core._event_repositories
        original_enqueue_scope = core._enqueue_scope

        def event_repositories(event: str, payload: dict[str, Any]) -> list[str]:
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
                if not core._managed_repository(target):
                    return result
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

    _install_liveness_recovery(core)
    return core


def _fallback_interval_seconds() -> int:
    raw = os.environ.get(
        "GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS",
        str(_DEFAULT_FALLBACK_INTERVAL_SECONDS),
    )
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS must be an integer"
        ) from exc
    if interval < _MIN_FALLBACK_INTERVAL_SECONDS:
        raise RuntimeError(
            "GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS must be at least "
            f"{_MIN_FALLBACK_INTERVAL_SECONDS}"
        )
    return interval


def _periodic_full_intake_once(core: ModuleType) -> dict[str, Any]:
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
        except Exception as exc:  # noqa: BLE001 - next interval is bounded retry
            print(
                "github-router periodic full intake warning: "
                f"{type(exc).__name__}",
                flush=True,
            )


def _startup_queue_recovery(
    core: ModuleType,
    *,
    attempts: int = _STARTUP_RECOVERY_ATTEMPTS,
    delay_seconds: float = _STARTUP_RECOVERY_DELAY_SECONDS,
) -> dict[str, Any]:
    if not _eligible_scope_waiting(core):
        return {"skipped": True, "reason": "no_eligible_scope"}
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            wake = core._wake()
        except Exception as exc:  # noqa: BLE001 - bounded startup recovery
            last_error = type(exc).__name__
            if attempt + 1 < max(1, attempts):
                time.sleep(max(0.0, delay_seconds))
                continue
            return {
                "skipped": False,
                "accepted": False,
                "reason": "wake_failed",
                "error_type": last_error,
            }
        return {
            "skipped": False,
            "accepted": True,
            "upstream_status": wake.get("upstream_status"),
        }
    return {
        "skipped": False,
        "accepted": False,
        "reason": "wake_failed",
        "error_type": last_error,
    }


_core = install(_load_core())


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    interval = _fallback_interval_seconds()
    recovery = _startup_queue_recovery(_core)
    print(
        "github-router startup queue recovery "
        f"skipped={bool(recovery.get('skipped'))} "
        f"accepted={bool(recovery.get('accepted'))} "
        f"reason={recovery.get('reason', '')}",
        flush=True,
    )
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
