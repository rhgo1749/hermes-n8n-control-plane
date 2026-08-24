#!/usr/bin/env python3
"""GitHub-backed Kanban reconciliation — edge integration (non-core).

Reconciles GitHub Issue intake cards against the authoritative GitHub PR
state, outside the Hermes core.  The core ``complete_task`` keeps its
upstream semantics (worker completion == DONE); this script is the only
writer that may move GitHub-backed cards between ``review`` and ``done``
based on a fresh GitHub re-query.

State contract (per operator decision):
  * DONE  + any effective required PR OPEN (or unresolved closed-not-merged) -> REVIEW
  * REVIEW + every effective required PR MERGED into target branch -> DONE
    (a closed Issue may ignore an older closed-unmerged PR only when a newer
    linked PR with the same head ref merged into target and no PR remains open)
  * REVIEW + one-shot agent-rework request (single open PR carrying a
    trusted ``agent-rework`` label, source Issue open + ``agent-ready``)
    -> READY: the sync context block is refreshed into the task body and
    one ``github_pr_rework`` event is recorded.  The rework label remains
    until the edge dispatcher successfully claims the Kanban task; only
    then is it atomically replaced by ``agent-working``.  A claim or label
    mutation failure therefore leaves the request intake-visible.
  * REVIEW + current-round ``rework_human_attention`` hold recovered by an
    operator (stale ``agent-rework`` label) + exact trusted
    ``AGENT_REWORK_RETRY`` comment -> READY through the same transaction and
    dispatch lane; without that comment the card stays REVIEW and no round
    is opened.
  * GitHub lookup failure / ambiguous decision            -> keep state
  * blocked/triage/todo/scheduled/running/archived/ready  -> never
    overwritten (REVIEW->READY is the only sync write into ``ready``;
    an existing ``ready`` is preserved — only stale label removal is
    retried)
  * BLOCKED reconciliation (no invisible blocked work — GitHub must
    stay the durable review surface):
    - BLOCKED + every required PR merged into target      -> DONE
    - BLOCKED + exactly one open PR with trusted
      ``agent-rework`` (Issue open + ``agent-ready``)     -> READY
      (direct, single tick; rework event; label removed last)
    - BLOCKED + open non-draft PR (no rework request)     -> REVIEW
    - BLOCKED + only Draft / closed-unmerged / no PR      -> stays
      BLOCKED; durable GitHub evidence is enforced: Issue gets the
      ``agent-blocked`` label and ONE sync-owned marker comment
      (``<!-- HERMES KANBAN BLOCKER task_id=... -->``), created once
      and patched in place when the reason changes (never appended)
    - BLOCKED + no PR + ``agent-blocked`` absent + trusted
      maintainer reply after the projection               -> READY
      (``github_blocked_resolved`` exactly once)
    - BLOCKED + Issue closed                          -> stays BLOCKED
      with NO projection: a closed Issue never gets the ``agent-blocked``
      label, a blocker marker comment, or a blocked-resume
      (``closed_issue_no_projection``); the merged-PR completion
      precedence still applies

Safety:
  * Schema compatibility is verified before any write; on mismatch the
    script fails without touching the DB (survives Hermes updates).
  * Every transition uses an optimistic ``UPDATE ... WHERE status=?`` and
    aborts if another actor changed the row first.
  * No ``complete_task``/hooks are invoked, so no duplicate completion
    side effects are re-fired.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_HERMES_HOME = "/home/hermes/.hermes"
GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PR_URL_RE_TEMPLATE = r"https?://github\.com/{repository}/pull/(\d+)"
_PROVENANCE_END = "## Canonical Issue body"

# Columns the sync may write. Verified against PRAGMA table_info before use.
_REQUIRED_TASK_COLUMNS = (
    "id", "body", "status", "assignee", "completed_at",
    "claim_lock", "claim_expires", "worker_pid", "block_kind", "block_recurrences",
    "workspace_path", "branch_name", "skills",
)
_EVENT_COLUMNS = ("task_id", "run_id", "kind", "payload", "created_at")

# States reconciliation never overwrites, whatever GitHub says.
_PRESERVED_STATES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "archived"}
)

# A GitHub-backed intake root is not eligible for external completion
# projection while any internal parent is still in flight. ``task_links``
# stores parent -> child edges; the gate therefore reads parents through
# ``WHERE child_id = ?`` and treats only terminal parent rows as satisfied.
_DEPENDENCY_TERMINAL_STATES = frozenset({"done", "archived"})

# Machine-readable prefix for the DONE -> REVIEW parking marker comment.
# ``review`` on a GitHub-backed card means "parked awaiting merge" — no human
# or worker action is required until fresh GitHub evidence resolves it. The
# full format is documented in docs/GITHUB_COMPLETION_LIFECYCLE.md and
# asserted by edge/test-kanban-github-sync-completion.py.
_PARKING_COMMENT_PREFIX = "[parked:"

# ---------------------------------------------------------------------------
# agent-rework loop (one-shot rework requests driven by PR labels)
# ---------------------------------------------------------------------------
# Trusted GitHub operators whose review/comment text is treated as work
# instructions (everything else is reference-only, never an instruction).
TRUSTED_GITHUB_ACTORS = {"rhgo1749"}
AGENT_READY_LABEL = "agent-ready"
REWORK_LABEL = "agent-rework"
WORKING_LABEL = "agent-working"
REVIEW_READY_LABEL = "agent-review-ready"
REWORK_COMPLETE_MARKER = "AGENT_REWORK_COMPLETE"
REWORK_ATTENTION_MARKER = "HERMES_KANBAN_REWORK_ATTENTION"
# Machine-readable explicit maintainer retry: a TRUSTED_GITHUB_ACTORS
# comment on the current PR containing exactly ``AGENT_REWORK_RETRY`` plus a
# ``task=<task_id>`` line, posted AFTER the last rework attention record.
# It is the ONLY human signal that may start a new rework round from a
# BLOCKED or operator-recovered REVIEW + rework_human_attention hold (label
# presence alone is never retry evidence — the edge restores ``agent-rework``
# during self-heal).
REWORK_RETRY_MARKER = "AGENT_REWORK_RETRY"

# Sync-owned body region.  Between these markers the whole block is
# REPLACED on every refresh — never appended — so the body cannot grow.
SYNC_CONTEXT_BEGIN = "<!-- BEGIN GITHUB SYNC CONTEXT -->"
SYNC_CONTEXT_END = "<!-- END GITHUB SYNC CONTEXT -->"

# Bounded context collection: truncated PR body, capped feedback items.
MAX_PR_BODY_CHARS = 4000
MAX_ITEM_CHARS = 1500
MAX_TRUSTED_ITEMS = 10
MAX_UNTRUSTED_ITEMS = 5

# ---------------------------------------------------------------------------
# BLOCKED visibility (no invisible blocked work)
# ---------------------------------------------------------------------------
BLOCKED_LABEL = "agent-blocked"
BLOCKER_MARKER_PREFIX = "<!-- HERMES KANBAN BLOCKER task_id="
BLOCKER_MARKER_SUFFIX = " -->"
MAX_BLOCKER_REASON_CHARS = 1500
MAX_ISSUE_RESPONSE_ITEMS = 3

_NEEDS_FROM_MAINTAINER: dict[Optional[str], str] = {
    None: "A maintainer decision is required.",
    "needs_input": "A decision or input from the maintainer (reply on this Issue).",
    "capability": "The required capability or environment access must be provided by the maintainer.",
    "transient": "Confirm whether the blocking condition has cleared, then unblock.",
}


class SyncError(RuntimeError):
    """A safe failure: nothing was written, or a transition was refused."""


class GithubCompletionError(RuntimeError):
    """A source-of-truth lookup could not be completed safely.

    ``status`` carries the HTTP status code when the failure came from a
    GitHub API HTTP error response; it is ``None`` for transport, decode,
    and locally raised failures.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GithubTaskRef:
    repository: str
    issue_number: int
    target_branch: str = "main"
    issue_title: str = ""


@dataclass(frozen=True)
class GithubPullRequest:
    number: int
    state: str
    merged: bool
    base_branch: str
    html_url: str = ""
    title: str = ""
    head_sha: str = ""
    head_ref: str = ""
    author: str = ""
    body: str = ""
    draft: bool = False

    @property
    def is_merged_into_target(self) -> bool:
        return self.merged and self.state == "closed"


@dataclass(frozen=True)
class GithubCompletionDecision:
    desired_status: Optional[str]
    reason: str
    linked_pr_numbers: tuple[int, ...] = ()
    pull_requests: tuple[GithubPullRequest, ...] = ()
    error: Optional[str] = None

    @property
    def authoritative(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "desired_status": self.desired_status,
            "reason": self.reason,
            "linked_pr_numbers": list(self.linked_pr_numbers),
            "pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "merged": pr.merged,
                    "base_branch": pr.base_branch,
                    "html_url": pr.html_url,
                    "title": pr.title,
                }
                for pr in self.pull_requests
            ],
            "error": self.error,
            "authoritative": self.authoritative,
        }


class GithubApiClient:
    """Small read-only GitHub REST client (token from environment)."""

    def __init__(self, token: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        token = str(token or "").strip()
        if not token:
            raise GithubCompletionError("GITHUB_TOKEN/GH_TOKEN is unavailable")
        self._token = token
        self._timeout = int(timeout)

    @classmethod
    def from_environment(cls) -> "GithubApiClient":
        return cls(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> tuple[Any, dict[str, str]]:
        query = urlencode(dict(params or {}))
        url = f"{GITHUB_API}{path}"
        if query:
            url += f"?{query}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-kanban-github-edge-sync",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                return payload, headers
        except HTTPError as exc:
            raise GithubCompletionError(
                f"GitHub API {exc.code} for {path}", status=int(exc.code)
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GithubCompletionError(
                f"GitHub API request failed for {path}: {type(exc).__name__}"
            ) from exc

    def get_paginated(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        max_pages: int = 10,
    ) -> list[Any]:
        base_params = dict(params or {})
        page = 1
        items: list[Any] = []
        while page <= max_pages:
            page_params = dict(base_params)
            page_params["page"] = page
            payload, headers = self.get(path, page_params)
            if not isinstance(payload, list):
                raise GithubCompletionError(f"GitHub returned a non-list response for {path}")
            items.extend(payload)
            link = headers.get("link", "")
            if 'rel="next"' not in link:
                return items
            page += 1
        raise GithubCompletionError(f"GitHub pagination exceeded {max_pages} pages for {path}")

    def delete(self, path: str) -> int:
        """DELETE with 404 treated as success (resource already gone)."""
        request = Request(
            f"{GITHUB_API}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-kanban-github-edge-sync",
            },
            method="DELETE",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return int(response.status)
        except HTTPError as exc:
            if exc.code == 404:
                return 404
            raise GithubCompletionError(f"GitHub API {exc.code} for DELETE {path}") from exc
        except (URLError, TimeoutError) as exc:
            raise GithubCompletionError(
                f"GitHub API request failed for DELETE {path}: {type(exc).__name__}"
            ) from exc

    def _write_json(self, method: str, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        """POST/PATCH returning ``(status, body)``; network errors raise."""
        request = Request(
            f"{GITHUB_API}{path}",
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-kanban-github-edge-sync",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
                try:
                    return int(response.status), json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return int(response.status), None
        except HTTPError as exc:
            return int(exc.code), None
        except (URLError, TimeoutError) as exc:
            raise GithubCompletionError(
                f"GitHub API request failed for {method} {path}: {type(exc).__name__}"
            ) from exc

    def post(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        return self._write_json("POST", path, payload)

    def patch(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        return self._write_json("PATCH", path, payload)


def parse_task_ref(body: Optional[str]) -> Optional[GithubTaskRef]:
    """Parse only the importer-owned provenance header from a task body.

    The canonical Issue body is untrusted input and can contain text that
    looks like provenance; it is excluded before parsing.  Legacy intake
    cards without ``completion contract: github-pr`` are accepted when their
    provenance contains ``source: github-issue``.
    """
    text = str(body or "")
    provenance = text.split(_PROVENANCE_END, 1)[0]
    values: dict[str, str] = {}
    for line in provenance.splitlines():
        match = re.match(
            r"^\s*-\s*(source|repository|issue number|issue title|target branch|target_branch|completion contract)\s*:\s*(.*?)\s*$",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            values[match.group(1).casefold().replace("_", " ")] = match.group(2).strip()

    source = values.get("source", "").casefold()
    contract = values.get("completion contract", "").casefold()
    if source != "github-issue" and contract != "github-pr":
        return None
    repository = values.get("repository", "").strip()
    if not _REPOSITORY_RE.fullmatch(repository):
        return None
    try:
        issue_number = int(values.get("issue number", ""))
    except (TypeError, ValueError):
        return None
    if issue_number < 1:
        return None
    target_branch = values.get("target branch", "main").strip() or "main"
    if any(ch.isspace() for ch in target_branch):
        return None
    issue_title = values.get("issue title", "").strip()
    return GithubTaskRef(repository, issue_number, target_branch, issue_title)


def is_github_backed_body(body: Optional[str]) -> bool:
    return parse_task_ref(body) is not None


def _pull_url_pattern(repository: str) -> re.Pattern[str]:
    return re.compile(
        _PR_URL_RE_TEMPLATE.format(repository=re.escape(repository)),
        flags=re.IGNORECASE,
    )


def _timeline_pr_numbers(items: Iterable[Any], repository: str) -> set[int]:
    numbers: set[int] = set()
    pattern = _pull_url_pattern(repository)
    for item in items:
        if not isinstance(item, dict) or item.get("event") != "cross-referenced":
            continue
        source = item.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(issue, dict) or not issue.get("pull_request"):
            continue
        repo_data = issue.get("repository")
        full_name = repo_data.get("full_name") if isinstance(repo_data, dict) else None
        if full_name and str(full_name).casefold() != repository.casefold():
            continue
        number = issue.get("number")
        if isinstance(number, int) and number > 0:
            numbers.add(number)
            continue
        match = pattern.search(str(issue.get("html_url", "")))
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def discover_linked_pr_numbers(
    client: Any,
    ref: GithubTaskRef,
    text_sources: Iterable[str] = (),
) -> tuple[int, ...]:
    """Discover PR identifiers (Issue timeline + handoff text), let GitHub
    verify every identifier."""
    timeline = client.get_paginated(
        f"/repos/{ref.repository}/issues/{ref.issue_number}/timeline",
        {"per_page": 100},
    )
    numbers = _timeline_pr_numbers(timeline, ref.repository)
    pattern = _pull_url_pattern(ref.repository)
    for source in text_sources:
        for match in pattern.finditer(str(source or "")):
            numbers.add(int(match.group(1)))
    return tuple(sorted(numbers))


def _parse_pull_request(number: int, payload: Any, ref: GithubTaskRef) -> GithubPullRequest:
    if not isinstance(payload, dict):
        raise GithubCompletionError(f"GitHub returned an invalid PR response for #{number}")
    state = str(payload.get("state", "")).casefold()
    base = payload.get("base")
    base_branch = str(base.get("ref", "")) if isinstance(base, dict) else ""
    merged_raw = payload.get("merged")
    head = payload.get("head")
    head_sha = str(head.get("sha", "")) if isinstance(head, dict) else ""
    draft_raw = payload.get("draft")
    if (
        state not in {"open", "closed"}
        or not isinstance(merged_raw, bool)
        or not isinstance(draft_raw, bool)
        or not base_branch
        or not head_sha
    ):
        raise GithubCompletionError(f"GitHub returned incomplete PR data for #{number}")
    merged = bool(merged_raw and base_branch == ref.target_branch)
    user = payload.get("user")
    return GithubPullRequest(
        number=number,
        state=state,
        merged=merged,
        base_branch=base_branch,
        html_url=str(payload.get("html_url", "")),
        title=str(payload.get("title", "")),
        head_sha=head_sha,
        head_ref=str(head.get("ref", "")) if isinstance(head, dict) else "",
        author=str(user.get("login", "")) if isinstance(user, dict) else "",
        body=str(payload.get("body") or ""),
        draft=bool(draft_raw),
    )


def _is_merged_into_target(
    ref: GithubTaskRef,
    pr: GithubPullRequest,
) -> bool:
    return (
        pr.is_merged_into_target
        and pr.base_branch == ref.target_branch
    )


def _superseded_closed_pr_numbers(
    ref: GithubTaskRef,
    pull_requests: Iterable[GithubPullRequest],
) -> frozenset[int]:
    """Return closed historical PRs proven superseded by a newer same-head merge.

    A historical PR is ignored only when:
      * it is closed but is not itself merged into the target branch;
      * its head ref is non-empty; and
      * a numerically newer linked PR with exactly the same head ref is
        merged into the target branch.

    This deliberately does not infer supersession across unrelated branches.
    """
    prs = tuple(pull_requests)
    merged_target_prs = tuple(
        pr for pr in prs
        if _is_merged_into_target(ref, pr)
    )

    return frozenset(
        pr.number
        for pr in prs
        if (
            pr.state == "closed"
            and not _is_merged_into_target(ref, pr)
            and bool(pr.head_ref)
            and any(
                replacement.number > pr.number
                and bool(replacement.head_ref)
                and replacement.head_ref == pr.head_ref
                for replacement in merged_target_prs
            )
        )
    )


def evaluate_completion(
    ref: GithubTaskRef,
    pull_requests: Iterable[GithubPullRequest],
    *,
    linked_pr_numbers: Optional[Iterable[int]] = None,
    issue_state: Optional[str] = None,
) -> GithubCompletionDecision:
    prs = tuple(sorted(pull_requests, key=lambda item: item.number))
    numbers = tuple(
        sorted(
            set(int(number) for number in (linked_pr_numbers or ()))
            | {pr.number for pr in prs}
        )
    )

    if not prs:
        return GithubCompletionDecision(
            desired_status="review",
            reason="no_linked_pr",
            linked_pr_numbers=numbers,
            pull_requests=prs,
        )

    if all(_is_merged_into_target(ref, pr) for pr in prs):
        return GithubCompletionDecision(
            desired_status="done",
            reason="all_linked_prs_merged",
            linked_pr_numbers=numbers,
            pull_requests=prs,
        )

    has_open_pr = any(pr.state == "open" for pr in prs)

    # A closed Issue may have historical PRs that were intentionally replaced
    # after main advanced. Do not let those stale PRs revive an already
    # completed card forever, but only accept supersession when the lineage is
    # unambiguous: no open linked PR remains, and every closed-unmerged PR has
    # a newer linked merge from exactly the same head branch.
    if str(issue_state or "").casefold() == "closed" and not has_open_pr:
        unresolved_closed = tuple(
            pr for pr in prs
            if (
                pr.state == "closed"
                and not _is_merged_into_target(ref, pr)
            )
        )
        superseded = _superseded_closed_pr_numbers(ref, prs)

        if (
            unresolved_closed
            and superseded
            and superseded == {pr.number for pr in unresolved_closed}
        ):
            effective_prs = tuple(
                pr for pr in prs
                if pr.number not in superseded
            )
            if (
                effective_prs
                and all(
                    _is_merged_into_target(ref, pr)
                    for pr in effective_prs
                )
            ):
                return GithubCompletionDecision(
                    desired_status="done",
                    reason="superseded_pr_merged",
                    linked_pr_numbers=numbers,
                    pull_requests=prs,
                )

    if has_open_pr:
        reason = "linked_pr_open"
    elif any(
        pr.state == "closed"
        and not _is_merged_into_target(ref, pr)
        for pr in prs
    ):
        reason = "linked_pr_closed_not_merged"
    else:
        reason = "linked_pr_not_merged"

    return GithubCompletionDecision(
        desired_status="review",
        reason=reason,
        linked_pr_numbers=numbers,
        pull_requests=prs,
    )


def verify_completion(
    client: Any,
    ref: GithubTaskRef,
    text_sources: Iterable[str] = (),
) -> GithubCompletionDecision:
    """Re-query linked PRs and use source-Issue state only for supersession.

    Ordinary completion checks keep the existing GitHub request shape.  The
    source Issue is fetched only when the PR set is a fully-provable
    same-head supersession candidate.
    """
    try:
        numbers = discover_linked_pr_numbers(client, ref, text_sources)
        pull_requests = tuple(
            _parse_pull_request(
                number,
                client.get(f"/repos/{ref.repository}/pulls/{number}")[0],
                ref,
            )
            for number in numbers
        )

        provisional = evaluate_completion(
            ref,
            pull_requests,
            linked_pr_numbers=numbers,
        )

        if provisional.reason != "linked_pr_closed_not_merged":
            return provisional

        unresolved_closed = {
            pr.number
            for pr in pull_requests
            if (
                pr.state == "closed"
                and not _is_merged_into_target(ref, pr)
            )
        }
        superseded = _superseded_closed_pr_numbers(ref, pull_requests)

        # Avoid adding one Issue API call to every normal completion tick.
        # Query it only when every unresolved historical PR already has an
        # unambiguous newer same-head merged replacement.
        if not unresolved_closed or superseded != unresolved_closed:
            return provisional

        issue_payload, _ = client.get(
            f"/repos/{ref.repository}/issues/{ref.issue_number}"
        )
        if not isinstance(issue_payload, dict):
            raise GithubCompletionError(
                f"GitHub returned an invalid Issue response for #{ref.issue_number}"
            )

        issue_state = str(issue_payload.get("state", "")).casefold()
        if issue_state not in {"open", "closed"}:
            raise GithubCompletionError(
                f"GitHub returned incomplete Issue data for #{ref.issue_number}"
            )

        return evaluate_completion(
            ref,
            pull_requests,
            linked_pr_numbers=numbers,
            issue_state=issue_state,
        )

    except GithubCompletionError as exc:
        return GithubCompletionDecision(
            desired_status=None,
            reason="github_query_failed",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# agent-rework evaluation (label trust + one-shot semantics + context)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReworkDecision:
    reason: str
    pr_number: Optional[int] = None
    head_sha: str = ""
    request_comment_id: Optional[int] = None
    context_block: str = ""
    label_present: bool = False
    label_added_at: Optional[int] = None
    label_actor: str = ""
    authoritative: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "request_comment_id": self.request_comment_id,
            "label_present": self.label_present,
            "label_added_at": self.label_added_at,
            "label_actor": self.label_actor,
            "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
            "authoritative": self.authoritative,
            "error": self.error,
        }


def _parse_iso_ts(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp())


def _labeled_events(client: Any, ref: GithubTaskRef, pr_number: int) -> list[tuple[int, str]]:
    """Return [(added_at_epoch, actor)] for ``agent-rework`` label additions."""
    items = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr_number}/timeline",
        {"per_page": 100},
    )
    events: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("event") != "labeled":
            continue
        label = item.get("label")
        if not isinstance(label, dict) or str(label.get("name")) != REWORK_LABEL:
            continue
        ts = _parse_iso_ts(item.get("created_at"))
        actor = str((item.get("actor") or {}).get("login") or "")
        if ts is not None:
            events.append((ts, actor))
    return events


def _rework_request_comment_id(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    label_added_at: int,
) -> Optional[int]:
    """Return the newest trusted PR comment before the rework label."""
    comments = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr_number}/comments",
        {"per_page": 100},
    )
    candidates: list[tuple[str, int]] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        author = str((item.get("user") or {}).get("login") or "")
        comment_id = item.get("id")
        if author not in TRUSTED_GITHUB_ACTORS or not isinstance(comment_id, int):
            continue
        created_at = _parse_iso_ts(item.get("created_at"))
        if created_at is None or created_at > label_added_at:
            continue
        candidates.append((str(item.get("created_at") or ""), comment_id))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def evaluate_rework(
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    *,
    current_status: str,
    last_rework_at: Optional[int],
) -> Optional[ReworkDecision]:
    """Evaluate the one-shot agent-rework request.

    Returns ``None`` when no open linked PR carries ``agent-rework``.
    A ``ReworkDecision`` with ``reason == "agent_rework"`` means the
    REVIEW -> READY transition is authorised.  Raises
    ``GithubCompletionError`` on any GitHub lookup failure (fail-closed).
    """
    if current_status not in {"review", "ready", "blocked"}:
        return None
    open_prs = [pr for pr in decision.pull_requests if pr.state == "open"]
    if not open_prs:
        return None
    rework_prs: list[GithubPullRequest] = []
    for pr in open_prs:
        # PRs come from the current decision, freshly fetched from
        # ``/pulls/{n}`` in this sync run: existence is authoritative.
        names = _pr_labels(client, ref.repository, pr.number, pr_exists=True)
        if REWORK_LABEL in names:
            rework_prs.append(pr)
    if not rework_prs:
        return None
    if len(rework_prs) > 1:
        return ReworkDecision(reason="multiple_rework_prs", label_present=True)
    pr = rework_prs[0]
    events = _labeled_events(client, ref, pr.number)
    if not events:
        # Label present without any labeled event: data anomaly — do nothing.
        return ReworkDecision(reason="rework_label_event_missing", pr_number=pr.number, label_present=True)
    label_added_at, label_actor = max(events, key=lambda item: item[0])
    request_comment_id = _rework_request_comment_id(
        client, ref, pr.number, label_added_at
    )
    if label_actor not in TRUSTED_GITHUB_ACTORS:
        return ReworkDecision(
            reason="untrusted_rework_label_actor",
            pr_number=pr.number,
            head_sha=pr.head_sha,
            request_comment_id=request_comment_id,
            label_present=True,
            label_added_at=label_added_at,
            label_actor=label_actor,
        )
    if last_rework_at is not None and label_added_at <= last_rework_at:
        # The request remains visible until the dispatcher has claimed the
        # Kanban task.  Do not remove it merely because the DB event exists.
        return ReworkDecision(
            reason="rework_claim_pending",
            pr_number=pr.number,
            head_sha=pr.head_sha,
            request_comment_id=request_comment_id,
            label_present=True,
            label_added_at=label_added_at,
            label_actor=label_actor,
        )
    issue_payload, _ = client.get(f"/repos/{ref.repository}/issues/{ref.issue_number}")
    if not isinstance(issue_payload, dict):
        raise GithubCompletionError(f"GitHub returned an invalid issue response for #{ref.issue_number}")
    issue_state = str(issue_payload.get("state", "")).casefold()
    issue_labels = {str(item.get("name")) for item in issue_payload.get("labels", []) if isinstance(item, dict)}
    if issue_state != "open" or AGENT_READY_LABEL not in issue_labels:
        return ReworkDecision(
            reason="issue_not_agent_ready",
            pr_number=pr.number,
            head_sha=pr.head_sha,
            request_comment_id=request_comment_id,
            label_present=True,
            label_added_at=label_added_at,
            label_actor=label_actor,
        )
    context_block = _build_context_block(client, ref, decision.pull_requests, rework_pr_number=pr.number)
    return ReworkDecision(
        reason="agent_rework",
        pr_number=pr.number,
        head_sha=pr.head_sha,
        request_comment_id=request_comment_id,
        context_block=context_block,
        label_present=True,
        label_added_at=label_added_at,
        label_actor=label_actor,
    )


def _collect_pr_context(client: Any, ref: GithubTaskRef, pr: GithubPullRequest) -> dict[str, Any]:
    return {
        "reviews": client.get_paginated(
            f"/repos/{ref.repository}/pulls/{pr.number}/reviews", {"per_page": 100}
        ),
        "review_comments": client.get_paginated(
            f"/repos/{ref.repository}/pulls/{pr.number}/comments", {"per_page": 100}
        ),
        "issue_comments": client.get_paginated(
            f"/repos/{ref.repository}/issues/{pr.number}/comments", {"per_page": 100}
        ),
    }


def _build_context_block(
    client: Any,
    ref: GithubTaskRef,
    pull_requests: Iterable[GithubPullRequest],
    *,
    rework_pr_number: Optional[int] = None,
) -> str:
    prs = tuple(pull_requests)
    contexts = {pr.number: _collect_pr_context(client, ref, pr) for pr in prs}
    return _render_sync_context(ref, prs, contexts, rework_pr_number=rework_pr_number)


def _truncate(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n…[truncated]"


def _feedback_items(
    context: Mapping[str, Any],
    *,
    trusted: bool,
) -> list[tuple[str, str, str, str]]:
    """Return (when, author, kind, body) items, newest-last sorting later."""
    items: list[tuple[str, str, str, str]] = []
    for review in context.get("reviews", []):
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        if state in {"PENDING", "DISMISSED"}:
            continue
        author = str((review.get("user") or {}).get("login") or "unknown")
        if (author in TRUSTED_GITHUB_ACTORS) != trusted:
            continue
        body = str(review.get("body") or "").strip() or f"(review {state}, no body)"
        items.append((str(review.get("submitted_at") or ""), author, f"review {state}", body))
    for comment in context.get("review_comments", []):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "unknown")
        if (author in TRUSTED_GITHUB_ACTORS) != trusted:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        path = str(comment.get("path") or "")
        line = comment.get("line") or comment.get("original_line") or ""
        loc = f"{path}:{line}" if path else ""
        kind = f"comment {loc}".strip()
        items.append((str(comment.get("created_at") or ""), author, kind, body))
    for comment in context.get("issue_comments", []):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "unknown")
        if (author in TRUSTED_GITHUB_ACTORS) != trusted:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        items.append((str(comment.get("created_at") or ""), author, "PR comment", body))
    return items


def _render_feedback_section(
    items: list[tuple[str, str, str, str]], limit: int
) -> list[str]:
    ordered = sorted(items, key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    for when, author, kind, body in ordered[:limit]:
        lines.append(f"- {author} ({kind}, {when}): {_truncate(body, MAX_ITEM_CHARS)}")
    if len(ordered) > limit:
        lines.append(f"- …and {len(ordered) - limit} more items omitted")
    if not lines:
        lines.append("(none)")
    return lines


def _render_sync_context(
    ref: GithubTaskRef,
    pull_requests: Iterable[GithubPullRequest],
    contexts: Mapping[int, Mapping[str, Any]],
    *,
    rework_pr_number: Optional[int] = None,
) -> str:
    prs = sorted(pull_requests, key=lambda item: item.number)
    lines: list[str] = [SYNC_CONTEXT_BEGIN, ""]
    lines.append("## Current linked PR" if len(prs) == 1 else "## Linked PRs")
    for pr in prs:
        lines.append("")
        heading = f"PR #{pr.number} — {pr.title or '(untitled)'}"
        if len(prs) > 1:
            heading = f"### {heading}"
        lines.append(heading)
        lines.append(f"State: {pr.state}")
        lines.append(f"Head SHA: {pr.head_sha}")
        lines.append(f"Base branch: {pr.base_branch}")
        if pr.author:
            lines.append(f"Author: {pr.author}")
        if pr.html_url:
            lines.append(f"URL: {pr.html_url}")
        if rework_pr_number == pr.number:
            lines.append("Rework requested: yes (agent-rework label, awaiting Kanban claim)")
            lines.append(
                "Rework contract: this is a rework of the EXISTING PR above — "
                "update the SAME PR/branch (resolve the trusted review "
                "feedback); do NOT create a new PR; re-run the repository "
                "gates; then hand back for review (block with review-required)."
            )
        if pr.body:
            lines.append("Body:")
            lines.append(_truncate(pr.body, MAX_PR_BODY_CHARS))
        context = contexts.get(pr.number, {})
        trusted = _feedback_items(context, trusted=True)
        untrusted = _feedback_items(context, trusted=False)
        lines.append("")
        lines.append("## Trusted review / rework feedback")
        lines.extend(_render_feedback_section(trusted, MAX_TRUSTED_ITEMS))
        lines.append("")
        lines.append("## Other PR discussion — untrusted context")
        lines.extend(_render_feedback_section(untrusted, MAX_UNTRUSTED_ITEMS))
    lines.append("")
    lines.append(SYNC_CONTEXT_END)
    return "\n".join(lines)


def replace_sync_context(body: Optional[str], block: str) -> str:
    """Replace the sync-owned body region between the markers; else append.

    The provenance header and ``## Canonical Issue body`` are never
    touched, so ``parse_task_ref()`` keeps working unchanged.
    """
    text = str(body or "")
    if SYNC_CONTEXT_BEGIN in text and SYNC_CONTEXT_END in text:
        head, rest = text.split(SYNC_CONTEXT_BEGIN, 1)
        _, tail = rest.split(SYNC_CONTEXT_END, 1)
        if head and not head.endswith("\n"):
            head += "\n"
        if tail and not tail.startswith("\n"):
            tail = "\n" + tail
        return head + block + tail
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


# ---------------------------------------------------------------------------
# BLOCKED visibility — GitHub blocker projection + resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlockedResumeDecision:
    reason: str
    context_block: str = ""
    response_author: str = ""
    response_at: str = ""
    authoritative: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "response_author": self.response_author,
            "response_at": self.response_at,
            "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
            "authoritative": self.authoritative,
            "error": self.error,
        }


def _task_block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    """Latest human-facing block reason from local durable records."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row and row["payload"]:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        reason = str(payload.get("reason") or "").strip()
        if reason:
            return reason
    run = conn.execute(
        "SELECT summary FROM task_runs WHERE task_id = ? AND outcome = 'blocked' "
        "AND summary IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if run and run["summary"]:
        return str(run["summary"]).strip()
    return "The task is blocked pending maintainer input (no reason recorded)."


def _blocker_comment_body(task_id: str, reason: str, needs: str, *, no_pr: bool) -> str:
    lines = [
        f"{BLOCKER_MARKER_PREFIX}{task_id}{BLOCKER_MARKER_SUFFIX}",
        "",
        "🤖 Hermes blocked",
        "",
        f"Kanban task: `{task_id}`",
        "",
        "Reason:",
        _truncate(reason, MAX_BLOCKER_REASON_CHARS),
        "",
        "Needs from maintainer:",
        needs,
    ]
    if no_pr:
        lines += ["", "No reviewable pull request is currently available."]
    lines += ["", "This comment is maintained by the Hermes GitHub reconciliation layer."]
    return "\n".join(lines)


def _resume_consumed_comment_body(
    task_id: str, response_at: str, response_author: str
) -> str:
    """Marker body for a one-shot-consumed resume (worker crash after unblock).

    Keeps the GitHub Issue surface visible: the maintainer can see the
    task WAS unblocked, that the worker crashed, and that a NEW comment
    is required to resume.  Reuses the marker prefix so the projection
    finder still recognises the comment.
    """
    lines = [
        f"{BLOCKER_MARKER_PREFIX}{task_id}{BLOCKER_MARKER_SUFFIX}",
        "",
        "🤖 Hermes blocked (resume consumed)",
        "",
        f"Kanban task: `{task_id}`",
        "",
        f"Maintainer response ({response_author}, {response_at}) already unblocked "
        "this task once, but the worker crashed before completing the work. "
        "The same response is consumed and will not re-fire automatically.",
        "",
        "Needs from maintainer: post a NEW comment on the Issue to resume this "
        "task once.",
        "",
        "No reviewable pull request is currently available.",
        "",
        "This comment is maintained by the Hermes GitHub reconciliation layer.",
    ]
    return "\n".join(lines)


def _find_blocker_marker(comments: Iterable[Any], task_id: str) -> Optional[dict[str, Any]]:
    marker = f"{BLOCKER_MARKER_PREFIX}{task_id}{BLOCKER_MARKER_SUFFIX}"
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if marker in str(comment.get("body") or ""):
            return comment
    return None


def _trusted_response_after(
    comments: Iterable[Any], baseline_iso: str
) -> Optional[dict[str, Any]]:
    """Newest trusted-actor Issue comment created after the projection baseline."""
    best: Optional[dict[str, Any]] = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        created = str(comment.get("created_at") or "")
        body = str(comment.get("body") or "").strip()
        if author not in TRUSTED_GITHUB_ACTORS or not body:
            continue
        if baseline_iso and created <= baseline_iso:
            continue
        if best is None or created > str(best.get("created_at") or ""):
            best = comment
    return best


def _render_issue_resolve_context(
    comments: Iterable[Any], baseline_iso: str
) -> str:
    """Sync-owned context for an Issue-only blocker resolution (bounded)."""
    trusted: list[dict[str, Any]] = []
    untrusted: list[dict[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        created = str(comment.get("created_at") or "")
        body = str(comment.get("body") or "").strip()
        if not body or (baseline_iso and created <= baseline_iso):
            continue
        (trusted if author in TRUSTED_GITHUB_ACTORS else untrusted).append(comment)
    trusted.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    untrusted.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    lines = [SYNC_CONTEXT_BEGIN, "", "## Issue blocker resolution", ""]
    for comment in trusted[:MAX_ISSUE_RESPONSE_ITEMS]:
        author = str((comment.get("user") or {}).get("login") or "unknown")
        created = str(comment.get("created_at") or "")
        lines.append(
            f"- {author} ({created}): {_truncate(comment.get('body'), MAX_ITEM_CHARS)}"
        )
    if not trusted:
        lines.append("(none)")
    lines += ["", "## Issue discussion — untrusted context (reference only)"]
    for comment in untrusted[:MAX_ISSUE_RESPONSE_ITEMS]:
        author = str((comment.get("user") or {}).get("login") or "unknown")
        created = str(comment.get("created_at") or "")
        lines.append(
            f"- {author} ({created}): {_truncate(comment.get('body'), MAX_ITEM_CHARS)}"
        )
    if not untrusted:
        lines.append("(none)")
    lines += ["", SYNC_CONTEXT_END]
    return "\n".join(lines)


def evaluate_blocked_resume(
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    *,
    marker_comment: Optional[dict[str, Any]],
    issue_payload: Mapping[str, Any],
    issue_comments: Iterable[Any],
    last_resume_response_at: str = "",
) -> BlockedResumeDecision:
    """Authorise the no-PR BLOCKED -> READY resume (#7 conditions).

    Resume requires: no linked PR, Issue open + ``agent-ready``,
    ``agent-blocked`` absent, and a trusted maintainer Issue comment
    created after the blocker projection baseline.  Anything else fails
    closed (stays BLOCKED).

    One-shot guard: a trusted response whose ``created_at`` equals the
    last consumed ``github_blocked_resolved`` event's ``response_at``
    must NOT re-authorise a resume (worker crash -> give_up -> BLOCKED
    would otherwise re-fire the same transition every tick).  Only a NEW
    trusted response re-enables the resume.
    """
    if decision.pull_requests:
        return BlockedResumeDecision(reason="linked_pr_present")
    if marker_comment is None:
        return BlockedResumeDecision(reason="no_projection_baseline")
    issue_state = str(issue_payload.get("state", "")).casefold()
    issue_labels = {
        str(item.get("name"))
        for item in issue_payload.get("labels", [])
        if isinstance(item, dict)
    }
    if issue_state != "open" or AGENT_READY_LABEL not in issue_labels:
        return BlockedResumeDecision(reason="issue_not_ready")
    if BLOCKED_LABEL in issue_labels:
        return BlockedResumeDecision(reason="agent_blocked_label_present")
    baseline = str(marker_comment.get("updated_at") or marker_comment.get("created_at") or "")
    response = _trusted_response_after(issue_comments, baseline)
    if response is None:
        return BlockedResumeDecision(reason="no_trusted_response")
    response_at = str(response.get("created_at") or "")
    if last_resume_response_at and response_at == last_resume_response_at:
        return BlockedResumeDecision(
            reason="resume_consumed",
            response_author=str((response.get("user") or {}).get("login") or ""),
            response_at=response_at,
        )
    context_block = _render_issue_resolve_context(issue_comments, baseline)
    return BlockedResumeDecision(
        reason="agent_blocked_resolved",
        context_block=context_block,
        response_author=str((response.get("user") or {}).get("login") or ""),
        response_at=response_at,
    )


def _add_label(
    client: Any,
    repository: str,
    number: int,
    label_name: str,
) -> None:
    """Add a label to an Issue/PR, creating the repo label if required.

    Fail-closed and idempotent under the 422 label-create race: a failed
    create is re-checked against the repo label list (another actor may
    have won the race between our GET and POST) before raising; any
    residual failure raises and is retried on the next tick.
    """

    def _repo_has_label() -> bool:
        labels, _ = client.get(f"/repos/{repository}/labels", {"per_page": 100})
        if not isinstance(labels, list):
            raise GithubCompletionError("invalid repo labels payload")
        return any(
            str(item.get("name")) == label_name
            for item in labels
            if isinstance(item, dict)
        )

    def _add_to_issue() -> tuple[int, Any]:
        return client.post(
            f"/repos/{repository}/issues/{number}/labels",
            {"labels": [label_name]},
        )

    status, _ = _add_to_issue()
    if 200 <= status < 300:
        return
    if status not in (404, 422):
        raise GithubCompletionError(
            f"could not add label {label_name} to issue (HTTP {status})"
        )
    if not _repo_has_label():
        create_status, _ = client.post(
            f"/repos/{repository}/labels",
            {
                "name": label_name,
                "color": "b60205",
                "description": "Hermes Kanban task is blocked; resolution is tracked in the linked issue comment.",
            },
        )
        if not (200 <= create_status < 300):
            # Race: someone else created the label between our GET and
            # POST.  Re-check once; if it now exists, proceed to the
            # issue-label add, otherwise fail closed for the next tick.
            if create_status in (404, 422) and _repo_has_label():
                pass
            else:
                raise GithubCompletionError(
                    f"could not create label {label_name} (HTTP {create_status})"
                )
    status, _ = _add_to_issue()
    if not (200 <= status < 300):
        raise GithubCompletionError(
            f"could not add label {label_name} to issue (HTTP {status})"
        )


def _add_issue_label(client: Any, ref: GithubTaskRef, label_name: str) -> None:
    _add_label(client, ref.repository, ref.issue_number, label_name)


def _remove_label(
    client: Any,
    repository: str,
    number: int,
    label_name: str,
) -> int:
    """Remove a label from an Issue/PR; a missing label is already success."""
    return client.delete(
        f"/repos/{repository}/issues/{number}/labels/{label_name}"
    )


def _pr_labels(
    client: Any,
    repository: str,
    pr_number: int,
    *,
    pr_exists: bool = False,
) -> set[str]:
    """Return label names for a PR, failing closed on lookup errors.

    GitHub serves some valid PRs (their ``pulls/{n}`` view exists and was
    fetched in the current decision) whose issues-side
    ``issues/{n}/labels`` endpoint answers 404 even though the PR carries
    no labels.  A 404 from this exact labels endpoint is treated as an
    empty label set ONLY when the caller has already authoritatively
    established PR existence in the current decision/context
    (``pr_exists=True``).  Every other failure — 401/403, other HTTP
    errors, transport/timeout, malformed payloads, and a labels-404
    without that proof — remains fail-closed.
    """
    try:
        labels, _ = client.get(f"/repos/{repository}/issues/{pr_number}/labels")
    except GithubCompletionError as exc:
        labels_404 = (
            pr_exists
            and exc.status == 404
            and re.search(r"/repos/[^/]+/[^/]+/issues/\d+/labels$", str(exc))
            is not None
        )
        if not labels_404:
            raise
        return set()
    if not isinstance(labels, list):
        raise GithubCompletionError(
            f"GitHub returned invalid labels for {repository}#{pr_number}"
        )
    return {
        str(item.get("name"))
        for item in labels
        if isinstance(item, dict) and item.get("name")
    }


def _remove_pr_label(client: Any, ref: GithubTaskRef, pr_number: int, label: str) -> int:
    return _remove_label(client, ref.repository, pr_number, label)


def _upsert_blocker_comment(
    client: Any, ref: GithubTaskRef, task_id: str, body: str
) -> tuple[str, Optional[int]]:
    """Create the marker comment or patch it in place; never append a second."""
    comments, _ = client.get(
        f"/repos/{ref.repository}/issues/{ref.issue_number}/comments"
    )
    marker = _find_blocker_marker(comments, task_id)
    if marker is None:
        status, payload = client.post(
            f"/repos/{ref.repository}/issues/{ref.issue_number}/comments",
            {"body": body},
        )
        if not (200 <= status < 300):
            raise GithubCompletionError(
                f"could not create blocker comment (HTTP {status})"
            )
        comment_id = payload.get("id") if isinstance(payload, dict) else None
        return "created", int(comment_id) if comment_id is not None else None
    if str(marker.get("body") or "") == body:
        return "unchanged", int(marker["id"]) if marker.get("id") is not None else None
    status, payload = client.patch(
        f"/repos/{ref.repository}/issues/comments/{marker['id']}",
        {"body": body},
    )
    if not (200 <= status < 300):
        raise GithubCompletionError(
            f"could not update blocker comment (HTTP {status})"
        )
    return "updated", int(marker["id"]) if marker.get("id") is not None else None


def _linked_pr_evidence(client: Any, ref: GithubTaskRef, pull_requests: Iterable[GithubPullRequest]) -> bool:
    """GitHub-visible blocker evidence on linked PRs: agent-rework label or
    trusted-actor content on an open/draft PR (a blocker handoff)."""
    for pr in pull_requests:
        # PRs come from the current decision, freshly fetched from
        # ``/pulls/{n}`` in this sync run: existence is authoritative.
        names = _pr_labels(client, ref.repository, pr.number, pr_exists=True)
        if REWORK_LABEL in names:
            return True
        if pr.state != "open" and not pr.draft:
            continue
        context = _collect_pr_context(client, ref, pr)
        if _feedback_items(context, trusted=True):
            return True
    return False


def _append_blocked_projection_event(
    conn: sqlite3.Connection,
    task_id: str,
    ref: GithubTaskRef,
    block_kind: Optional[str],
    action: str,
    comment_id: Optional[int],
) -> None:
    payload = {
        "previous_status": "blocked",
        "new_status": "blocked",
        "repository": ref.repository,
        "issue_number": ref.issue_number,
        "task_id": task_id,
        "reason": "blocker_projection",
        "action": action,
        "comment_id": comment_id,
        "block_kind": block_kind,
        "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
        "merge_authority": "human",
        "auto_merge": False,
        "source": "github",
    }
    _append_sync_event(conn, task_id, payload, kind="github_blocked_projection")


def apply_blocked_resume(
    conn: sqlite3.Connection,
    task_id: str,
    resume: BlockedResumeDecision,
    ref: GithubTaskRef,
) -> dict[str, Any]:
    """BLOCKED -> READY with the hydrated Issue response; one event.

    Optimistic (``WHERE status = 'blocked'``); body update and the
    ``github_blocked_resolved`` event share the transaction.
    """
    row = conn.execute(
        "SELECT status, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "changed": False, "reason": "task_missing"}
    current_status = str(row["status"])
    if current_status != "blocked":
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    new_body = replace_sync_context(row["body"], resume.context_block)
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'ready',
               completed_at = NULL,
               assignee = NULL,
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               block_kind = NULL,
               block_recurrences = 0,
               body = ?
         WHERE id = ? AND status = 'blocked'
        """,
        (new_body, task_id),
    )
    if cur.rowcount != 1:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    payload = {
        "previous_status": "blocked",
        "new_status": "ready",
        "repository": ref.repository,
        "issue_number": ref.issue_number,
        "task_id": task_id,
        "reason": "agent_blocked_resolved",
        "response_author": resume.response_author,
        "response_at": resume.response_at,
        "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
        "merge_authority": "human",
        "auto_merge": False,
        "source": "github",
    }
    _append_sync_event(conn, task_id, payload, kind="github_blocked_resolved")
    return {
        "task_id": task_id,
        "status": "ready",
        "changed": True,
        "reason": "agent_blocked_resolved",
        "resume": resume.to_dict(),
    }


# ---------------------------------------------------------------------------
# Kanban DB layer (edge only — direct transactional writes, approved scope)
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    return Path(
        os.environ.get("HERMES_KANBAN_INTAKE_HOME")
        or os.environ.get("HERMES_HOME")
        or DEFAULT_HERMES_HOME
    )


_EDGE_LOCK_NAME = "github-edge-sync.lock"


def _edge_lock_path() -> Path:
    """Return the guarded runtime lock path shared by every edge caller."""
    raw_home = _hermes_home().expanduser()
    if raw_home.is_symlink():
        raise SyncError("edge_single_flight_path_invalid")
    try:
        home = raw_home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncError("edge_single_flight_path_invalid") from exc
    if not home.is_dir():
        raise SyncError("edge_single_flight_path_invalid")

    kanban_root = home / "kanban"
    if kanban_root.is_symlink() or (kanban_root.exists() and not kanban_root.is_dir()):
        raise SyncError("edge_single_flight_path_invalid")
    try:
        kanban_root.mkdir(mode=0o700, exist_ok=True)
        kanban_root = kanban_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncError("edge_single_flight_path_invalid") from exc
    if kanban_root.parent != home or kanban_root.is_symlink():
        raise SyncError("edge_single_flight_path_invalid")

    lock_dir = kanban_root / ".resource-locks"
    if lock_dir.is_symlink() or (lock_dir.exists() and not lock_dir.is_dir()):
        raise SyncError("edge_single_flight_path_invalid")
    try:
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_dir = lock_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncError("edge_single_flight_path_invalid") from exc
    if lock_dir.parent != kanban_root or lock_dir.is_symlink():
        raise SyncError("edge_single_flight_path_invalid")

    lock_path = lock_dir / _EDGE_LOCK_NAME
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise SyncError("edge_single_flight_path_invalid")
    return lock_path


@contextmanager
def _edge_single_flight():
    """Serialize the complete edge run across processes with kernel locking.

    Callers already impose the finite edge-sync deadline.  Waiting here is a
    blocking ``flock`` operation rather than a polling loop; a killed owner
    releases the kernel lock, and a caller that reaches its deadline reports a
    failed wake instead of claiming a successful reconciliation.
    """
    try:
        import fcntl  # POSIX; the deployed edge runtime is Linux.
    except ImportError as exc:  # pragma: no cover - unsupported host
        raise SyncError("edge_single_flight_unavailable") from exc

    fd: int | None = None
    handle: Any = None
    try:
        lock_path = _edge_lock_path()
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError("edge lock permissions or type invalid")
        handle = os.fdopen(fd, "a+b", closefd=True)
        fd = None
        # The kernel owns contention and releases this lock if the process
        # crashes; do not replace it with a sleep/retry loop.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except SyncError:
        if handle is not None:
            handle.close()
        elif fd is not None:
            os.close(fd)
        raise
    except (OSError, ValueError) as exc:
        if handle is not None:
            handle.close()
        elif fd is not None:
            os.close(fd)
        raise SyncError("edge_single_flight_unavailable") from exc

    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _import_kanban_db():
    """Import the kanban core DB module.  A Hermes update that breaks the
    import must fail loudly before any write, never silently."""
    try:
        from hermes_cli import kanban_db  # type: ignore
        return kanban_db
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SyncError(
            "hermes_cli.kanban_db is not importable from this interpreter "
            f"({sys.executable}): {exc}. Run with the Hermes venv python "
            "(e.g. /ws/hermes-agent/venv/bin/python3) or fix the environment."
        ) from exc


def verify_schema(conn: sqlite3.Connection, kanban_db: Any) -> None:
    """Fail-closed schema compatibility check.  No write may happen after a
    mismatch — callers must abort the whole run."""
    problems: list[str] = []

    valid_statuses = getattr(kanban_db, "VALID_STATUSES", None)
    if valid_statuses is None or "review" not in set(valid_statuses):
        problems.append(
            "kanban_db.VALID_STATUSES is missing or does not contain 'review' "
            "(Hermes status model changed?)"
        )

    task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for column in _REQUIRED_TASK_COLUMNS:
        if column not in task_cols:
            problems.append(f"tasks.{column} column missing (schema changed?)")

    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
    for column in _EVENT_COLUMNS:
        if column not in event_cols:
            problems.append(f"task_events.{column} column missing (schema changed?)")

    if problems:
        raise SyncError("schema compatibility check failed: " + "; ".join(problems))


def _append_sync_event(
    conn: sqlite3.Connection,
    task_id: str,
    payload: dict[str, Any],
    *,
    kind: str = "github_pr_sync",
    event_table: str = "task_events",
    created_at: Optional[int] = None,
) -> None:
    timestamp = int(time.time()) if created_at is None else int(created_at)
    conn.execute(
        f"INSERT INTO {event_table} (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp),
    )


def _internal_dependency_gate(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Read the direct parent gate for a GitHub-backed intake task.

    The query deliberately validates the live ``task_links`` direction and
    does not infer dependencies from task metadata. A dangling link is a
    lookup failure, not an implicitly satisfied dependency.
    """
    rows = conn.execute(
        "SELECT l.parent_id, t.status AS parent_status "
        "FROM task_links AS l LEFT JOIN tasks AS t ON t.id = l.parent_id "
        "WHERE l.child_id = ? ORDER BY l.parent_id",
        (task_id,),
    ).fetchall()
    parents: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for row in rows:
        parent_id = str(row["parent_id"])
        parent_status = row["parent_status"]
        if parent_status is None:
            raise SyncError(f"missing parent task for link: {parent_id}")
        status = str(parent_status)
        parent = {"id": parent_id, "status": status}
        parents.append(parent)
        if status not in _DEPENDENCY_TERMINAL_STATES:
            pending.append(parent)
    return {"parents": parents, "pending": pending, "all_terminal": not pending}


def _restore_pending_dependency(
    conn: sqlite3.Connection,
    task_id: str,
    gate: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Keep an intake root runnable until all internal parents terminate."""
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "status": None, "changed": False, "reason": "task_missing"}
    current_status = str(row["status"])
    pending = list(gate.get("pending") or [])
    evidence = {"parents": pending, "dependency_gate": "pending"}
    if current_status not in {"review", "done"}:
        return {
            "task_id": task_id, "status": current_status, "changed": False,
            "reason": "internal_dependency_pending", "evidence": evidence,
        }
    if dry_run:
        return {
            "task_id": task_id, "status": "todo", "changed": False,
            "reason": "internal_dependency_pending_predicted", "evidence": evidence,
        }
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL, assignee = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "block_kind = NULL, block_recurrences = 0 WHERE id = ? AND status = ?",
            (task_id, current_status),
        )
        if cur.rowcount != 1:
            return {
                "task_id": task_id, "status": current_status, "changed": False,
                "reason": "state_changed_during_sync", "evidence": evidence,
            }
        _append_sync_event(
            conn, task_id, {
                **evidence, "source": "github",
                "previous_status": current_status, "new_status": "todo",
                "reason": "internal_dependency_pending",
                "merge_authority": "human", "auto_merge": False,
            }, kind="github_dependency_gate",
        )
    return {
        "task_id": task_id, "status": "todo", "changed": True,
        "reason": "internal_dependency_pending", "evidence": evidence,
    }


def _dependency_gate_evidence(
    conn: sqlite3.Connection,
    task_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Read the live dependency gate without raising.

    Returns ``(gate, None)`` on success and ``(None, error)`` when the
    lookup itself fails (dangling link, missing table, SQL error).  The
    caller fails closed on any error: a lookup failure is never an
    implicitly satisfied dependency, and a single bad task must never
    crash the whole board sync.
    """
    try:
        return _internal_dependency_gate(conn, task_id), None
    except (SyncError, sqlite3.Error) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _dependency_guard_sql() -> str:
    """Atomic gate folded into the final ``UPDATE ... WHERE``.

    Counts direct parent links whose parent is NOT terminal (see
    ``_DEPENDENCY_TERMINAL_STATES``); a dangling link also counts (the
    inner ``NOT EXISTS`` is true when the parent row is missing).  The
    update therefore only fires when every direct parent is terminal and
    present — the same condition the ``sync_board`` pre-gate checked,
    re-evaluated in the very statement that writes.  Because the subquery
    and the write are one statement, no writer can interleave between the
    check and the write: a parent that flips non-terminal (or a new parent
    link) after the pre-gate is visible to this statement and the update
    affects zero rows.  Appends exactly one ``?`` placeholder (the child
    task id).
    """
    terminal = ",".join(
        f"'{status}'" for status in sorted(_DEPENDENCY_TERMINAL_STATES)
    )
    return (
        f"AND (SELECT count(*) FROM task_links AS l "
        f"WHERE l.child_id = ? "
        f"AND NOT EXISTS (SELECT 1 FROM tasks AS t "
        f"WHERE t.id = l.parent_id AND t.status IN ({terminal}))) = 0"
    )


def _last_resume_response_at(conn: sqlite3.Connection, task_id: str) -> str:
    """``response_at`` of the newest consumed ``github_blocked_resolved`` event.

    One-shot resume guard: a trusted Issue response whose ``created_at``
    matches this value has already been consumed and must not re-authorise
    a BLOCKED -> READY transition after a worker crash.
    """
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'github_blocked_resolved' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["payload"]:
        return ""
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return ""
    return str(payload.get("response_at") or "")


def _parking_comment_for_decision(
    task_id: str,
    decision: GithubCompletionDecision,
) -> Optional[str]:
    """Render the one-line DONE -> REVIEW parking marker comment.

    ``review`` here means "parked awaiting external GitHub resolution": the
    card needs no human or worker action until the edge observes fresh
    evidence (merge, PR link, closure). The first token is machine-readable;
    the trailing sentence states explicitly that no action is required.
    Returns None for decisions that must not park a card with a marker
    (non-authoritative outcomes never reach this transition anyway).
    """
    if decision.desired_status != "review":
        return None
    reason = str(decision.reason or "")
    if reason == "no_linked_pr":
        marker = f"{_PARKING_COMMENT_PREFIX} awaiting-pr] reason=no_linked_pr"
        next_step = (
            "next=github-edge(PR 링크 감지 시 자동 재평가). "
            "사람 행동 불필요."
        )
    else:
        numbers = [int(n) for n in decision.linked_pr_numbers if int(n) > 0]
        pr_token = ",".join(str(n) for n in sorted(numbers))
        if not pr_token:
            return None
        if reason == "linked_pr_open":
            marker = f"{_PARKING_COMMENT_PREFIX} awaiting-merge] reason=linked_pr_open"
        else:
            # closed-not-merged / linked_pr_not_merged: still waiting on
            # GitHub merge evidence, not on any person.
            marker = f"{_PARKING_COMMENT_PREFIX} awaiting-merge] reason={reason}"
        marker = f"{marker} pr=#{pr_token}"
        next_step = (
            "next=github-edge(merge 감지 시 자동 해제). "
            "사람 행동 불필요."
        )
    return f"{marker} {next_step}"


def _append_parking_comment_if_absent(
    conn: sqlite3.Connection,
    task_id: str,
    comment_body: str,
) -> bool:
    """Insert the parking marker comment once per exact body.

    Idempotency key IS the rendered body (task+reason+pr combination):
    repeated syncs over an unchanged situation re-append nothing, while a
    genuinely new situation renders a different line and is recorded once.
    """
    existing = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    for row in existing:
        if str(row["body"] or "").strip() == comment_body:
            return False
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (task_id, "github-edge", comment_body, int(time.time())),
    )
    return True


def apply_decision(
    conn: sqlite3.Connection,
    task_id: str,
    decision: GithubCompletionDecision,
    *,
    context_block: Optional[str] = None,
    allow_blocked_source: bool = False,
) -> dict[str, Any]:
    """One optimistic transition for one card.  Raises SyncError on refusal;
    returns the result dict otherwise.  The caller owns the transaction.

    ``context_block`` (only meaningful for DONE -> REVIEW) refreshes the
    sync-owned body region in the same transaction; the transition is
    never blocked by a missing context block.

    ``allow_blocked_source`` lets the BLOCKED reconciliation reuse this
    canonical transition path (BLOCKED -> REVIEW / BLOCKED -> DONE) with
    the same stale blocker/claim/run cleanup; the preserved-state guard
    is skipped only for ``blocked``.
    """
    row = conn.execute(
        "SELECT status, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "changed": False, "reason": "task_missing"}
    current_status = str(row["status"])
    if not is_github_backed_body(row["body"]):
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "not_github_backed",
        }
    if not decision.authoritative:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": decision.reason,
            "error": decision.error,
        }
    desired = decision.desired_status
    if desired not in {"review", "done"}:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "invalid_sync_decision",
        }

    evidence = decision.to_dict()
    if current_status == desired:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": decision.reason,
            "sync_action": "idempotent",
            "evidence": evidence,
        }
    if current_status in _PRESERVED_STATES and not (
        allow_blocked_source and current_status == "blocked"
    ):
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_preserved",
            "evidence": evidence,
        }

    now = int(time.time())
    # Re-verify the dependency gate in the SAME transaction as the write.
    # The pre-gate in sync_board ran before the (seconds-long) GitHub
    # fetch; this evidence read plus the guard folded into the UPDATE
    # below is what closes the TOCTOU window at the write itself.  A
    # lookup failure is fail-closed: the transition is refused, never
    # guessed through.
    gate, gate_error = _dependency_gate_evidence(conn, task_id)
    if gate_error is not None:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "dependency_recheck_failed",
            "error": gate_error,
        }
    # Invariant: ``_dependency_gate_evidence`` returns exactly one of a
    # gate dict or an error string, never both.  The error path returned
    # above, so a None gate here is a programming error — fail closed
    # rather than guess.
    if gate is None:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "dependency_recheck_failed",
            "error": "gate evidence unavailable",
        }
    guard = _dependency_guard_sql()
    parked_comment: Optional[str] = None
    if desired == "review":
        # DONE -> REVIEW (required PR open / closed-not-merged).
        new_body = row["body"]
        if context_block is not None:
            new_body = replace_sync_context(row["body"], context_block)
        parked_comment = _parking_comment_for_decision(task_id, decision)
        cur = conn.execute(
            f"""
            UPDATE tasks
               SET status = 'review',
                   completed_at = NULL,
                   assignee = NULL,
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   block_kind = NULL,
                   block_recurrences = 0,
                   body = ?
             WHERE id = ? AND status = ?
             {guard}
            """,
            (new_body, task_id, current_status, task_id),
        )
    else:
        # REVIEW -> DONE (every required PR merged into target branch).
        cur = conn.execute(
            f"""
            UPDATE tasks
               SET status = 'done',
                   completed_at = ?,
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   block_kind = NULL,
                   block_recurrences = 0
             WHERE id = ? AND status = ?
             {guard}
            """,
            (now, task_id, current_status, task_id),
        )
    if cur.rowcount != 1:
        if gate["pending"]:
            # The pre-gate passed but a parent went non-terminal — or a
            # new parent link appeared — between the pre-gate and this
            # write.  Refuse the transition and leave the card untouched;
            # the next tick repairs it through the canonical
            # _restore_pending_dependency lane.
            return {
                "task_id": task_id,
                "status": current_status,
                "changed": False,
                "reason": "dependency_recheck_pending",
                "evidence": {
                    "parents": gate["pending"],
                    "dependency_gate": "recheck_pending",
                },
            }
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }

    if desired == "review" and parked_comment is not None:
        # Same-transaction UX marker: the parked review card states why it
        # exists and that no human action is required. Appended once per
        # exact (task+reason+pr) line; a later merge/done transition leaves
        # it behind as durable provenance of why the card was parked.
        _append_parking_comment_if_absent(conn, task_id, parked_comment)

    payload = dict(evidence)
    payload.update(
        {
            "source": "github",
            "previous_status": current_status,
            "new_status": desired,
            "merge_authority": "human",
            "auto_merge": False,
        }
    )
    _append_sync_event(conn, task_id, payload)
    return {
        "task_id": task_id,
        "status": desired,
        "changed": True,
        "reason": decision.reason,
        "evidence": evidence,
    }


def _last_rework_event_at(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_events "
        "WHERE task_id = ? AND kind IN ('github_pr_rework', 'github_pr_rework_retry')",
        (task_id,),
    ).fetchone()
    value = row[0] if row else None
    return int(value) if value is not None else None


def apply_rework(
    conn: sqlite3.Connection,
    task_id: str,
    rework: ReworkDecision,
    ref: GithubTaskRef,
    *,
    retry_comment_id: Optional[int] = None,
) -> dict[str, Any]:
    """REVIEW|BLOCKED -> READY with the refreshed sync context and one event.

    Optimistic (``WHERE status = <current>``); the body update and the
    ``github_pr_rework`` event are in the same transaction as the
    transition.  BLOCKED sources additionally clear stale blocker
    metadata (block_kind / block_recurrences) in the same UPDATE.
    The caller owns the transaction.

    ``retry_comment_id`` marks the round as an explicit maintainer
    retry: the new ``github_pr_rework`` event carries
    ``trigger: "maintainer_retry"`` and the retry comment id, which
    permanently disqualifies that comment from ever opening another
    round (one-shot consumption).
    """
    row = conn.execute(
        "SELECT status, body, completed_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "changed": False, "reason": "task_missing"}
    current_status = str(row["status"])
    if current_status not in {"review", "blocked", "done"}:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    new_body = replace_sync_context(row["body"], rework.context_block)
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'ready',
               completed_at = NULL,
               assignee = NULL,
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               block_kind = NULL,
               block_recurrences = 0,
               body = ?
         WHERE id = ? AND status = ?
        """,
        (new_body, task_id, current_status),
    )
    if cur.rowcount != 1:
        return {
            "task_id": task_id,
            "status": current_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    count_row = conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework'",
        (task_id,),
    ).fetchone()
    rework_round = int(count_row[0]) + 1
    payload = {
        "previous_status": current_status,
        "new_status": "ready",
        "repository": ref.repository,
        "issue_number": ref.issue_number,
        "pr_number": rework.pr_number,
        "head_sha": rework.head_sha,
        "request_comment_id": rework.request_comment_id,
        "label_added_at": rework.label_added_at,
        "reason": "agent_rework",
        "rework_round": rework_round,
        "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
        "label_actor": rework.label_actor,
        "merge_authority": "human",
        "auto_merge": False,
        "source": "github",
    }
    if retry_comment_id is not None:
        payload["trigger"] = "maintainer_retry"
        payload["retry_comment_id"] = int(retry_comment_id)
    _append_sync_event(conn, task_id, payload, kind="github_pr_rework")
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            task_id,
            "kanban-main",
            "\n".join([
                "## Rework delivery contract",
                "",
                "Update the EXISTING linked PR only; do not create a new PR.",
                "Run the repository-required validation and publish the final head.",
                "After the remote PR head is verified, add the following exact machine-readable lines to the PR:",
                REWORK_COMPLETE_MARKER,
                f"task={task_id}",
                f"request_comment={rework.request_comment_id if rework.request_comment_id is not None else 'none'}",
                "head=<full 40-character PR head SHA>",
                "validation=passed",
                "Keep the existing human-readable final report in the same comment.",
            ]),
            int(time.time()),
        ),
    )
    return {
        "task_id": task_id,
        "status": "ready",
        "changed": True,
        "reason": "agent_rework",
        "rework_round": rework_round,
        "rework": rework.to_dict(),
    }


def _reconcile_blocked(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    row: Mapping[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """BLOCKED reconciliation (precedence: merged > rework > open PR > stay).

    Never mutates anything when ``dry_run`` is set.  GitHub failures fail
    closed: the task stays BLOCKED untouched.
    """
    task_id = str(row["id"])
    block_kind = row["block_kind"] if "block_kind" in row.keys() else None

    def _entry(reason: str, **extra: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "task_id": task_id,
            "status": "blocked",
            "changed": False,
            "reason": reason,
        }
        entry.update(extra)
        return entry

    if not decision.authoritative:
        return _entry(decision.reason, evidence=decision.to_dict())

    # Precedence 1: merged completion evidence is stronger than a stale
    # worker-created BLOCKED projection.
    if decision.desired_status == "done":
        if dry_run:
            return _entry("blocked_merged_done_predicted", evidence=decision.to_dict())
        try:
            with conn:
                result = apply_decision(
                    conn, task_id, decision, allow_blocked_source=True
                )
        except sqlite3.Error as exc:
            conn.rollback()
            return _entry("db_write_failed", error=f"{type(exc).__name__}: {exc}")
        return result

    # Precedence 2: one-shot trusted agent-rework on exactly one open PR.
    try:
        rework = evaluate_rework(
            client,
            ref,
            decision,
            current_status="blocked",
            last_rework_at=_last_rework_event_at(conn, task_id),
        )
    except GithubCompletionError as exc:
        rework = ReworkDecision(reason="rework_query_failed", error=str(exc), authoritative=False)

    if rework is not None and rework.authoritative and rework.reason == "agent_rework":
        if dry_run:
            return _entry(
                "blocked_rework_ready_predicted", rework=rework.to_dict()
            )
        try:
            with conn:
                result = apply_rework(conn, task_id, rework, ref)
        except sqlite3.Error as exc:
            conn.rollback()
            return _entry("db_write_failed", error=f"{type(exc).__name__}: {exc}")
        return result

    if rework is not None and rework.authoritative and rework.reason == "rework_claim_pending":
        result = _entry("rework_claim_pending", rework=rework.to_dict())
        return result

    if rework is not None and rework.authoritative and rework.reason == "multiple_rework_prs":
        # Ambiguity: fail closed, never guess which PR to rework.
        return _entry("multiple_rework_prs", rework=rework.to_dict())

    # Precedence 3: any open non-draft PR is a reviewable human handoff.
    if any(pr.state == "open" and not pr.draft for pr in decision.pull_requests):
        context_block: Optional[str] = None
        try:
            context_block = _build_context_block(client, ref, decision.pull_requests)
        except GithubCompletionError:
            context_block = None
        if dry_run:
            return _entry("blocked_open_pr_review_predicted", evidence=decision.to_dict())
        try:
            with conn:
                result = apply_decision(
                    conn,
                    task_id,
                    decision,
                    context_block=context_block,
                    allow_blocked_source=True,
                )
        except sqlite3.Error as exc:
            conn.rollback()
            return _entry("db_write_failed", error=f"{type(exc).__name__}: {exc}")
        return result

    # Precedence 4-6: staying BLOCKED — enforce durable GitHub evidence
    # and, for no-PR cards, the human resolution loop.
    try:
        issue_payload, _ = client.get(f"/repos/{ref.repository}/issues/{ref.issue_number}")
    except GithubCompletionError as exc:
        return _entry("github_query_failed", error=str(exc))
    if not isinstance(issue_payload, dict):
        return _entry("github_query_failed", error="invalid issue payload")
    # Re-attachment guard: a closed Issue must never receive a blocker
    # marker comment or the agent-blocked label again, and no blocked
    # resume may fire.  The merged-PR -> DONE precedence above is
    # untouched — completion transitions still apply to closed Issues.
    issue_state = str(issue_payload.get("state", "")).casefold()
    if issue_state != "open":
        return _entry("closed_issue_no_projection")
    try:
        issue_comments, _ = client.get(
            f"/repos/{ref.repository}/issues/{ref.issue_number}/comments"
        )
    except GithubCompletionError as exc:
        return _entry("github_query_failed", error=str(exc))
    if not isinstance(issue_comments, list):
        return _entry("github_query_failed", error="invalid issue comments payload")

    marker = _find_blocker_marker(issue_comments, task_id)
    baseline = str(marker.get("updated_at") or marker.get("created_at") or "") if marker else ""

    # Resolution loop: only for no-PR cards (#7).
    if not decision.pull_requests:
        resume = evaluate_blocked_resume(
            ref,
            decision,
            marker_comment=marker,
            issue_payload=issue_payload,
            issue_comments=issue_comments,
            last_resume_response_at=_last_resume_response_at(conn, task_id),
        )
        if resume.reason == "agent_blocked_resolved":
            if dry_run:
                return _entry("blocked_resume_ready_predicted", resume=resume.to_dict())
            try:
                with conn:
                    result = apply_blocked_resume(conn, task_id, resume, ref)
            except sqlite3.Error as exc:
                conn.rollback()
                return _entry("db_write_failed", error=f"{type(exc).__name__}: {exc}")
            return result
        if resume.reason == "resume_consumed":
            # One-shot guard hit: the same trusted response already resumed
            # this card once; after the worker crash it must not re-fire.
            # Keep the GitHub marker visible as consumed so the state stays
            # observable and a NEW maintainer comment re-enables resume.
            consumed_body = _resume_consumed_comment_body(
                task_id, resume.response_at, resume.response_author
            )
            if dry_run:
                return _entry("resume_consumed", resume=resume.to_dict())
            try:
                comment_action, comment_id = _upsert_blocker_comment(
                    client, ref, task_id, consumed_body
                )
            except GithubCompletionError as exc:
                return _entry("blocker_projection_failed", error=str(exc))
            return _entry(
                "resume_consumed",
                resume=resume.to_dict(),
                comment_action=comment_action,
                comment_id=comment_id,
            )

    # Durable evidence: the sync-owned marker comment (B), or visible
    # PR-side evidence (A): agent-rework label / trusted handoff content
    # on an open or draft PR.
    reason = _task_block_reason(conn, task_id)
    needs = _NEEDS_FROM_MAINTAINER.get(block_kind) or _NEEDS_FROM_MAINTAINER[None]
    body = _blocker_comment_body(
        task_id, reason, needs, no_pr=not decision.pull_requests
    )
    issue_labels = {
        str(item.get("name"))
        for item in issue_payload.get("labels", [])
        if isinstance(item, dict)
    }
    trusted_response = _trusted_response_after(issue_comments, baseline) if marker else None

    comment_action: str = "unchanged"
    label_action: str = "unchanged"
    if marker is None:
        try:
            pr_evidence = (
                _linked_pr_evidence(client, ref, decision.pull_requests)
                if decision.pull_requests
                else False
            )
        except GithubCompletionError as exc:
            return _entry("github_query_failed", error=str(exc))
        if pr_evidence:
            return _entry("blocked_evidence_ok")

    if marker is None or str(marker.get("body") or "") != body:
        if dry_run:
            return _entry("blocker_projection_predicted")
        try:
            comment_action, comment_id = _upsert_blocker_comment(
                client, ref, task_id, body
            )
        except GithubCompletionError as exc:
            return _entry("blocker_projection_failed", error=str(exc))
    else:
        comment_id = int(marker["id"]) if marker.get("id") is not None else None

    # Hold label maintenance: keep the visible hold while no trusted
    # maintainer response exists (a response means the label removal is
    # the maintainer's step 2 — never re-add then).
    if BLOCKED_LABEL not in issue_labels and trusted_response is None:
        if dry_run:
            label_action = "add_predicted"
        else:
            try:
                _add_issue_label(client, ref, BLOCKED_LABEL)
                label_action = "added"
            except GithubCompletionError as exc:
                return _entry("blocker_projection_failed", error=str(exc))

    if dry_run:
        return _entry(
            "blocker_projection_predicted",
            projection={"comment_action": comment_action, "label_action": label_action},
        )
    if comment_action != "unchanged":
        # GitHub projection already succeeded; a local event-record failure
        # must not abort the rest of the board — report and continue.
        try:
            with conn:
                _append_blocked_projection_event(
                    conn, task_id, ref, block_kind, comment_action, comment_id
                )
        except sqlite3.Error as exc:
            conn.rollback()
            return _entry(
                "blocker_projected_event_failed",
                projection_action=comment_action,
                comment_id=comment_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        return _entry(
            "blocker_projected",
            projection_action=comment_action,
            comment_id=comment_id,
            label_action=label_action,
        )
    return _entry("blocked_evidence_ok", label_action=label_action)


# ---------------------------------------------------------------------------
# Rework worker dispatch — edge-owned existing-PR rework lane
# ---------------------------------------------------------------------------
# The core dispatcher refuses to re-spawn a ``ready`` task whose comments
# mention a GitHub PR URL (``respawn_guarded`` reason ``active_pr``, 24h
# window).  That guard prevents duplicate PRs on one task and is left
# untouched.  A consumed ``agent-rework`` (the ``github_pr_rework`` event)
# is the operator's instruction to UPDATE THE EXISTING PR, so the respawn
# for exactly those tasks is owned here, in the edge: the sync claims the
# rework-pending task itself and launches its worker through the same core
# spawn helpers the dispatcher uses.  Every other ``ready`` task keeps the
# normal dispatcher protection — the bypass is scoped strictly to tasks
# whose governing transition is a consumed rework.
#
# Gated by the ``HERMES_KANBAN_REWORK_DISPATCH=1`` environment variable
# (set by the intake cron) so direct CLI invocations of this script stay
# pure reconciliation.

REWORK_DISPATCH_ENV = "HERMES_KANBAN_REWORK_DISPATCH"

# Event kinds that define which transition currently governs a task's
# state.  Everything else (assigned/spawned/claimed/heartbeat/
# respawn_guarded/commented/...) never supersedes a rework.
_REWORK_GOVERNING_KINDS = frozenset({
    "created", "changes_requested", "github_pr_rework", "github_pr_sync",
    "github_pr_rework_retry",
    "github_blocked_resolved", "github_blocked_projection",
    "blocked", "completed", "status", "promoted", "unblocked",
    "reclaimed", "scheduled", "archived",
})


def _kanban_config() -> dict[str, Any]:
    """Read the ``kanban:`` config section for the current HERMES_HOME."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        section = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _governing_event_kind(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Kind of the newest status-affecting event for a task (or None)."""
    kinds = tuple(sorted(_REWORK_GOVERNING_KINDS))
    placeholders = ",".join("?" * len(kinds))
    row = conn.execute(
        f"SELECT kind FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,) + kinds,
    ).fetchone()
    return str(row[0]) if row else None


def _latest_rework_event(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[tuple[dict[str, Any], int, str]]:
    """Return the latest edge rework event payload, timestamp, and kind."""
    row = conn.execute(
        "SELECT payload, created_at, kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('github_pr_rework', 'github_pr_rework_retry') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return (
        payload if isinstance(payload, dict) else {},
        int(row["created_at"] or 0),
        str(row["kind"]),
    )


def _task_run_after_rework(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
) -> Optional[sqlite3.Row]:
    """Return the newest finished run started by this rework round."""
    return conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, max(0, rework_at - 1)),
    ).fetchone()


def _run_metadata(run: Optional[sqlite3.Row]) -> dict[str, Any]:
    if run is None or not run["metadata"]:
        return {}
    try:
        value = json.loads(run["metadata"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _rework_head_candidates(run: Optional[sqlite3.Row]) -> set[str]:
    """Extract full SHA evidence from the durable worker run metadata."""
    metadata = _run_metadata(run)
    candidates: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child_value in value:
                visit(child_value, key)
        elif isinstance(value, str) and key.casefold() in {
            "head", "head_sha", "pr_head_sha", "commit", "sha", "oid",
        }:
            candidates.update(re.findall(r"\b[0-9a-fA-F]{40}\b", value))

    visit(metadata)
    text = "\n".join(
        str(run[column] or "")
        for column in ("summary", "error")
        if run is not None and column in run.keys()
    )
    candidates.update(re.findall(r"\b[0-9a-fA-F]{40}\b", text))
    return {item.casefold() for item in candidates}


def _completion_marker(
    client: Any,
    ref: GithubTaskRef,
    pr: GithubPullRequest,
    task_id: str,
    rework_at: int,
    request_comment_id: Optional[int],
) -> Optional[dict[str, Any]]:
    """Find a trusted, post-rework machine-readable delivery handoff."""
    comments = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr.number}/comments",
        {"per_page": 100},
    )
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        if author not in TRUSTED_GITHUB_ACTORS:
            continue
        created_at = _parse_iso_ts(comment.get("created_at"))
        if created_at is None or created_at < rework_at:
            continue
        body = str(comment.get("body") or "")
        if REWORK_COMPLETE_MARKER not in body:
            continue
        fields: dict[str, str] = {}
        for line in body.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
        head = fields.get("head", "").casefold()
        request_comment = fields.get("request_comment", "")
        if fields.get("task") != task_id or not re.fullmatch(r"[0-9a-f]{40}", head):
            continue
        if fields.get("validation") != "passed" or head != pr.head_sha.casefold():
            continue
        if request_comment_id is not None and request_comment != str(request_comment_id):
            continue
        return {
            "comment_id": comment.get("id"),
            "author": author,
            "created_at": created_at,
            "task": task_id,
            "request_comment": request_comment,
            "head": head,
            "validation": "passed",
        }
    return None


def _malformed_completion_marker(
    client: Any,
    ref: GithubTaskRef,
    pr: GithubPullRequest,
    task_id: str,
    rework_at: int,
    request_comment_id: Optional[int],
) -> Optional[tuple[int, list[str]]]:
    """Locate the newest trust-eligible completion comment that FAILED parsing.

    A comment that merely contains the ``AGENT_REWORK_COMPLETE`` marker
    string but omits the strict key=value contract (``task=``,
    ``request_comment=``, ``head=<40-hex>``, ``validation=passed``) is the
    exact recurrence this helper exists to expose (ctrl-hangul PR #74
    round 12: the marker was posted as a prose title line and the delivery
    was silently rejected).  Structural validity is checked only — a comment
    whose fields parse correctly is NOT malformed even when it cannot bind to
    the live head (that is a separate ``run_head_mismatch`` /
    ``rework_head_unchanged`` diagnostic, not a formatting defect).

    Returns ``(comment_id, missing_fields)`` for the newest malformed
    marker comment after the rework event, or ``None`` when no such comment
    exists.
    """
    comments = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr.number}/comments",
        {"per_page": 100},
    )
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        if author not in TRUSTED_GITHUB_ACTORS:
            continue
        created_at = _parse_iso_ts(comment.get("created_at"))
        if created_at is None or created_at < rework_at:
            continue
        body = str(comment.get("body") or "")
        if REWORK_COMPLETE_MARKER not in body:
            continue
        fields: dict[str, str] = {}
        for line in body.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
        missing: list[str] = []
        if fields.get("task") != task_id:
            missing.append("task")
        head = fields.get("head", "")
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            missing.append("head")
        if fields.get("validation") != "passed":
            missing.append("validation")
        if (
            request_comment_id is not None
            and fields.get("request_comment") != str(request_comment_id)
        ):
            missing.append("request_comment")
        if not missing:
            return None  # a structurally valid marker exists on the PR
        comment_id = comment.get("id")
        if comment_id is None:
            continue
        return comment_id, missing
    return None


def _rework_round_is_maintainer_retry(
    payload: Mapping[str, Any],
) -> bool:
    """True when the current round is an explicit trusted-maintainer retry.

    ``apply_rework`` writes ``trigger: "maintainer_retry"`` plus a
    ``retry_comment_id`` onto the ``github_pr_rework`` event that opens such
    a round.  That round was opened by a trusted, one-shot
    ``AGENT_REWORK_RETRY`` signal (``_consume_explicit_rework_retry``), which
    is the authoritative proof that a previous delivery was rejected for a
    protocol/handoff reason and a human re-opened it to verify or re-post the
    handoff rather than to change code.
    """
    return (
        str(payload.get("trigger") or "") == "maintainer_retry"
        and payload.get("retry_comment_id") is not None
    )


def _rework_run_is_review_requested(run: Optional[sqlite3.Row]) -> bool:
    """True when the active/latest run ended with a review-requested outcome.

    A terminal ``review_requested`` outcome is the worker's explicit statement
    that human review is needed (distinct from an ordinary crash/error, which
    carries a ``crashed``/error outcome).  It is only consulted in the
    not-delivered branch — i.e. no valid completion marker was accepted for
    the round — so it never reinterprets a run whose delivery already landed.
    """
    return (
        run is not None
        and "outcome" in run.keys()
        and str(run["outcome"] or "") == "review_requested"
    )


def _rework_delivery_evidence(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    task_id: str,
    pr: GithubPullRequest,
    event: tuple[dict[str, Any], int, str],
) -> tuple[bool, str, dict[str, Any]]:
    """Check the complete remote-delivery contract without mutating state."""
    payload, rework_at, _ = event
    # A pending newer rework request (a trusted agent-rework label addition
    # postdating this round's governing event) means this round is spent:
    # its delivery can no longer project REVIEW-READY because that would
    # swallow the maintainer's newer request (t_aff9017c incident).  The
    # caller repairs the card through the classic DONE -> REVIEW path and
    # the classic intake owns the new round on a later tick.
    if _label_is_newer_than_event(
        client,
        ref,
        pr.number,
        rework_at,
        current_label_at=payload.get("label_added_at"),
    ):
        return False, "delivery_superseded_by_new_rework", {"rework_at": rework_at}
    run = _task_run_after_rework(conn, task_id, rework_at)
    if run is None or run["ended_at"] is None:
        return False, "delivery_run_missing", {"rework_at": rework_at}
    if str(run["outcome"] or "") not in {"completed", "blocked", "review_requested"}:
        return False, "delivery_run_failed", {
            "outcome": run["outcome"], "run_id": run["id"],
        }
    marker = _completion_marker(
        client,
        ref,
        pr,
        task_id,
        rework_at,
        int(payload["request_comment_id"])
        if payload.get("request_comment_id") is not None
        else None,
    )
    if marker is None:
        malformed = _malformed_completion_marker(
            client,
            ref,
            pr,
            task_id,
            rework_at,
            int(payload["request_comment_id"])
            if payload.get("request_comment_id") is not None
            else None,
        )
        if malformed is not None:
            comment_id, missing_fields = malformed
            return False, "completion_marker_malformed", {
                "run_id": run["id"],
                "completion_comment_id": comment_id,
                "missing_fields": missing_fields,
            }
        return False, "completion_handoff_missing", {"run_id": run["id"]}
    run_heads = _rework_head_candidates(run)
    if run_heads and marker["head"] not in run_heads:
        return False, "run_head_mismatch", {
            "run_id": run["id"], "run_heads": sorted(run_heads),
            "marker_head": marker["head"],
        }
    requested_head = str(payload.get("head_sha") or "").casefold()
    if requested_head and requested_head == marker["head"]:
        # The marker claims the very head this round started from.  That is a
        # genuine no-op (a worker asked to change something produced no
        # relevant change) and MUST stay rejected — UNLESS the round is an
        # explicit trusted-maintainer retry opened to verify or re-post the
        # handoff after a protocol-only rejection.
        #
        # Verification-only acceptance is deliberately conservative: it is
        # allowed only when ALL of the following already hold (checked above)
        # and are re-stated here:
        #   * the round is a maintainer retry (trigger + retry_comment_id) —
        #     i.e. a trusted, one-shot AGENT_REWORK_RETRY re-opened it;
        #   * the completion marker bound to this task, with validation=passed,
        #     whose head equals the LIVE PR head exactly (a stale historical
        #     marker cannot close the round: it either predates the round's
        #     governing event or fails the run-head binding);
        #   * the active/latest run for the round carried that same head, so
        #     the round's worker provably observed and attested the current
        #     head (the requested rework is already present there).
        # Anything ambiguous — no trusted retry, a run that did not reach the
        # live head, or a marker not bound to the live head — fails closed to
        # the no-op rejection.
        # run_heads (computed above) already guarantees marker["head"] is in
        # run_heads whenever run_heads is non-empty (otherwise the earlier
        # run_head_mismatch check returned).  So a run that attested the live
        # head — required for a verification-only acceptance — is exactly the
        # case where run_heads is non-empty.  An empty run_heads (the round's
        # worker attested no head) fails closed.
        if (
            _rework_round_is_maintainer_retry(payload)
            and bool(run_heads)
        ):
            evidence = {
                "run_id": run["id"],
                "run_outcome": run["outcome"],
                "request_comment_id": payload.get("request_comment_id"),
                "head": marker["head"],
                "validation": marker["validation"],
                "completion_comment_id": marker.get("comment_id"),
                "verification_only": True,
                "retry_comment_id": payload.get("retry_comment_id"),
            }
            return True, "delivery_complete_verification_only", evidence
        return False, "rework_head_unchanged", {
            "requested_head": requested_head, "head": marker["head"],
        }
    evidence = {
        "run_id": run["id"],
        "run_outcome": run["outcome"],
        "request_comment_id": payload.get("request_comment_id"),
        "head": marker["head"],
        "validation": marker["validation"],
        "completion_comment_id": marker.get("comment_id"),
    }
    return True, "delivery_complete", evidence


def _project_pr_lifecycle_labels(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> tuple[bool, str, dict[str, Any]]:
    """Atomically reconcile the three lifecycle labels on one PR."""
    current = _pr_labels(client, ref.repository, pr_number)
    lifecycle = {REWORK_LABEL, WORKING_LABEL, REVIEW_READY_LABEL}
    desired = (current - set(remove))
    desired.difference_update(set(remove) & lifecycle)
    desired.update(str(label) for label in add)
    if desired == current:
        return True, "labels_unchanged", {"labels": sorted(current)}
    status, _ = client.patch(
        f"/repos/{ref.repository}/issues/{pr_number}",
        {"labels": sorted(desired)},
    )
    if not 200 <= status < 300:
        raise GithubCompletionError(
            f"could not reconcile lifecycle labels (HTTP {status})"
        )
    observed = _pr_labels(client, ref.repository, pr_number)
    if observed != desired:
        raise GithubCompletionError(
            "lifecycle label read-back mismatch"
        )
    return True, "labels_updated", {
        "before": sorted(current), "after": sorted(observed),
    }


def _append_rework_retry_event(
    conn: sqlite3.Connection,
    task_id: str,
    payload: Mapping[str, Any],
    *,
    reason: str,
) -> None:
    retry_payload = dict(payload)
    retry_payload.update({
        "reason": "agent_rework",
        "retry": True,
        "retry_reason": reason,
        "source": "github_edge_rework_recovery",
    })
    _append_sync_event(conn, task_id, retry_payload, kind="github_pr_rework_retry")


def _rework_human_attention(reason: str, run: Optional[sqlite3.Row]) -> bool:
    text = " ".join(
        str(run[column] or "")
        for column in ("summary", "error")
        if run is not None and column in run.keys()
    ).casefold()
    if _rework_run_is_review_requested(run):
        # A terminal review_requested outcome is itself a strong human-attention
        # signal: the worker explicitly requested human review and no valid
        # completion marker was accepted for this round.  Do not fall through
        # to an automatic requeue just because the summary text happened not
        # to contain one of the keyword markers.  Ordinary crashes carry a
        # 'crashed'/error outcome (never 'review_requested'), so they remain
        # recoverable and requeued.
        return True
    return any(marker in text for marker in (
        "review-required", "needs_input", "needs maintainer",
        "human review", "host_validation_required", "human_validation_required",
    )) or reason in {
        "delivery_run_missing", "completion_handoff_missing",
        "completion_marker_malformed",
    }


def _rework_context(
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    task_id: str,
    event: Optional[tuple[dict[str, Any], int, str]],
) -> Optional[dict[str, Any]]:
    """Build the exact PR/label context used by lifecycle and dispatch."""
    if event is None:
        return None
    payload, event_at, event_kind = event
    raw_pr_number = payload.get("pr_number")
    try:
        pr_number = int(raw_pr_number) if raw_pr_number is not None else None
    except (TypeError, ValueError):
        pr_number = None
    prs = tuple(
        pr for pr in decision.pull_requests
        if pr_number is None or pr.number == pr_number
    )
    if len(prs) != 1:
        return None
    pr = prs[0]
    # The PR comes from the current decision, freshly fetched from
    # ``/pulls/{n}`` in this sync run: existence is authoritative.
    labels = _pr_labels(client, ref.repository, pr.number, pr_exists=True)
    return {
        "task_id": task_id,
        "repository": ref.repository,
        "issue_number": ref.issue_number,
        "pr_number": pr.number,
        "head_sha": pr.head_sha,
        "pr": pr,
        "labels": labels,
        "event": event,
        "event_kind": event_kind,
        "event_at": event_at,
        "implicit_request": payload.get("trigger") == "changes_requested",
    }


def _task_has_active_rework_claim(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> bool:
    status = (
        str(row["status"])
        if "status" in row.keys() and row["status"] is not None
        else ""
    )
    if status != "running":
        return False
    claim_lock = row["claim_lock"] if "claim_lock" in row.keys() else None
    run_id = row["current_run_id"] if "current_run_id" in row.keys() else None
    if not claim_lock or run_id is None:
        return False
    run = conn.execute(
        "SELECT ended_at FROM task_runs WHERE id = ? AND task_id = ?",
        (int(run_id), str(row["id"])),
    ).fetchone()
    return run is not None and run["ended_at"] is None


def _record_rework_attention(
    conn: sqlite3.Connection,
    task_id: str,
    context: Mapping[str, Any],
    *,
    reason: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> bool:
    event = context.get("event")
    payload = dict(event[0]) if isinstance(event, tuple) else {}
    payload.update({
        "reason": "rework_human_attention",
        "diagnostic": reason,
        "source": "github_edge_rework_reconciliation",
    })
    if evidence:
        payload["evidence"] = dict(evidence)
    existing = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'github_pr_rework_attention' AND payload LIKE ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, f"%{reason}%"),
    ).fetchone()
    if existing is not None:
        return False  # already recorded for this round/diagnostic
    with conn:
        attention_body = "\n".join([
            f"{REWORK_ATTENTION_MARKER} task={task_id} reason={reason}",
            "",
            "This rework round could not be completed autonomously. The task stays",
            "BLOCKED and the PR keeps the agent-rework label; no automatic retry",
            "will start.",
            "",
            "To explicitly start a NEW rework round, post a comment on the linked",
            "PR (by a trusted maintainer, after this attention record) containing",
            "exactly the following lines:",
            "",
            REWORK_RETRY_MARKER,
            f"task={task_id}",
            "",
            "The retry signal is consumed exactly once for this task.",
        ])
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                task_id,
                "kanban-main",
                attention_body,
                int(time.time()),
            ),
        )
        _append_sync_event(
            conn,
            task_id,
            payload,
            kind="github_pr_rework_attention",
        )
    return True


def _post_rework_attention_pr_comment(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    task_id: str,
    reason: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Post one idempotent machine-readable attention comment on the PR.

    The Kanban-side attention record (task_comments + task_events) is
    invisible on GitHub.  Without PR feedback a rejected completion marker
    leaves the worker/reviewer blind and the round stalls silently
    (ctrl-hangul PR #74 round 12 regression: the malformed marker was never
    answered on the PR and the hold sat for two days).  The comment is
    posted at most once per (task, reason) and never changes any state — the
    fail-closed hold (BLOCKED card + restored agent-rework label, no
    automatic retry) is untouched.  The exact regeneration templates are
    included so the next round can proceed without guessing.

    Returns True when a new comment was posted, False when it already
    exists; raises :class:`GithubCompletionError` on API failure (callers
    degrade to the existing hold without aborting the reconciliation).
    """
    existing = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr_number}/comments",
        {"per_page": 100},
    )
    needle = f"{REWORK_ATTENTION_MARKER} task={task_id} reason={reason}"
    for comment in existing:
        if isinstance(comment, dict) and needle in str(comment.get("body") or ""):
            return False
    lines = [
        f"{REWORK_ATTENTION_MARKER} task={task_id} reason={reason}",
        "",
        "This rework round's delivery could not be accepted automatically. The "
        "Kanban task stays BLOCKED and the PR keeps the agent-rework label; no "
        "automatic retry will start.",
    ]
    missing = evidence.get("missing_fields") if isinstance(evidence, dict) else None
    if isinstance(missing, list) and missing:
        lines += [
            "",
            "The newest completion comment contains AGENT_REWORK_COMPLETE but "
            "failed the strict machine-readable contract. "
            "Missing/invalid fields: " + ", ".join(str(x) for x in missing) + ".",
        ]
    lines += [
        "",
        "Re-post the completion handoff on this PR with a comment whose first "
        "line is exactly AGENT_REWORK_COMPLETE, followed by these lines:",
        "",
        REWORK_COMPLETE_MARKER,
        f"task={task_id}",
        "request_comment=<github_comment_id | none>",
        "head=<full 40-char PR head SHA>",
        "validation=passed",
        "",
        "To open a NEW rework round instead, a trusted maintainer posts:",
        "",
        REWORK_RETRY_MARKER,
        f"task={task_id}",
        "",
        "Each signal is consumed exactly once.",
    ]
    status, _ = client.post(
        f"/repos/{ref.repository}/issues/{pr_number}/comments",
        {"body": "\n".join(lines)},
    )
    if not 200 <= status < 300:
        raise GithubCompletionError(
            f"could not post rework attention comment (HTTP {status})"
        )
    return True


def _rework_provenance_attention(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    row: Mapping[str, Any],
    decision: GithubCompletionDecision,
    lifecycle_event: tuple[dict[str, Any], int, str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fail-closed attention for a consumed round whose provenance is broken.

    A consumed ``github_pr_rework`` event whose ``pr_number`` does not
    resolve to exactly one canonical open PR (missing, ambiguous,
    recreated or mismatched PR, wrong task) still leaves the BLOCKED card
    owned by that round.  The card is never projected to REVIEW and never
    reaches the generic blocked reconciliation: no ``github_pr_sync``, no
    ``github_pr_rework_delivery``, and no label mutation — an active
    worker keeps ``agent-working`` untouched.  One attention record is
    kept per diagnostic so a repeated reconciliation stays idempotent.
    """
    task_id = str(row["id"])
    status = str(row["status"])
    payload, _event_at, _event_kind = lifecycle_event
    raw_pr_number = payload.get("pr_number")
    try:
        pr_number = int(raw_pr_number) if raw_pr_number is not None else None
    except (TypeError, ValueError):
        pr_number = None
    matching = tuple(
        pr for pr in decision.pull_requests
        if pr_number is None or pr.number == pr_number
    )
    diagnostic = (
        "rework_pr_unresolved" if raw_pr_number is not None
        else "rework_pr_ambiguous"
    )
    evidence = {
        "round_pr_number": raw_pr_number,
        "decision_pr_numbers": sorted(pr.number for pr in decision.pull_requests),
        "matching_prs": len(matching),
        "head_sha": payload.get("head_sha"),
        "request_comment_id": payload.get("request_comment_id"),
    }
    if dry_run:
        return {
            "task_id": task_id,
            "status": status,
            "changed": False,
            "reason": "rework_human_attention_predicted",
            "diagnostic": diagnostic,
            "evidence": evidence,
        }
    _record_rework_attention(
        conn, task_id, {"event": lifecycle_event},
        reason=diagnostic, evidence=evidence,
    )
    return {
        "task_id": task_id,
        "status": status,
        "changed": False,
        "reason": "rework_human_attention",
        "diagnostic": diagnostic,
        "evidence": evidence,
    }


_OPERATOR_ATTENTION_REASONS = frozenset({
    "rework_retry_blocked",
    "rework_dispatch_failed",
    "claim_projection_reclaim_failed",
    "workspace_resolve_failed",
    "spawn_failed",
    "lifecycle_label_conflict",
    "rework_context_failed",
    "delivery_query_failed",
    "review_ready_label_projection_failed",
    "merged_lifecycle_cleanup_failed",
    "stale_review_ready_normalize_failed",
    "dispatch_lock_failed",
    "dispatch_lock_unavailable",
})
_OPERATOR_ATTENTION_MARKERS = (
    "needs_input",
    "needs maintainer",
    "review-required",
    "host_validation_required",
    "human_validation_required",
    "human review",
)


def _operator_attention_reason(entry: Mapping[str, Any]) -> Optional[str]:
    reason = str(entry.get("reason") or "")
    if reason == "rework_human_attention":
        return reason
    if reason in _OPERATOR_ATTENTION_REASONS:
        return reason
    block_kind = str(entry.get("block_kind") or "")
    if block_kind in {"needs_input", "capability"} and str(entry.get("status") or "") == "blocked":
        return block_kind
    evidence = entry.get("evidence")
    evidence_reason = evidence.get("reason") if isinstance(evidence, Mapping) else ""
    text = " ".join(
        str(value or "")
        for value in (reason, entry.get("diagnostic"), entry.get("retry_reason"), entry.get("error"), evidence_reason)
    ).casefold()
    if any(marker in text for marker in _OPERATOR_ATTENTION_MARKERS):
        return reason or "human_attention_required"
    rework = entry.get("rework")
    for value in (rework, entry):
        if not isinstance(value, Mapping):
            continue
        try:
            if int(value.get("rework_round") or 0) >= 3:
                return "rework_threshold_exceeded"
        except (TypeError, ValueError):
            continue
    return None


def _record_operator_attention(
    conn: sqlite3.Connection,
    entry: dict[str, Any],
) -> bool:
    """Record one deduped operator-attention event in existing task_events."""
    reason = _operator_attention_reason(entry)
    task_id = str(entry.get("task_id") or "")
    repository = str(entry.get("repository") or "")
    issue_number = entry.get("issue_number")
    if reason is None or not task_id or not repository or not issue_number:
        return False
    # Exclude prior operator-attention rows from the cursor. A new ordinary
    # lifecycle event therefore permits a later recurrence, while an unchanged
    # incident remains quiet on every five-minute tick.
    cursor_row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_events "
        "WHERE task_id = ? AND kind != 'github_operator_attention'",
        (task_id,),
    ).fetchone()
    cursor = int(cursor_row[0] or 0)
    key = f"{reason}:{cursor}"
    existing = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'github_operator_attention' "
        "AND payload LIKE ? LIMIT 1",
        (task_id, f"%attention_key%{key}%"),
    ).fetchone()
    if existing is not None:
        return False
    payload = {
        "reason": reason,
        "attention_key": key,
        "repository": repository,
        "issue_number": int(issue_number),
        "previous_status": entry.get("from_state"),
        "new_status": entry.get("to_state") or entry.get("status"),
        "source": "github_edge_operator_attention",
    }
    with conn:
        _append_sync_event(conn, task_id, payload, kind="github_operator_attention")
    return True


def _restore_rework_labels(
    client: Any,
    context: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    pr = context.get("pr")
    if not isinstance(pr, GithubPullRequest):
        raise GithubCompletionError("rework context has no pull request")
    _, reason, evidence = _project_pr_lifecycle_labels(
        client,
        GithubTaskRef(
            str(context["repository"]),
            int(context["issue_number"]),
        ),
        int(pr.number),
        add=(REWORK_LABEL,),
        remove=(WORKING_LABEL, REVIEW_READY_LABEL),
    )
    return reason, evidence


def _retry_failure_limit(cfg: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    section = cfg if cfg is not None else _kanban_config()
    try:
        value = section.get("failure_limit")
        return int(value) if value is not None and int(value) > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _requeue_rework_task(
    conn: sqlite3.Connection,
    kanban_db: Any,
    task_id: str,
    context: Mapping[str, Any],
    *,
    reason: str,
    failure_limit: Optional[int],
) -> dict[str, Any]:
    """Return a failed/incomplete rework to READY without bypassing the breaker."""
    event = context["event"]
    payload = dict(event[0])
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "status": None, "changed": False, "reason": "task_missing"}
    with conn:
        conn.execute(
            """
            UPDATE tasks
               SET status = 'ready', completed_at = NULL, assignee = NULL,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   block_kind = NULL,
                   block_recurrences = 0, last_heartbeat_at = NULL
             WHERE id = ? AND status IN ('done', 'review', 'blocked', 'running', 'ready')
            """,
            (task_id,),
        )
        _append_rework_retry_event(conn, task_id, payload, reason=reason)
    auto_blocked = bool(kanban_db._record_task_failure(
        conn,
        task_id,
        f"rework delivery retry: {reason}",
        outcome="rework_delivery_failed",
        failure_limit=failure_limit,
        release_claim=False,
        end_run=False,
    ))
    return {
        "task_id": task_id,
        "status": "blocked" if auto_blocked else "ready",
        "changed": True,
        "reason": "rework_retry_scheduled" if not auto_blocked else "rework_retry_blocked",
        "retry_reason": reason,
        "auto_blocked": auto_blocked,
    }


def _label_is_newer_than_event(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    event_at: int,
    current_label_at: Optional[int] = None,
) -> bool:
    """True when a newer agent-rework label addition follows this round.

    The governing event is written a few seconds AFTER the label is observed,
    so comparing only against ``event_at`` falsely classifies the current
    round's own label as a newer request.  Persisted ``label_added_at`` binds
    the event to the label that opened the round; legacy events fall back to
    the event timestamp.
    """
    events = _labeled_events(client, ref, pr_number)
    if not events:
        return False
    latest_label_at, _ = max(events, key=lambda item: item[0])
    baseline = max(int(event_at), int(current_label_at or 0))
    return latest_label_at > baseline


def _latest_delivery_head(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[str]:
    """Head SHA of the newest recorded rework delivery event (idempotency)."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'github_pr_rework_delivery' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    head = payload.get("head") if isinstance(payload, dict) else None
    return str(head).casefold() if head else None


def _current_round_delivery(
    conn: sqlite3.Connection,
    task_id: str,
    event: tuple[dict[str, Any], int, str],
) -> Optional[dict[str, Any]]:
    """Newest delivery event bound to the CURRENT rework round.

    A delivery is current-round evidence only when ALL of:
    - it was recorded at/after the round's governing ``github_pr_rework``
      event (inclusive, so same-second ticks cannot lose the round);
    - when both the round event and the delivery carry a request-comment
      identity, the identities match;
    - its head differs from the head the current round started from (a
      *fresh* round's worker has not yet pushed, so a recorded head equal
      to the requested head is a PAST round's delivery).  Verification-only
      / handoff-repair rounds (trusted ``AGENT_REWORK_RETRY``,
      ``trigger == "maintainer_retry"``) are exempt from this same-head
      rule because their requested rework is already present at the live
      head and a same-head completion is the expected current-round
      delivery — the identity and time bounds above still exclude a
      genuinely past-round delivery.

    Past-round deliveries (e.g. the round N-1 head still equal to the
    live PR head while the round-N worker has not yet pushed) never
    qualify, so an active current-round worker keeps ``agent-working``
    and a past delivery can never justify ``agent-review-ready``.
    """
    payload, event_at, _ = event
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_delivery' "
        "AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, int(event_at)),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        delivery_payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(delivery_payload, dict):
        return None
    round_request_id = payload.get("request_comment_id")
    delivery_request_id = delivery_payload.get("request_comment_id")
    if (
        round_request_id is not None
        and delivery_request_id is not None
        and int(round_request_id) != int(delivery_request_id)
    ):
        # Identity mismatch: the recorded delivery belongs to another round.
        return None
    round_requested_head = str(payload.get("head_sha") or "").casefold()
    delivery_head = str(delivery_payload.get("head") or "").casefold()
    if round_requested_head and delivery_head == round_requested_head:
        # The recorded delivery head equals the head this round started
        # from.  For a *fresh* rework round that means the round's worker
        # has not yet pushed a new head, so the recorded same-head delivery
        # is the previous round's delivery.  For a verification-only /
        # handoff-repair round (trusted maintainer retry) the requested
        # rework is already present at the live head, so a same-head
        # completion is the *expected* current-round delivery, not a stale
        # one.  The identity bound (request_comment_id match) and the time
        # bound (delivery at/after this round's event) still exclude a
        # genuinely past-round delivery, so exempting same-head for
        # maintainer-retry rounds is safe.
        if payload.get("trigger") == "maintainer_retry":
            return delivery_payload
        # The recorded delivery head equals the head this round started
        # from: it is the previous round's delivery, not current-round
        # evidence.
        return None
    return delivery_payload


def _last_delivery_event_at(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[int]:
    """Epoch time of the newest recorded rework delivery event, if any."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_delivery'",
        (task_id,),
    ).fetchone()
    value = row[0] if row else None
    return int(value) if value is not None else None


def _latest_rework_label_at(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
) -> Optional[int]:
    """Epoch time of the newest agent-rework label addition, if any."""
    events = _labeled_events(client, ref, pr_number)
    if not events:
        return None
    added_at, _ = max(events, key=lambda item: item[0])
    return added_at


def _delivery_review_transition(
    conn: sqlite3.Connection,
    task_id: str,
    current_status: str,
    ref: GithubTaskRef,
    pr_number: int,
    evidence: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Optimistic running/ready -> review once the delivery marker validates."""
    with conn:
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'review', completed_at = NULL, assignee = NULL,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   block_kind = NULL, block_recurrences = 0
             WHERE id = ? AND status = ?
            """,
            (task_id, current_status),
        )
        if cur.rowcount != 1:
            return None
        _append_sync_event(
            conn,
            task_id,
            {
                "previous_status": current_status,
                "new_status": "review",
                "reason": "agent_review_ready",
                "repository": ref.repository,
                "pr_number": pr_number,
                **dict(evidence),
            },
            kind="github_pr_rework_delivery",
        )
    return {
        "task_id": task_id,
        "status": "review",
        "changed": True,
        "reason": "agent_review_ready",
        "evidence": dict(evidence),
    }


def _normalize_stale_review_ready(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    task_id: str,
    status: str,
    labels: set[str],
    *,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Remove a stale ``agent-review-ready`` superseded by a newer rework.

    A new trusted ``agent-rework`` request that postdates the previous
    delivery makes the delivered round's ``agent-review-ready`` stale.
    Only that single label is removed (``agent-rework`` is kept so the
    classic REVIEW -> READY intake path or the dispatch lane owns the new
    round on a later tick).  Returns ``None`` — keeping the fail-closed
    lifecycle conflict guard — unless the newest ``agent-rework`` label
    addition provably postdates the last ``github_pr_rework_delivery``
    event.
    """
    delivery_at = _last_delivery_event_at(conn, task_id)
    request_at = _latest_rework_label_at(client, ref, pr_number)
    if delivery_at is None or request_at is None or request_at <= delivery_at:
        return None
    if dry_run:
        return {
            "task_id": task_id,
            "status": status,
            "changed": False,
            "reason": "stale_review_ready_normalized_predicted",
            "lifecycle": {"labels": sorted(labels)},
        }
    _, label_reason, label_evidence = _project_pr_lifecycle_labels(
        client,
        ref,
        pr_number,
        add=(REWORK_LABEL,),
        remove=(REVIEW_READY_LABEL,),
    )
    print(
        f"kanban-github-sync: stale agent-review-ready removed for "
        f"{ref.repository}#{pr_number} task={task_id} ({label_reason})",
        file=sys.stderr,
    )
    return {
        "task_id": task_id,
        "status": status,
        "changed": False,
        "reason": "stale_review_ready_normalized",
        "label_action": label_reason,
        "lifecycle": label_evidence,
    }


def _last_rework_attention_at(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[int]:
    """Epoch time of the newest rework attention record, if any."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_attention'",
        (task_id,),
    ).fetchone()
    value = row[0] if row else None
    return int(value) if value is not None else None


def _current_rework_attention_at(
    conn: sqlite3.Connection,
    task_id: str,
    event: tuple[dict[str, Any], int, str],
) -> Optional[int]:
    """Return the current round's attention timestamp, if one exists."""
    event_payload, event_at, _ = event
    round_number = event_payload.get("rework_round")
    pr_number = event_payload.get("pr_number")
    rows = conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_attention' "
        "ORDER BY created_at DESC, id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        created_at = int(row["created_at"] or 0)
        if created_at < int(event_at):
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if round_number is not None and payload.get("rework_round") != round_number:
            continue
        if pr_number is not None and payload.get("pr_number") != pr_number:
            continue
        return created_at
    return None


def _consumed_retry_comment_ids(
    conn: sqlite3.Connection,
    task_id: str,
) -> set[int]:
    """Retry comment ids already consumed by any rework round of this task."""
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'github_pr_rework'",
        (task_id,),
    ).fetchall()
    consumed: set[int] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("retry_comment_id")
        if raw is None:
            continue
        try:
            consumed.add(int(raw))
        except (TypeError, ValueError):
            continue
    return consumed


def _find_rework_retry_signal(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    task_id: str,
    *,
    baseline_at: int,
    consumed_ids: set[int],
) -> Optional[dict[str, Any]]:
    """Newest trusted explicit retry comment on the current PR.

    A retry signal is a PR comment by a TRUSTED_GITHUB_ACTORS maintainer
    containing the exact ``AGENT_REWORK_RETRY`` line plus a
    ``task=<task_id>`` line, created strictly after ``baseline_at`` (the
    later of the governing rework event and the last attention record),
    and never consumed before.  Everything else — untrusted author,
    missing/wrong task binding, malformed marker, pre-attention timing,
    or an already-consumed comment — is ignored (fails closed).
    """
    comments = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr_number}/comments",
        {"per_page": 100},
    )
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        if author not in TRUSTED_GITHUB_ACTORS:
            continue
        created_at = _parse_iso_ts(comment.get("created_at"))
        if created_at is None or created_at <= baseline_at:
            continue
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or comment_id in consumed_ids:
            continue
        body = str(comment.get("body") or "")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines != [REWORK_RETRY_MARKER, f"task={task_id}"]:
            continue
        return {
            "comment_id": comment_id,
            "author": author,
            "created_at": created_at,
        }
    return None


def _has_fresh_retry_comment(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    task_id: str,
    row: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """Cheap existence probe for an unconsumed trusted retry comment.

    Mirrors the baseline/consumption rules of
    ``_consume_explicit_rework_retry`` (postdates the round event, the last
    attention record, and the provisional completion; never consumed) but
    without re-verifying the source Issue — used purely as a gate so a DONE
    card without any retry signal keeps flowing through the ordinary repair
    paths.
    """
    event = context["event"]
    _, event_at, _ = event
    baseline_at = max(
        int(event_at or 0),
        _last_rework_attention_at(conn, task_id) or 0,
    )
    row_completed_at = (
        row.get("completed_at") if "completed_at" in row.keys() else None
    )
    if row_completed_at:
        try:
            baseline_at = max(baseline_at, int(row_completed_at))
        except (TypeError, ValueError):
            pass
    try:
        return _find_rework_retry_signal(
            client,
            ref,
            int(context["pr_number"]),
            task_id,
            baseline_at=baseline_at,
            consumed_ids=_consumed_retry_comment_ids(conn, task_id),
        ) is not None
    except GithubCompletionError:
        return False


def _consume_explicit_rework_retry(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    task_id: str,
    row: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Start a NEW rework round from an explicit maintainer retry.

    Runs for a card held by a consumed rework round's attention hold
    (``rework_human_attention``) or parked in a false-terminal DONE while
    its linked PR is still OPEN.  When a fresh trusted
    ``AGENT_REWORK_RETRY`` comment exists, the held/spent round is closed
    and a new ``github_pr_rework`` event (``trigger: maintainer_retry``)
    is written through the classic ``apply_rework`` intake, so the edge
    dispatch lane claims the new round exactly like a label-requested
    round.  Returns ``None`` when no valid retry signal exists — the
    caller keeps its fail-closed / repair path.
    """
    event = context["event"]
    _, event_at, _ = event
    row_completed_at = row.get("completed_at") if "completed_at" in row.keys() else None
    baseline_at = max(
        int(event_at or 0),
        _last_rework_attention_at(conn, task_id) or 0,
    )
    if row_completed_at:
        try:
            baseline_at = max(baseline_at, int(row_completed_at))
        except (TypeError, ValueError):
            pass
    consumed_ids = _consumed_retry_comment_ids(conn, task_id)
    try:
        retry_comment = _find_rework_retry_signal(
            client,
            ref,
            int(context["pr_number"]),
            task_id,
            baseline_at=baseline_at,
            consumed_ids=consumed_ids,
        )
    except GithubCompletionError as exc:
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "retry_signal_query_failed", "error": str(exc),
        }
    if retry_comment is None:
        return None
    # Re-verify the classic intake conditions: source Issue OPEN and
    # agent-ready.  A closed Issue must never start a new round.
    try:
        issue_payload, _ = client.get(
            f"/repos/{ref.repository}/issues/{ref.issue_number}"
        )
    except GithubCompletionError as exc:
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "retry_signal_query_failed", "error": str(exc),
        }
    if not isinstance(issue_payload, dict):
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "retry_signal_query_failed",
            "error": "invalid issue payload",
        }
    issue_state = str(issue_payload.get("state", "")).casefold()
    issue_labels = {
        str(item.get("name"))
        for item in issue_payload.get("labels", [])
        if isinstance(item, dict)
    }
    if issue_state != "open" or AGENT_READY_LABEL not in issue_labels:
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "retry_issue_not_agent_ready",
            "issue_state": issue_state,
            "issue_agent_ready": AGENT_READY_LABEL in issue_labels,
            "retry_comment_id": retry_comment["comment_id"],
        }
    retry_comment_id = int(retry_comment["comment_id"])
    if dry_run:
        return {
            "task_id": task_id, "status": "ready", "changed": False,
            "reason": "maintainer_retry_predicted",
            "retry_comment_id": retry_comment_id,
            "retry_comment_author": retry_comment["author"],
            "baseline_at": baseline_at,
        }
    try:
        context_block = _build_context_block(
            client,
            ref,
            decision.pull_requests,
            rework_pr_number=int(context["pr_number"]),
        )
    except GithubCompletionError as exc:
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "retry_context_failed", "error": str(exc),
        }
    rework = ReworkDecision(
        reason="agent_rework",
        pr_number=int(context["pr_number"]),
        head_sha=context["pr"].head_sha,
        request_comment_id=retry_comment_id,
        context_block=context_block,
        label_actor=retry_comment["author"],
        authoritative=True,
    )
    try:
        with conn:
            result = apply_rework(
                conn, task_id, rework, ref,
                retry_comment_id=retry_comment_id,
            )
    except sqlite3.Error as exc:
        conn.rollback()
        return {
            "task_id": task_id, "status": "blocked", "changed": False,
            "reason": "db_write_failed", "error": f"{type(exc).__name__}: {exc}",
        }
    if not result.get("changed"):
        return result
    # A maintainer retry is a fresh human-authorized attempt: give the new
    # round the full circuit-breaker budget instead of inheriting the
    # failed round's failure count.
    try:
        with conn:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 0 WHERE id = ?",
                (task_id,),
            )
    except sqlite3.Error:
        pass  # non-critical; the new round is already open
    # The request must stay visible until the dispatch claim atomically
    # swaps it for agent-working (claim-first contract).  The self-heal
    # already restored the label during the attention hold, so this is
    # idempotent; a label projection failure never loses the new round.
    try:
        _, label_reason, label_evidence = _project_pr_lifecycle_labels(
            client,
            ref,
            int(context["pr_number"]),
            add=(REWORK_LABEL,),
            remove=(WORKING_LABEL, REVIEW_READY_LABEL),
        )
    except GithubCompletionError as exc:
        result["label_action"] = "label_projection_failed"
        result["label_error"] = str(exc)
        return result
    result["label_action"] = label_reason
    result["lifecycle"] = label_evidence
    result["reason"] = "maintainer_retry_consumed"
    result["retry_comment_id"] = retry_comment_id
    result["retry_comment_author"] = retry_comment["author"]
    return result


def _reconcile_rework_lifecycle(
    conn: sqlite3.Connection,
    kanban_db: Any,
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    row: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    *,
    dry_run: bool,
    failure_limit: Optional[int],
) -> Optional[dict[str, Any]]:
    """Project worker ownership and delivery only for a known rework round."""
    if context is None or "_error" in context:
        return None
    task_id = str(row["id"])
    status = str(row["status"])
    labels = set(context["labels"])
    lifecycle = {REWORK_LABEL, WORKING_LABEL, REVIEW_READY_LABEL}

    # A re-applied agent-rework after a spent delivery makes the previous
    # round's agent-review-ready stale: normalize only that unambiguous
    # pair (agent-rework + agent-review-ready, no agent-working) so the
    # classic REVIEW -> READY intake path can own the new round.  Every
    # other multi-label combination keeps the fail-closed guard below.
    if (
        REWORK_LABEL in labels
        and REVIEW_READY_LABEL in labels
        and WORKING_LABEL not in labels
        and status in {"review", "ready"}
    ):
        try:
            normalized = _normalize_stale_review_ready(
                conn,
                client,
                ref,
                int(context["pr_number"]),
                task_id,
                status,
                labels,
                dry_run=dry_run,
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id,
                "status": status,
                "changed": False,
                "reason": "stale_review_ready_normalize_failed",
                "error": str(exc),
            }
        if normalized is not None:
            if dry_run:
                # Nothing was mutated in a dry run; keep the original stop
                # behavior (the predicted entry already records the intent).
                return normalized
            # Non-dry-run: normalization removed the stale agent-review-ready.
            # The ``labels`` snapshot captured at the top of this function is
            # now stale on GitHub.  Refetch the live label set so the rest of
            # this SAME reconciliation pass evaluates against fresh state
            # instead of deferring to the next cron tick (the observed
            # 5-10 minute normalization latency).  Task state (``status``),
            # the PR decision, and the governing rework event are all
            # untouched by a label-only removal, so only ``labels`` is stale.
            #
            # This is idempotent: a re-run of the same pass sees
            # agent-review-ready already absent, so _normalize_stale_review_
            # ready returns None and the pass proceeds to the same transition.
            # If the refetch fails we cannot trust a label snapshot, so we
            # fall back to the original stop behavior (next tick re-evaluates
            # with a fresh snapshot) rather than act on a stale one.
            try:
                labels = _pr_labels(
                    client, ref.repository, int(context["pr_number"]),
                )
            except GithubCompletionError:
                return normalized
            # Fall through to the remainder of this function with the fresh
            # labels instead of returning the normalized entry; a ``return
            # None`` here hands the task to the classic intake / dispatch
            # lane in the same tick.

    if len(labels & lifecycle) > 1:
        # RC3 (t_aff9017c incident): an ACTIVE worker legitimately owns
        # agent-working while a fresh trusted agent-rework request arrives
        # mid-round.  Fail-closed here would strand the pending round behind
        # an operator hand-restore, so defer instead: the worker keeps its
        # label and the new request stays on the PR.  The deferral holds only
        # while the worker's run is open — once it ends, the stale
        # agent-working is cleaned below and the pending round proceeds.
        if WORKING_LABEL in labels and _task_has_active_rework_claim(conn, row):
            return {
                "task_id": task_id,
                "status": status,
                "changed": False,
                "reason": "lifecycle_conflict_deferred_active_worker",
                "lifecycle": {"labels": sorted(labels)},
            }
        # RC1 invariant sweep: never leave a GitHub-backed card parked in
        # false-terminal DONE while its linked PR is still OPEN.  Repair
        # DONE -> REVIEW first (clearing the stale non-rework labels) and let
        # the next tick re-evaluate the remaining conflict against REVIEW.
        if status == "done" and context["pr"].state == "open":
            if dry_run:
                return {
                    "task_id": task_id, "status": "done", "changed": True,
                    "reason": "done_open_pr_repair_predicted",
                    "diagnostic": "lifecycle_label_conflict",
                    "lifecycle": {"labels": sorted(labels)},
                }
            try:
                context_block = _build_context_block(
                    client, ref, decision.pull_requests
                )
            except GithubCompletionError:
                context_block = None
            with conn:
                db_result = apply_decision(
                    conn, task_id, decision, context_block=context_block,
                )
            try:
                _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                    client, ref, int(context["pr_number"]),
                    add=(REVIEW_READY_LABEL,),
                    remove=(WORKING_LABEL,),
                )
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id,
                    "status": str(db_result.get("status") or "review"),
                    "changed": bool(db_result.get("changed")),
                    "reason": "review_ready_label_projection_failed",
                    "error": str(exc),
                    "diagnostic": "lifecycle_label_conflict",
                }
            return {
                "task_id": task_id,
                "status": str(db_result.get("status") or "review"),
                "changed": bool(db_result.get("changed")),
                "reason": "lifecycle_conflict_done_repaired_to_review",
                "previous_status": status,
                "label_action": label_reason,
                "lifecycle": label_evidence,
            }
        print(
            f"kanban-github-sync: lifecycle label conflict for {ref.repository}#{context['pr_number']} task={task_id}",
            file=sys.stderr,
        )
        return {
            "task_id": task_id,
            "status": status,
            "changed": False,
            "reason": "lifecycle_label_conflict",
            "lifecycle": {"labels": sorted(labels)},
        }

    # Operator recovery can move a consumed attention hold back to REVIEW.
    # Only the current round's attention evidence plus the stale rework label
    # authorizes this narrow retry ingress.  Normal review-ready cards and
    # ordinary REVIEW label intake never enter this branch; a lifecycle-label
    # conflict has already failed closed above.
    # A false-terminal DONE card with an OPEN PR also accepts an explicit
    # maintainer retry (RC4, t_aff9017c incident): without this the maintainer
    # re-request is stranded because no lane owns DONE.  The DONE gate does
    # not require a prior attention record — the fresh trusted retry comment
    # itself is the human evidence — but the comment must postdate the
    # provisional completion (enforced by the baseline in
    # _consume_explicit_rework_retry).  A DONE card WITHOUT any retry comment
    # must not be held here: it falls through to the delivery/repair paths so
    # the ordinary DONE + OPEN PR repair keeps working.
    if (
        status in {"review", "done"}
        and context["pr"].state == "open"
        and REWORK_LABEL in labels
        and WORKING_LABEL not in labels
    ):
        has_retry_signal = _has_fresh_retry_comment(
            conn, client, ref, task_id, row, context,
        )
        attention_at = (
            None if status == "done"
            else _current_rework_attention_at(conn, task_id, context["event"])
        )
        if (status == "done" and has_retry_signal) or (
            status == "review" and attention_at is not None
        ):
            retry_result = _consume_explicit_rework_retry(
                conn, client, ref, decision, task_id, row, context,
                dry_run=dry_run,
            )
            if retry_result is not None:
                return retry_result
            return {
                "task_id": task_id,
                "status": status,
                "changed": False,
                "reason": "rework_retry_pending",
                "retry_required": True,
                "attention_at": attention_at,
                "lifecycle": {"labels": sorted(labels)},
            }

    # A freshly (re-)requested rework round stays owned by the classic intake
    # transitions (REVIEW/BLOCKED -> READY) and by the dispatch lane.  A
    # blocked task is different once its current-round worker has supplied a
    # complete delivery handoff: the blocked outcome is a human-validation
    # gate, not permission to lose the delivery projection.  Promote only
    # from the same bounded marker/run/head evidence used for other states.
    if status == "blocked":
        if context["pr"].state != "open":
            return None
        # Explicit maintainer retry (new round ingress): a fresh trusted
        # AGENT_REWORK_RETRY comment on the PR closes the held round and
        # opens a new one through the classic intake contract.  Without it
        # the card stays BLOCKED with the attention record — the
        # self-heal-restored agent-rework label is never retry evidence.
        retry_result = _consume_explicit_rework_retry(
            conn, client, ref, decision, task_id, row, context,
            dry_run=dry_run,
        )
        if retry_result is not None:
            return retry_result
        try:
            delivered, _delivery_reason, evidence = _rework_delivery_evidence(
                conn, client, ref, task_id, context["pr"], context["event"],
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "delivery_query_failed", "error": str(exc),
            }
        if not delivered:
            # A consumed rework round owns this BLOCKED card even when the
            # delivery evidence is incomplete or invalid.  Do not fall
            # through to generic blocked reconciliation: an open PR would
            # otherwise be projected to REVIEW while agent-working remains.
            # Keep the card BLOCKED and record one idempotent attention event
            # for every diagnostic, without emitting sync or delivery events.
            if dry_run:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "rework_human_attention_predicted",
                    "diagnostic": _delivery_reason, "evidence": evidence,
                }
            try:
                label_reason, label_evidence = _restore_rework_labels(client, context)
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "rework_attention_label_projection_failed",
                    "error": str(exc),
                }
            _record_rework_attention(
                conn, task_id, context, reason=_delivery_reason, evidence=evidence,
            )
            try:
                _post_rework_attention_pr_comment(
                    client, ref, int(context["pr_number"]), task_id,
                    reason=_delivery_reason, evidence=evidence,
                )
            except GithubCompletionError as exc:
                print(
                    f"kanban-github-sync: attention PR comment failed for "
                    f"{ref.repository}#{context['pr_number']} task={task_id}: {exc}",
                    file=sys.stderr,
                )
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "rework_human_attention",
                "diagnostic": _delivery_reason, "lifecycle": label_evidence,
                "label_action": label_reason,
            }
        if dry_run:
            return {
                "task_id": task_id, "status": "review", "changed": False,
                "reason": "agent_review_ready_predicted", "evidence": evidence,
            }
        db_result = _delivery_review_transition(
            conn, task_id, status, ref, int(context["pr_number"]), evidence,
        )
        if db_result is None:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "state_changed_during_sync", "evidence": evidence,
            }
        try:
            _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                client, ref, int(context["pr_number"]),
                add=(REVIEW_READY_LABEL,),
                remove=(REWORK_LABEL, WORKING_LABEL),
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": "review", "changed": True,
                "reason": "review_ready_label_projection_failed",
                "error": str(exc), "evidence": evidence,
            }
        return {
            "task_id": task_id, "status": "review", "changed": True,
            "reason": "agent_review_ready", "evidence": evidence,
            "label_action": label_reason, "lifecycle": label_evidence,
        }

    # Merged PR: clear any lifecycle labels, then fall through so the classic
    # completion transition records DONE in the same tick.
    if context["pr"].is_merged_into_target and labels & lifecycle:
        if dry_run:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "merged_rework_labels_predicted",
            }
        try:
            _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                client, ref, int(context["pr_number"]),
                remove=(REWORK_LABEL, WORKING_LABEL, REVIEW_READY_LABEL),
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "merged_lifecycle_cleanup_failed", "error": str(exc),
            }
        print(
            f"kanban-github-sync: merged PR {ref.repository}#{context['pr_number']} "
            f"lifecycle labels cleared for task={task_id} ({label_reason})",
            file=sys.stderr,
        )
        return None

    active = _task_has_active_rework_claim(conn, row)
    if active:
        # The active claim belongs to the CURRENT rework round (the
        # governing github_pr_rework event).  Only a delivery bound to
        # that same round (recorded after its governing event,
        # identity-matched when available) can make this claim the core
        # review lane.  A past-round delivery whose head still equals the
        # live PR head (round-N worker not yet pushed) is never
        # current-round evidence: the active round-N worker keeps
        # agent-working, which takes precedence over any past delivery.
        current_delivery = _current_round_delivery(
            conn, task_id, context["event"],
        )
        if current_delivery is not None:
            if dry_run:
                return {
                    "task_id": task_id, "status": "running", "changed": False,
                    "reason": "agent_review_ready_predicted",
                    "lifecycle": {"labels": sorted(labels)},
                }
            try:
                _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                    client, ref, int(context["pr_number"]),
                    add=(REVIEW_READY_LABEL,),
                    remove=(REWORK_LABEL, WORKING_LABEL),
                )
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id, "status": "running", "changed": False,
                    "reason": "review_ready_label_projection_failed",
                    "error": str(exc),
                }
            return {
                "task_id": task_id, "status": "running", "changed": False,
                "reason": "agent_review_ready", "label_action": label_reason,
                "lifecycle": label_evidence,
            }
        if dry_run:
            return {
                "task_id": task_id, "status": "running", "changed": False,
                "reason": "agent_working_predicted",
                "lifecycle": {"labels": sorted(labels)},
            }
        try:
            _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                client, ref, int(context["pr_number"]),
                add=(WORKING_LABEL,),
                remove=(REWORK_LABEL, REVIEW_READY_LABEL),
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": "running", "changed": False,
                "reason": "working_label_projection_failed",
                "error": str(exc),
            }
        return {
            "task_id": task_id, "status": "running", "changed": False,
            "reason": "agent_working", "label_action": label_reason,
            "lifecycle": label_evidence,
        }

    # Intake states with a visible rework label belong to the classic
    # transitions / dispatch lane.  A NEW label on a spent round (done) also
    # flows through the classic DONE -> REVIEW path first.
    if REWORK_LABEL in labels and status in {"ready", "review"}:
        return None
    if status == "ready":
        if WORKING_LABEL in labels:
            return {
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "working_label_present",
                "lifecycle": {"labels": sorted(labels)},
            }
        return None  # dispatch lane owns intake
    if REWORK_LABEL in labels and status == "done" and _label_is_newer_than_event(
        client,
        ref,
        int(context["pr_number"]),
        int(context["event_at"]),
        current_label_at=context["event"][0].get("label_added_at"),
    ):
        # RC2 (t_aff9017c incident): a fresh rework request postdating the
        # round means the worker is gone — any lingering agent-working label
        # is stale ownership from a projection that never ran.  Clean it so
        # the pending request stays the single lifecycle signal, then hand
        # this tick to the classic DONE -> REVIEW repair below.
        if WORKING_LABEL in labels:
            if dry_run:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "stale_working_clean_predicted",
                    "lifecycle": {"labels": sorted(labels)},
                }
            try:
                _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                    client, ref, int(context["pr_number"]),
                    add=(REWORK_LABEL,),
                    remove=(WORKING_LABEL, REVIEW_READY_LABEL),
                )
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "stale_working_clean_failed", "error": str(exc),
                }
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "stale_working_cleaned_new_round_pending",
                "label_action": label_reason,
                "lifecycle": label_evidence,
            }
        return None

    if context["pr"].state == "open":
        try:
            delivered, delivery_reason, evidence = _rework_delivery_evidence(
                conn, client, ref, task_id, context["pr"], context["event"],
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "delivery_query_failed", "error": str(exc),
            }
        if delivered:
            if dry_run:
                entry: dict[str, Any] = {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "agent_review_ready_predicted", "evidence": evidence,
                }
                if status == "done" and decision.desired_status == "review":
                    # Predict the DONE + OPEN PR repair without mutating.
                    entry["repair_predicted"] = "done_open_pr_repaired"
                    entry["status"] = "review"
                return entry
            existing_head = _latest_delivery_head(conn, task_id)
            if existing_head == str(evidence.get("head") or "").casefold():
                # Worker completion is never a DONE ground for an OPEN PR:
                # a delivered round whose card was re-completed by the core
                # review lane is repaired back to REVIEW via the classic
                # path (assignee/claim/completed_at cleared so the review
                # lane cannot auto-claim it again).
                repair: Optional[dict[str, Any]] = None
                if status == "done" and decision.desired_status == "review":
                    context_block: Optional[str] = None
                    try:
                        context_block = _build_context_block(
                            client, ref, decision.pull_requests
                        )
                    except GithubCompletionError:
                        context_block = None
                    with conn:
                        db_result = apply_decision(
                            conn, task_id, decision, context_block=context_block,
                        )
                    repair = {
                        "previous_status": "done",
                        "new_status": str(db_result.get("status") or "review"),
                        "changed": bool(db_result.get("changed")),
                        "reason": str(db_result.get("reason") or "linked_pr_open"),
                    }
                    entry_status = str(db_result.get("status") or "review")
                    entry_changed = bool(db_result.get("changed"))
                else:
                    entry_status = status
                    entry_changed = False
                # Delivered rounds expose agent-review-ready only; a stale
                # agent-working (post-delivery label regression) is cleaned
                # here, idempotently.
                entry = {
                    "task_id": task_id, "status": entry_status,
                    "changed": entry_changed, "reason": "agent_review_ready",
                    "evidence": evidence,
                }
                if repair is not None:
                    entry["repair"] = repair
                if status in {"running", "review", "done"}:
                    # A pending newer rework request (postdating this round)
                    # survives the delivery projection untouched: the classic
                    # REVIEW -> READY intake owns it on the next tick.
                    pending_rework = REWORK_LABEL in labels and _label_is_newer_than_event(
                        client,
                        ref,
                        int(context["pr_number"]),
                        int(context["event_at"]),
                        current_label_at=context["event"][0].get("label_added_at"),
                    )
                    try:
                        _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                            client, ref, int(context["pr_number"]),
                            add=(REVIEW_READY_LABEL,),
                            remove=()
                            + ((WORKING_LABEL,) if pending_rework else ())
                            + (() if pending_rework else (REWORK_LABEL, WORKING_LABEL)),
                        )
                    except GithubCompletionError as exc:
                        return {
                            "task_id": task_id,
                            "status": entry_status,
                            "changed": entry_changed,
                            "reason": "review_ready_label_projection_failed",
                            "error": str(exc),
                            "evidence": evidence,
                        }
                    entry["label_action"] = label_reason
                    entry["lifecycle"] = label_evidence
                return entry
            db_result: Optional[dict[str, Any]] = None
            if status in {"ready", "running"}:
                db_result = _delivery_review_transition(
                    conn, task_id, status, ref,
                    int(context["pr_number"]), evidence,
                )
            elif status in {"done", "blocked"} and decision.desired_status == "review":
                with conn:
                    db_result = apply_decision(
                        conn,
                        task_id,
                        decision,
                        allow_blocked_source=status == "blocked",
                    )
            try:
                _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                    client, ref, int(context["pr_number"]),
                    add=(REVIEW_READY_LABEL,),
                    remove=(REWORK_LABEL, WORKING_LABEL),
                )
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id,
                    "status": db_result.get("status", status) if db_result else status,
                    "changed": bool(db_result and db_result.get("changed")),
                    "reason": "review_ready_label_projection_failed",
                    "error": str(exc),
                    "evidence": evidence,
                }
            if db_result is None:
                # Status was already review (or an optimistic transition lost
                # the race): record the delivery event once for provenance.
                delivery_new_status = "review"
            else:
                delivery_new_status = str(db_result.get("status") or "review")
            with conn:
                _append_sync_event(
                    conn,
                    task_id,
                    {
                        "previous_status": status,
                        "new_status": delivery_new_status,
                        "reason": "agent_review_ready",
                        "repository": ref.repository,
                        "pr_number": context["pr_number"],
                        **dict(evidence),
                        "label_action": label_reason,
                    },
                    kind="github_pr_rework_delivery",
                )
            return {
                "task_id": task_id,
                "status": db_result.get("status", "review") if db_result else "review",
                "changed": bool(db_result and db_result.get("changed")),
                "reason": "agent_review_ready",
                "evidence": evidence,
                "lifecycle": label_evidence,
            }

        if status == "review":
            # The human review lane owns this card; never requeue from here.
            return None
        run = _task_run_after_rework(conn, task_id, int(context["event_at"]))
        if _rework_human_attention(delivery_reason, run):
            if status == "done" and decision.desired_status == "review":
                # A human-attention hold must never leave a GitHub-backed card
                # parked in false-terminal DONE while its linked PR is still
                # OPEN: the core review lane may complete the root
                # provisionally, but authoritative projection belongs to the
                # edge. Repair DONE -> REVIEW through the canonical
                # transition; the attention diagnostic travels in the entry.
                # A fresh agent-rework label re-opening a new round has
                # already returned earlier, so this never swallows a new
                # round request.
                if dry_run:
                    return {
                        "task_id": task_id,
                        "status": "done",
                        "changed": True,
                        "reason": "done_open_pr_repair_predicted",
                        "repair_predicted": "done_open_pr_repaired",
                        "diagnostic": delivery_reason,
                    }
                context_block: Optional[str] = None
                try:
                    context_block = _build_context_block(
                        client, ref, decision.pull_requests
                    )
                except GithubCompletionError:
                    context_block = None
                with conn:
                    db_result = apply_decision(
                        conn, task_id, decision, context_block=context_block,
                    )
                try:
                    _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                        client, ref, int(context["pr_number"]),
                        add=(REVIEW_READY_LABEL,),
                        remove=(REWORK_LABEL, WORKING_LABEL),
                    )
                except GithubCompletionError as exc:
                    return {
                        "task_id": task_id,
                        "status": str(db_result.get("status") or "review"),
                        "changed": bool(db_result.get("changed")),
                        "reason": "review_ready_label_projection_failed",
                        "error": str(exc),
                        "evidence": decision.to_dict(),
                    }
                return {
                    "task_id": task_id,
                    "status": str(db_result.get("status") or "review"),
                    "changed": bool(db_result.get("changed")),
                    "reason": str(db_result.get("reason") or "linked_pr_open"),
                    "diagnostic": delivery_reason,
                    "label_action": label_reason,
                    "lifecycle": label_evidence,
                }
            if dry_run:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "rework_human_attention_predicted",
                    "diagnostic": delivery_reason, "evidence": evidence,
                }
            try:
                label_reason, label_evidence = _restore_rework_labels(client, context)
            except GithubCompletionError as exc:
                return {
                    "task_id": task_id, "status": status, "changed": False,
                    "reason": "rework_attention_label_projection_failed",
                    "error": str(exc),
                }
            _record_rework_attention(
                conn, task_id, context, reason=delivery_reason, evidence=evidence,
            )
            try:
                _post_rework_attention_pr_comment(
                    client, ref, int(context["pr_number"]), task_id,
                    reason=delivery_reason, evidence=evidence,
                )
            except GithubCompletionError as exc:
                print(
                    f"kanban-github-sync: attention PR comment failed for "
                    f"{ref.repository}#{context['pr_number']} task={task_id}: {exc}",
                    file=sys.stderr,
                )
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "rework_human_attention",
                "diagnostic": delivery_reason, "lifecycle": label_evidence,
                "label_action": label_reason,
            }
        if dry_run:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "rework_retry_predicted", "diagnostic": delivery_reason,
                "evidence": evidence,
            }
        try:
            label_reason, label_evidence = _restore_rework_labels(client, context)
        except GithubCompletionError as exc:
            return {
                "task_id": task_id, "status": status, "changed": False,
                "reason": "rework_retry_label_projection_failed", "error": str(exc),
            }
        result = _requeue_rework_task(
            conn, kanban_db, task_id, context,
            reason=delivery_reason, failure_limit=failure_limit,
        )
        result["lifecycle"] = label_evidence
        result["label_action"] = label_reason
        return result
    return None


def _canonical_open_pr_for_changes_requested(
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
) -> Optional[GithubPullRequest]:
    """Return the single canonical open PR eligible for changes-requested rework.

    ``decision.linked_pr_numbers`` is produced by the existing Issue-timeline
    plus importer-owned handoff discovery, and every identifier is re-queried
    from the exact repository before it reaches this helper.  Require exactly
    one open linked PR and the expected target branch so a bare PR number,
    unrelated open PR, or ambiguous multi-PR card can never bypass the core
    ``active_pr`` guard.
    """
    if not decision.authoritative or decision.reason != "linked_pr_open":
        return None
    linked_numbers = {int(number) for number in decision.linked_pr_numbers}
    open_prs = tuple(pr for pr in decision.pull_requests if pr.state == "open")
    if len(open_prs) != 1:
        return None
    pr = open_prs[0]
    if pr.number not in linked_numbers or pr.base_branch != ref.target_branch:
        return None
    return pr


def _normalize_changes_requested_rework(
    conn: sqlite3.Connection,
    task_id: str,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    *,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Normalize one verified ``changes_requested`` transition to rework.

    The normalization is deliberately idempotent: it only applies while the
    newest governing event is ``changes_requested``.  A successful real run
    records the same canonical ``github_pr_rework`` event consumed by the
    existing edge dispatch lane; it does not change the READY status itself.
    """
    row = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None or str(row["status"]) != "ready":
        return None
    if row["claim_lock"] is not None:
        return None
    if _governing_event_kind(conn, task_id) != "changes_requested":
        return None
    pr = _canonical_open_pr_for_changes_requested(ref, decision)
    if pr is None:
        return None

    count_row = conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework'",
        (task_id,),
    ).fetchone()
    rework_round = int(count_row[0]) + 1
    rework_evidence = {
        "reason": "agent_rework",
        "trigger": "changes_requested",
        "canonical_open_pr": True,
        "pr_number": pr.number,
        "head_sha": pr.head_sha,
        "rework_round": rework_round,
    }
    if dry_run:
        return {
            "task_id": task_id,
            "status": "ready",
            "changed": False,
            "reason": "changes_requested_rework_predicted",
            "rework": rework_evidence,
            "evidence": decision.to_dict(),
        }

    payload = {
        "previous_status": "ready",
        "new_status": "ready",
        "repository": ref.repository,
        "issue_number": ref.issue_number,
        "pr_number": pr.number,
        "head_sha": pr.head_sha,
        "reason": "agent_rework",
        "trigger": "changes_requested",
        "canonical_open_pr": True,
        "canonical_pr_source": "github_linked_pr",
        "rework_round": rework_round,
        "merge_authority": "human",
        "auto_merge": False,
        "source": "github_edge_rework_normalization",
    }
    with conn:
        latest = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            latest is None
            or str(latest["status"]) != "ready"
            or latest["claim_lock"] is not None
            or _governing_event_kind(conn, task_id) != "changes_requested"
        ):
            return {
                "task_id": task_id,
                "status": str(latest["status"]) if latest is not None else "ready",
                "changed": False,
                "reason": "state_changed_during_sync",
            }
        source_row = conn.execute(
            "SELECT MAX(created_at) FROM task_events "
            "WHERE task_id = ? AND kind = 'changes_requested'",
            (task_id,),
        ).fetchone()
        source_created_at = int(source_row[0] or 0)
        _append_sync_event(
            conn,
            task_id,
            payload,
            kind="github_pr_rework",
            created_at=max(int(time.time()), source_created_at + 1),
        )
    return {
        "task_id": task_id,
        "status": "ready",
        "changed": False,
        "reason": "changes_requested_rework_normalized",
        "rework": rework_evidence,
        "evidence": decision.to_dict(),
    }


def _pending_rework_tasks(
    conn: sqlite3.Connection,
    *,
    task_ids: Optional[Iterable[str]] = None,
    normalized_task_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """``ready`` tasks whose governing transition is a consumed rework.

    These are the tasks waiting for an existing-PR rework worker: the
    core dispatcher's ``active_pr`` respawn guard will never spawn them,
    so the edge lane owns their respawn.  ``normalized_task_ids`` is used
    only by dry-run to represent a verified changes-requested normalization
    without writing its canonical event.
    """
    rows = conn.execute(
        "SELECT id, assignee, workspace_path, branch_name, skills, claim_lock "
        "FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY created_at ASC, id ASC"
    ).fetchall()
    selected = {str(tid) for tid in task_ids} if task_ids is not None else None
    normalized = {
        str(tid) for tid in normalized_task_ids
    } if normalized_task_ids is not None else set()
    pending: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["id"])
        if selected is not None and task_id not in selected:
            continue
        governing = _governing_event_kind(conn, task_id)
        if governing != "github_pr_rework" and not (
            governing == "changes_requested" and task_id in normalized
        ):
            continue
        pending.append(dict(row))
    return pending


def _resolve_rework_assignee(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    default_assignee: Optional[str],
    dry_run: bool,
) -> tuple[Optional[str], Optional[str]]:
    """Return (assignee, error_reason) for a rework-pending task.

    ``apply_rework`` clears the assignee; mirror the core dispatcher's
    auto-assign behaviour by persisting ``kanban.default_assignee`` on the
    row (with an ``assigned`` event) before the worker is spawned.
    """
    assignee = str(row.get("assignee") or "").strip() or None
    if assignee:
        return assignee, None
    if not default_assignee:
        return None, "unassigned"
    if dry_run:
        return default_assignee, None
    cur = conn.execute(
        "UPDATE tasks SET assignee = ? WHERE id = ? "
        "AND (assignee IS NULL OR assignee = '')",
        (default_assignee, row["id"]),
    )
    if cur.rowcount == 1:
        _append_sync_event(
            conn, str(row["id"]),
            {"assignee": default_assignee, "source": "edge_rework_dispatch"},
            kind="assigned",
        )
    return default_assignee, None


def _dispatch_pending_rework_locked(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
    *,
    dry_run: bool = False,
    task_ids: Optional[Iterable[str]] = None,
    normalized_task_ids: Optional[Iterable[str]] = None,
    spawn_fn: Any = None,
    cfg: Optional[Mapping[str, Any]] = None,
    on_claim: Any = None,
    on_failure: Any = None,
    client: Any = None,
    rework_contexts: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None,
    active_pr_owners: Optional[Mapping[tuple[str, int], str]] = None,
) -> list[dict[str, Any]]:
    """Spawn the worker for the oldest rework-pending task on this board.

    Bypasses the core ``active_pr`` respawn guard ONLY for tasks whose
    governing transition is a consumed agent-rework.  At most one worker
    per board per tick, and only while the board's running count is below
    ``kanban.max_in_progress`` (default 1) — mirrors the dispatcher caps,
    so the general active-PR duplicate-spawn protection is unchanged.

    ``normalized_task_ids`` is a dry-run-only representation of a verified
    ``changes_requested`` -> canonical rework normalization.  Real runs
    persist the canonical event before entering this lane.

    ``spawn_fn`` is injectable for tests; it receives the claimed
    ``Task``, the resolved workspace and the board (dispatcher signature).
    """
    if cfg is None:
        cfg = _kanban_config()
    try:
        max_in_progress = max(1, int(cfg.get("max_in_progress") or 1))
    except (TypeError, ValueError):
        max_in_progress = 1
    default_assignee = str(cfg.get("default_assignee") or "").strip() or None
    try:
        raw_failure_limit = cfg.get("failure_limit")
        failure_limit = (
            int(raw_failure_limit) if raw_failure_limit is not None else None
        )
        if failure_limit is not None and failure_limit < 1:
            failure_limit = None
    except (TypeError, ValueError):
        failure_limit = None

    running = int(
        conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    )
    if running >= max_in_progress:
        return [{
            "task_id": None, "status": None, "changed": False,
            "reason": "board_busy",
            "running": running, "max_in_progress": max_in_progress,
        }]

    pending = _pending_rework_tasks(
        conn,
        task_ids=task_ids,
        normalized_task_ids=normalized_task_ids,
    )
    if not pending:
        return []
    row = pending[0]
    task_id = str(row["id"])

    # Duplicate-ownership guards (GitHub label + Kanban PR owner), applied
    # before any claim so a second worker is never spawned beside a live one.
    if client is not None and rework_contexts is not None:
        ctx = rework_contexts.get(task_id)
        if ctx is not None and "labels" in ctx and WORKING_LABEL in ctx["labels"]:
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "working_label_present",
                "lifecycle": {"labels": sorted(ctx["labels"])},
            }]
        if ctx is not None and "labels" in ctx and active_pr_owners:
            owner_key = (str(ctx["repository"]), int(ctx["pr_number"]))
            for other_key, owner_task in active_pr_owners.items():
                if other_key == owner_key and owner_task != task_id:
                    return [{
                        "task_id": task_id, "status": "ready", "changed": False,
                        "reason": "pr_worker_active",
                        "owner_task": owner_task,
                        "repository": ctx["repository"],
                        "pr_number": ctx["pr_number"],
                    }]

    assignee, err = _resolve_rework_assignee(
        conn, row, default_assignee=default_assignee, dry_run=dry_run,
    )
    if err:
        return [{"task_id": task_id, "status": "ready", "changed": False, "reason": err}]
    if assignee is None:
        return [{
            "task_id": task_id,
            "status": "ready",
            "changed": False,
            "reason": "unassigned",
        }]

    if dry_run:
        return [{
            "task_id": task_id, "status": "ready", "changed": False,
            "reason": "rework_spawn_predicted",
            "assignee": assignee, "board": board,
        }]

    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        profile_exists = None
    if profile_exists is not None:
        try:
            profile_ok = bool(profile_exists(assignee))
        except Exception:
            profile_ok = True
        if not profile_ok:
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "assignee_profile_missing", "assignee": assignee,
            }]

    claimed = kanban_db.claim_task(conn, task_id)
    if claimed is None:
        return [{
            "task_id": task_id, "status": "ready", "changed": False,
            "reason": "claim_failed",
        }]

    if on_claim is not None:
        try:
            claim_projection = on_claim(claimed)
        except Exception as exc:
            claim_projection = {"ok": False, "error": str(exc)}
        if not isinstance(claim_projection, Mapping) or not claim_projection.get("ok"):
            try:
                kanban_db.reclaim_task(
                    conn, task_id,
                    reason="rework working-label projection failed",
                )
            except Exception as exc:
                return [{
                    "task_id": task_id, "status": "running", "changed": True,
                    "reason": "claim_projection_reclaim_failed",
                    "error": f"{claim_projection!r}; {type(exc).__name__}: {exc}",
                }]
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "working_label_projection_failed",
                "error": str((claim_projection or {}).get("error") or "unknown"),
                "lifecycle": dict(claim_projection or {}),
            }]

    # Resolve the workspace exactly like the core dispatcher does (the
    # rework worktree already exists from the first run; re-resolution
    # keeps parity if it was moved or recreated).
    try:
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch = kanban_db._resolve_worktree_workspace(
                claimed, board=board
            )
        else:
            workspace = kanban_db.resolve_workspace(claimed, board=board)
            resolved_branch = None
    except Exception as exc:
        auto_blocked = bool(kanban_db._record_spawn_failure(
            conn, claimed.id, f"workspace: {exc}",
            failure_limit=failure_limit,
        ))
        if on_failure is not None:
            try:
                on_failure(claimed, "workspace_resolve_failed")
            except Exception as projection_exc:
                print(
                    f"kanban-github-sync: failed to restore rework label after workspace failure: {type(projection_exc).__name__}",
                    file=sys.stderr,
                )
        return [{
            "task_id": task_id,
            "status": "blocked" if auto_blocked else "ready",
            "changed": False,
            "reason": "workspace_resolve_failed",
            "error": str(exc),
            "auto_blocked": auto_blocked,
        }]
    kanban_db.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        kanban_db.set_branch_name(
            conn, claimed.id,
            resolved_branch or (claimed.branch_name or "").strip() or f"wt/{claimed.id}",
        )

    spawn = spawn_fn if spawn_fn is not None else kanban_db._default_spawn
    try:
        pid = spawn(claimed, str(workspace), board=board)
        if pid:
            kanban_db._set_worker_pid(conn, claimed.id, int(pid))
    except Exception as exc:
        auto_blocked = bool(kanban_db._record_spawn_failure(
            conn, claimed.id, str(exc), failure_limit=failure_limit,
        ))
        if on_failure is not None:
            try:
                on_failure(claimed, "spawn_failed")
            except Exception as projection_exc:
                print(
                    f"kanban-github-sync: failed to restore rework label after spawn failure: {type(projection_exc).__name__}",
                    file=sys.stderr,
                )
        return [{
            "task_id": task_id,
            "status": "blocked" if auto_blocked else "ready",
            "changed": False,
            "reason": "spawn_failed",
            "error": str(exc),
            "auto_blocked": auto_blocked,
        }]

    return [{
        "task_id": task_id, "status": "running", "changed": True,
        "reason": "rework_worker_spawned",
        "pid": int(pid) if pid else None,
        "run_id": getattr(claimed, "current_run_id", None),
        "assignee": claimed.assignee,
        "board": board,
    }]


def _dispatch_pending_rework(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
    *,
    dry_run: bool = False,
    task_ids: Optional[Iterable[str]] = None,
    normalized_task_ids: Optional[Iterable[str]] = None,
    spawn_fn: Any = None,
    cfg: Optional[Mapping[str, Any]] = None,
    on_claim: Any = None,
    on_failure: Any = None,
    client: Any = None,
    rework_contexts: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None,
    active_pr_owners: Optional[Mapping[tuple[str, int], str]] = None,
) -> list[dict[str, Any]]:
    """Run one rework admission under the core's board-scoped dispatch lock.

    The edge lane must serialize its running-count snapshot with claim/spawn;
    otherwise two overlapping intake ticks can both pass ``max_in_progress``
    before claiming different ready tasks.  Reuse the existing core lock
    without changing Hermes core behavior.  Fail closed if the lock API or
    board path cannot be resolved.
    """
    try:
        db_path = kanban_db.kanban_db_path(board=board)
    except Exception as exc:
        return [{
            "task_id": None,
            "status": None,
            "changed": False,
            "reason": "dispatch_lock_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "board": board,
        }]
    lock_factory = getattr(kanban_db, "_dispatch_tick_lock", None)
    if not callable(lock_factory):
        return [{
            "task_id": None,
            "status": None,
            "changed": False,
            "reason": "dispatch_lock_unavailable",
            "board": board,
        }]
    lock_context: Any = lock_factory(db_path)
    with lock_context as held:
        if not held:
            return [{
                "task_id": None,
                "status": None,
                "changed": False,
                "reason": "dispatch_locked",
                "board": board,
            }]
        return _dispatch_pending_rework_locked(
            conn,
            kanban_db,
            board,
            dry_run=dry_run,
            task_ids=task_ids,
            normalized_task_ids=normalized_task_ids,
            spawn_fn=spawn_fn,
            cfg=cfg,
            on_claim=on_claim,
            on_failure=on_failure,
            client=client,
            rework_contexts=rework_contexts,
            active_pr_owners=active_pr_owners,
        )


def sync_board(
    board: str,
    task_ids: Optional[list[str]] = None,
    *,
    dry_run: bool = False,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Reconcile every non-archived GitHub-backed card on one board.

    GitHub API lookups happen outside any DB transaction; only the final
    optimistic transition is transactional.  A single card failure (e.g.
    GitHub query error) never aborts the rest of the board.
    """
    kanban_db = _import_kanban_db()

    def _annotate(
        entry: dict[str, Any], row: Mapping[str, Any], ref: GithubTaskRef
    ) -> dict[str, Any]:
        """Minimal structured-result extension for the intake Telegram observer.

        ``repository``/``issue_number`` are attached to every entry;
        ``from_state``/``to_state`` only when the entry records an actual
        transition (``changed=True``).  No state machine behavior changes.
        """
        entry["repository"] = ref.repository
        entry["issue_number"] = ref.issue_number
        entry["issue_title"] = ref.issue_title
        entry["block_kind"] = row["block_kind"] if "block_kind" in row.keys() else None
        if entry.get("changed") and entry.get("status"):
            entry["from_state"] = str(row["status"])
            entry["to_state"] = str(entry["status"])
        attention_reason = _operator_attention_reason(entry)
        if attention_reason is not None:
            if dry_run:
                entry["operator_attention_predicted"] = {
                    "reason": attention_reason,
                }
            elif _record_operator_attention(conn, entry):
                entry["operator_attention"] = {"reason": attention_reason}
        return entry

    with kanban_db.connect_closing(board=board) as conn:
        verify_schema(conn, kanban_db)
        requested = {str(tid) for tid in task_ids} if task_ids else None
        rows = conn.execute(
            "SELECT id, body, status, block_kind, claim_lock, current_run_id, "
            "worker_pid FROM tasks WHERE status != 'archived' "
            "AND body IS NOT NULL ORDER BY created_at ASC, id ASC"
        ).fetchall()

        results: list[dict[str, Any]] = []
        normalized_rework_ids: set[str] = set()
        lifecycle_contexts_by_task: dict[str, Optional[dict[str, Any]]] = {}
        for row in rows:
            task_id = str(row["id"])
            if requested is not None and task_id not in requested:
                continue
            ref = parse_task_ref(row["body"])
            if ref is None:
                continue

            # Internal Kanban dependencies are evaluated before any GitHub
            # lookup. This prevents an OPEN/merged PR from projecting REVIEW
            # or DONE while specialist work is still in flight. Lookup errors
            # fail closed and never manufacture BLOCKED.
            try:
                dependency_gate = _internal_dependency_gate(conn, task_id)
            except (sqlite3.Error, SyncError) as exc:
                results.append(_annotate({
                    "task_id": task_id,
                    "status": row["status"],
                    "changed": False,
                    "reason": "internal_dependency_lookup_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }, row, ref))
                continue
            if dependency_gate["pending"]:
                results.append(_annotate(
                    _restore_pending_dependency(
                        conn, task_id, dependency_gate, dry_run=dry_run,
                    ),
                    row,
                    ref,
                ))
                continue

            text_sources: list[str] = [str(row["body"] or "")]
            try:
                for comment in kanban_db.list_comments(conn, task_id):
                    text_sources.append(comment.body)
                for run in kanban_db.list_runs(conn, task_id):
                    for item in (run.summary, run.error):
                        if item:
                            text_sources.append(item)
                    if run.metadata:
                        text_sources.append(
                            json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                        )
            except Exception as exc:  # run/comment API drift -> fail closed
                results.append(
                    _annotate(
                        {
                            "task_id": task_id,
                            "status": row["status"],
                            "changed": False,
                            "reason": "text_source_lookup_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        row,
                        ref,
                    )
                )
                continue

            decision = verify_completion(client, ref, text_sources)

            # PR rework lifecycle: when a consumed rework round governs this
            # task, GitHub-visible worker ownership (agent-working /
            # agent-review-ready) is projected from Kanban state, and stale or
            # failed rounds are recovered.  Classic REVIEW/BLOCKED intake with
            # a fresh agent-rework label stays on the existing paths below.
            lifecycle_event = _latest_rework_event(conn, task_id)
            lifecycle_context: Optional[dict[str, Any]] = None
            if lifecycle_event is not None:
                try:
                    lifecycle_context = _rework_context(
                        client, ref, decision, task_id, lifecycle_event,
                    )
                except GithubCompletionError as exc:
                    lifecycle_context = {"_error": str(exc)}
                lifecycle_contexts_by_task[task_id] = lifecycle_context
                if lifecycle_context is not None and "_error" in lifecycle_context:
                    results.append(
                        _annotate(
                            {
                                "task_id": task_id,
                                "status": row["status"],
                                "changed": False,
                                "reason": "rework_context_failed",
                                "error": lifecycle_context["_error"],
                            },
                            row,
                            ref,
                        )
                    )
                    continue

            # An internal review can return a READY card through
            # ``changes_requested`` without producing the historical
            # ``github_pr_rework`` event.  Normalize only when the current
            # governing event is exactly changes_requested and the existing
            # GitHub decision proves one canonical OPEN PR.  This keeps the
            # normal active-PR guard intact for every other READY card.
            normalized_rework: Optional[dict[str, Any]] = None
            if row["status"] == "ready":
                try:
                    normalized_rework = _normalize_changes_requested_rework(
                        conn,
                        task_id,
                        ref,
                        decision,
                        dry_run=dry_run,
                    )
                except sqlite3.Error as exc:
                    results.append(
                        _annotate(
                            {
                                "task_id": task_id,
                                "status": row["status"],
                                "changed": False,
                                "reason": "db_write_failed",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            row,
                            ref,
                        )
                    )
                    continue
            if normalized_rework is not None:
                if dry_run:
                    normalized_rework_ids.add(task_id)
                else:
                    # The canonical event was just written: rebuild the
                    # lifecycle context so this same tick can dispatch it.
                    lifecycle_event2 = _latest_rework_event(conn, task_id)
                    if lifecycle_event2 is not None:
                        try:
                            lifecycle_contexts_by_task[task_id] = _rework_context(
                                client, ref, decision, task_id, lifecycle_event2,
                            )
                        except GithubCompletionError:
                            lifecycle_contexts_by_task[task_id] = None
                results.append(_annotate(normalized_rework, row, ref))
                continue

            if row["status"] == "blocked" and lifecycle_context is not None:
                lifecycle_entry = _reconcile_rework_lifecycle(
                    conn,
                    kanban_db,
                    client,
                    ref,
                    decision,
                    row,
                    lifecycle_context,
                    dry_run=dry_run,
                    failure_limit=_retry_failure_limit(),
                )
                if lifecycle_entry is not None:
                    results.append(_annotate(lifecycle_entry, row, ref))
                    continue

            if (
                row["status"] == "blocked"
                and lifecycle_context is None
                and lifecycle_event is not None
            ):
                # A consumed rework round still owns this BLOCKED card even
                # when its provenance cannot be resolved (pr_number that
                # does not match exactly one canonical PR, a recreated or
                # mismatched PR, an ambiguous decision).  Preserve the round
                # ownership and fail closed: never fall through to the
                # generic blocked reconciliation, which would project the
                # open PR to REVIEW via a github_pr_sync transition while
                # the active worker keeps agent-working.
                results.append(
                    _annotate(
                        _rework_provenance_attention(
                            conn,
                            client,
                            ref,
                            row,
                            decision,
                            lifecycle_event,
                            dry_run=dry_run,
                        ),
                        row,
                        ref,
                    )
                )
                continue

            if row["status"] == "blocked":
                results.append(
                    _annotate(
                        _reconcile_blocked(
                            conn,
                            client,
                            ref,
                            decision,
                            row,
                            dry_run=dry_run,
                        ),
                        row,
                        ref,
                    )
                )
                continue

            # Worker-owned lifecycle reconciliation (running/ready/review/done
            # with a consumed rework round).  Returns an entry for every state
            # it owns; None means the classic paths below apply.
            if lifecycle_context is not None:
                lifecycle_entry = _reconcile_rework_lifecycle(
                    conn,
                    kanban_db,
                    client,
                    ref,
                    decision,
                    row,
                    lifecycle_context,
                    dry_run=dry_run,
                    failure_limit=_retry_failure_limit(),
                )
                if lifecycle_entry is not None:
                    results.append(_annotate(lifecycle_entry, row, ref))
                    continue

            rework: Optional[ReworkDecision] = None
            if decision.authoritative and row["status"] in {"review", "ready"}:
                try:
                    rework = evaluate_rework(
                        client,
                        ref,
                        decision,
                        current_status=row["status"],
                        last_rework_at=_last_rework_event_at(conn, task_id),
                    )
                except GithubCompletionError as exc:
                    rework = ReworkDecision(
                        reason="rework_query_failed", error=str(exc), authoritative=False
                    )

            if dry_run or not decision.authoritative:
                entry = {
                    "task_id": task_id,
                    "status": row["status"],
                    "changed": False,
                    "reason": decision.reason,
                    "evidence": decision.to_dict(),
                }
                if rework is not None:
                    entry["rework"] = rework.to_dict()
                results.append(_annotate(entry, row, ref))
                continue

            # Rework transition: REVIEW -> READY (body context + event in one
            # transaction, label removal LAST and outside the transaction).
            if (
                rework is not None
                and rework.authoritative
                and rework.reason == "agent_rework"
                and row["status"] == "review"
            ):
                try:
                    with conn:
                        result = apply_rework(conn, task_id, rework, ref)
                except sqlite3.Error as exc:
                    conn.rollback()
                    results.append(
                        _annotate(
                            {
                                "task_id": task_id,
                                "status": row["status"],
                                "changed": False,
                                "reason": "db_write_failed",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            row,
                            ref,
                        )
                    )
                    continue
                if result.get("changed"):
                    # The agent-rework label intentionally stays on the PR
                    # until the edge dispatcher claims the Kanban task and
                    # atomically swaps it for agent-working.
                    pass
                results.append(_annotate(result, row, ref))
                continue

            context_block: Optional[str] = None
            if decision.desired_status == "review" and row["status"] == "done":
                # DONE -> REVIEW: refresh the sync context best-effort; a
                # context fetch failure never blocks the transition.
                try:
                    context_block = _build_context_block(
                        client, ref, decision.pull_requests
                    )
                except GithubCompletionError:
                    context_block = None
            try:
                with conn:
                    result = apply_decision(
                        conn, task_id, decision, context_block=context_block
                    )
            except sqlite3.Error as exc:
                conn.rollback()
                results.append(
                    _annotate(
                        {
                            "task_id": task_id,
                            "status": row["status"],
                            "changed": False,
                            "reason": "db_write_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        row,
                        ref,
                    )
                )
                continue
            if rework is not None:
                result["rework"] = rework.to_dict()
            results.append(_annotate(result, row, ref))

        # Edge-owned rework respawn lane (existing-PR rework): the core
        # dispatcher's ``active_pr`` respawn guard intentionally never
        # spawns these, so the edge does — but ONLY for tasks whose
        # governing transition is a consumed agent-rework, and only when
        # the intake cron enables this lane via the env flag.  A transition
        # normalized earlier in this same tick (including
        # changes_requested -> github_pr_rework) is picked up immediately
        # here.
        if os.environ.get(REWORK_DISPATCH_ENV, "").strip() == "1":
            def _claim_projection(claimed: Any) -> dict[str, Any]:
                ctx = lifecycle_contexts_by_task.get(str(claimed.id))
                if not ctx or "_error" in ctx or "labels" not in ctx:
                    return {"ok": False, "error": "rework context unavailable"}
                try:
                    _, label_reason, label_evidence = _project_pr_lifecycle_labels(
                        client,
                        GithubTaskRef(str(ctx["repository"]), int(ctx["issue_number"])),
                        int(ctx["pr_number"]),
                        add=(WORKING_LABEL,),
                        remove=(REWORK_LABEL, REVIEW_READY_LABEL),
                    )
                except GithubCompletionError as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "label_action": label_reason, "lifecycle": label_evidence}

            def _failure_projection(claimed: Any, stage: str) -> None:
                ctx = lifecycle_contexts_by_task.get(str(claimed.id))
                if not ctx or "_error" in ctx:
                    return
                try:
                    _restore_rework_labels(client, ctx)
                except GithubCompletionError as exc:
                    print(
                        f"kanban-github-sync: failed to restore rework label after {stage} (task {claimed.id}): {type(exc).__name__}",
                        file=sys.stderr,
                    )

            active_pr_owners: dict[tuple[str, int], str] = {}
            for other_row in rows:
                if str(other_row["status"]) != "running":
                    continue
                other_ctx = lifecycle_contexts_by_task.get(str(other_row["id"]))
                if other_ctx and "pr_number" in other_ctx and "_error" not in other_ctx:
                    active_pr_owners[
                        (str(other_ctx["repository"]), int(other_ctx["pr_number"]))
                    ] = str(other_row["id"])
            try:
                dispatch_entries = _dispatch_pending_rework(
                    conn,
                    kanban_db,
                    board,
                    dry_run=dry_run,
                    task_ids=requested,
                    normalized_task_ids=normalized_rework_ids,
                    client=client,
                    rework_contexts=lifecycle_contexts_by_task,
                    active_pr_owners=active_pr_owners,
                    on_claim=_claim_projection,
                    on_failure=_failure_projection,
                )
            except Exception as exc:  # never let the spawn lane break the sync
                dispatch_entries = [{
                    "task_id": None, "status": None, "changed": False,
                    "reason": "rework_dispatch_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }]
            # Structured-result extension (intake Telegram observer): a
            # spawned rework worker is a READY -> RUNNING transition; every
            # dispatch entry carrying a task id gets its repository/issue
            # number attached via the task body ref.
            for entry in dispatch_entries:
                if not entry.get("task_id"):
                    continue
                if entry.get("changed") and entry.get("status") == "running":
                    entry["from_state"] = "ready"
                    entry["to_state"] = "running"
                if "repository" not in entry:
                    body_row = conn.execute(
                        "SELECT body FROM tasks WHERE id = ?", (entry["task_id"],)
                    ).fetchone()
                    if body_row is not None:
                        dref = parse_task_ref(str(body_row["body"] or ""))
                        if dref is not None:
                            entry["repository"] = dref.repository
                            entry["issue_number"] = dref.issue_number
                            entry["issue_title"] = dref.issue_title
            results.extend(dispatch_entries)
        return results


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, help="Kanban board slug to reconcile")
    parser.add_argument(
        "task_ids", nargs="*",
        help="Optional task ids; omit to sync every GitHub-backed task on the board",
    )
    parser.add_argument("--dry-run", action="store_true", help="Query only; never mutate")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        with _edge_single_flight():
            client = GithubApiClient.from_environment()
            results = sync_board(
                args.board,
                args.task_ids or None,
                dry_run=args.dry_run,
                client=client,
            )
    except SyncError as exc:
        print(f"kanban-github-sync: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for item in results:
            marker = "changed" if item.get("changed") else "kept"
            suffix = ""
            if "label_removed" in item:
                suffix = f"; label_removed={item['label_removed']}"
            print(
                f"{item.get('task_id')}: {item.get('status', '?')} "
                f"({marker}; {item.get('reason', '')}){suffix}"
            )
        if not results:
            print("(no GitHub-backed tasks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
