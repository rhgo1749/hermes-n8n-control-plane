#!/usr/bin/env python3
"""Strict machine-readable guards and terminal fallbacks for Hermes edge sync.

The canonical edge state machine intentionally treats ``AGENT_REWORK_RETRY`` as
an explicit trusted-maintainer control message.  Edge-generated attention
comments also show that token as documentation.  When both are authored through
the same GitHub identity, a loose "contains marker + task line" parser can
consume its own help text as a fresh retry round.

This overlay narrows admission only: a retry comment is accepted iff its entire
non-empty body is exactly these two lines (surrounding whitespace is ignored):

    AGENT_REWORK_RETRY
    task=<task_id>

It also supplies one narrowly-scoped terminal fallback for GitHub-backed cards
parked in REVIEW with ``no_linked_pr``.  A fresh source-Issue read may complete
that card only when GitHub says the Issue is ``closed`` with
``state_reason=completed`` and the ``closed_by`` actor is already trusted by the
canonical edge policy.  Bare cross-reference mentions remain non-authoritative.

All existing trust, time-bound, relationship, and one-shot guards are preserved.
"""
from __future__ import annotations

from typing import Any


def install_retry_signal_guard(core: Any) -> Any:
    """Install strict whole-comment parsing for explicit rework retries."""
    if getattr(core, "_retry_signal_guard_installed", False):
        return core

    def find_rework_retry_signal(
        client: Any,
        ref: Any,
        pr_number: int,
        task_id: str,
        *,
        baseline_at: int,
        consumed_ids: set[int],
    ) -> dict[str, Any] | None:
        comments = client.get_paginated(
            f"/repos/{ref.repository}/issues/{pr_number}/comments",
            {"per_page": 100},
        )
        expected_lines = [core.REWORK_RETRY_MARKER, f"task={task_id}"]

        for comment in reversed(comments):
            if not isinstance(comment, dict):
                continue
            author = str((comment.get("user") or {}).get("login") or "")
            if author not in core.TRUSTED_GITHUB_ACTORS:
                continue
            created_at = core._parse_iso_ts(comment.get("created_at"))
            if created_at is None or created_at <= baseline_at:
                continue
            comment_id = comment.get("id")
            if not isinstance(comment_id, int) or comment_id in consumed_ids:
                continue

            # Whole-comment contract.  In particular, an edge-owned attention
            # comment that merely DOCUMENTS the retry template must never be
            # reinterpreted as the maintainer issuing that control message.
            lines = [
                line.strip()
                for line in str(comment.get("body") or "").splitlines()
                if line.strip()
            ]
            if lines != expected_lines:
                continue

            return {
                "comment_id": comment_id,
                "author": author,
                "created_at": created_at,
            }
        return None

    core._find_rework_retry_signal = find_rework_retry_signal
    core._retry_signal_guard_installed = True
    return core



def install_supersede_signal_guard(core: Any) -> Any:
    """Install strict whole-comment parsing for PR supersede signals."""
    if getattr(core, "_supersede_signal_guard_installed", False):
        return core

    def find_pr_supersede_signal(
        client: Any,
        ref: Any,
        pr_number: int,
        task_id: str,
        *,
        baseline_at: int,
        consumed_ids: set[int],
    ) -> dict[str, Any] | None:
        comments = client.get_paginated(
            f"/repos/{ref.repository}/issues/{pr_number}/comments",
            {"per_page": 100},
        )
        expected_lines = [
            core.SUPERSEDE_MARKER,
            f"pr={pr_number} task={task_id}",
        ]
        for comment in reversed(comments):
            if not isinstance(comment, dict):
                continue
            author = str((comment.get("user") or {}).get("login") or "")
            if author not in core.TRUSTED_GITHUB_ACTORS:
                continue
            created_at = core._parse_iso_ts(comment.get("created_at"))
            if created_at is None or created_at < baseline_at:
                continue
            comment_id = comment.get("id")
            if (
                not isinstance(comment_id, int)
                or isinstance(comment_id, bool)
                or comment_id <= 0
                or comment_id in consumed_ids
            ):
                continue
            lines = [
                line.strip()
                for line in str(comment.get("body") or "").splitlines()
                if line.strip()
            ]
            if lines != expected_lines:
                continue
            return {
                "comment_id": comment_id,
                "author": author,
                "created_at": created_at,
                "pr_number": pr_number,
            }
        return None

    core._find_pr_supersede_signal = find_pr_supersede_signal
    core._supersede_signal_guard_installed = True
    return core



def install_closed_completed_terminal_fallback(core: Any) -> Any:
    """Converge only trusted ``closed/completed`` no-linked-pr cards to DONE.

    The canonical verifier remains the first and primary authority.  This
    wrapper runs only for the exact authoritative parking result
    ``review/no_linked_pr``.  It deliberately does not reinterpret timeline
    cross-references or PR body mentions as closing relationships.
    """
    if getattr(core, "_closed_completed_terminal_fallback_installed", False):
        return core

    original_verify_completion = core.verify_completion

    def verify_completion(
        client: Any,
        ref: Any,
        text_sources: Any = (),
    ) -> Any:
        decision = original_verify_completion(client, ref, text_sources)
        if not (
            decision.authoritative
            and decision.desired_status == "review"
            and decision.reason == "no_linked_pr"
        ):
            return decision

        try:
            issue_payload, _ = client.get(
                f"/repos/{ref.repository}/issues/{ref.issue_number}"
            )
            if not isinstance(issue_payload, dict):
                raise core.GithubCompletionError(
                    f"GitHub returned an invalid Issue response for #{ref.issue_number}"
                )

            issue_state = str(issue_payload.get("state", "")).casefold()
            if issue_state not in {"open", "closed"}:
                raise core.GithubCompletionError(
                    f"GitHub returned incomplete Issue data for #{ref.issue_number}"
                )

            state_reason = str(issue_payload.get("state_reason") or "").casefold()
            closed_by = issue_payload.get("closed_by")
            closed_by_login = (
                str(closed_by.get("login") or "")
                if isinstance(closed_by, dict)
                else ""
            )

            if (
                issue_state == "closed"
                and state_reason == "completed"
                and closed_by_login in core.TRUSTED_GITHUB_ACTORS
            ):
                return core.GithubCompletionDecision(
                    desired_status="done",
                    reason="trusted_issue_completed",
                    linked_pr_numbers=decision.linked_pr_numbers,
                    pull_requests=decision.pull_requests,
                )
            return decision
        except core.GithubCompletionError as exc:
            return core.GithubCompletionDecision(
                desired_status=None,
                reason="github_query_failed",
                linked_pr_numbers=decision.linked_pr_numbers,
                pull_requests=decision.pull_requests,
                error=str(exc),
            )

    core.verify_completion = verify_completion
    core._closed_completed_terminal_fallback_installed = True
    return core
