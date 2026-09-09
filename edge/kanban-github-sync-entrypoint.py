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
requiring a new human-authorized retry round. The rework-context overlay only
normalizes stale worker-facing handoff prose and does not change lifecycle
state. The trusted completed-Issue fallback terminalizes only stale
``review/no_linked_pr`` cards explicitly closed as completed by a trusted
maintainer.
"""
from __future__ import annotations

import importlib.util
import json
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

    Edge recovery may restore ``agent-rework`` more than once during one
    immutable rework round: once for an automatic same-round retry and again
    when a later human-attention hold is projected. Canonical label freshness
    sees those GitHub timeline additions but cannot tell that they were emitted
    by the edge itself. For an exact current same-round recovery only, this
    wrapper identifies edge-owned label projections by two independent facts:

      * GitHub shows ``agent-working`` removed and ``agent-rework`` added at the
        same second; and
      * a durable same-round ``github_pr_rework_retry`` or
        ``github_pr_rework_attention`` event follows within 30 seconds.

    Only those label-addition events are hidden for one strict delivery
    re-evaluation. Any unmatched/new maintainer label remains visible, and all
    completion-marker, exact-head, run, validation, and specialist provenance
    checks still execute through the canonical delivery function.
    """
    if getattr(core, "_rework_attention_delivery_recovery_installed", False):
        return

    original_reconcile = core._reconcile_rework_lifecycle

    def _json_payload(raw):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _current_same_round_recovery(conn, task_id, context):
        latest_event = None
        latest_fn = getattr(core, "_latest_rework_event", None)
        if latest_fn is not None:
            try:
                latest_event = latest_fn(conn, task_id)
            except Exception:
                latest_event = None
        if latest_event is None and isinstance(context, dict):
            latest_event = context.get("event")
        if not isinstance(latest_event, tuple) or len(latest_event) != 3:
            return None
        payload, _event_at, kind = latest_event
        if not (
            isinstance(payload, dict)
            and kind == "github_pr_rework_retry"
            and payload.get("source") == "github_edge_rework_recovery"
            and isinstance(payload.get("rework_round"), int)
            and not isinstance(payload.get("rework_round"), bool)
            and int(payload["rework_round"]) > 0
        ):
            return None

        origin_fn = getattr(core, "_rework_round_origin_event", None)
        origin_event = (
            origin_fn(conn, task_id, latest_event)
            if origin_fn is not None
            else latest_event
        )
        if not isinstance(origin_event, tuple) or len(origin_event) != 3:
            return None
        origin_payload, origin_at, origin_kind = origin_event
        if not (
            isinstance(origin_payload, dict)
            and origin_kind == "github_pr_rework"
            and origin_payload.get("rework_round") == payload.get("rework_round")
            and origin_payload.get("pr_number") == payload.get("pr_number")
            and str(origin_payload.get("head_sha") or "").casefold()
            == str(payload.get("head_sha") or "").casefold()
        ):
            return None
        return latest_event, origin_event

    def _edge_projection_label_times(
        conn,
        task_id,
        latest_event,
        origin_event,
        timeline_items,
        parse_ts,
    ):
        latest_payload, _latest_at, _latest_kind = latest_event
        _origin_payload, origin_at, _origin_kind = origin_event
        target_round = latest_payload.get("rework_round")
        target_pr = latest_payload.get("pr_number")
        target_head = str(latest_payload.get("head_sha") or "").casefold()

        durable_times = []
        rows = conn.execute(
            "SELECT kind, payload, created_at FROM task_events "
            "WHERE task_id = ? AND kind IN "
            "('github_pr_rework_retry', 'github_pr_rework_attention') "
            "ORDER BY created_at ASC, id ASC",
            (task_id,),
        ).fetchall()
        for row in rows:
            payload = _json_payload(row["payload"])
            if payload.get("rework_round") != target_round:
                continue
            if payload.get("pr_number") != target_pr:
                continue
            row_head = str(payload.get("head_sha") or "").casefold()
            if target_head and row_head and row_head != target_head:
                continue
            kind = str(row["kind"] or "")
            if (
                kind == "github_pr_rework_retry"
                and payload.get("source") == "github_edge_rework_recovery"
            ) or (
                kind == "github_pr_rework_attention"
                and payload.get("source") == "github_edge_rework_reconciliation"
                and payload.get("reason") == "rework_human_attention"
            ):
                durable_times.append(int(row["created_at"] or 0))

        if not durable_times:
            return set()

        working_unlabeled_times = set()
        rework_labeled_times = []
        for item in timeline_items:
            if not isinstance(item, dict):
                continue
            event_name = str(item.get("event") or "")
            label = item.get("label")
            if not isinstance(label, dict):
                continue
            label_name = str(label.get("name") or "")
            ts = parse_ts(item.get("created_at"))
            if ts is None:
                continue
            ts = int(ts)
            if event_name == "unlabeled" and label_name == core.WORKING_LABEL:
                working_unlabeled_times.add(ts)
            elif event_name == "labeled" and label_name == core.REWORK_LABEL:
                rework_labeled_times.append(ts)

        edge_times = set()
        for label_at in rework_labeled_times:
            if label_at <= int(origin_at):
                continue
            if label_at not in working_unlabeled_times:
                continue
            if any(0 <= durable_at - label_at <= 30 for durable_at in durable_times):
                edge_times.add(label_at)
        return edge_times

    def _retry_without_edge_projection_labels(
        conn,
        client,
        ref,
        task_id,
        pr,
        context,
        pending_result,
    ):
        recovery = _current_same_round_recovery(conn, task_id, context)
        if recovery is None:
            return None
        latest_event, origin_event = recovery

        parse_ts = getattr(core, "_parse_iso_ts", None)
        if parse_ts is None:
            return None
        try:
            pr_number = int(context["pr_number"])
        except (KeyError, TypeError, ValueError):
            return None
        timeline_path = f"/repos/{ref.repository}/issues/{pr_number}/timeline"
        try:
            timeline_items = client.get_paginated(
                timeline_path,
                {"per_page": 100},
            )
        except core.GithubCompletionError:
            return None

        edge_label_times = _edge_projection_label_times(
            conn,
            task_id,
            latest_event,
            origin_event,
            timeline_items,
            parse_ts,
        )
        if not edge_label_times:
            return None

        filtered_items = []
        for item in timeline_items:
            hide = False
            if isinstance(item, dict) and item.get("event") == "labeled":
                label = item.get("label")
                if (
                    isinstance(label, dict)
                    and str(label.get("name") or "") == core.REWORK_LABEL
                ):
                    event_at = parse_ts(item.get("created_at"))
                    hide = event_at is not None and int(event_at) in edge_label_times
            if not hide:
                filtered_items.append(item)

        class _EdgeProjectionLabelFilteredClient:
            def __init__(self, base):
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

            def get_paginated(self, path, params=None, *, max_pages=10):
                if path == timeline_path:
                    return list(filtered_items)
                return self._base.get_paginated(
                    path, params, max_pages=max_pages
                )

        filtered_client = _EdgeProjectionLabelFilteredClient(client)
        try:
            delivered, reason, evidence = core._rework_delivery_evidence(
                conn,
                filtered_client,
                ref,
                task_id,
                pr,
                latest_event,
            )
        except core.GithubCompletionError:
            return None
        if not delivered:
            return delivered, reason, evidence

        recovered_evidence = dict(evidence)
        recovered_evidence.update({
            "edge_projection_labels_recovered": True,
            "filtered_rework_label_times": sorted(edge_label_times),
            "attention_at": int(pending_result.get("attention_at") or 0),
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
            retried = _retry_without_edge_projection_labels(
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


def _install_rework_context_contract(core: ModuleType) -> None:
    """Normalize the worker-facing rework handoff text without changing state.

    The canonical core still contains an older prose sentence telling a worker
    to hand back for review by blocking with ``review-required``. The current
    lifecycle instead requires ordinary Kanban completion with durable
    ``validation=passed`` + full ``head_sha`` metadata; the edge then creates
    and reads back the canonical completion marker before projecting
    ``agent-review-ready``. This wrapper changes only that rendered text.
    """
    if getattr(core, "_rework_context_contract_installed", False):
        return

    original_render = core._render_sync_context
    stale = (
        "Rework contract: this is a rework of the EXISTING PR above — "
        "update the SAME PR/branch (resolve the trusted review feedback); "
        "do NOT create a new PR; re-run the repository gates; then hand back "
        "for review (block with review-required)."
    )
    current = (
        "Rework contract: this is a rework of the EXISTING PR above — "
        "update the SAME PR/branch (resolve the trusted review feedback); "
        "do NOT create a new PR; re-run the repository gates; complete through "
        "the normal Kanban completion surface; record validation=passed and the "
        "full validated head_sha in run metadata. The edge owns the canonical "
        "completion marker and agent-review-ready projection."
    )

    def render_sync_context(*args, **kwargs):
        rendered = original_render(*args, **kwargs)
        if not isinstance(rendered, str):
            return rendered
        return rendered.replace(stale, current)

    core._render_sync_context = render_sync_context
    core._rework_context_contract_installed = True


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

    # Patch worker-facing prose before any overlay can capture the renderer.
    # This changes no lifecycle transition or evidence gate.
    _install_rework_context_contract(module)

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