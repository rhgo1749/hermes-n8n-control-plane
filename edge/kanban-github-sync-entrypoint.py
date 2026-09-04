#!/usr/bin/env python3
"""Deployment entrypoint for GitHub/Kanban edge reconciliation.

The large canonical reconciliation implementation stays in
``kanban-github-sync.py`` in the repository. During deployment it is copied
beside this entrypoint as ``kanban-github-sync-core.py`` while this file is
installed under the historical live name ``kanban-github-sync.py``.

Small, independently reviewable overlays are installed here so the canonical
state machine can stay unchanged: resource admission controls worker capacity,
dynamic backend resolution (when installed) makes that admission follow each
profile's current provider/endpoint, head-binding feedback adds observational
PR guidance without changing rework transitions, the retry-signal guard
prevents edge-owned help text from being consumed as a fresh maintainer retry,
the rework-delivery provenance guard preserves specialist delivery ownership,
and attention recovery re-evaluates that same strict delivery evidence before
requiring a new human-authorized retry round. The trusted completed-Issue
fallback terminalizes only stale ``review/no_linked_pr`` cards explicitly
closed as completed by a trusted maintainer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import kanban_resource_admission as resource_admission
from kanban_head_binding_feedback import install_head_binding_feedback
from kanban_retry_signal_guard import (
    install_closed_completed_terminal_fallback,
    install_rework_delivery_provenance_guard,
    install_retry_signal_guard,
    install_supersede_signal_guard,
)
try:
    from kanban_workspace_admission import install_workspace_admission
except ImportError:  # backwards-compatible deploy before the overlay exists
    install_workspace_admission = None  # type: ignore[assignment]

try:
    from kanban_dynamic_resource import install_everywhere
except ImportError:  # backwards-compatible deploy before scheduler install
    install_everywhere = None  # type: ignore[assignment]


def _install_rework_attention_delivery_recovery(core: ModuleType) -> None:
    """Recover a held REVIEW when its current round now has valid delivery.

    The canonical lifecycle intentionally keeps ``review + agent-rework`` in a
    human-attention hold until a fresh ``AGENT_REWORK_RETRY`` opens a new round.
    That fail-closed rule predates the specialist provenance repair: a round
    that was held only because delivery evidence was misclassified can become
    valid after the repair, but the retry gate returns before the canonical
    delivery branch gets a chance to re-evaluate it.

    This wrapper runs only after the canonical function returns exactly
    ``rework_retry_pending``. A real fresh retry therefore keeps precedence.
    The held round is recovered only when the already-installed strict delivery
    evidence function accepts it; no timestamp-only or ordinary core run can
    enter this path.

    Attention self-heal restores ``agent-rework`` before recording the durable
    attention event. Canonical delivery freshness can therefore mistake that
    edge-owned label restoration for a new maintainer request. For the exact
    same-round recovery shape, and only when the newest label addition lands
    immediately before the current attention record, retry delivery validation
    once with that single timeline event hidden. All later marker/head/run
    validation still executes through the canonical delivery function.
    """
    if getattr(core, "_rework_attention_delivery_recovery_installed", False):
        return

    original_reconcile = core._reconcile_rework_lifecycle

    def _same_round_recovery_event(context):
        if not isinstance(context, dict):
            return False
        event = context.get("event")
        if not isinstance(event, tuple) or len(event) != 3:
            return False
        payload, _event_at, kind = event
        return bool(
            isinstance(payload, dict)
            and kind == "github_pr_rework_retry"
            and payload.get("source") == "github_edge_rework_recovery"
            and isinstance(payload.get("rework_round"), int)
            and not isinstance(payload.get("rework_round"), bool)
            and int(payload["rework_round"]) > 0
        )

    def _retry_without_attention_selfheal_label(
        conn,
        client,
        ref,
        task_id,
        pr,
        context,
        pending_result,
    ):
        if not _same_round_recovery_event(context):
            return None
        try:
            attention_at = int(pending_result.get("attention_at") or 0)
        except (TypeError, ValueError):
            return None
        if attention_at <= 0:
            return None

        latest_label_fn = getattr(core, "_latest_rework_label_at", None)
        parse_ts = getattr(core, "_parse_iso_ts", None)
        if latest_label_fn is None or parse_ts is None:
            return None
        try:
            latest_label_at = latest_label_fn(
                client, ref, int(context["pr_number"])
            )
        except core.GithubCompletionError:
            return None
        if latest_label_at is None:
            return None
        try:
            latest_label_at = int(latest_label_at)
        except (TypeError, ValueError):
            return None

        # _restore_rework_labels() runs before _record_rework_attention().
        # Require exactly that ordering and a tight bounded window. A label
        # added after attention (or long before it) remains genuine newer
        # lifecycle evidence and is never hidden.
        delta = attention_at - latest_label_at
        if delta < 0 or delta > 30:
            return None

        timeline_path = (
            f"/repos/{ref.repository}/issues/{int(context['pr_number'])}/timeline"
        )

        class _AttentionLabelFilteredClient:
            def __init__(self, base):
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

            def get_paginated(self, path, params=None, *, max_pages=10):
                items = self._base.get_paginated(
                    path, params, max_pages=max_pages
                )
                if path != timeline_path:
                    return items
                filtered = []
                for item in items:
                    if not isinstance(item, dict) or item.get("event") != "labeled":
                        filtered.append(item)
                        continue
                    label = item.get("label")
                    if (
                        not isinstance(label, dict)
                        or str(label.get("name") or "") != core.REWORK_LABEL
                    ):
                        filtered.append(item)
                        continue
                    event_at = parse_ts(item.get("created_at"))
                    if event_at is not None and int(event_at) == latest_label_at:
                        continue
                    filtered.append(item)
                return filtered

        filtered_client = _AttentionLabelFilteredClient(client)
        try:
            delivered, reason, evidence = core._rework_delivery_evidence(
                conn,
                filtered_client,
                ref,
                task_id,
                pr,
                context["event"],
            )
        except core.GithubCompletionError:
            return None
        if not delivered:
            return delivered, reason, evidence
        recovered_evidence = dict(evidence)
        recovered_evidence.update({
            "attention_selfheal_label_recovered": True,
            "attention_at": attention_at,
            "selfheal_label_at": latest_label_at,
        })
        return True, reason, recovered_evidence

    def reconcile_rework_lifecycle(
        conn,
        kanban_db,
        client,
        ref,
        decision,
        row,
        context,
        *,
        dry_run,
        failure_limit,
    ):
        result = original_reconcile(
            conn,
            kanban_db,
            client,
            ref,
            decision,
            row,
            context,
            dry_run=dry_run,
            failure_limit=failure_limit,
        )
        if not isinstance(result, dict) or result.get("reason") != "rework_retry_pending":
            return result
        if not isinstance(context, dict) or "_error" in context:
            return result

        status = str(row["status"] or "")
        labels = set(context.get("labels") or ())
        pr = context.get("pr")
        if not (
            status == "review"
            and pr is not None
            and getattr(pr, "state", "") == "open"
            and core.REWORK_LABEL in labels
            and core.WORKING_LABEL not in labels
        ):
            return result

        task_id = str(row["id"])
        try:
            delivered, delivery_reason, evidence = core._rework_delivery_evidence(
                conn,
                client,
                ref,
                task_id,
                pr,
                context["event"],
            )
        except core.GithubCompletionError:
            return result
        if (
            not delivered
            and delivery_reason == "delivery_superseded_by_new_rework"
        ):
            retried = _retry_without_attention_selfheal_label(
                conn,
                client,
                ref,
                task_id,
                pr,
                context,
                result,
            )
            if retried is not None:
                delivered, delivery_reason, evidence = retried
        if not delivered:
            return result

        if dry_run:
            return {
                "task_id": task_id,
                "status": "review",
                "changed": False,
                "reason": "agent_review_ready_predicted",
                "evidence": evidence,
                "attention_recovered": True,
            }

        try:
            _, label_reason, label_evidence = core._project_pr_lifecycle_labels(
                client,
                ref,
                int(context["pr_number"]),
                add=(core.REVIEW_READY_LABEL,),
                remove=(core.REWORK_LABEL, core.WORKING_LABEL),
            )
        except core.GithubCompletionError as exc:
            return {
                "task_id": task_id,
                "status": "review",
                "changed": False,
                "reason": "review_ready_label_projection_failed",
                "error": str(exc),
                "evidence": evidence,
                "attention_recovered": True,
            }

        evidence_head = str(evidence.get("head") or "").casefold()
        if core._latest_delivery_head(conn, task_id) != evidence_head:
            with conn:
                core._append_sync_event(
                    conn,
                    task_id,
                    {
                        "previous_status": "review",
                        "new_status": "review",
                        "reason": "agent_review_ready",
                        "repository": ref.repository,
                        "pr_number": int(context["pr_number"]),
                        **dict(evidence),
                        "label_action": label_reason,
                        "attention_recovered": True,
                    },
                    kind="github_pr_rework_delivery",
                )

        return {
            "task_id": task_id,
            "status": "review",
            "changed": False,
            "reason": "agent_review_ready",
            "evidence": evidence,
            "lifecycle": label_evidence,
            "attention_recovered": True,
        }

    core._reconcile_rework_lifecycle = reconcile_rework_lifecycle
    core._rework_attention_delivery_recovery_installed = True


def _core_path() -> Path:
    here = Path(__file__).resolve()
    deployed = here.with_name("kanban-github-sync-core.py")
    if deployed.is_file():
        return deployed

    # Repository checkout mode: the entrypoint sits beside the canonical
    # implementation under its normal source name.
    source = here.with_name("kanban-github-sync.py")
    if source != here and source.is_file():
        return source
    raise RuntimeError("canonical kanban-github-sync implementation is missing")


def _load_core() -> ModuleType:
    path = _core_path()
    name = "kanban_github_sync_core"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load edge sync core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    # Patch policy parsing and cross-board helpers before the legacy edge
    # admission wrapper captures them. Core READY/REVIEW claim admission is
    # installed separately by the h4v3-resource-scheduler user plugin in the
    # long-lived gateway process. If an older deployment has not installed
    # the dynamic module yet, preserve the previous assignee-only behavior.
    if install_everywhere is not None:
        install_everywhere(resource_admission)
    resource_admission.install_resource_admission(module)
    install_head_binding_feedback(module)
    install_retry_signal_guard(module)
    install_supersede_signal_guard(module)
    install_closed_completed_terminal_fallback(module)
    install_rework_delivery_provenance_guard(module)
    _install_rework_attention_delivery_recovery(module)
    if install_workspace_admission is not None:
        install_workspace_admission(module)
    return module


_core = _load_core()

# Preserve import compatibility for callers/tests that load the historical
# live script and access its public or private helpers directly.
def __getattr__(name: str):
    return getattr(_core, name)


def _main(argv=None) -> int:
    return int(_core._main(argv))


if __name__ == "__main__":
    raise SystemExit(_main())
