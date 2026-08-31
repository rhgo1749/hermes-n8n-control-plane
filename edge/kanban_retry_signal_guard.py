#!/usr/bin/env python3
"""Strict machine-readable guard for explicit Hermes rework retry signals.

The canonical edge state machine intentionally treats ``AGENT_REWORK_RETRY`` as
an explicit trusted-maintainer control message.  Edge-generated attention
comments also show that token as documentation.  When both are authored through
the same GitHub identity, a loose "contains marker + task line" parser can
consume its own help text as a fresh retry round.

This overlay narrows admission only: a retry comment is accepted iff its entire
non-empty body is exactly these two lines (surrounding whitespace is ignored):

    AGENT_REWORK_RETRY
    task=<task_id>

All existing trust, time-bound, and one-shot guards are preserved.
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
