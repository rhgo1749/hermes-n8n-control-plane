#!/usr/bin/env python3
"""Reason-aware PR feedback for rework head-binding delivery rejection.

This is a narrow edge overlay.  It deliberately leaves the canonical rework
state machine in ``kanban-github-sync.py`` unchanged and only augments:

* evidence for ``run_head_mismatch`` with the round-requested head;
* the existing idempotent PR attention comment with reason-specific recovery
  guidance; and
* retryable head-binding failures with the same PR feedback before/after the
  existing retry routing completes.

PR #36's trusted-maintainer verification-only same-head acceptance remains
owned exclusively by the canonical ``_rework_delivery_evidence`` function.
The overlay never pre-classifies same-head as an error and never changes task,
retry, label, failure-limit, or delivery transitions.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping, Optional

HEAD_BINDING_REJECT_REASONS = frozenset({
    "rework_head_unchanged",
    "run_head_mismatch",
})


def install_head_binding_feedback(core: Any) -> Any:
    """Install the feedback-only overlay onto a loaded edge-sync module."""
    if getattr(core, "_head_binding_feedback_installed", False):
        return core

    original_delivery_evidence = core._rework_delivery_evidence
    original_reconcile = core._reconcile_rework_lifecycle

    def delivery_evidence(
        conn: Any,
        client: Any,
        ref: Any,
        task_id: str,
        pr: Any,
        event: tuple[dict[str, Any], int, str],
    ) -> tuple[bool, str, dict[str, Any]]:
        delivered, reason, evidence = original_delivery_evidence(
            conn, client, ref, task_id, pr, event
        )
        # Evidence-only enrichment.  Acceptance/rejection remains entirely
        # authoritative in the canonical PR #36 validator above.
        if reason == "run_head_mismatch":
            payload = event[0] if event and isinstance(event[0], dict) else {}
            evidence = dict(evidence or {})
            requested_head = str(payload.get("head_sha") or "").casefold()
            if requested_head:
                evidence["requested_head"] = requested_head
        return delivered, reason, evidence

    def post_attention_pr_comment(
        client: Any,
        ref: Any,
        pr_number: int,
        task_id: str,
        reason: str,
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Post one idempotent reason-aware attention comment on the PR."""
        existing = client.get_paginated(
            f"/repos/{ref.repository}/issues/{pr_number}/comments",
            {"per_page": 100},
        )
        needle = f"{core.REWORK_ATTENTION_MARKER} task={task_id} reason={reason}"
        for item in existing:
            if isinstance(item, dict) and needle in str(item.get("body") or ""):
                return False

        ev = dict(evidence or {})
        requested_head = str(ev.get("requested_head") or "").casefold()
        lines = [
            needle,
            "",
            "This rework round's delivery could not be accepted automatically. "
            "This feedback does not change the existing Kanban retry/state/label "
            "routing.",
        ]

        missing = ev.get("missing_fields")
        if isinstance(missing, list) and missing:
            lines += [
                "",
                "The newest completion comment contains AGENT_REWORK_COMPLETE but "
                "failed the strict machine-readable contract. Missing/invalid "
                "fields: " + ", ".join(str(value) for value in missing) + ".",
            ]

        if reason == "rework_head_unchanged":
            lines += [
                "",
                "Head-binding rejection: this ordinary rework round did not "
                "advance the PR head beyond the round-requested head.",
            ]
            if requested_head:
                lines.append(f"Round-requested head: {requested_head}")
            lines += [
                "Create and push a bounded worker-owned commit so the live PR "
                "head advances, validate that new live head, then re-post "
                "AGENT_REWORK_COMPLETE with the new full 40-char SHA.",
            ]
        elif reason == "run_head_mismatch":
            lines += [
                "",
                "Head-attestation rejection: the completion marker/live PR head "
                "could not be bound to the current round's finished worker run.",
            ]
            if requested_head:
                lines.append(f"Round-requested head: {requested_head}")
            lines += [
                "The marker head must equal the live PR head, and the current "
                "round's finished worker run summary/metadata must attest that "
                "same full SHA.",
                "A new source commit is not inherently required. If the live "
                "head already contains the requested fix, a trusted maintainer "
                "may open a fresh AGENT_REWORK_RETRY verification-only round and "
                "the worker may re-validate/attest that same head.",
            ]

        lines += [
            "",
            "Re-post the completion handoff on this PR with a comment whose "
            "first line is exactly AGENT_REWORK_COMPLETE, followed by:",
            "",
            core.REWORK_COMPLETE_MARKER,
            f"task={task_id}",
            "request_comment=<github_comment_id | none>",
            "head=<full 40-char PR head SHA>",
            "validation=passed",
            "",
            "To explicitly open a NEW rework round instead, a trusted "
            "maintainer posts:",
            "",
            core.REWORK_RETRY_MARKER,
            f"task={task_id}",
            "",
            "Each signal is consumed exactly once.",
        ]
        status, _ = client.post(
            f"/repos/{ref.repository}/issues/{pr_number}/comments",
            {"body": "\n".join(lines)},
        )
        if not 200 <= status < 300:
            raise core.GithubCompletionError(
                f"could not post rework attention comment (HTTP {status})"
            )
        return True

    def reconcile_rework_lifecycle(
        conn: Any,
        kanban_db: Any,
        client: Any,
        ref: Any,
        decision: Any,
        row: Mapping[str, Any],
        context: Optional[Mapping[str, Any]],
        *,
        dry_run: bool,
        failure_limit: Optional[int],
    ) -> Optional[dict[str, Any]]:
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
        if dry_run or not isinstance(result, dict) or context is None:
            return result

        retry_reason = str(result.get("retry_reason") or "")
        if retry_reason not in HEAD_BINDING_REJECT_REASONS:
            return result

        # Retryable head-binding failures were historically silent on the PR.
        # Re-evaluate only to reconstruct the already-decided diagnostic
        # evidence; then post observational feedback.  The result returned by
        # the canonical state machine is preserved byte-for-behavior.
        try:
            task_id = str(row["id"])
            delivered, reason, evidence = core._rework_delivery_evidence(
                conn,
                client,
                ref,
                task_id,
                context["pr"],
                context["event"],
            )
            if not delivered and reason == retry_reason:
                core._post_rework_attention_pr_comment(
                    client,
                    ref,
                    int(context["pr_number"]),
                    task_id,
                    reason=reason,
                    evidence=evidence,
                )
        except core.GithubCompletionError as exc:
            print(
                "kanban-github-sync: head-binding PR feedback failed for "
                f"{ref.repository} task={row['id']}: {exc}",
                file=sys.stderr,
            )
        return result

    core.HEAD_BINDING_REJECT_REASONS = HEAD_BINDING_REJECT_REASONS
    core._rework_delivery_evidence = delivery_evidence
    core._post_rework_attention_pr_comment = post_attention_pr_comment
    core._reconcile_rework_lifecycle = reconcile_rework_lifecycle
    core._head_binding_feedback_installed = True
    return core
