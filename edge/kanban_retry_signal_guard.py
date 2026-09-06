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

import json
import re
from typing import Any, Mapping


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



def install_rework_delivery_provenance_guard(core: Any) -> Any:
    """Keep one rework round bound to its original edge claim across specialists.

    A GitHub edge rework claim may intentionally end BLOCKED after it creates a
    developer/reviewer dependency graph.  The root is then re-run by the core as
    those dependencies settle.  The canonical reader currently returns the
    first edge-provenanced BLOCKED bootstrap run before considering the later
    specialist-completed root run.  A recovery ``github_pr_rework_retry`` also
    carries the SAME ``rework_round`` but a newer timestamp, which can move the
    delivery time origin past the only edge-provenanced claim and produce the
    false ``delivery_run_missing`` seen on ctrlhangul/t_ac34f08d.

    This overlay is deliberately narrow:
      * an internal recovery retry keeps the immutable timestamp/payload of the
        original ``github_pr_rework`` event for delivery evidence only;
      * a later root run inherits delivery ownership only when a matching
        edge-dispatch bootstrap exists for that exact round, every direct parent
        is terminal, and the newest current-round reviewer has a completed PASS
        run bound to the requested full head SHA;
      * unchanged-head delivery is accepted only for that specialist-PASS shape.

    Ordinary timestamp-matching core runs never become delivery evidence.
    """
    if getattr(core, "_rework_delivery_provenance_guard_installed", False):
        return core

    original_task_run_after_rework = core._task_run_after_rework
    original_rework_delivery_evidence = core._rework_delivery_evidence

    def _json_payload(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    _HEAD_KEYS = frozenset({
        "head", "head_sha", "pr_head_sha", "commit", "sha", "oid",
    })

    def _run_metadata(run: Any) -> dict[str, Any]:
        if run is None or "metadata" not in run.keys():
            return {}
        return _json_payload(run["metadata"])

    def _metadata_head_candidates(run: Any) -> set[str]:
        """Return only full-SHA evidence carried by run metadata."""
        candidates: set[str] = set()

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key))
            elif isinstance(value, (list, tuple)):
                for child_value in value:
                    visit(child_value, key)
            elif isinstance(value, str) and key.casefold() in _HEAD_KEYS:
                candidates.update(re.findall(r"\b[0-9a-fA-F]{40}\b", value))

        visit(_run_metadata(run))
        return {item.casefold() for item in candidates}

    def _identity(payload: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(
            payload.get(key)
            for key in (
                "repository",
                "issue_number",
                "pr_number",
                "rework_round",
                "head_sha",
                "request_comment_id",
            )
        )

    def _round_origin_event(
        conn: Any,
        task_id: str,
        event: Any,
    ) -> Any:
        if not isinstance(event, tuple) or len(event) != 3:
            return event
        payload, _event_at, kind = event
        if not isinstance(payload, dict):
            return event
        if (
            kind != "github_pr_rework_retry"
            or payload.get("source") != "github_edge_rework_recovery"
        ):
            return event
        raw_round = payload.get("rework_round")
        if not isinstance(raw_round, int) or isinstance(raw_round, bool) or raw_round <= 0:
            return event

        rows = conn.execute(
            "SELECT payload, created_at, kind FROM task_events "
            "WHERE task_id = ? AND kind = 'github_pr_rework' "
            "ORDER BY created_at ASC, id ASC",
            (task_id,),
        ).fetchall()
        retry_identity = _identity(payload)
        for row in rows:
            origin_payload = _json_payload(row["payload"])
            if _identity(origin_payload) != retry_identity:
                continue
            return (
                origin_payload,
                int(row["created_at"] or 0),
                str(row["kind"]),
            )
        return event

    def _event_payload_at_round(
        conn: Any,
        task_id: str,
        rework_at: int,
        rework_round: Any,
    ) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND created_at = ? "
            "AND kind IN ('github_pr_rework', 'github_pr_rework_retry') "
            "ORDER BY id DESC",
            (task_id, int(rework_at)),
        ).fetchall()
        for row in rows:
            payload = _json_payload(row["payload"])
            if payload.get("rework_round") == rework_round:
                return payload
        return {}

    def _has_round_bootstrap(
        conn: Any,
        task_id: str,
        rework_at: int,
        rework_round: Any,
    ) -> bool:
        rows = conn.execute(
            "SELECT run_id, payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id ASC",
            (task_id, core.REWORK_DISPATCH_PROVENANCE_KIND),
        ).fetchall()
        for row in rows:
            payload = _json_payload(row["payload"])
            if payload.get("source") != core.REWORK_DISPATCH_PROVENANCE_SOURCE:
                continue
            if payload.get("task_id") != task_id:
                continue
            if payload.get("phase") not in {"claimed", "spawned"}:
                continue
            try:
                if int(str(payload.get("rework_event_at"))) != int(rework_at):
                    continue
                if int(str(payload.get("rework_round"))) != int(rework_round):
                    continue
            except (TypeError, ValueError):
                continue
            if row["run_id"] is None:
                continue
            return True
        return False

    def _direct_parent_rows(conn: Any, task_id: str) -> list[Any]:
        return conn.execute(
            "SELECT t.id, t.status, t.assignee, t.title, t.completed_at "
            "FROM task_links AS l JOIN tasks AS t ON t.id = l.parent_id "
            "WHERE l.child_id = ? ORDER BY t.completed_at ASC, t.id ASC",
            (task_id,),
        ).fetchall()

    def _ancestor_rows(conn: Any, task_id: str) -> list[Any]:
        """Return the append-only parent graph without revisiting cycles."""
        pending = [str(task_id)]
        visited = {str(task_id)}
        ancestors: list[Any] = []
        while pending:
            child_id = pending.pop()
            for row in _direct_parent_rows(conn, child_id):
                parent_id = str(row["id"])
                if parent_id in visited:
                    continue
                visited.add(parent_id)
                ancestors.append(row)
                pending.append(parent_id)
        return ancestors

    def _is_current_round_task(row: Any, rework_at: int) -> bool:
        completed_at = row["completed_at"]
        return (
            str(row["status"] or "") in {"done", "archived"}
            and completed_at is not None
            and int(completed_at) >= int(rework_at)
        )

    def _latest_current_round_reviewer_pass(
        conn: Any,
        task_id: str,
        rework_at: int,
    ) -> dict[str, Any] | None:
        """Select the newest direct reviewer and its current-round PASS run."""
        parents = _direct_parent_rows(conn, task_id)
        if not parents or any(
            str(row["status"] or "") not in {"done", "archived"}
            for row in parents
        ):
            return None
        reviewers = [
            row for row in parents
            if _is_current_round_task(row, rework_at)
            and "review" in f"{row['assignee'] or ''} {row['title'] or ''}".casefold()
        ]
        if not reviewers:
            return None
        reviewer = max(
            reviewers,
            key=lambda row: (int(row["completed_at"] or 0), str(row["id"])),
        )
        runs = conn.execute(
            "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC",
            (str(reviewer["id"]),),
        ).fetchall()
        for run in runs:
            if run["ended_at"] is None or run["started_at"] is None:
                continue
            if int(run["started_at"]) < int(rework_at):
                continue
            if int(run["ended_at"]) < int(rework_at):
                continue
            if str(run["outcome"] or "") not in {"completed", "done"}:
                continue
            summary = str(run["summary"] or "")
            if re.search(r"(?<![A-Z0-9_])PASS(?![A-Z0-9_])", summary.upper()) is None:
                continue
            heads = {
                head for head in core._rework_head_candidates(run)
                if re.fullmatch(r"[0-9a-f]{40}", head)
            }
            if not heads:
                continue
            return {
                "reviewer_task_id": str(reviewer["id"]),
                "reviewer_run_id": int(run["id"]),
                "reviewer_completed_at": int(reviewer["completed_at"]),
                "head_candidates": heads,
            }
        return None

    def _latest_current_round_developer_validation(
        conn: Any,
        reviewer_task_id: str,
        rework_at: int,
        reviewer_heads: set[str],
    ) -> dict[str, Any] | None:
        """Find a current-round developer attestation upstream of the reviewer."""
        developers = [
            row for row in _ancestor_rows(conn, reviewer_task_id)
            if _is_current_round_task(row, rework_at)
            and "developer" in f"{row['assignee'] or ''} {row['title'] or ''}".casefold()
        ]
        developers.sort(
            key=lambda row: (int(row["completed_at"] or 0), str(row["id"])),
            reverse=True,
        )
        for developer in developers:
            runs = conn.execute(
                "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC",
                (str(developer["id"]),),
            ).fetchall()
            for run in runs:
                if run["ended_at"] is None or run["started_at"] is None:
                    continue
                if int(run["started_at"]) < int(rework_at):
                    continue
                if int(run["ended_at"]) < int(rework_at):
                    continue
                if str(run["outcome"] or "") not in {"completed", "done"}:
                    continue
                metadata = _run_metadata(run)
                if metadata.get("validation") != "passed":
                    continue
                heads = _metadata_head_candidates(run)
                matching_heads = heads & reviewer_heads
                if not matching_heads:
                    continue
                return {
                    "run": run,
                    "developer_task_id": str(developer["id"]),
                    "developer_run_id": int(run["id"]),
                    "head_candidates": heads,
                    "matching_heads": matching_heads,
                }
        return None

    def _latest_clean_lead_run(
        conn: Any,
        task_id: str,
        rework_at: int,
        reviewer_completed_at: int,
    ) -> Any | None:
        """Require the root/Lead provisional run after the specialist graph."""
        runs = conn.execute(
            "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
            "FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY id DESC",
            (task_id, int(rework_at)),
        ).fetchall()
        for run in runs:
            if run["ended_at"] is None:
                continue
            if int(run["ended_at"]) < int(reviewer_completed_at):
                continue
            if str(run["outcome"] or "") in {"completed", "done"}:
                return run
        return None

    def _specialist_delivery_candidate(
        conn: Any,
        task_id: str,
        rework_at: int,
        rework_round: Any,
        requested_head: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        if re.fullmatch(r"[0-9a-f]{40}", requested_head) is None:
            return None
        if not _has_round_bootstrap(conn, task_id, rework_at, rework_round):
            return None
        reviewer = _latest_current_round_reviewer_pass(
            conn, task_id, rework_at
        )
        if reviewer is None:
            return None
        developer = _latest_current_round_developer_validation(
            conn,
            reviewer["reviewer_task_id"],
            rework_at,
            reviewer["head_candidates"],
        )
        if developer is None:
            return None
        matching_heads = set(developer["matching_heads"])
        if requested_head in matching_heads:
            selected_head = requested_head
        elif len(matching_heads) == 1:
            selected_head = next(iter(matching_heads))
        else:
            return None
        lead = _latest_clean_lead_run(
            conn,
            task_id,
            rework_at,
            reviewer["reviewer_completed_at"],
        )
        if lead is None:
            return None
        return developer["run"], {
            "developer_task_id": developer["developer_task_id"],
            "developer_run_id": developer["developer_run_id"],
            "lead_run_id": int(lead["id"]),
            "reviewer_task_id": reviewer["reviewer_task_id"],
            "reviewer_run_id": reviewer["reviewer_run_id"],
            "reviewer_completed_at": reviewer["reviewer_completed_at"],
            "head": selected_head,
        }

    def task_run_after_rework(
        conn: Any,
        task_id: str,
        rework_at: int,
        *,
        rework_round: Any = None,
    ) -> Any:
        direct = original_task_run_after_rework(
            conn,
            task_id,
            rework_at,
            rework_round=rework_round,
        )
        payload = _event_payload_at_round(conn, task_id, rework_at, rework_round)
        requested_head = str(payload.get("head_sha") or "").casefold()
        candidate = _specialist_delivery_candidate(
            conn,
            task_id,
            rework_at,
            rework_round,
            requested_head,
        )
        if candidate is not None:
            # The canonical reader may return a completed Main/root
            # provisional run.  Specialist evidence is authoritative once
            # the current-round graph has passed all of its gates.
            return candidate[0]
        if direct is None:
            return None
        return direct

    def rework_delivery_evidence(
        conn: Any,
        client: Any,
        ref: Any,
        task_id: str,
        pr: Any,
        event: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, str, dict[str, Any]]:
        origin_event = _round_origin_event(conn, task_id, event)
        delivered, reason, evidence = original_rework_delivery_evidence(
            conn,
            client,
            ref,
            task_id,
            pr,
            origin_event,
            *args,
            **kwargs,
        )
        if delivered or reason != "rework_head_unchanged":
            return delivered, reason, evidence
        if not isinstance(origin_event, tuple) or len(origin_event) != 3:
            return delivered, reason, evidence
        payload, rework_at, _kind = origin_event
        if not isinstance(payload, dict):
            return delivered, reason, evidence
        rework_round = payload.get("rework_round")
        requested_head = str(payload.get("head_sha") or "").casefold()
        candidate = _specialist_delivery_candidate(
            conn,
            task_id,
            int(rework_at),
            rework_round,
            requested_head,
        )
        if candidate is None:
            return delivered, reason, evidence
        run, specialist = candidate
        return True, "delivery_complete_verification_only", {
            "run_id": int(run["id"]),
            "run_outcome": str(run["outcome"] or ""),
            "head": specialist["head"],
            "requested_head": requested_head,
            "verification_only": True,
            "provenance": "specialist_reviewer_pass_same_head",
            **specialist,
        }

    core._task_run_after_rework = task_run_after_rework
    core._rework_delivery_evidence = rework_delivery_evidence
    core._rework_round_origin_event = _round_origin_event
    core._rework_specialist_delivery_candidate = _specialist_delivery_candidate
    core._rework_delivery_provenance_guard_installed = True
    return core
