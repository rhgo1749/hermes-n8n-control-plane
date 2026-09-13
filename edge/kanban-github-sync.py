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

Blocked-state read contract:
  * A blocked projection carries ``block_kind`` (``untyped`` for legacy rows),
    pending direct parent ids/statuses, ``dependency_driven``, and
    ``auto_promotable``.  It is derived from ``tasks`` + ``task_links`` only;
    no parallel state store is introduced.
  * The same machine-readable ``block:`` section is included in refreshed
    sync context and blocker comments, so dependency holds and human
    attention holds cannot collapse into ``status=blocked`` alone.
  * The durable block-history read surface (Issue #92) is status-agnostic: a
    canonical ``kanban_block(kind="dependency")`` routes the task to ``todo``
    (auto-promotable) and later ``ready``, so the history is exposed while the
    task is in the dependency path and after auto-promotion, not only while it
    sits in a human ``blocked`` state.  Each historical entry's fallback reason
    is bound to its own ``run_id`` (``task_runs.id = run_id``), never to an
    unrelated newer run; a legacy event without a ``run_id`` leaves the reason
    unavailable rather than attaching a cross-run summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_HERMES_HOME = "/home/hermes/.hermes"
GITHUB_API = "https://api.github.com"
_GITHUB_GRAPHQL_API = f"{GITHUB_API}/graphql"
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
# BLOCKED or operator-recovered REVIEW + rework_human_attention hold. Label
# presence alone is never retry evidence; the edge never restores or creates
# ``agent-rework`` after the one-shot command has been consumed.
REWORK_RETRY_MARKER = "AGENT_REWORK_RETRY"
# Machine-readable explicit abandonment of one closed-unmerged PR.  Unlike a
# plain PR close, this trusted, one-shot signal is the only authorization for
# returning an Issue to a fresh implementation round.
SUPERSEDE_MARKER = "AGENT_PR_SUPERSEDE"
SUPERSEDE_EVENT_KIND = "github_pr_superseded"

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

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        failure_class: str | None = None,
        before_labels: Iterable[str] | None = None,
        desired_labels: Iterable[str] | None = None,
        observed_labels: Iterable[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = status
        self.failure_class = failure_class
        self.before_labels = (
            sorted(str(label) for label in before_labels)
            if before_labels is not None else None
        )
        self.desired_labels = (
            sorted(str(label) for label in desired_labels)
            if desired_labels is not None else None
        )
        self.observed_labels = (
            sorted(str(label) for label in observed_labels)
            if observed_labels is not None else None
        )


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
    # ``None`` means a direct evaluator caller did not provide relationship
    # evidence.  Live verification always replaces this with a finite set
    # from GitHub's PullRequest.closingIssuesReferences field.
    closing_issue_numbers: frozenset[int] | None = None

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
                    "closing_issue_numbers": (
                        sorted(pr.closing_issue_numbers)
                        if pr.closing_issue_numbers is not None else None
                    ),
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

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        """Run one bounded GraphQL read and return its data object.

        GraphQL is used only for fields unavailable from the REST PR payload,
        notably ``PullRequest.closingIssuesReferences``.  Transport, payload,
        and provider-level errors are all non-authoritative failures.
        """
        request = Request(
            _GITHUB_GRAPHQL_API,
            data=json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "hermes-kanban-github-edge-sync",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise GithubCompletionError(
                f"GitHub GraphQL API {exc.code}", status=int(exc.code)
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GithubCompletionError(
                f"GitHub GraphQL request failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, dict) or body.get("errors"):
            raise GithubCompletionError("GitHub GraphQL returned errors")
        data = body.get("data")
        if not isinstance(data, dict):
            raise GithubCompletionError("GitHub GraphQL returned no data")
        return data

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


_CLOSING_ISSUES_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){"
    "number closingIssuesReferences(first:100){"
    "nodes{number} pageInfo{hasNextPage}"
    "}}}}"
)


def _pull_request_closing_issue_numbers(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
) -> frozenset[int]:
    """Read one PR's authoritative GitHub closing-relationship set.

    REST PR payloads do not expose ``closingIssuesReferences``.  A missing,
    malformed, paginated, or unavailable GraphQL response is deliberately an
    error: the caller must preserve the current Kanban state rather than
    treating a mention as either a closer or proof that no closer exists.
    """
    graphql = getattr(client, "graphql", None)
    if not callable(graphql):
        raise GithubCompletionError(
            "GitHub closing-relationship lookup is unavailable"
        )
    owner, separator, name = ref.repository.partition("/")
    if not separator or not owner or not name:
        raise GithubCompletionError(
            f"invalid repository name for closing-relationship lookup: {ref.repository!r}"
        )
    data = graphql(
        _CLOSING_ISSUES_QUERY,
        {"owner": owner, "name": name, "number": int(pr_number)},
    )
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = (
        repository.get("pullRequest")
        if isinstance(repository, dict) else None
    )
    if not isinstance(pull_request, dict):
        raise GithubCompletionError(
            f"GitHub returned no relationship data for PR #{pr_number}"
        )
    if pull_request.get("number") != pr_number:
        raise GithubCompletionError(
            f"GitHub returned mismatched relationship data for PR #{pr_number}"
        )
    connection = pull_request.get("closingIssuesReferences")
    if not isinstance(connection, dict):
        raise GithubCompletionError(
            f"GitHub returned malformed closing relationships for PR #{pr_number}"
        )
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise GithubCompletionError(
            f"GitHub returned incomplete closing relationships for PR #{pr_number}"
        )
    has_next_page = page_info.get("hasNextPage")
    if not isinstance(has_next_page, bool):
        raise GithubCompletionError(
            f"GitHub returned invalid closing-relationship pagination for PR #{pr_number}"
        )
    if has_next_page:
        raise GithubCompletionError(
            f"GitHub closing relationships exceeded the bounded page for PR #{pr_number}"
        )
    issue_numbers: set[int] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise GithubCompletionError(
                f"GitHub returned malformed closing relationship for PR #{pr_number}"
            )
        issue_number = node.get("number")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number < 1
        ):
            raise GithubCompletionError(
                f"GitHub returned invalid closing Issue number for PR #{pr_number}"
            )
        issue_numbers.add(issue_number)
    return frozenset(issue_numbers)


_CLOSING_KEYWORD_PATTERN = re.compile(
    r"(?i)\b(?:closes|close|closed|fixes|fix|fixed|resolves|resolve|resolved)\s+(?:#|https?://github\.com/[^/\s]+/[^/\s]+/issues/)(\d+)\b"
)


def _is_effective_linked_pr(ref: GithubTaskRef, pr: GithubPullRequest) -> bool:
    """Return whether a PR is proven to close this source Issue.

    Multi-tier evidence evaluation:
      1. Explicit GraphQL closingIssuesReferences (strongest GitHub-native relationship)
      2. For merged/closed PRs: explicit plain-text closing keyword in the PR body
         matching ref.issue_number (durable fallback when GraphQL relationship is pruned).
    """
    relationships = pr.closing_issue_numbers
    if relationships is not None and ref.issue_number in relationships:
        return True
    if relationships is None:
        return True
    if pr.is_merged_into_target or pr.state == "closed":
        body = pr.body or ""
        for match in _CLOSING_KEYWORD_PATTERN.finditer(body):
            try:
                if int(match.group(1)) == ref.issue_number:
                    return True
            except ValueError:
                pass
    return False


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
    superseded_pr_numbers: Optional[Iterable[int]] = None,
) -> GithubCompletionDecision:
    """Evaluate completion, excluding only explicitly superseded PRs.

    ``superseded_pr_numbers`` is durable edge evidence recorded by the
    trusted maintainer transition.  It is intentionally separate from the
    existing same-head automatic supersession rule: a closed-unmerged PR is
    never excluded merely because it was closed.
    """
    prs = tuple(sorted(pull_requests, key=lambda item: item.number))
    explicitly_superseded = {
        int(number) for number in (superseded_pr_numbers or ())
    }
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

    effective_prs = tuple(
        pr for pr in prs
        if (
            pr.number not in explicitly_superseded
            and _is_effective_linked_pr(ref, pr)
        )
    )
    if not effective_prs:
        return GithubCompletionDecision(
            desired_status="review",
            reason="no_linked_pr",
            linked_pr_numbers=numbers,
            pull_requests=effective_prs,
        )

    if all(_is_merged_into_target(ref, pr) for pr in effective_prs):
        return GithubCompletionDecision(
            desired_status="done",
            reason="all_linked_prs_merged",
            linked_pr_numbers=numbers,
            pull_requests=effective_prs,
        )

    has_open_pr = any(pr.state == "open" for pr in effective_prs)

    # A closed Issue may have historical PRs that were intentionally replaced
    # after main advanced. Do not let those stale PRs revive an already
    # completed card forever, but only accept supersession when the lineage is
    # unambiguous: no open linked PR remains, and every closed-unmerged PR has
    # a newer linked merge from exactly the same head branch.
    if str(issue_state or "").casefold() == "closed" and not has_open_pr:
        unresolved_closed = tuple(
            pr for pr in effective_prs
            if (
                pr.state == "closed"
                and not _is_merged_into_target(ref, pr)
            )
        )
        superseded = _superseded_closed_pr_numbers(ref, effective_prs)

        if (
            unresolved_closed
            and superseded
            and superseded == {pr.number for pr in unresolved_closed}
        ):
            remaining_prs = tuple(
                pr for pr in effective_prs
                if pr.number not in superseded
            )
            if (
                remaining_prs
                and all(
                    _is_merged_into_target(ref, pr)
                    for pr in remaining_prs
                )
            ):
                return GithubCompletionDecision(
                    desired_status="done",
                    reason="superseded_pr_merged",
                    linked_pr_numbers=numbers,
                    pull_requests=effective_prs,
                )

    if has_open_pr:
        reason = "linked_pr_open"
    elif any(
        pr.state == "closed"
        and not _is_merged_into_target(ref, pr)
        for pr in effective_prs
    ):
        reason = "linked_pr_closed_not_merged"
    else:
        reason = "linked_pr_not_merged"

    return GithubCompletionDecision(
        desired_status="review",
        reason=reason,
        linked_pr_numbers=numbers,
        pull_requests=effective_prs,
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
        pull_requests = tuple(
            replace(
                pr,
                closing_issue_numbers=_pull_request_closing_issue_numbers(
                    client, ref, pr.number
                ),
            )
            for pr in pull_requests
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
            for pr in provisional.pull_requests
            if (
                pr.state == "closed"
                and not _is_merged_into_target(ref, pr)
            )
        }
        superseded = _superseded_closed_pr_numbers(ref, provisional.pull_requests)

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
    """Do not infer a request identity from an arbitrary prior comment.

    A GitHub ``agent-rework`` label is a label-only request.  The previous
    implementation treated the newest trusted comment before the label as its
    request, which allowed a prior completion handoff (or unrelated review
    comment) to contaminate a fresh round.  ``request_comment_id`` is reserved
    for the exact trusted ``AGENT_REWORK_RETRY`` path, whose consumed comment
    id is passed to :func:`apply_rework` explicitly.

    Keep this helper and its signature for callers/tests that exercise the
    label evaluator directly, but deliberately return no inferred identity.
    """
    del client, ref, pr_number, label_added_at
    return None


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
    block_projection: Mapping[str, Any] | None = None,
    block_history: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    prs = tuple(pull_requests)
    contexts = {pr.number: _collect_pr_context(client, ref, pr) for pr in prs}
    return _render_sync_context(
        ref,
        prs,
        contexts,
        rework_pr_number=rework_pr_number,
        block_projection=block_projection,
        block_history=block_history,
    )


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
    block_projection: Mapping[str, Any] | None = None,
    block_history: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    prs = sorted(pull_requests, key=lambda item: item.number)
    lines: list[str] = [SYNC_CONTEXT_BEGIN, ""]
    # The ``## Block`` read surface is emitted when EITHER the current blocked
    # projection is available OR a historical block history is present.  The
    # two are intentionally decoupled: a canonical dependency block routes the
    # task to ``todo`` (auto-promotable) and later ``ready``, so there is no
    # current ``blocked`` projection — yet the dependency-wait history must
    # still be exposed on the same read surface (Issue #92).  A dependency
    # hold therefore never collapses into an undifferentiated reading.
    if block_projection is not None or block_history:
        lines.append("## Block")
        lines.append("")
        if block_projection is not None:
            block_kind = str(block_projection.get("block_kind") or "untyped")
            pending = list(block_projection.get("pending_parents") or [])
            pending_ids = [
                str(item.get("id"))
                for item in pending
                if isinstance(item, Mapping) and item.get("id")
            ]
            lines.extend(
                [
                    "block:",
                    f"  block_kind: {block_kind}",
                    f"  dependency_driven: {str(bool(block_projection.get('dependency_driven'))).lower()}",
                    f"  auto_promotable: {str(bool(block_projection.get('auto_promotable'))).lower()}",
                    f"  pending_parent_ids: {json.dumps(pending_ids, ensure_ascii=False)}",
                    f"  pending_parents: {json.dumps(pending, ensure_ascii=False, sort_keys=True)}",
                ]
            )
            if block_projection.get("projection_error"):
                lines.append(f"  projection_error: {block_projection['projection_error']}")
        if block_history:
            lines.append("block_history:")
            for entry in block_history:
                lines.append(
                    "- at: "
                    f"{entry.get('at')} kind={entry.get('kind')} "
                    f"block_kind={entry.get('block_kind')} "
                    f"dependency_driven={str(bool(entry.get('dependency_driven'))).lower()} "
                    f"auto_promotable={str(bool(entry.get('auto_promotable'))).lower()}"
                )
                if entry.get("source_status"):
                    lines.append(f"  source_status: {entry['source_status']}")
                if entry.get("reason"):
                    lines.append(f"  reason: {entry['reason']}")
        lines.append("")
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


def _blocked_state_projection(
    conn: sqlite3.Connection,
    task_id: str,
    block_kind: Any,
) -> dict[str, Any]:
    """Project durable blocked semantics without inferring a missing kind.

    ``block_kind`` is intentionally rendered as ``untyped`` when the legacy
    nullable column is empty.  Pending parents are read from the canonical
    direct ``task_links`` graph; a lookup failure is represented explicitly
    and makes ``auto_promotable`` false rather than guessing.
    """
    kind = str(block_kind or "untyped")
    projection: dict[str, Any] = {
        "block_kind": kind,
        "pending_parents": [],
        "pending_parent_ids": [],
        "dependency_driven": kind == "dependency",
        "auto_promotable": kind == "dependency",
    }
    try:
        gate = _internal_dependency_gate(conn, task_id)
        pending = list(gate.get("pending") or [])
        projection["pending_parents"] = pending
        projection["pending_parent_ids"] = [str(item["id"]) for item in pending]
    except (SyncError, sqlite3.Error) as exc:
        projection["projection_error"] = f"{type(exc).__name__}: {exc}"
        projection["auto_promotable"] = False
    return projection


def _task_block_history(conn: sqlite3.Connection, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Reconstruct the block-semantics history for ``task_id``.

    Issue #92 requires that a past ``status=blocked`` / ``outcome=blocked`` run
    does not collapse to an undifferentiated ``blocked`` reading.  This reads
    the canonical durable sources (``task_events`` kind ``blocked`` /
    ``dependency_wait`` / ``block_loop_detected``, plus the matching
    ``task_runs`` ``outcome=blocked`` row) and projects each historical block
    with its ``block_kind``, ``dependency_driven`` / ``auto_promotable``
    semantics, and a bounded reason.  A missing legacy ``kind`` is rendered as
    ``untyped`` rather than guessed.  No new parallel store is introduced.
    """
    entries: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT kind, payload, created_at, id, run_id FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'dependency_wait', 'block_loop_detected') "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    for row in rows:
        payload: dict[str, Any] = {}
        if row["payload"]:
            try:
                loaded = json.loads(row["payload"])
                if isinstance(loaded, dict):
                    payload = loaded
            except (TypeError, ValueError):
                payload = {}
        kind = payload.get("kind")
        kind_text = "untyped" if kind in (None, "") else str(kind)
        reason = str(payload.get("reason") or "").strip()
        run_id = row["run_id"]
        if not reason and run_id is not None:
            # Bind the fallback reason to THIS event's own run, never to an
            # unrelated (e.g. newer) blocked run.  A legacy event without a
            # run id leaves the reason unavailable rather than attaching a
            # cross-run summary (Issue #92 run-provenance invariant).
            run = conn.execute(
                "SELECT summary FROM task_runs "
                "WHERE task_id = ? AND id = ? AND outcome = 'blocked' "
                "AND summary IS NOT NULL LIMIT 1",
                (task_id, run_id),
            ).fetchone()
            if run and run["summary"]:
                reason = str(run["summary"]).strip()
        entries.append(
            {
                "kind": row["kind"],
                "block_kind": kind_text,
                "dependency_driven": kind == "dependency",
                "auto_promotable": kind == "dependency",
                "recurrences": payload.get("recurrences"),
                "source_status": payload.get("source_status"),
                "run_id": run_id,
                "reason": _truncate(reason, MAX_ITEM_CHARS) if reason else "",
                "at": row["created_at"],
            }
        )
    return entries


def _blocker_comment_body(
    task_id: str,
    reason: str,
    needs: str,
    *,
    no_pr: bool,
    block_projection: Mapping[str, Any] | None = None,
) -> str:
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
    if block_projection is not None:
        block_kind = str(block_projection.get("block_kind") or "untyped")
        pending_ids = list(block_projection.get("pending_parent_ids") or [])
        lines += [
            "",
            "Block metadata:",
            f"block: block_kind={block_kind}",
            f"block: dependency_driven={str(bool(block_projection.get('dependency_driven'))).lower()}",
            f"block: auto_promotable={str(bool(block_projection.get('auto_promotable'))).lower()}",
            f"block: pending_parent_ids={json.dumps(pending_ids, ensure_ascii=False)}",
        ]
        if block_projection.get("projection_error"):
            lines.append(f"block: projection_error={block_projection['projection_error']}")
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


def _import_kanban_db_connect():
    """Import the canonical Hermes DB connection module.

    ``connect_closing`` moved out of ``hermes_cli.kanban_db`` and its plugin
    compatibility alias is removed on 2026-09-14.  Keep the edge on the
    supported split-module boundary instead of depending on that shim.
    """
    try:
        from hermes_cli import kanban_db_connect  # type: ignore
        return kanban_db_connect
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SyncError(
            "hermes_cli.kanban_db_connect is not importable from this interpreter "
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
    run_id: Optional[int] = None,
) -> None:
    timestamp = int(time.time()) if created_at is None else int(created_at)
    conn.execute(
        f"INSERT INTO {event_table} (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            task_id,
            int(run_id) if run_id is not None else None,
            kind,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            timestamp,
        ),
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


# ---------------------------------------------------------------------------
# Terminal merge convergence (Issue #73): a stale rework graph under a
# closed Issue whose required PR(s) merged.
#
# A merged GitHub PR is an authoritative fact, but the internal dependency
# gate (``_restore_pending_dependency``) holds the intake root open while
# ANY reachable node is still non-terminal -- including *stale* blocked or
# unstarted rework/reviewer nodes whose work was already delivered and
# merged on GitHub.  ``_attempt_terminal_merge_convergence`` closes that
# gap in one safe reconciliation pass, and only when the full authority
# chain is freshly proven: the card is a canonical GitHub Issue intake
# root, the reachable dependency chain has no active claim/run/worker
# ownership, the source Issue is freshly read as ``closed``, and every
# linked PR is freshly read as closed+merged into the target branch.
# Any missing or non-authoritative evidence fails closed and preserves the
# graph untouched; the path never bypasses the dependency gate for active
# work, and never promotes, claims, spawns, or re-runs a worker.
# ---------------------------------------------------------------------------

# Ancestor states the convergence pass may terminalize / archive.  Anything
# else (unknown status, active pipeline states) fails the pass closed.
_TERMINAL_CONVERGE_OK_TERMINAL = frozenset({"done", "archived"})
_TERMINAL_CONVERGE_TERMINALIZE = frozenset({"blocked"})
_TERMINAL_CONVERGE_ARCHIVE = frozenset({"todo", "review", "ready", "scheduled"})
# Intake-root states eligible for the authoritative done projection.
_TERMINAL_CONVERGE_ROOT_STATUSES = frozenset({"todo", "review"})
_TERMINAL_CONVERGE_DEVELOPER_ASSIGNEES = frozenset({"kanban-developer"})
_TERMINAL_CONVERGE_REVIEWER_ASSIGNEES = frozenset({"kanban-reviewer"})
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _terminal_chain_ancestors(
    conn: sqlite3.Connection,
    root_id: str,
) -> tuple[Optional[dict[str, dict[str, Any]]], Optional[str]]:
    """Walk the direct ``task_links`` parent edges above ``root_id``.

    Returns ``(nodes, None)`` where ``nodes`` maps every reachable id
    (root plus all ancestors) to a row dict carrying the ownership
    signals, and ``(None, error)`` for a dangling or missing edge.  One
    broken edge is an ambiguous graph: it must fail the convergence pass
    closed, never crash the whole board sync and never be treated as a
    satisfied dependency.
    """
    columns = (
        "t.id, t.status, t.assignee, t.claim_lock, t.claim_expires, "
        "t.worker_pid, t.current_run_id, t.block_kind, t.block_recurrences, "
        "t.completed_at"
    )
    root_row = conn.execute(
        f"SELECT {columns} FROM tasks AS t WHERE t.id = ?", (root_id,)
    ).fetchone()
    if root_row is None:
        return None, f"missing root task: {root_id}"
    nodes: dict[str, dict[str, Any]] = {
        root_id: {key: root_row[key] for key in root_row.keys()}
    }
    def _parent_rows(child_id: str) -> list[Any]:
        return conn.execute(
            f"SELECT l.parent_id, {columns} "
            f"FROM task_links AS l LEFT JOIN tasks AS t ON t.id = l.parent_id "
            "WHERE l.child_id = ? ORDER BY l.parent_id",
            (child_id,),
        ).fetchall()

    # Iterative depth-first traversal with white/gray/black colors.  A gray
    # parent is on the active path and therefore proves a cycle; a black
    # parent is a completed shared ancestor and is safe to skip.  Keeping the
    # active path separate from pending sibling work preserves diamond graphs
    # without re-queuing a cycle forever.
    colors: dict[str, int] = {root_id: 1}
    stack: list[tuple[str, list[Any], int]] = [
        (root_id, _parent_rows(root_id), 0)
    ]
    while stack:
        child_id, parent_rows, index = stack[-1]
        if index >= len(parent_rows):
            colors[child_id] = 2
            stack.pop()
            continue
        row = parent_rows[index]
        stack[-1] = (child_id, parent_rows, index + 1)
        parent_id = str(row["parent_id"])
        if row["id"] is None:
            return None, f"missing parent task for link: {parent_id}"
        parent_color = colors.get(parent_id, 0)
        if parent_color == 1:
            return None, f"cyclic task link: {parent_id} -> {child_id}"
        if parent_color == 2:
            continue
        nodes[parent_id] = {
            "id": parent_id,
            "status": row["status"],
            "assignee": row["assignee"],
            "claim_lock": row["claim_lock"],
            "claim_expires": row["claim_expires"],
            "worker_pid": row["worker_pid"],
            "current_run_id": row["current_run_id"],
            "block_kind": row["block_kind"],
            "block_recurrences": row["block_recurrences"],
            "completed_at": row["completed_at"],
        }
        colors[parent_id] = 1
        stack.append((parent_id, _parent_rows(parent_id), 0))
    return nodes, None


def _terminal_chain_edge_snapshot(
    conn: sqlite3.Connection,
    node_ids: Iterable[str],
) -> tuple[Optional[tuple[tuple[str, str], ...]], Optional[str]]:
    """Return every reachable parent edge, including duplicate rows.

    The edge list is part of the convergence snapshot: a late parent link,
    removed link, duplicate, or dangling link must not be hidden by comparing
    node rows alone.
    """
    ids = tuple(sorted({str(node_id) for node_id in node_ids}))
    if not ids:
        return (), None
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links "
            f"WHERE child_id IN ({placeholders}) "
            "ORDER BY child_id, parent_id",
            ids,
        ).fetchall()
    except sqlite3.Error as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return tuple(
        (str(row["parent_id"]), str(row["child_id"])) for row in rows
    ), None


def _task_creation_provenance(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Return the durable role and parent snapshot for one task.

    ``create_task`` records the initial assignee and parent list in its
    ``created`` event.  Later canonical ``link_tasks`` calls record ``linked``
    events, so the accepted parent snapshot includes both sources and can be
    compared with the live edge set without trusting title/body text.
    """
    rows = conn.execute(
        "SELECT kind, payload, created_at, id FROM task_events "
        "WHERE task_id = ? AND kind IN ('created', 'linked') "
        "ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    created_payload: Optional[dict[str, Any]] = None
    recorded_parents: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        kind = str(row["kind"])
        if kind == "created":
            if created_payload is not None:
                return None
            raw_parents = payload.get("parents")
            if (
                not isinstance(raw_parents, list)
                or any(
                    not isinstance(parent_id, str) or not parent_id
                    for parent_id in raw_parents
                )
            ):
                return None
            created_payload = payload
            recorded_parents.update(raw_parents)
        else:
            parent_id = payload.get("parent")
            child_id = payload.get("child")
            if str(child_id) != task_id or not isinstance(parent_id, str) or not parent_id:
                return None
            recorded_parents.add(parent_id)
    if created_payload is None:
        return None
    assignee = created_payload.get("assignee")
    if not isinstance(assignee, str) or not assignee:
        return None
    return {
        "assignee": assignee,
        "parents": tuple(sorted(recorded_parents)),
    }


def _terminal_convergence_rework_is_current_governing_event(
    conn: sqlite3.Connection,
    task_id: str,
    rework_kind: str,
    rework_created_at: int,
    rework_event_id: int,
) -> bool:
    """Prove the selected rework event is still the governing transition.

    The edge has one canonical ordering for status-affecting lifecycle events:
    ``(created_at, id)`` over ``_REWORK_GOVERNING_KINDS``.  Terminal
    convergence must not reuse a validated old rework event after any newer
    governing transition, even when the task row still looks stale.
    """
    kinds = tuple(sorted(_REWORK_GOVERNING_KINDS))
    placeholders = ",".join("?" * len(kinds))
    row = conn.execute(
        "SELECT kind, created_at, id FROM task_events "
        f"WHERE task_id = ? AND kind IN ({placeholders}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,) + kinds,
    ).fetchone()
    if row is None:
        return False
    return (
        str(row["kind"]) == rework_kind
        and int(row["created_at"] or 0) == rework_created_at
        and int(row["id"]) == rework_event_id
    )


def _terminal_convergence_rework_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    ref: GithubTaskRef,
    merged_prs: Optional[Mapping[int, str]] = None,
) -> Optional[dict[str, Any]]:
    """Return the latest validated rework round for a stale node.

    The round must be edge-owned, tied to this exact Issue, and carry a full
    head SHA.  When fresh GitHub evidence is available, both the PR number and
    head SHA must match that merged evidence; an old or copied rework marker is
    not enough to authorize terminal cleanup.
    """
    row = conn.execute(
        "SELECT kind, payload, created_at, id FROM task_events "
        "WHERE task_id = ? AND kind IN ('github_pr_rework', 'github_pr_rework_retry') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    rework_created_at = int(row["created_at"] or 0)
    rework_event_id = int(row["id"])
    if not _terminal_convergence_rework_is_current_governing_event(
        conn,
        task_id,
        str(row["kind"]),
        rework_created_at,
        rework_event_id,
    ):
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("repository") != ref.repository
        or payload.get("issue_number") != ref.issue_number
        or payload.get("reason") != "agent_rework"
        or payload.get("merge_authority") != "human"
        or payload.get("auto_merge") is not False
        or payload.get("source") not in {
            "github", "github_edge_rework_recovery",
        }
        or payload.get("new_status") != "ready"
    ):
        return None
    raw_pr_number = payload.get("pr_number")
    raw_round = payload.get("rework_round")
    pr_number = raw_pr_number if isinstance(raw_pr_number, int) and not isinstance(raw_pr_number, bool) else None
    rework_round = raw_round if isinstance(raw_round, int) and not isinstance(raw_round, bool) else None
    head_sha = payload.get("head_sha")
    if (
        pr_number is None
        or pr_number <= 0
        or rework_round is None
        or rework_round <= 0
        or not isinstance(head_sha, str)
        or _FULL_SHA_RE.fullmatch(head_sha) is None
    ):
        return None
    if merged_prs is not None:
        expected_head = merged_prs.get(pr_number)
        if expected_head is None or head_sha.casefold() != expected_head.casefold():
            return None
    human_hold = _terminal_convergence_current_round_human_hold(
        conn,
        task_id,
        payload,
        rework_created_at,
        rework_event_id,
    )
    if human_hold is not None:
        return {
            "role": "human_hold",
            "human_hold": human_hold,
        }
    return {
        "role": "stale_rework",
        "event_kind": str(row["kind"]),
        "pr_number": pr_number,
        "rework_round": rework_round,
        "head_sha": head_sha,
    }


def _terminal_convergence_current_round_human_hold(
    conn: sqlite3.Connection,
    task_id: str,
    rework_payload: Mapping[str, Any],
    rework_created_at: int,
    rework_event_id: int,
) -> Optional[dict[str, Any]]:
    """Find a durable human hold newer than the governing rework round.

    A stale ``blocked`` status is not itself a human hold: the original
    rework graph intentionally converges even when the old row still carries
    block metadata.  A later canonical ``blocked`` event, however, is an
    explicit worker/operator hold and must remain sticky.  Attention events
    are guarded more narrowly by the current round's repository/Issue/PR and
    round identity; malformed or mismatched later attention is ambiguous and
    must remain sticky rather than being ignored.  An earlier-round attention
    record cannot suppress a newer round.  Event id is included in the
    ordering because the DB timestamps have second-level precision and a
    later event may share the rework timestamp.
    """
    current_identity = tuple(
        rework_payload.get(key)
        for key in ("repository", "issue_number", "pr_number", "rework_round")
    )
    rows = conn.execute(
        "SELECT kind, payload, created_at, id FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'github_pr_rework_attention') "
        "ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    for row in rows:
        event_order = (int(row["created_at"] or 0), int(row["id"]))
        if event_order <= (rework_created_at, rework_event_id):
            continue
        kind = str(row["kind"])
        if kind == "blocked":
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            return {
                "event_kind": kind,
                "event_id": int(row["id"]),
                "created_at": event_order[0],
                "block_kind": payload.get("kind"),
            }
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            return {
                "event_kind": kind,
                "event_id": int(row["id"]),
                "created_at": event_order[0],
                "reason": "rework_attention_malformed",
            }
        attention_identity = tuple(
            payload.get(key)
            for key in ("repository", "issue_number", "pr_number", "rework_round")
        )
        if attention_identity != current_identity:
            return {
                "event_kind": kind,
                "event_id": int(row["id"]),
                "created_at": event_order[0],
                "reason": "rework_attention_mismatched",
            }
        return {
            "event_kind": kind,
            "event_id": int(row["id"]),
            "created_at": event_order[0],
            "rework_round": rework_payload.get("rework_round"),
            "pr_number": rework_payload.get("pr_number"),
        }
    return None


def _terminal_convergence_node_provenance(
    conn: sqlite3.Connection,
    root_id: str,
    ref: GithubTaskRef,
    nodes: Mapping[str, Mapping[str, Any]],
    edges: Iterable[tuple[str, str]],
    *,
    merged_prs: Optional[Mapping[int, str]] = None,
) -> tuple[Optional[dict[str, dict[str, Any]]], Optional[dict[str, Any]]]:
    """Classify every mutable ancestor from durable role/round evidence.

    Status alone is intentionally insufficient.  A mutable ``blocked`` node
    needs a validated edge rework round, while every unstarted allowed-status
    node needs the canonical ``kanban-reviewer`` creation role and a recorded
    dependency on that stale rework node.  Existing terminal ancestors are
    safe because this pass never mutates them.
    """
    direct_parents: dict[str, set[str]] = {str(node_id): set() for node_id in nodes}
    for parent_id, child_id in edges:
        if child_id in direct_parents:
            direct_parents[child_id].add(parent_id)

    provenance: dict[str, dict[str, Any]] = {
        root_id: {"role": "intake_root"},
    }
    stale_rework_ids: set[str] = set()

    def failure(node_id: str, status: str, reason: str) -> tuple[None, dict[str, Any]]:
        return None, {
            "node_id": node_id,
            "node_status": status,
            "provenance_reason": reason,
        }

    for node_id, node in sorted(nodes.items()):
        if node_id == root_id:
            continue
        status = str(node.get("status") or "")
        if status in _TERMINAL_CONVERGE_OK_TERMINAL:
            provenance[node_id] = {"role": "already_terminal", "status": status}
            continue
        if status not in _TERMINAL_CONVERGE_TERMINALIZE | _TERMINAL_CONVERGE_ARCHIVE:
            return failure(node_id, status, "status_not_convergeable")
        if status not in _TERMINAL_CONVERGE_TERMINALIZE:
            continue
        event = _terminal_convergence_rework_provenance(
            conn, node_id, ref, merged_prs
        )
        creation = _task_creation_provenance(conn, node_id)
        if (
            event is None
            or creation is None
            or creation["assignee"] not in _TERMINAL_CONVERGE_DEVELOPER_ASSIGNEES
            or set(creation["parents"]) != direct_parents[node_id]
            or not direct_parents[node_id]
        ):
            return failure(node_id, status, "missing_stale_rework_provenance")
        if event.get("role") == "human_hold":
            return failure(node_id, status, "current_round_human_hold")
        provenance[node_id] = {
            **event,
            "role": "stale_rework",
        }
        stale_rework_ids.add(node_id)

    for node_id, node in sorted(nodes.items()):
        if node_id == root_id:
            continue
        status = str(node.get("status") or "")
        if status not in _TERMINAL_CONVERGE_ARCHIVE:
            continue
        creation = _task_creation_provenance(conn, node_id)
        parents = direct_parents[node_id]
        nonterminal_parents = {
            parent_id
            for parent_id in parents
            if parent_id in nodes
            and str(nodes[parent_id].get("status") or "")
            not in _TERMINAL_CONVERGE_OK_TERMINAL
        }
        if (
            creation is None
            or creation["assignee"] not in _TERMINAL_CONVERGE_REVIEWER_ASSIGNEES
            or set(creation["parents"]) != parents
            or not parents
            or not parents.intersection(stale_rework_ids)
            or not nonterminal_parents.issubset(stale_rework_ids)
        ):
            return failure(node_id, status, "missing_reviewer_provenance")
        provenance[node_id] = {
            "role": "stale_reviewer",
            "assignee": creation["assignee"],
            "parents": sorted(parents),
        }
    return provenance, None


def _node_has_active_ownership(node: Mapping[str, Any]) -> bool:
    """Live claim/run/worker ownership on a node blocks convergence."""
    if str(node.get("status") or "") == "running":
        return True
    return bool(
        node.get("claim_lock")
        or node.get("worker_pid")
        or node.get("current_run_id")
    )


def _issue_is_closed(client: Any, ref: GithubTaskRef) -> bool:
    """Fresh authoritative source-Issue state read.

    A missing/invalid/non-authoritative read raises
    ``GithubCompletionError`` -- the caller fails closed on it rather than
    guessing the Issue state.
    """
    payload, _ = client.get(f"/repos/{ref.repository}/issues/{ref.issue_number}")
    if not isinstance(payload, dict):
        raise GithubCompletionError(
            f"GitHub returned an invalid Issue response for #{ref.issue_number}"
        )
    state = str(payload.get("state", "")).casefold()
    if state not in {"open", "closed"}:
        raise GithubCompletionError(
            f"GitHub returned incomplete Issue data for #{ref.issue_number}"
        )
    return state == "closed"


def _terminal_convergence_evidence(
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
) -> Optional[dict[str, Any]]:
    """Assemble fresh GitHub evidence for a qualifying merged convergence.

    Returns None when the evidence is not fully authoritative: a
    non-authoritative or non-done decision, a PR set where not every
    linked PR is closed+merged into the configured target branch (an
    open or closed-unmerged PR fails closed), or a non-authoritative /
    open source-Issue read.  On success the result carries the merged PR
    records that become the durable convergence provenance.
    """
    if not decision.authoritative or decision.desired_status != "done":
        return None
    prs = decision.pull_requests
    if not prs or any(not _is_merged_into_target(ref, pr) for pr in prs):
        return None
    try:
        if not _issue_is_closed(client, ref):
            return None
    except GithubCompletionError:
        return None
    return {
        "issue_state": "closed",
        "target_branch": ref.target_branch,
        "merged_prs": [
            {
                "number": pr.number,
                "head_sha": pr.head_sha,
                "base_branch": pr.base_branch,
            }
            for pr in prs
        ],
    }


def _attempt_terminal_merge_convergence(
    conn: sqlite3.Connection,
    client: Any,
    task_id: str,
    row: Mapping[str, Any],
    ref: GithubTaskRef,
    text_sources: list[str],
    *,
    dry_run: bool = False,
) -> Optional[dict[str, Any]]:
    """Single-pass terminal convergence of a stale rework graph.

    Eligible only when the card is a canonical GitHub Issue intake root
    in a pre-terminal state whose dependency gate is PENDING (the classic
    lane would otherwise restore it), and when every reachable ancestor
    is in a stale shape: terminal (``done``/``archived``), a stale
    ``blocked`` implementation/rework node, or an unstarted
    ``todo``/``review``/``ready``/``scheduled`` reviewer/waiting node --
    with no active claim/run/worker ownership anywhere in the chain.  Every
    mutable ancestor must also carry durable role evidence: a matching
    ``github_pr_rework`` round for blocked developer work, or a canonical
    reviewer creation event linked to that rework node for unstarted review
    work.
    The fresh GitHub read must then prove the source Issue ``closed`` and
    every linked PR closed+merged into the target branch.

    When all of that holds, one transaction terminalizes the whole
    graph: stale ``blocked`` nodes become ``done`` (the merged PR is the
    authoritative record that their work was delivered), unstarted
    reviewer/waiting nodes become ``archived`` (never a fabricated
    reviewer PASS), and the intake root projects to authoritative
    ``done`` -- each with a durable ``github_pr_sync`` event carrying the
    fresh GitHub provenance and stale claim/block fields cleared.

    Every other shape fails closed with a diagnostic entry and leaves the
    graph untouched; no worker is promoted, claimed, spawned, or re-run.
    A second pass over a converged graph is a no-op: the root is already
    terminal and the dependency gate is satisfied, so the classic lanes
    handle it through the existing idempotent contract.
    """
    root_status = str(row["status"])
    if root_status not in _TERMINAL_CONVERGE_ROOT_STATUSES:
        return None
    if not is_github_backed_body(row["body"]):
        return None
    try:
        gate = _internal_dependency_gate(conn, task_id)
    except (SyncError, sqlite3.Error) as exc:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_dependency_ambiguous",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not gate["pending"]:
        # No pending internal dependency: the classic lanes own this card.
        return None
    nodes, error = _terminal_chain_ancestors(conn, task_id)
    if nodes is None:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_ambiguous_graph",
            "error": error,
        }
    edges, edge_error = _terminal_chain_edge_snapshot(conn, nodes.keys())
    if edges is None:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_ambiguous_graph",
            "error": edge_error or "edge snapshot unavailable",
        }
    root_body = str(row["body"] or "")
    for node_id, node in sorted(nodes.items()):
        if _node_has_active_ownership(node):
            # A parked GitHub-backed review root must never stay externally
            # projected while live internal work is still owned.  Terminal
            # convergence is not eligible in this shape, so fall through to
            # the classic dependency lane, which repairs review -> todo and
            # leaves the active parent untouched.  Keep the existing
            # diagnostic for already-todo roots where no repair is needed.
            if root_status == "review":
                return None
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_active_ownership",
                "evidence": {
                    "node_id": node_id,
                    "node_status": str(node.get("status") or ""),
                },
            }
    provenance, provenance_error = _terminal_convergence_node_provenance(
        conn, task_id, ref, nodes, edges
    )
    if provenance is None:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_node_unconvergeable",
            "evidence": provenance_error or {"provenance": "unavailable"},
        }
    terminalize = sorted(
        node_id
        for node_id, item in provenance.items()
        if item.get("role") == "stale_rework"
    )
    archive = sorted(
        node_id
        for node_id, item in provenance.items()
        if item.get("role") == "stale_reviewer"
    )
    # DB-local shape pre-filter passed: the pending chain is stale, not
    # active work.  The fresh GitHub read is now the authority gate.
    decision = verify_completion(client, ref, text_sources)
    evidence = _terminal_convergence_evidence(client, ref, decision)
    if evidence is None:
        # Open Issue, open/closed-unmerged PR, or non-authoritative read:
        # preserve the graph; the classic gate lane reports the pending
        # dependency unchanged.
        return None
    merged_pr_heads = {
        int(item["number"]): str(item["head_sha"])
        for item in evidence.get("merged_prs", [])
        if isinstance(item, dict)
        and isinstance(item.get("number"), int)
        and isinstance(item.get("head_sha"), str)
    }
    verified_provenance, verified_provenance_error = (
        _terminal_convergence_node_provenance(
            conn,
            task_id,
            ref,
            nodes,
            edges,
            merged_prs=merged_pr_heads,
        )
    )
    if verified_provenance is None:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_node_unconvergeable",
            "evidence": verified_provenance_error or {"provenance": "unavailable"},
        }
    if verified_provenance != provenance:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_convergence_state_changed",
            "evidence": {
                "expected_provenance": provenance,
                "actual_provenance": verified_provenance,
            },
        }
    evidence = {
        **evidence,
        "convergence_provenance": {
            node_id: item
            for node_id, item in verified_provenance.items()
            if node_id != task_id
        },
    }
    if dry_run:
        return {
            "task_id": task_id,
            "status": root_status,
            "changed": False,
            "reason": "terminal_merge_convergence_predicted",
            "evidence": {
                **evidence,
                "terminalize": terminalize,
                "archive": archive,
            },
        }
    with conn:
        # Acquire the write lock before the final closure read.  GitHub is an
        # external authority and may have taken seconds to answer; the
        # original prevalidation is only a snapshot.  Re-read every node and
        # edge while the write transaction owns the database, then refuse the
        # pass before any mutation when the graph drifted.
        conn.execute("BEGIN IMMEDIATE")
        fresh_nodes, fresh_error = _terminal_chain_ancestors(conn, task_id)
        if fresh_nodes is None:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_ambiguous_graph",
                "error": fresh_error or "ancestor closure unavailable",
            }
        fresh_edges, fresh_edge_error = _terminal_chain_edge_snapshot(
            conn, fresh_nodes.keys()
        )
        if fresh_edges is None:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_ambiguous_graph",
                "error": fresh_edge_error or "edge snapshot unavailable",
            }
        if fresh_nodes != nodes or fresh_edges != edges:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_state_changed",
                "evidence": {
                    "expected_node_ids": sorted(nodes),
                    "actual_node_ids": sorted(fresh_nodes),
                    "expected_edges": [list(edge) for edge in edges],
                    "actual_edges": [list(edge) for edge in fresh_edges],
                },
            }
        root_row = conn.execute(
            "SELECT body, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            root_row is None
            or str(root_row["body"] or "") != root_body
            or str(root_row["status"] or "") != root_status
        ):
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_state_changed",
                "evidence": {"node_id": task_id, "node_status": root_status},
            }
        try:
            fresh_gate = _internal_dependency_gate(conn, task_id)
        except (SyncError, sqlite3.Error) as exc:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_ambiguous_graph",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if fresh_gate.get("parents") != gate.get("parents"):
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_state_changed",
                "evidence": {
                    "expected_parents": gate.get("parents"),
                    "actual_parents": fresh_gate.get("parents"),
                },
            }
        for node_id, node in sorted(fresh_nodes.items()):
            if _node_has_active_ownership(node):
                return {
                    "task_id": task_id,
                    "status": root_status,
                    "changed": False,
                    "reason": "terminal_convergence_active_ownership",
                    "evidence": {
                        "node_id": node_id,
                        "node_status": str(node.get("status") or ""),
                    },
                }
        fresh_provenance, fresh_provenance_error = (
            _terminal_convergence_node_provenance(
                conn,
                task_id,
                ref,
                fresh_nodes,
                fresh_edges,
                merged_prs=merged_pr_heads,
            )
        )
        if fresh_provenance is None:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_node_unconvergeable",
                "evidence": fresh_provenance_error or {"provenance": "unavailable"},
            }
        if fresh_provenance != verified_provenance:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_state_changed",
                "evidence": {
                    "expected_provenance": verified_provenance,
                    "actual_provenance": fresh_provenance,
                },
            }
        fresh_terminalize = sorted(
            node_id
            for node_id, item in fresh_provenance.items()
            if item.get("role") == "stale_rework"
        )
        fresh_archive = sorted(
            node_id
            for node_id, item in fresh_provenance.items()
            if item.get("role") == "stale_reviewer"
        )
        if fresh_terminalize != terminalize or fresh_archive != archive:
            return {
                "task_id": task_id,
                "status": root_status,
                "changed": False,
                "reason": "terminal_convergence_state_changed",
                "evidence": {
                    "expected_terminalize": terminalize,
                    "actual_terminalize": fresh_terminalize,
                    "expected_archive": archive,
                    "actual_archive": fresh_archive,
                },
            }
        now = int(time.time())
        # Stale blocked implementation/rework node: the merged PR is the
        # authoritative record that the work was delivered, so
        # terminalize it to done with explicit GitHub-merge provenance.
        for node_id in terminalize:
            node = nodes[node_id]
            cur = conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ?, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "current_run_id = NULL, block_kind = NULL, "
                "block_recurrences = 0 "
                "WHERE id = ? AND status = ? AND claim_lock IS NULL "
                "AND worker_pid IS NULL AND current_run_id IS NULL",
                (now, node_id, str(node["status"])),
            )
            if cur.rowcount != 1:
                raise SyncError(
                    f"terminal convergence state changed during sync: {node_id}"
                )
            _append_sync_event(
                conn,
                node_id,
                {
                    "source": "github",
                    "previous_status": str(node["status"]),
                    "new_status": "done",
                    "reason": "terminal_merge_convergence",
                    **evidence,
                    "merge_authority": "human",
                    "auto_merge": False,
                },
                kind="github_pr_sync",
            )
        # Unstarted reviewer/waiting node: archive it rather than fabricate
        # a reviewer PASS; the merged PR supersedes the review lane.
        for node_id in archive:
            node = nodes[node_id]
            cur = conn.execute(
                "UPDATE tasks SET status = 'archived', assignee = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "current_run_id = NULL, block_kind = NULL, "
                "block_recurrences = 0 "
                "WHERE id = ? AND status = ? AND claim_lock IS NULL "
                "AND worker_pid IS NULL AND current_run_id IS NULL",
                (node_id, str(node["status"])),
            )
            if cur.rowcount != 1:
                raise SyncError(
                    f"terminal convergence state changed during sync: {node_id}"
                )
            _append_sync_event(
                conn,
                node_id,
                {
                    "source": "github",
                    "previous_status": str(node["status"]),
                    "new_status": "archived",
                    "reason": "terminal_merge_convergence_archived_unstarted",
                    **evidence,
                    "merge_authority": "human",
                    "auto_merge": False,
                },
                kind="github_pr_sync",
            )
        # Intake root: authoritative done with the same fresh evidence and
        # the full converged set recorded as the durable event trail.
        dependency_guard = _dependency_guard_sql()
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ?, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "current_run_id = NULL, block_kind = NULL, block_recurrences = 0 "
            "WHERE id = ? AND status = ? AND claim_lock IS NULL "
            "AND worker_pid IS NULL AND current_run_id IS NULL "
            f"{dependency_guard}",
            (now, task_id, root_status, task_id),
        )
        if cur.rowcount != 1:
            raise SyncError(
                "terminal convergence state changed during sync: root"
            )
        _append_sync_event(
            conn,
            task_id,
            {
                "source": "github",
                "previous_status": root_status,
                "new_status": "done",
                "reason": "terminal_merge_convergence",
                "converged": sorted(terminalize + archive),
                **evidence,
                "merge_authority": "human",
                "auto_merge": False,
            },
            kind="github_pr_sync",
        )
    return {
        "task_id": task_id,
        "status": "done",
        "changed": True,
        "reason": "terminal_merge_convergence",
        "evidence": {
            **evidence,
            "converged": sorted(terminalize + archive),
        },
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



def apply_supersede(
    conn: sqlite3.Connection,
    task_id: str,
    ref: GithubTaskRef,
    signal: Mapping[str, Any],
    pr: GithubPullRequest,
) -> dict[str, Any]:
    """Move a parked GitHub Issue card to READY after explicit supersession.

    This is deliberately a separate transition from PR rework.  The closed
    PR is abandoned, not reworked, so the normal Kanban dispatcher owns the
    fresh Issue round and no edge rework worker is spawned.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return {"task_id": task_id, "changed": False, "reason": "task_missing"}
    previous_status = str(row["status"])
    if previous_status not in {"review", "blocked"}:
        return {
            "task_id": task_id,
            "status": previous_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'ready', completed_at = NULL, assignee = NULL,
               claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
               block_kind = NULL, block_recurrences = 0,
               last_heartbeat_at = NULL
         WHERE id = ? AND status = ?
        """,
        (task_id, previous_status),
    )
    if cur.rowcount != 1:
        return {
            "task_id": task_id,
            "status": previous_status,
            "changed": False,
            "reason": "state_changed_during_sync",
        }
    _append_sync_event(
        conn,
        task_id,
        {
            "previous_status": previous_status,
            "new_status": "ready",
            "repository": ref.repository,
            "issue_number": ref.issue_number,
            "pr_number": pr.number,
            "head_sha": pr.head_sha,
            "signal_comment_id": int(signal["comment_id"]),
            "signal_author": str(signal["author"]),
            "signal_created_at": int(signal["created_at"]),
            "reason": "explicit_pr_superseded",
            "superseded": True,
            "trusted_actor_policy": sorted(TRUSTED_GITHUB_ACTORS),
            "merge_authority": "human",
            "auto_merge": False,
            "source": "github",
        },
        kind=SUPERSEDE_EVENT_KIND,
    )
    return {
        "task_id": task_id,
        "status": "ready",
        "changed": True,
        "reason": "explicit_pr_superseded",
        "pr_number": pr.number,
        "signal_comment_id": int(signal["comment_id"]),
    }


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
        "SELECT status, body, completed_at, assignee FROM tasks WHERE id = ?",
        (task_id,),
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
               assignee = ?,
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               block_kind = NULL,
               block_recurrences = 0,
               body = ?
         WHERE id = ? AND status = ?
        """,
        (REWORK_RESERVED_ASSIGNEE, new_body, task_id, current_status),
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
        "previous_assignee": row["assignee"],
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
                "Complete the task through the normal Kanban completion surface; do not post AGENT_REWORK_COMPLETE.",
                "Record machine-readable run metadata validation=passed and the full head_sha observed after validation.",
                "The edge resolves task, request binding, current PR head, and creates the canonical completion marker after read-back.",
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
    block_projection = _blocked_state_projection(conn, task_id, block_kind)

    def _entry(reason: str, **extra: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "task_id": task_id,
            "status": "blocked",
            "changed": False,
            "reason": reason,
            "block": block_projection,
            "block_kind": block_projection["block_kind"],
            "auto_promotable": block_projection["auto_promotable"],
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
            context_block = _build_context_block(
                client,
                ref,
                decision.pull_requests,
                block_projection=block_projection,
                block_history=_task_block_history(conn, task_id),
            )
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
        task_id,
        reason,
        needs,
        no_pr=not decision.pull_requests,
        block_projection=block_projection,
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

# ``kanban_db.dispatch_once`` is intentionally unaware of GitHub rework.  A
# fresh round therefore reserves its READY row with an assignee that cannot be
# resolved to a Hermes profile until this edge lane replaces it immediately
# before its own claim.  This is a reservation, not a worker/profile name.
REWORK_RESERVED_ASSIGNEE = "__github_edge_rework_dispatcher__"
REWORK_DISPATCH_CLAIM_PREFIX = "github-edge-rework:"
REWORK_DISPATCH_PROVENANCE_KIND = "github_pr_rework_dispatch"
REWORK_DISPATCH_PROVENANCE_SOURCE = "github_edge_rework_dispatch"

# Event kinds that define which transition currently governs a task's
# state.  The claim-projection failure diagnostic is included as a
# retryable pending gate; everything else (assigned/spawned/claimed/
# heartbeat/respawn_guarded/commented/...) never supersedes a rework.
_REWORK_GOVERNING_KINDS = frozenset({
    "created", "changes_requested", "github_pr_rework", "github_pr_sync",
    "github_pr_rework_retry", "github_pr_rework_projection_failure",
    SUPERSEDE_EVENT_KIND,
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
        payload = json.loads(_attention_row_value(row, "payload", 0) or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload = cast(dict[str, object], payload)
    return (
        payload,
        int(_attention_row_value(row, "created_at", 1) or 0),
        str(_attention_row_value(row, "kind", 2)),
    )


def _positive_rework_int(value: object) -> bool:
    """Return whether ``value`` is a strict positive JSON integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_rework_event(
    event: object,
    *,
    ref: GithubTaskRef | None = None,
    expected_pr_number: int | None = None,
    decision: GithubCompletionDecision | None = None,
) -> str | None:
    """Validate the governing event before it can authorize rework work.

    Governing event payloads are durable authorization evidence, not optional
    context.  Missing or malformed fields must therefore fail closed instead
    of being coerced to a wildcard/legacy value.  ``ref`` and ``decision`` are
    supplied at live reconciliation boundaries to bind repository, Issue, and
    canonical PR identity; callers that only have an event still receive the
    strict round/head/comment validation.
    """
    if not isinstance(event, tuple):
        return "rework_event_invalid_shape"
    typed_event = cast(tuple[object, object, object], event)
    if len(typed_event) != 3:
        return "rework_event_invalid_shape"
    if (
        not isinstance(typed_event[0], dict)
        or typed_event[2] not in {"github_pr_rework", "github_pr_rework_retry"}
    ):
        return "rework_event_invalid_shape"
    payload = cast(dict[str, object], typed_event[0])

    if ref is not None:
        if payload.get("repository") != ref.repository:
            return "rework_event_repository_mismatch"
        if payload.get("issue_number") != ref.issue_number:
            return "rework_event_issue_mismatch"

    pr_number = payload.get("pr_number")
    if not _positive_rework_int(pr_number):
        return "rework_event_pr_invalid"
    if expected_pr_number is not None and pr_number != expected_pr_number:
        return "rework_event_pr_mismatch"
    if decision is not None:
        matching_prs = tuple(pr for pr in decision.pull_requests if pr.number == pr_number)
        if len(matching_prs) != 1:
            return "rework_event_pr_unresolved"

    rework_round = payload.get("rework_round")
    if not _positive_rework_int(rework_round):
        return "rework_event_round_invalid"
    head_sha = payload.get("head_sha")
    if not isinstance(head_sha, str) or _FULL_SHA_RE.fullmatch(head_sha) is None:
        return "rework_event_head_invalid"

    request_comment_id = payload.get("request_comment_id")
    if request_comment_id is not None and not _positive_rework_int(request_comment_id):
        return "rework_event_request_comment_invalid"

    retry_comment_id = payload.get("retry_comment_id")
    if retry_comment_id is not None:
        if not _positive_rework_int(retry_comment_id):
            return "rework_event_retry_comment_invalid"
        if request_comment_id != retry_comment_id:
            return "rework_event_retry_comment_mismatch"
    if payload.get("trigger") == "maintainer_retry" and retry_comment_id is None:
        return "rework_event_retry_comment_missing"
    return None


def _same_rework_event(
    left: Any,
    right: Any,
) -> bool:
    """Return whether two rework-event snapshots identify the same event."""
    if (
        not isinstance(left, tuple)
        or len(left) != 3
        or not isinstance(right, tuple)
        or len(right) != 3
    ):
        return False
    return left[0] == right[0] and left[1] == right[1] and left[2] == right[2]


def _claim_projection_failure_is_retryable(
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    """Whether the newest claim projection failure permits a later retry."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_projection_failure' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("retryable") is True


def _has_prior_authoritative_done(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when this task has prior authoritative all_linked_prs_merged sync evidence."""
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_sync' "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            pl = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(pl, dict)
            and pl.get("desired_status") == "done"
            and pl.get("reason") in {"all_linked_prs_merged", "authoritative_done_preserved"}
            and pl.get("authoritative") is True
        ):
            return True
    return False


def _specialist_graph_delivery_run(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
    expected_round: int,
) -> sqlite3.Row | None:
    """Return a run inherited from the current specialist graph only.

    ``task_links`` is append-only, so a lead can retain reviewer parents from
    earlier rework rounds.  The current reviewer is selected from the newest
    terminal reviewer parent completed in this round; older direct parents
    must be terminal, but they are not allowed to veto that selection.  When a
    current developer ancestor exists, its explicit validation and the
    reviewer's exact PASS/head evidence are both required.  The legacy
    reviewer-direct shape remains supported for already persisted graphs.
    """
    candidate = _specialist_graph_delivery_candidate(
        conn,
        task_id,
        rework_at,
        expected_round,
    )
    return candidate[0] if candidate is not None else None


def _specialist_graph_delivery_candidate(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
    expected_round: int,
) -> tuple[sqlite3.Row, dict[str, Any]] | None:
    """Select one specialist delivery using graph and run attestations."""
    if not _positive_rework_int(expected_round):
        return None
    try:
        round_number = int(expected_round)
        round_at = int(rework_at)
    except (TypeError, ValueError):
        return None

    parent_rows = conn.execute(
        "SELECT id, status, assignee, completed_at, title "
        "FROM tasks WHERE id IN (SELECT parent_id FROM task_links WHERE child_id = ?)",
        (task_id,),
    ).fetchall()
    if not parent_rows:
        return None

    # Historical direct parents remain part of the graph, but an unresolved
    # parent still makes the graph unsafe.  Do not apply the current-round
    # completion timestamp requirement to every parent here.
    for parent in parent_rows:
        if str(parent["status"] or "").casefold() not in {"done", "archived"}:
            return None

    current_reviewers = [
        parent
        for parent in parent_rows
        if str(parent["assignee"] or "").casefold() == "kanban-reviewer"
        and _specialist_task_completed_in_round(parent, round_at)
    ]
    if not current_reviewers:
        return None
    reviewer_task = max(
        current_reviewers,
        key=lambda row: (_specialist_timestamp(row["completed_at"]), str(row["id"])),
    )
    reviewer_run = _latest_specialist_reviewer_run(conn, reviewer_task["id"], round_at)
    if reviewer_run is None:
        return None
    reviewer_heads = _specialist_full_heads(reviewer_run)
    if len(reviewer_heads) != 1:
        return None

    round_event = _specialist_round_event(conn, task_id, round_at, round_number)
    requested_head = round_event[0] if round_event is not None else None
    if round_event is not None and (
        requested_head is None or not _FULL_SHA_RE.fullmatch(requested_head)
    ):
        return None

    developer_ancestors = _specialist_developer_ancestors(
        conn,
        str(reviewer_task["id"]),
    )
    developer_tasks = [
        row
        for row in developer_ancestors
        if (
            str(row["status"] or "").casefold() in {"done", "archived"}
            and _specialist_task_completed_in_round(row, round_at)
        )
    ]
    # A developer-linked specialist graph must bind to the governing rework
    # event.  Without this gate, asking for a different round could reuse the
    # same timestamp-matching developer/reviewer runs when no event exists for
    # that round.  Preserve the legacy reviewer-direct compatibility path only
    # when the reviewer has no developer ancestor at all.
    if developer_ancestors and round_event is None:
        return None
    if developer_ancestors and not developer_tasks:
        return None
    root_run = _latest_specialist_lead_run(conn, task_id, round_at)
    if root_run is None:
        return None

    # New specialist graphs must carry an explicit developer validation
    # attestation.  If there is a current developer ancestor but no valid run,
    # fail closed rather than falling back to an older reviewer-only path.
    if developer_tasks:
        developer_task = max(
            developer_tasks,
            key=lambda row: (_specialist_timestamp(row["completed_at"]), str(row["id"])),
        )
        developer_run = _latest_specialist_developer_run(
            conn,
            developer_task["id"],
            round_at,
        )
        if developer_run is None:
            return None
        developer_heads = _specialist_metadata_heads(developer_run)
        if len(developer_heads) != 1 or developer_heads != reviewer_heads:
            return None
        reviewer_done_at = max(
            _specialist_timestamp(reviewer_task["completed_at"]),
            _specialist_timestamp(reviewer_run["ended_at"]),
        )
        if _specialist_timestamp(root_run["ended_at"]) < reviewer_done_at:
            return None
        return developer_run, {
            "round": round_number,
            "developer_task_id": str(developer_task["id"]),
            "reviewer_task_id": str(reviewer_task["id"]),
            "developer_run_id": int(developer_run["id"]),
            "reviewer_run_id": int(reviewer_run["id"]),
            "lead_run_id": int(root_run["id"]),
            "head_sha": next(iter(developer_heads)),
            "requested_head_sha": requested_head,
        }

    # Compatibility for the original reviewer-direct graph.  It still needs a
    # terminal PASS and an unambiguous head, but its lead run is the delivery
    # evidence because no developer attestation exists in that old shape.
    return root_run, {
        "round": round_number,
        "reviewer_task_id": str(reviewer_task["id"]),
        "reviewer_run_id": int(reviewer_run["id"]),
        "reviewer_completed_at": _specialist_timestamp(reviewer_task["completed_at"]),
        "lead_run_id": int(root_run["id"]),
        "head_sha": next(iter(reviewer_heads)),
        "requested_head_sha": requested_head,
        "legacy_reviewer_direct": True,
    }


def _specialist_timestamp(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _specialist_task_completed_in_round(row: sqlite3.Row, rework_at: int) -> bool:
    completed_at = _specialist_timestamp(row["completed_at"])
    return completed_at >= rework_at


def _specialist_round_event(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
    expected_round: int,
) -> tuple[str | None, dict[str, Any]] | None:
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND created_at = ? "
        "AND kind IN ('github_pr_rework', 'github_pr_rework_retry') "
        "ORDER BY id DESC",
        (task_id, rework_at),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("rework_round") != expected_round:
            continue
        head = payload.get("head_sha")
        if head is None:
            return None, payload
        return str(head), payload
    return None


def _specialist_current_developer_ancestors(
    conn: sqlite3.Connection,
    reviewer_id: str,
    rework_at: int,
) -> list[sqlite3.Row]:
    return [
        row
        for row in _specialist_developer_ancestors(conn, reviewer_id)
        if (
            str(row["status"] or "").casefold() in {"done", "archived"}
            and _specialist_task_completed_in_round(row, rework_at)
        )
    ]


def _specialist_developer_ancestors(
    conn: sqlite3.Connection,
    reviewer_id: str,
) -> list[sqlite3.Row]:
    ancestors: list[sqlite3.Row] = []
    queue = [reviewer_id]
    seen = {reviewer_id}
    while queue:
        child_id = queue.pop(0)
        parents = conn.execute(
            "SELECT t.id, t.status, t.assignee, t.completed_at, t.title "
            "FROM task_links l JOIN tasks t ON t.id = l.parent_id "
            "WHERE l.child_id = ?",
            (child_id,),
        ).fetchall()
        for parent in parents:
            parent_id = str(parent["id"])
            if parent_id in seen:
                continue
            seen.add(parent_id)
            if str(parent["assignee"] or "").casefold() == "kanban-developer":
                ancestors.append(parent)
            queue.append(parent_id)
    return ancestors


def _latest_specialist_reviewer_run(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
) -> sqlite3.Row | None:
    runs = conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY id DESC",
        (task_id, rework_at),
    ).fetchall()
    if not runs:
        return None
    run = runs[0]
    if (
        run["ended_at"] is None
        or str(run["outcome"] or "").casefold() not in {"completed", "done"}
        or not _specialist_review_pass(run)
    ):
        return None
    return run


def _latest_specialist_developer_run(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
) -> sqlite3.Row | None:
    runs = conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY id DESC",
        (task_id, rework_at),
    ).fetchall()
    if not runs:
        return None
    run = runs[0]
    metadata = _run_metadata(run)
    if (
        run["ended_at"] is None
        or str(run["outcome"] or "").casefold() not in {"completed", "done"}
        or metadata.get("validation") != "passed"
    ):
        return None
    return run


def _latest_specialist_lead_run(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
) -> sqlite3.Row | None:
    runs = conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY id DESC",
        (task_id, rework_at),
    ).fetchall()
    if not runs:
        return None
    run = runs[0]
    if (
        run["ended_at"] is None
        or str(run["outcome"] or "").casefold() not in {"completed", "done"}
    ):
        return None
    return run


def _specialist_review_pass(run: sqlite3.Row) -> bool:
    metadata = _run_metadata(run)
    verdict = metadata.get("verdict")
    if verdict is not None:
        return isinstance(verdict, str) and verdict.strip().casefold() == "pass"
    return re.search(r"(?<![A-Z0-9_])PASS(?![A-Z0-9_])", str(run["summary"] or ""), re.I) is not None


def _specialist_full_heads(run: sqlite3.Row) -> set[str]:
    return {
        candidate.casefold()
        for candidate in _rework_head_candidates(run)
        if _FULL_SHA_RE.fullmatch(candidate)
    }


def _specialist_metadata_heads(run: sqlite3.Row) -> set[str]:
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
    return {candidate.casefold() for candidate in candidates if _FULL_SHA_RE.fullmatch(candidate)}


def _task_run_after_rework(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
    *,
    rework_round: object = None,
) -> sqlite3.Row | None:
    """Return the newest run claimed by this edge rework round.

    ``started_at >= rework_at`` is only a time hint.  The core dispatcher can
    create an indistinguishable run after a rework event, so accepting the
    newest timestamp-matching row would let an ordinary/default-assignee run
    satisfy the rework delivery contract.  The edge dispatch lane records a
    run-linked provenance event after its claim; only that exact source and
    round identity are eligible here.
    """
    runs = conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? "
        "ORDER BY id DESC",
        (task_id, max(0, rework_at - 1)),
    ).fetchall()
    if not _positive_rework_int(rework_round):
        return None
    expected_round = cast(int, rework_round)
    edge_claimed_run: sqlite3.Row | None = None
    for run in runs:
        run_id = int(str(run["id"]))
        provenance_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = ? ORDER BY id DESC",
            (task_id, run_id, REWORK_DISPATCH_PROVENANCE_KIND),
        ).fetchall()
        for provenance_row in provenance_rows:
            try:
                provenance = json.loads(provenance_row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(provenance, dict):
                continue
            if provenance.get("source") != REWORK_DISPATCH_PROVENANCE_SOURCE:
                continue
            if provenance.get("task_id") != task_id:
                continue
            try:
                if int(str(provenance.get("rework_event_at"))) != int(rework_at):
                    continue
            except (TypeError, ValueError):
                continue
            if expected_round is not None:
                try:
                    if int(str(provenance.get("rework_round"))) != expected_round:
                        continue
                except (TypeError, ValueError):
                    continue
            if provenance.get("phase") not in {"claimed", "spawned"}:
                continue
            edge_claimed_run = run
            break
        if edge_claimed_run is not None:
            break

    # Specialist graph orchestration provenance inheritance:
    # If the Lead has a bootstrap run, do not let that incomplete run mask a
    # verified current-round developer/reviewer graph.  A direct edge run is
    # the fallback for the ordinary single-worker path.
    specialist_run = _specialist_graph_delivery_run(
        conn,
        task_id,
        rework_at,
        cast(int, expected_round),
    )
    return specialist_run if specialist_run is not None else edge_claimed_run


def _untrusted_task_run_after_rework(
    conn: sqlite3.Connection,
    task_id: str,
    rework_at: int,
) -> sqlite3.Row | None:
    """Return an ordinary run only for crash/attention classification.

    This is intentionally separate from ``_task_run_after_rework``.  An
    unproven core run can explain why a rework is recoverable after a crash,
    but it can never satisfy the completion-delivery contract.  Runs carrying
    any edge provenance are skipped here when the strict round check rejected
    them, so a prior round cannot be mistaken for the current one.
    """
    runs = conn.execute(
        "SELECT id, status, outcome, summary, error, metadata, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? AND started_at >= ? "
        "ORDER BY id DESC",
        (task_id, max(0, rework_at - 1)),
    ).fetchall()
    for run in runs:
        run_id = int(str(run["id"]))
        provenance_rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = ? LIMIT 1",
            (task_id, run_id, REWORK_DISPATCH_PROVENANCE_KIND),
        ).fetchall()
        if provenance_rows:
            continue
        return run
    return None


def _rework_dispatch_claim_lock(
    task_id: str,
    event: tuple[dict[str, Any], int, str] | None,
) -> str:
    """Build a stable edge-owned lock identity for one rework round."""
    round_value = "unknown"
    if event is not None:
        try:
            round_value = str(int(str(event[0].get("rework_round"))))
        except (TypeError, ValueError):
            pass
    return f"{REWORK_DISPATCH_CLAIM_PREFIX}{task_id}:{round_value}"


def _append_rework_dispatch_provenance(
    conn: sqlite3.Connection,
    claimed: Any,
    *,
    context: Mapping[str, Any] | None,
    phase: str,
    pid: int | None = None,
) -> None:
    """Record claim/spawn ownership linked to the exact task run."""
    task_id = str(claimed.id)
    run_id = getattr(claimed, "current_run_id", None)
    if run_id is None:
        raise RuntimeError(f"edge rework claim has no run id: {task_id}")
    run_id_int = int(str(run_id))
    event = context.get("event") if isinstance(context, Mapping) else None
    if not isinstance(event, tuple) or len(event) != 3:
        event = _latest_rework_event(conn, task_id)
    if event is None:
        raise RuntimeError(f"edge rework claim has no governing event: {task_id}")
    payload, event_at, _event_kind = event
    dispatch_payload: dict[str, Any] = {
        "source": REWORK_DISPATCH_PROVENANCE_SOURCE,
        "phase": phase,
        "task_id": task_id,
        "run_id": run_id_int,
        "rework_event_at": int(event_at),
        "rework_round": payload.get("rework_round"),
        "request_comment_id": payload.get("request_comment_id"),
        "head_sha": payload.get("head_sha"),
        "claim_lock": getattr(claimed, "claim_lock", None),
    }
    if pid is not None:
        dispatch_payload["pid"] = int(pid)
    _append_sync_event(
        conn,
        task_id,
        dispatch_payload,
        kind=REWORK_DISPATCH_PROVENANCE_KIND,
        run_id=run_id_int,
    )


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
    *,
    expected_round: Optional[int] = None,
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
        marker_round = fields.get("rework_round")
        if expected_round is not None and marker_round is not None:
            try:
                if int(marker_round) != expected_round:
                    continue
            except (TypeError, ValueError):
                continue
        expected_request_comment = (
            str(request_comment_id) if request_comment_id is not None else "none"
        )
        if request_comment != expected_request_comment:
            continue
        return {
            "comment_id": comment.get("id"),
            "author": author,
            "created_at": created_at,
            "task": task_id,
            "request_comment": request_comment,
            "head": head,
            "validation": "passed",
            "rework_round": marker_round,
        }
    return None


def _edge_owned_completion_marker(
    client: Any,
    ref: GithubTaskRef,
    pr: GithubPullRequest,
    task_id: str,
    event: tuple[dict[str, Any], int, str],
    run: sqlite3.Row,
    *,
    open_pr_count: Optional[int] = None,
) -> tuple[Optional[dict[str, Any]], str, dict[str, Any]]:
    """Create and read back the canonical completion marker at the edge.

    The worker only attests validation and the observed head in its durable
    ``task_runs.metadata``.  Every value written to the GitHub marker is
    resolved here from the governing event or a fresh GitHub read.  The
    existing marker parser remains the acceptance gate: a marker is posted
    only after its exact body is assembled, then the newly-created comment is
    fetched again before delivery can proceed.
    """
    payload, rework_at, event_kind = event
    round_value = payload.get("rework_round")
    if not isinstance(round_value, int) or isinstance(round_value, bool) or round_value <= 0:
        return None, "rework_event_round_invalid", {}
    if event_kind not in {"github_pr_rework", "github_pr_rework_retry"}:
        return None, "rework_event_invalid_shape", {}
    if open_pr_count is not None and open_pr_count != 1:
        return None, "rework_pr_ambiguous", {"open_pr_count": open_pr_count}

    # Do not use the PR snapshot that opened the reconciliation pass for the
    # marker fields.  A worker can finish while the branch advances, so the
    # edge must resolve the live PR and target branch immediately before the
    # write.
    fresh_payload, _ = client.get(
        f"/repos/{ref.repository}/pulls/{pr.number}"
    )
    live_pr = _parse_pull_request(pr.number, fresh_payload, ref)
    if live_pr.state != "open" or live_pr.base_branch != ref.target_branch:
        return None, "rework_pr_not_open", {
            "pr_number": live_pr.number,
            "state": live_pr.state,
            "base_branch": live_pr.base_branch,
        }
    live_head = live_pr.head_sha.casefold()
    if _FULL_SHA_RE.fullmatch(live_head) is None:
        return None, "rework_head_invalid", {"head": live_pr.head_sha}

    request_comment_id = payload.get("request_comment_id")
    typed_request_comment = (
        int(request_comment_id) if request_comment_id is not None else None
    )

    # This second lookup closes the race between the initial delivery probe
    # and the fresh PR read.  It also preserves D1: a valid trusted marker
    # already present is consumed without requiring a new worker attestation.
    marker = _completion_marker(
        client,
        ref,
        live_pr,
        task_id,
        rework_at,
        typed_request_comment,
        expected_round=round_value,
    )
    if marker is not None:
        return marker, "delivery_complete", {
            "completion_comment_id": marker.get("comment_id"),
            "edge_created": False,
        }
    if str(run["outcome"] or "") == "review_requested":
        return None, "delivery_run_review_requested", {"run_id": run["id"]}

    issue_payload, _ = client.get(
        f"/repos/{ref.repository}/issues/{ref.issue_number}"
    )
    if not isinstance(issue_payload, dict):
        return None, "issue_query_invalid", {}
    issue_state = str(issue_payload.get("state", "")).casefold()
    issue_labels = {
        str(item.get("name"))
        for item in issue_payload.get("labels", [])
        if isinstance(item, dict)
    }
    if issue_state != "open" or AGENT_READY_LABEL not in issue_labels:
        return None, "issue_not_agent_ready", {
            "issue_state": issue_state,
            "issue_agent_ready": AGENT_READY_LABEL in issue_labels,
        }

    # A non-null request binding can only be the exact trusted retry comment.
    # Label-only rounds deliberately carry null and render as ``none``.
    if typed_request_comment is not None:
        request_comments = client.get_paginated(
            f"/repos/{ref.repository}/issues/{live_pr.number}/comments",
            {"per_page": 100},
        )
        request_comment = next(
            (
                item for item in request_comments
                if isinstance(item, dict) and item.get("id") == typed_request_comment
            ),
            None,
        )
        if request_comment is None:
            return None, "rework_request_missing", {
                "request_comment_id": typed_request_comment,
            }
        request_author = str((request_comment.get("user") or {}).get("login") or "")
        if request_author not in TRUSTED_GITHUB_ACTORS:
            return None, "rework_request_untrusted", {
                "request_comment_id": typed_request_comment,
                "author": request_author,
            }
        if _rework_round_is_maintainer_retry(payload):
            request_lines = [
                line.strip()
                for line in str(request_comment.get("body") or "").splitlines()
                if line.strip()
            ]
            if request_lines != [
                REWORK_RETRY_MARKER,
                f"task={task_id}",
            ]:
                return None, "rework_request_invalid", {
                    "request_comment_id": typed_request_comment,
                }

    metadata = _run_metadata(run)
    if metadata.get("validation") != "passed":
        return None, "validation_attestation_missing", {
            "run_id": run["id"],
            "validation": metadata.get("validation"),
        }
    run_heads = _rework_head_candidates(run)
    if not run_heads:
        return None, "run_head_missing", {"run_id": run["id"]}
    if live_head not in run_heads:
        return None, "run_head_mismatch", {
            "run_id": run["id"],
            "run_heads": sorted(run_heads),
            "live_head": live_head,
        }

    # Ordinary rounds must advance beyond the head captured by the rework
    # request.  Explicit maintainer retries retain the existing verification-
    # only exception in the delivery gate below.
    requested_head = str(payload.get("head_sha") or "").casefold()
    if requested_head and requested_head == live_head and not _rework_round_is_maintainer_retry(payload):
        return None, "rework_head_unchanged", {
            "requested_head": requested_head,
            "head": live_head,
        }

    # The edge actor is resolved from GitHub, never inferred from the worker
    # or from the POST response.  A token owned by an untrusted actor cannot
    # create a completion handoff.
    actor_payload, _ = client.get("/user")
    actor = str(actor_payload.get("login") or "") if isinstance(actor_payload, dict) else ""
    if actor not in TRUSTED_GITHUB_ACTORS:
        return None, "edge_actor_untrusted", {"actor": actor}

    body = "\n".join([
        REWORK_COMPLETE_MARKER,
        f"task={task_id}",
        f"request_comment={typed_request_comment if typed_request_comment is not None else 'none'}",
        f"head={live_head}",
        "validation=passed",
        f"rework_round={round_value}",
        "source=edge-reconciliation",
    ])
    status, _ = client.post(
        f"/repos/{ref.repository}/issues/{live_pr.number}/comments",
        {"body": body},
    )
    if not 200 <= status < 300:
        return None, "completion_marker_post_failed", {"http_status": int(status)}

    observed = _completion_marker(
        client,
        ref,
        live_pr,
        task_id,
        rework_at,
        typed_request_comment,
        expected_round=round_value,
    )
    if observed is None:
        return None, "completion_marker_unobserved", {
            "http_status": int(status),
            "head": live_head,
            "rework_round": round_value,
        }
    return observed, "edge_marker_created", {
        "completion_comment_id": observed.get("comment_id"),
        "edge_created": True,
        "edge_actor": actor,
    }


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
        expected_request_comment = (
            str(request_comment_id) if request_comment_id is not None else "none"
        )
        if fields.get("request_comment") != expected_request_comment:
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
    *,
    open_pr_count: Optional[int] = None,
    allow_edge_creation: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    """Check the complete remote-delivery contract without mutating state."""
    invalid_event = _validate_rework_event(
        event,
        ref=ref,
        expected_pr_number=pr.number,
    )
    if invalid_event is not None:
        return False, invalid_event, {}
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
    run = _task_run_after_rework(
        conn,
        task_id,
        rework_at,
        rework_round=payload.get("rework_round"),
    )
    if run is None or run["ended_at"] is None:
        return False, "delivery_run_missing", {"rework_at": rework_at}
    if str(run["outcome"] or "") not in {"completed", "blocked", "review_requested"}:
        return False, "delivery_run_failed", {
            "outcome": run["outcome"], "run_id": run["id"],
        }
    expected_round = int(payload["rework_round"])
    request_comment_id = (
        int(payload["request_comment_id"])
        if payload.get("request_comment_id") is not None
        else None
    )
    marker = _completion_marker(
        client,
        ref,
        pr,
        task_id,
        rework_at,
        request_comment_id,
        expected_round=expected_round,
    )
    marker_source: dict[str, Any] = {}
    if marker is None:
        if allow_edge_creation:
            marker, marker_reason, marker_evidence = _edge_owned_completion_marker(
                client,
                ref,
                pr,
                task_id,
                event,
                run,
                open_pr_count=open_pr_count,
            )
        else:
            marker, marker_reason, marker_evidence = (
                None,
                "edge_marker_creation_skipped_dry_run",
                {},
            )
        if marker is None and marker_reason not in {
            "validation_attestation_missing",
            "run_head_missing",
        }:
            return False, marker_reason, {
                "run_id": run["id"],
                **marker_evidence,
            }
        marker_source = dict(marker_evidence)
        if marker is None:
            malformed = _malformed_completion_marker(
                client,
                ref,
                pr,
                task_id,
                rework_at,
                request_comment_id,
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
                "rework_round": payload.get("rework_round"),
                "rework_event_at": rework_at,
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
        "rework_round": payload.get("rework_round"),
        "rework_event_at": rework_at,
        "request_comment_id": payload.get("request_comment_id"),
        "head": marker["head"],
        "validation": marker["validation"],
        "completion_comment_id": marker.get("comment_id"),
    }
    if marker_source.get("edge_created") is True:
        evidence["completion_source"] = "edge-reconciliation"
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
            f"could not reconcile lifecycle labels (HTTP {status})",
            status=int(status),
            failure_class="patch_non_2xx",
            before_labels=current,
            desired_labels=desired,
        )
    observed = _pr_labels(client, ref.repository, pr_number)
    if observed != desired:
        raise GithubCompletionError(
            "lifecycle label read-back mismatch",
            failure_class="read_back_mismatch",
            before_labels=current,
            desired_labels=desired,
            observed_labels=observed,
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


_CLAIM_PROJECTION_FAILURE_CLASSES = frozenset({
    "patch_non_2xx",
    "read_back_mismatch",
})


def _append_claim_projection_failure_event(
    conn: sqlite3.Connection,
    task_id: str,
    context: Mapping[str, Any],
    claim_projection: Mapping[str, Any],
) -> None:
    """Persist safe evidence when claim-time label projection fails.

    The claim is reclaimed before this event is written, so a later edge wake
    can retry the same ``agent-rework`` request without spawning in this tick.
    Only the two failures that have a trustworthy label diff are recorded;
    generic lookup/transport failures keep their existing fail-closed path.
    """
    failure_class = claim_projection.get("failure_class")
    if failure_class not in _CLAIM_PROJECTION_FAILURE_CLASSES:
        return
    _append_sync_event(
        conn,
        task_id,
        {
            "task_id": task_id,
            "repository": str(context["repository"]),
            "issue_number": int(context["issue_number"]),
            "pr_number": int(context["pr_number"]),
            "stage": "claim",
            "operation": "lifecycle_label_projection",
            "failure_class": failure_class,
            "http_status": claim_projection.get("http_status"),
            "before_labels": claim_projection.get("before_labels"),
            "desired_labels": claim_projection.get("desired_labels"),
            "observed_labels": claim_projection.get("observed_labels"),
            "retryable": True,
            "source": "github_edge_rework_dispatch",
        },
        kind="github_pr_rework_projection_failure",
    )


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
    if reason == "delivery_run_missing" and run is not None:
        terminal_state = str(run["status"] or "").casefold()
        outcome = str(run["outcome"] or "").casefold()
        if terminal_state in {"crashed", "reclaimed", "failed"} or outcome in {
            "crashed", "reclaimed", "failed",
        }:
            return False
    return any(marker in text for marker in (
        "review-required", "needs_input", "needs maintainer",
        "human review", "host_validation_required", "human_validation_required",
    )) or reason in {
        "delivery_run_missing", "completion_handoff_missing",
        "completion_marker_malformed",
        "issue_query_invalid", "issue_not_agent_ready", "rework_pr_ambiguous",
        "rework_pr_not_open", "rework_head_invalid", "rework_request_missing",
        "rework_request_untrusted", "rework_request_invalid", "edge_actor_untrusted",
        "completion_marker_post_failed", "completion_marker_unobserved",
        "delivery_run_review_requested",
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
    invalid_event = _validate_rework_event(
        event,
        ref=ref,
    )
    if invalid_event is not None:
        return {
            "_error": invalid_event,
            "event": event,
        }
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
    open_pr_count = sum(item.state == "open" for item in decision.pull_requests)
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
        "open_pr_count": open_pr_count,
    }


def _fresh_rework_context(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    task_id: str,
) -> dict[str, Any] | None:
    """Reload the context after a same-wake rework state transition."""
    event = _latest_rework_event(conn, task_id)
    if event is None:
        return None
    try:
        return _rework_context(client, ref, decision, task_id, event)
    except GithubCompletionError as exc:
        return {"_error": str(exc)}


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
    fail-closed hold (durable attention evidence, no synthetic command label,
    no automatic retry) is untouched.  The exact regeneration templates are
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
    "rework_attention_label_projection_failed",
    "rework_retry_label_projection_failed",
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
_REWORK_OPERATOR_ATTENTION_REASONS = frozenset({
    "rework_human_attention",
    "rework_threshold_exceeded",
    "rework_retry_blocked",
    "rework_attention_label_projection_failed",
    "rework_retry_label_projection_failed",
    "rework_dispatch_failed",
    "claim_projection_reclaim_failed",
    "workspace_resolve_failed",
    "spawn_failed",
    "rework_context_failed",
    "delivery_query_failed",
    "review_ready_label_projection_failed",
    "merged_lifecycle_cleanup_failed",
    "stale_review_ready_normalize_failed",
    "lifecycle_label_conflict",
})
_BOARD_OPERATOR_ATTENTION_REASONS = frozenset({
    "dispatch_lock_failed",
    "dispatch_lock_unavailable",
})
_ATTENTION_COMPONENT_LIMIT = 96
_ATTENTION_REF_LIMIT = 384


def _operator_attention_reason(entry: Mapping[str, Any]) -> Optional[str]:
    reason = str(entry.get("reason") or "")
    if reason in {"rework_human_attention", "rework_human_attention_predicted"}:
        return "rework_human_attention"
    if reason in _OPERATOR_ATTENTION_REASONS:
        return reason
    block_kind = str(entry.get("block_kind") or "")
    if (
        block_kind in {"needs_input", "capability"}
        and str(entry.get("status") or "") == "blocked"
    ):
        return block_kind
    evidence = entry.get("evidence")
    evidence_reason = evidence.get("reason") if isinstance(evidence, Mapping) else ""
    text = " ".join(
        str(value or "")
        for value in (
            reason,
            entry.get("diagnostic"),
            entry.get("retry_reason"),
            entry.get("error"),
            evidence_reason,
        )
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


def _positive_attention_int(value: object) -> Optional[int]:
    """Return a positive integer identity component, without coercion drift."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    parsed = int(value.strip())
    return parsed if parsed > 0 else None


def _attention_component(value: object, *, missing: str = "unknown") -> str:
    """Encode one bounded identity component for a delimiter-based ref."""
    if value is None:
        text = missing
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        text = str(value).strip() or missing
    # ``:`` belongs to the attention-key separator and ``|`` to the
    # incident-ref field separator. Percent-encoding keeps the ref
    # unambiguous while preserving short, human-readable values.
    text = (
        text.replace("%", "%25")
        .replace(":", "%3A")
        .replace("|", "%7C")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )
    if len(text) <= _ATTENTION_COMPONENT_LIMIT:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{text[:_ATTENTION_COMPONENT_LIMIT - 17]}~{digest}"


def _attention_ref(parts: Iterable[object]) -> str:
    ref = "|".join(_attention_component(part) for part in parts)
    if len(ref) <= _ATTENTION_REF_LIMIT:
        return ref
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:16]
    return f"{ref[:_ATTENTION_REF_LIMIT - 17]}~{digest}"


def _entry_pr_number(entry: Mapping[str, Any]) -> Optional[int]:
    """Find the one canonical PR identity carried by an observer entry."""
    containers: list[Mapping[str, Any]] = [entry]
    for key in ("rework", "evidence"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        for key in ("pr_number", "round_pr_number"):
            value = _positive_attention_int(container.get(key))
            if value is not None:
                return value
        decision_pr_numbers = container.get("decision_pr_numbers")
        if (
            isinstance(decision_pr_numbers, (list, tuple))
            and len(decision_pr_numbers) == 1
        ):
            value = _positive_attention_int(decision_pr_numbers[0])
            if value is not None:
                return value
    return None


def _entry_rework_round(entry: Mapping[str, Any]) -> Optional[int]:
    for key in ("rework", "evidence", ""):
        value = entry if not key else entry.get(key)
        if not isinstance(value, Mapping):
            continue
        round_value = _positive_attention_int(value.get("rework_round"))
        if round_value is not None:
            return round_value
    return None


def _entry_request_comment_id(entry: Mapping[str, Any]) -> object:
    for key in ("rework", "evidence"):
        value = entry.get(key)
        if isinstance(value, Mapping) and value.get("request_comment_id") is not None:
            return value.get("request_comment_id")
    return None


def _attention_row_value(row: Any, name: str, index: int) -> Any:
    """Read sqlite.Row and tuple rows alike in focused edge tests."""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _latest_blocked_event(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[tuple[dict[str, Any], int, int]]:
    """Return the latest blocked payload, timestamp, and durable row id."""
    row = conn.execute(
        "SELECT payload, created_at, id FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(_attention_row_value(row, "payload", 0) or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return (
        cast(dict[str, Any], payload),
        int(_attention_row_value(row, "created_at", 1) or 0),
        int(_attention_row_value(row, "id", 2)),
    )


def _rework_attention_identity(
    conn: sqlite3.Connection,
    entry: Mapping[str, Any],
    reason: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    latest = _latest_rework_event(conn, str(entry.get("task_id") or ""))
    event_payload = latest[0] if latest is not None else {}
    event_created_at = latest[1] if latest is not None else None
    event_kind = latest[2] if latest is not None else None
    repository = str(
        event_payload.get("repository") or entry.get("repository") or ""
    ).strip()
    issue_number = _positive_attention_int(
        event_payload.get("issue_number") or entry.get("issue_number")
    )
    pr_number = _positive_attention_int(event_payload.get("pr_number"))
    if pr_number is None:
        pr_number = _entry_pr_number(entry)
    rework_round = _positive_attention_int(event_payload.get("rework_round"))
    if rework_round is None:
        rework_round = _entry_rework_round(entry)
    if (
        not repository
        or issue_number is None
        or pr_number is None
        or rework_round is None
    ):
        return None
    request_comment_id = event_payload.get("request_comment_id")
    if request_comment_id is None:
        request_comment_id = _entry_request_comment_id(entry)
    incident_ref = _attention_ref(
        (
            repository,
            issue_number,
            pr_number,
            rework_round,
            request_comment_id if request_comment_id is not None else "none",
        )
    )
    provenance: dict[str, Any] = {
        "source": "rework_round",
        "repository": repository,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "rework_round": rework_round,
        "request_comment_id": request_comment_id,
        "reason": reason,
        "incident_ref": incident_ref,
    }
    if event_kind is not None:
        provenance["rework_event_kind"] = event_kind
    if event_created_at is not None:
        provenance["rework_event_created_at"] = event_created_at
    return incident_ref, provenance


def _blocked_attention_identity(
    conn: sqlite3.Connection,
    entry: Mapping[str, Any],
    reason: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    latest = _latest_blocked_event(conn, str(entry.get("task_id") or ""))
    if latest is None:
        return None
    event_payload, created_at, blocked_event_id = latest
    payload_kind = event_payload.get("kind")
    payload_reason = event_payload.get("reason")
    block_kind = str(entry.get("block_kind") or "untyped")
    incident_ref = _attention_ref(
        (
            block_kind,
            payload_kind if payload_kind is not None else "untyped",
            payload_reason if payload_reason is not None else "unknown",
            created_at,
            blocked_event_id,
        )
    )
    provenance = {
        "source": "blocked_event",
        "blocked_event_id": blocked_event_id,
        "blocked_event_kind": payload_kind,
        "blocked_event_reason": payload_reason,
        "blocked_event_created_at": created_at,
        "block_kind": block_kind,
        "reason": reason,
        "incident_ref": incident_ref,
    }
    return incident_ref, provenance


def _operator_attention_payload(
    conn: sqlite3.Connection,
    entry: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    reason = _operator_attention_reason(entry)
    task_id = str(entry.get("task_id") or "")
    repository = str(entry.get("repository") or "")
    issue_number = _positive_attention_int(entry.get("issue_number"))
    board = str(entry.get("board") or "").strip()
    if reason is None:
        return None

    def _payload(
        incident_ref: Optional[str],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        attention_key = (
            f"{reason}:{incident_ref}" if incident_ref is not None else reason
        )
        payload: dict[str, Any] = {
            "reason": reason,
            "attention_key": attention_key,
            "repository": repository or None,
            "issue_number": issue_number,
            "previous_status": entry.get("from_state"),
            "new_status": entry.get("to_state") or entry.get("status"),
            "source": "github_edge_operator_attention",
            "incident_provenance": provenance,
        }
        if incident_ref is None:
            # Do not invent identity from a PR number or task-event cursor.
            # Board-global lock failures remain visible but unresolved.
            payload["incident_unresolved"] = True
        return payload

    if reason in _BOARD_OPERATOR_ATTENTION_REASONS:
        if not board:
            return None
        # Dispatch-lock failures describe the board admission boundary, not
        # the task/PR that happened to be in the pending batch.  Keep that
        # scope explicit even when a caller decorates the result with task
        # context; a PR number is not a governing lock generation.
        payload = _payload(None, {
            "source": "board_context",
            "board": board,
            "reason": reason,
            "incident_ref": None,
        })
        payload["repository"] = None
        payload["issue_number"] = None
        return payload

    if not task_id:
        return None
    if not repository or issue_number is None:
        return None

    identity: Optional[tuple[str, dict[str, Any]]] = None
    if reason in _REWORK_OPERATOR_ATTENTION_REASONS:
        identity = _rework_attention_identity(conn, entry, reason)
    if identity is None and reason in {"needs_input", "capability"}:
        identity = _blocked_attention_identity(conn, entry, reason)
    if identity is None:
        # PR context alone is not a resolved identity for rework-family
        # reasons; retain it as provenance while remaining fail-open.
        pr_number = _entry_pr_number(entry)
        if reason in _REWORK_OPERATOR_ATTENTION_REASONS:
            incident_ref = None
        elif pr_number is not None:
            incident_ref = _attention_ref((pr_number,))
        else:
            incident_ref = None
        provenance: dict[str, Any] = {
            "source": "entry_context",
            "pr_number": pr_number,
            "reason": reason,
            "incident_ref": incident_ref,
        }
    else:
        incident_ref, provenance = identity
    return _payload(incident_ref, provenance)


def _attach_operator_attention(
    entry: dict[str, Any],
    payload: Mapping[str, Any],
) -> None:
    entry["operator_attention"] = {
        "reason": payload["reason"],
        "attention_key": payload["attention_key"],
        "incident_provenance": payload["incident_provenance"],
    }
    if payload.get("incident_unresolved"):
        entry["operator_attention"]["incident_unresolved"] = True


def _record_operator_attention(
    conn: sqlite3.Connection,
    entry: dict[str, Any],
) -> bool:
    """Record one deduped operator-attention event in existing task_events."""
    task_id = str(entry.get("task_id") or "")
    payload = _operator_attention_payload(conn, entry)
    if payload is None:
        return False
    _attach_operator_attention(entry, payload)
    if (
        not task_id
        or payload["incident_provenance"].get("source") == "board_context"
    ):
        # Board-global attention has no task row to own a durable event. Keep
        # the explicit unresolved observer identity for Telegram delivery.
        return False
    key = str(payload["attention_key"])
    # Keep the task-scoped LIKE lookup for compatibility with existing SQLite
    # deployments, then require an exact decoded JSON key. The post-filter
    # prevents a legacy ``reason:<cursor>`` row from suppressing a semantic
    # key that merely contains the same reason prefix.
    existing_rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'github_operator_attention' AND payload LIKE ?",
        (task_id, f"%attention_key%{key}%"),
    ).fetchall()
    for existing_row in existing_rows:
        try:
            existing_payload = json.loads(
                _attention_row_value(existing_row, "payload", 0) or "{}"
            )
        except (TypeError, ValueError):
            continue
        if (
            isinstance(existing_payload, dict)
            and existing_payload.get("attention_key") == key
            and isinstance(existing_payload.get("incident_provenance"), Mapping)
        ):
            return False
    with conn:
        _append_sync_event(conn, task_id, payload, kind="github_operator_attention")
    return True


def _clear_rework_execution_labels(
    client: Any,
    context: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    pr = context.get("pr")
    if not isinstance(pr, GithubPullRequest):
        raise GithubCompletionError("rework context has no pull request")
    # ``agent-rework`` is a one-shot maintainer command, never an edge-owned
    # state label.  Once the round has been consumed, failure/attention cleanup
    # may clear execution/output labels but must not synthesize a new command.
    _, reason, evidence = _project_pr_lifecycle_labels(
        client,
        GithubTaskRef(
            str(context["repository"]),
            int(context["issue_number"]),
        ),
        int(pr.number),
        remove=(REWORK_LABEL, WORKING_LABEL, REVIEW_READY_LABEL),
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
    from hermes_cli import kanban_db_dispatch

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
    auto_blocked = bool(kanban_db_dispatch._record_task_failure(
        conn,
        task_id,
        f"rework delivery retry: {reason}",
        outcome="rework_delivery_failed",
        failure_limit=(failure_limit if failure_limit is not None
                       else kanban_db_dispatch.DEFAULT_FAILURE_LIMIT),
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
    *,
    ref: GithubTaskRef | None = None,
    expected_pr_number: int | None = None,
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
    if _validate_rework_event(
        event,
        ref=ref,
        expected_pr_number=expected_pr_number,
    ) is not None:
        return None
    payload, event_at, _ = event
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_rework_delivery' "
        "AND created_at >= ? ORDER BY created_at DESC, id DESC",
        (task_id, int(event_at)),
    ).fetchall()
    if not rows:
        return None
    expected_round = cast(int, payload["rework_round"])

    def request_identity(value: Any) -> str | None:
        if value is None or str(value).casefold() == "none":
            return None
        try:
            return str(int(str(value)))
        except (TypeError, ValueError):
            return str(value)

    expected_request = request_identity(payload.get("request_comment_id"))
    round_requested_head = str(payload.get("head_sha") or "").casefold()
    for row in rows:
        if not row["payload"]:
            continue
        try:
            delivery_payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(delivery_payload, dict):
            continue
        # New round events must bind delivery to the same explicit round;
        # legacy events without a round are not trusted for a new round.
        if expected_round is not None:
            try:
                if int(str(delivery_payload.get("rework_round"))) != expected_round:
                    continue
            except (TypeError, ValueError):
                continue
        if request_identity(delivery_payload.get("request_comment_id")) != expected_request:
            # Identity mismatch: the recorded delivery belongs to another
            # round.  Continue so a later valid event can still be found.
            continue
        delivery_head = str(delivery_payload.get("head") or "").casefold()
        if round_requested_head and delivery_head == round_requested_head:
            # The recorded delivery head equals the head this round started
            # from.  For a *fresh* rework round that means the round's worker
            # has not yet pushed a new head, so the recorded same-head
            # delivery is the previous round's delivery.  Verification-only /
            # handoff-repair rounds are exempt.
            if payload.get("trigger") == "maintainer_retry":
                return delivery_payload
            continue
        return delivery_payload
    return None


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


def _event_targets_pr(payload: Mapping[str, Any], pr_number: int) -> bool:
    """Return whether a sync event's explicit PR evidence matches ``pr_number``."""
    linked_numbers = payload.get("linked_pr_numbers")
    if isinstance(linked_numbers, (list, tuple)):
        try:
            return int(pr_number) in {int(number) for number in linked_numbers}
        except (TypeError, ValueError):
            return False
    raw_pr_number = payload.get("pr_number")
    try:
        return raw_pr_number is not None and int(raw_pr_number) == int(pr_number)
    except (TypeError, ValueError):
        return False


def _last_review_parking_event_at(
    conn: sqlite3.Connection,
    task_id: str,
    pr_number: int,
) -> int | None:
    """Epoch time of a prior GitHub sync that parked this PR in REVIEW.

    ``github_pr_sync`` is the durable provenance for the ordinary
    ``DONE -> REVIEW`` open-PR repair.  It is a valid stale
    ``agent-review-ready`` baseline even when no rework delivery event exists
    for the prior round.  Malformed or unrelated events are ignored so they
    cannot manufacture a normalization decision.
    """
    rows = conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pr_sync' "
        "ORDER BY created_at DESC, id DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("previous_status") != "done" or payload.get("new_status") != "review":
            continue
        if not _event_targets_pr(payload, pr_number):
            continue
        try:
            return int(row["created_at"])
        except (TypeError, ValueError):
            return None
    return None


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
    delivery or a durable ``DONE -> REVIEW`` GitHub sync makes the prior
    round's ``agent-review-ready`` stale.
    Only that single label is removed (``agent-rework`` is kept so the
    classic REVIEW -> READY intake path or the dispatch lane owns the new
    round on a later tick).  Returns ``None`` — keeping the fail-closed
    lifecycle conflict guard — unless the newest ``agent-rework`` label
    addition provably postdates one of those prior review-parking events.
    """
    delivery_at = _last_delivery_event_at(conn, task_id)
    parking_at = _last_review_parking_event_at(conn, task_id, pr_number)
    baselines = [value for value in (delivery_at, parking_at) if value is not None]
    request_at = _latest_rework_label_at(client, ref, pr_number)
    if not baselines or request_at is None or request_at <= max(baselines):
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
        "WHERE task_id = ? AND kind IN ('github_pr_rework_attention', 'github_operator_attention') "
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



def _last_task_event_at(conn: sqlite3.Connection, task_id: str) -> int:
    """Return the latest durable task-event timestamp for signal freshness."""
    row = conn.execute(
        "SELECT COALESCE(MAX(created_at), 0) FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _consumed_supersede_comment_ids(
    conn: sqlite3.Connection,
    task_id: str,
) -> set[int]:
    """Return supersede signal ids consumed by prior edge transitions."""
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('github_pr_superseded', 'github_pr_supersede')",
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
        raw = payload.get("signal_comment_id")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            consumed.add(raw)
    return consumed


def _explicit_superseded_pr_numbers(
    conn: sqlite3.Connection,
    task_id: str,
    ref: GithubTaskRef,
) -> frozenset[int]:
    """Read valid durable supersede evidence for this Issue task."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('github_pr_superseded', 'github_pr_supersede')",
        (task_id,),
    ).fetchall()
    numbers: set[int] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pr_number = payload.get("pr_number")
        signal_id = payload.get("signal_comment_id")
        if (
            payload.get("repository") != ref.repository
            or payload.get("issue_number") != ref.issue_number
            or payload.get("new_status") != "ready"
            or payload.get("reason") != "explicit_pr_superseded"
            or payload.get("superseded") is not True
            or not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
            or not _positive_rework_int(signal_id)
        ):
            continue
        numbers.add(pr_number)
    return frozenset(numbers)


def _find_pr_supersede_signal(
    client: Any,
    ref: GithubTaskRef,
    pr_number: int,
    task_id: str,
    *,
    baseline_at: int,
    consumed_ids: set[int],
) -> Optional[dict[str, Any]]:
    """Find the newest trusted exact two-line PR supersede signal."""
    comments = client.get_paginated(
        f"/repos/{ref.repository}/issues/{pr_number}/comments",
        {"per_page": 100},
    )
    expected_lines = [SUPERSEDE_MARKER, f"pr={pr_number} task={task_id}"]
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        if author not in TRUSTED_GITHUB_ACTORS:
            continue
        created_at = _parse_iso_ts(comment.get("created_at"))
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
        row["completed_at"]
        if isinstance(row, sqlite3.Row) and "completed_at" in row.keys()
        else (row.get("completed_at") if isinstance(row, dict) else None)
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
    row_completed_at = (
        row["completed_at"]
        if isinstance(row, sqlite3.Row) and "completed_at" in row.keys()
        else (row.get("completed_at") if isinstance(row, dict) else None)
    )
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
    # The retry comment itself is the trusted one-shot command.  Persisted
    # ``github_pr_rework`` provenance is sufficient for the edge dispatch lane;
    # never synthesize ``agent-rework`` as an intermediate state label.  Clear
    # any stale lifecycle projection left by the spent round instead.
    try:
        _, label_reason, label_evidence = _project_pr_lifecycle_labels(
            client,
            ref,
            int(context["pr_number"]),
            remove=(REWORK_LABEL, WORKING_LABEL, REVIEW_READY_LABEL),
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



def _consume_explicit_pr_supersede(
    conn: sqlite3.Connection,
    client: Any,
    ref: GithubTaskRef,
    decision: GithubCompletionDecision,
    task_id: str,
    row: Mapping[str, Any],
    *,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Admit one trusted signal for one closed-unmerged linked PR.

    The signal is intentionally considered before the normal completion
    decision is filtered by prior supersede evidence.  Every external fact is
    fresh in this pass; any failed or ambiguous lookup returns a no-mutation
    diagnostic instead of allowing the ordinary blocked/review lanes to infer
    a new round.
    """
    if not decision.authoritative or str(row["status"]) not in {"review", "blocked"}:
        return None
    effective_prs = tuple(
        pr for pr in decision.pull_requests
        if _is_effective_linked_pr(ref, pr)
    )
    closed_unmerged = tuple(
        pr for pr in effective_prs
        if pr.state == "closed" and not _is_merged_into_target(ref, pr)
    )
    if not closed_unmerged:
        return None

    consumed_ids = _consumed_supersede_comment_ids(conn, task_id)
    signals: list[dict[str, Any]] = []
    for candidate in closed_unmerged:
        try:
            signal = _find_pr_supersede_signal(
                client,
                ref,
                candidate.number,
                task_id,
                baseline_at=_last_task_event_at(conn, task_id),
                consumed_ids=consumed_ids,
            )
        except GithubCompletionError as exc:
            return {
                "task_id": task_id,
                "status": str(row["status"]),
                "changed": False,
                "reason": "supersede_signal_query_failed",
                "error": str(exc),
            }
        if signal is not None:
            signals.append(signal)

    # A signal cannot choose among multiple effective PRs, even if only one
    # of the comments looks valid.  In particular an open replacement must
    # prevent a closed historical PR from being abandoned in isolation.
    if len(effective_prs) != 1:
        if signals:
            return {
                "task_id": task_id,
                "status": str(row["status"]),
                "changed": False,
                "reason": "supersede_ambiguous_linked_prs",
                "signal_comment_id": signals[-1]["comment_id"],
            }
        return None
    pr = effective_prs[0]
    if len(closed_unmerged) != 1 or not signals:
        return None
    if pr.number in _explicit_superseded_pr_numbers(conn, task_id, ref):
        return None
    signal = signals[-1]

    # A second rework lineage on another PR makes the requested transition
    # ambiguous.  Malformed durable provenance is equally unsafe.
    rework_rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('github_pr_rework', 'github_pr_rework_retry')",
        (task_id,),
    ).fetchall()
    for event_row in rework_rows:
        try:
            payload = json.loads(event_row["payload"] or "{}")
        except (TypeError, ValueError):
            return {
                "task_id": task_id, "status": str(row["status"]),
                "changed": False, "reason": "supersede_rework_ambiguous",
            }
        if not isinstance(payload, dict):
            return {
                "task_id": task_id, "status": str(row["status"]),
                "changed": False, "reason": "supersede_rework_ambiguous",
            }
        if (
            payload.get("repository") != ref.repository
            or payload.get("issue_number") != ref.issue_number
            or payload.get("pr_number") != pr.number
        ):
            return {
                "task_id": task_id, "status": str(row["status"]),
                "changed": False, "reason": "supersede_rework_ambiguous",
            }

    try:
        issue_payload, _ = client.get(
            f"/repos/{ref.repository}/issues/{ref.issue_number}"
        )
    except GithubCompletionError as exc:
        return {
            "task_id": task_id, "status": str(row["status"]),
            "changed": False, "reason": "supersede_issue_lookup_failed",
            "error": str(exc),
        }
    if not isinstance(issue_payload, dict):
        return {
            "task_id": task_id, "status": str(row["status"]),
            "changed": False, "reason": "supersede_issue_lookup_failed",
            "error": "invalid issue payload",
        }
    issue_state = str(issue_payload.get("state", "")).casefold()
    raw_labels = issue_payload.get("labels")
    if issue_state not in {"open", "closed"} or not isinstance(raw_labels, list):
        return {
            "task_id": task_id, "status": str(row["status"]),
            "changed": False, "reason": "supersede_issue_lookup_failed",
            "error": "incomplete issue payload",
        }
    issue_labels = {
        str(item.get("name")) for item in raw_labels if isinstance(item, dict)
    }
    if issue_state != "open" or AGENT_READY_LABEL not in issue_labels:
        return {
            "task_id": task_id,
            "status": str(row["status"]),
            "changed": False,
            "reason": "supersede_issue_not_agent_ready",
            "issue_state": issue_state,
            "issue_agent_ready": AGENT_READY_LABEL in issue_labels,
        }
    if _task_has_active_rework_claim(conn, row):
        return {
            "task_id": task_id, "status": str(row["status"]),
            "changed": False, "reason": "supersede_active_worker",
        }
    if dry_run:
        return {
            "task_id": task_id,
            "status": "ready",
            "changed": False,
            "reason": "explicit_pr_supersede_predicted",
            "pr_number": pr.number,
            "signal_comment_id": signal["comment_id"],
        }
    try:
        with conn:
            result = apply_supersede(conn, task_id, ref, signal, pr)
    except sqlite3.Error as exc:
        conn.rollback()
        return {
            "task_id": task_id, "status": str(row["status"]),
            "changed": False, "reason": "supersede_db_write_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
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
            # instead of deferring to a later reconciliation pass. Task state
            # (``status``),
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

    # DONE + OPEN + agent-rework must use the strict current-round delivery
    # gate below.  Generic conflict repair would otherwise project
    # agent-review-ready before delivery provenance is proven.
    if len(labels & lifecycle) > 1 and not (
        status == "done"
        and context["pr"].state == "open"
        and REWORK_LABEL in labels
        and REVIEW_READY_LABEL in labels
    ):
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
    # The durable current-round event plus a trusted retry comment authorizes
    # this narrow ingress; ``agent-rework`` is not required because it is a
    # one-shot maintainer command, not persistent round state.  Normal REVIEW
    # cards without current-round attention/retry evidence simply fall through.
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
        and WORKING_LABEL not in labels
    ):
        has_retry_signal = _has_fresh_retry_comment(
            conn, client, ref, task_id, row, context,
        )
        attention_at = (
            None if status == "done"
            else _current_rework_attention_at(conn, task_id, context["event"])
        )
        retry_lane = (
            (status == "done" and has_retry_signal)
            or (status == "review" and attention_at is not None)
        )
        if retry_lane:
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
        # If maintainer added a fresh agent-rework label after the round event,
        # hand off to classic rework intake to open the new round immediately.
        if _label_is_newer_than_event(
            client, ref, int(context["pr_number"]), int(context["event"][1]),
            current_label_at=context["event"][0].get("label_added_at") if isinstance(context["event"][0], dict) else None,
        ):
            return None
        # Explicit maintainer retry (new round ingress): a fresh trusted
        # AGENT_REWORK_RETRY comment on the PR closes the held round and
        # opens a new one through the classic intake contract.  Without it
        # the card stays BLOCKED with the durable attention record; no command
        # label is synthesized by the edge.
        retry_result = _consume_explicit_rework_retry(
            conn, client, ref, decision, task_id, row, context,
            dry_run=dry_run,
        )
        if retry_result is not None:
            return retry_result
        try:
            delivered, _delivery_reason, evidence = _rework_delivery_evidence(
                conn, client, ref, task_id, context["pr"], context["event"],
                open_pr_count=context.get("open_pr_count"),
                allow_edge_creation=not dry_run,
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
                label_reason, label_evidence = _clear_rework_execution_labels(client, context)
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
            ref=ref,
            expected_pr_number=cast(GithubPullRequest, context["pr"]).number,
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
                open_pr_count=context.get("open_pr_count"),
                allow_edge_creation=not dry_run,
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
        run = _task_run_after_rework(
            conn,
            task_id,
            int(context["event_at"]),
            rework_round=context["event"][0].get("rework_round"),
        )
        if run is None:
            run = _untrusted_task_run_after_rework(
                conn, task_id, int(context["event_at"]),
            )
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
                        "operator_attention_predicted": {
                            "reason": "rework_human_attention",
                        },
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
                    label_reason, label_evidence = _clear_rework_execution_labels(
                        client, context,
                    )
                except GithubCompletionError as exc:
                    return {
                        "task_id": task_id,
                        "status": str(db_result.get("status") or "review"),
                        "changed": bool(db_result.get("changed")),
                        "reason": "rework_attention_label_projection_failed",
                        "error": str(exc),
                        "diagnostic": delivery_reason,
                    }
                _record_rework_attention(
                    conn,
                    task_id,
                    context,
                    reason=delivery_reason,
                    evidence=evidence,
                )
                try:
                    _post_rework_attention_pr_comment(
                        client,
                        ref,
                        int(context["pr_number"]),
                        task_id,
                        reason=delivery_reason,
                        evidence=evidence,
                    )
                except GithubCompletionError as exc:
                    print(
                        f"kanban-github-sync: attention PR comment failed for "
                        f"{ref.repository}#{context['pr_number']} task={task_id}: {exc}",
                        file=sys.stderr,
                    )
                return {
                    "task_id": task_id,
                    "status": str(db_result.get("status") or "review"),
                    "changed": bool(db_result.get("changed")),
                    "reason": "rework_human_attention",
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
                label_reason, label_evidence = _clear_rework_execution_labels(client, context)
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
            label_reason, label_evidence = _clear_rework_execution_labels(client, context)
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
        "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,)
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
        "previous_assignee": row["assignee"],
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
        "previous_assignee": row["assignee"],
        "merge_authority": "human",
        "auto_merge": False,
        "source": "github_edge_rework_normalization",
    }
    with conn:
        latest = conn.execute(
            "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,)
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
        conn.execute(
            "UPDATE tasks SET assignee = ? "
            "WHERE id = ? AND status = 'ready' AND claim_lock IS NULL",
            (REWORK_RESERVED_ASSIGNEE, task_id),
        )
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
        if governing == "github_pr_rework_projection_failure":
            if not _claim_projection_failure_is_retryable(conn, task_id):
                continue
        elif governing != "github_pr_rework" and not (
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

    Fresh rework rows are reserved with ``REWORK_RESERVED_ASSIGNEE`` so the
    core dispatcher cannot claim them before this edge lane.  Replace that
    reservation (or a legacy NULL assignee) with
    ``kanban.default_assignee`` only while holding the shared dispatch lock,
    immediately before the edge-owned claim.
    """
    assignee = str(row.get("assignee") or "").strip() or None
    if assignee == REWORK_RESERVED_ASSIGNEE:
        assignee = None
    if assignee:
        return assignee, None
    if not default_assignee:
        event = _latest_rework_event(conn, str(row["id"]))
        previous_assignee = (
            event[0].get("previous_assignee") if event is not None else None
        )
        if (
            isinstance(previous_assignee, str)
            and previous_assignee.strip()
            and previous_assignee.strip() != REWORK_RESERVED_ASSIGNEE
        ):
            default_assignee = previous_assignee.strip()
    if not default_assignee:
        return None, "unassigned"
    if dry_run:
        return default_assignee, None
    cur = conn.execute(
        "UPDATE tasks SET assignee = ? WHERE id = ? "
        "AND (assignee IS NULL OR assignee = '' OR assignee = ?)",
        (default_assignee, row["id"], REWORK_RESERVED_ASSIGNEE),
    )
    if cur.rowcount == 1:
        _append_sync_event(
            conn, str(row["id"]),
            {"assignee": default_assignee, "source": "edge_rework_dispatch"},
            kind="assigned",
        )
    return default_assignee, None


def _reserve_rework_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Keep a released rework row out of the generic core dispatcher."""
    conn.execute(
        "UPDATE tasks SET assignee = ? "
        "WHERE id = ? AND status = 'ready' AND claim_lock IS NULL",
        (REWORK_RESERVED_ASSIGNEE, task_id),
    )


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
    context = (
        rework_contexts.get(task_id)
        if isinstance(rework_contexts, Mapping)
        else None
    )
    latest_event = _latest_rework_event(conn, task_id)
    normalized_dry_run = bool(
        dry_run
        and normalized_task_ids is not None
        and task_id in normalized_task_ids
    )
    if isinstance(rework_contexts, Mapping) and not normalized_dry_run:
        # The context map is a same-wake snapshot.  Do not claim a task when
        # a direct rework transition replaced the governing event after that
        # snapshot was built; using the old event would mis-bind provenance.
        if not isinstance(context, Mapping) or "_error" in context:
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "rework_context_unavailable",
            }]
        context_event = context.get("event")
        if not _same_rework_event(context_event, latest_event):
            latest_payload = (
                latest_event[0] if isinstance(latest_event, tuple) else {}
            )
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "stale_rework_context",
                "rework_round": latest_payload.get("rework_round"),
            }]
        claim_event = latest_event
    else:
        claim_event = (
            context.get("event")
            if isinstance(context, Mapping)
            else None
        )
        if not isinstance(claim_event, tuple) or len(claim_event) != 3:
            claim_event = latest_event
    if not normalized_dry_run:
        event_ref: GithubTaskRef | None = None
        expected_pr_number: int | None = None
        if isinstance(context, Mapping):
            typed_context = cast(Mapping[str, object], context)
            try:
                repository = typed_context["repository"]
                issue_number = typed_context["issue_number"]
                pr_number = typed_context["pr_number"]
                if (
                    not isinstance(repository, str)
                    or not _positive_rework_int(issue_number)
                    or not _positive_rework_int(pr_number)
                ):
                    raise ValueError("invalid rework context identity")
                event_ref = GithubTaskRef(repository, cast(int, issue_number))
                expected_pr_number = cast(int, pr_number)
            except (KeyError, TypeError, ValueError):
                event_ref = None
        if event_ref is None:
            body_row = cast(sqlite3.Row | None, conn.execute(
                "SELECT body FROM tasks WHERE id = ?", (task_id,)
            ).fetchone())
            if body_row is not None:
                body_value = cast(object, body_row["body"])
                event_ref = parse_task_ref(str(body_value or ""))
        invalid_event = _validate_rework_event(
            cast(object, claim_event),
            ref=event_ref,
            expected_pr_number=expected_pr_number,
        )
        if invalid_event is not None:
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "rework_context_unavailable",
                "diagnostic": invalid_event,
            }]
    claim_lock = _rework_dispatch_claim_lock(task_id, claim_event)

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

    # Resolve private implementation owners before taking the claim. Hermes
    # no longer exports these helpers through kanban_db.
    from hermes_cli import kanban_db_dispatch, kanban_db_workspace

    claimed = kanban_db.claim_task(conn, task_id, claimer=claim_lock)
    if claimed is None:
        return [{
            "task_id": task_id, "status": "ready", "changed": False,
            "reason": "claim_failed",
        }]

    try:
        _append_rework_dispatch_provenance(
            conn,
            claimed,
            context=context if isinstance(context, Mapping) else None,
            phase="claimed",
        )
    except Exception as exc:  # noqa: BLE001 - reclaim on provenance failure
        try:
            kanban_db.reclaim_task(
                conn,
                task_id,
                reason="edge rework claim provenance write failed",
            )
        except Exception as reclaim_exc:  # noqa: BLE001 - preserve READY fail-closed
            return [{
                "task_id": task_id, "status": "running", "changed": True,
                "reason": "claim_projection_reclaim_failed",
                "error": f"{type(exc).__name__}: {exc}; "
                          f"{type(reclaim_exc).__name__}: {reclaim_exc}",
            }]
        _reserve_rework_task(conn, task_id)
        if on_failure is not None:
            try:
                on_failure(claimed, "claim_provenance_failed")
            except Exception as projection_exc:  # noqa: BLE001 - isolate lifecycle cleanup failure
                print(
                    f"kanban-github-sync: failed to clear rework execution labels after "
                    f"claim provenance failure (task {task_id}): "
                    f"{type(projection_exc).__name__}",
                    file=sys.stderr,
                )
        return [{
            "task_id": task_id, "status": "ready", "changed": False,
            "reason": "rework_claim_provenance_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }]

    if on_claim is not None:
        try:
            claim_projection = on_claim(claimed)
        except Exception as exc:
            claim_projection = {"ok": False, "error": str(exc)}
        projection_details: Mapping[str, Any] = (
            claim_projection if isinstance(claim_projection, Mapping) else {}
        )
        if not projection_details.get("ok"):
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
            _reserve_rework_task(conn, task_id)
            if on_failure is not None:
                try:
                    on_failure(claimed, "claim")
                except Exception as exc:  # noqa: BLE001 - isolate lifecycle cleanup failure
                    print(
                        f"kanban-github-sync: failed to clear rework execution labels after claim "
                        f"(task {claimed.id}): {type(exc).__name__}",
                        file=sys.stderr,
                    )
            if client is not None and rework_contexts is not None:
                failure_context = rework_contexts.get(task_id)
                if isinstance(failure_context, Mapping):
                    try:
                        with conn:
                            _append_claim_projection_failure_event(
                                conn,
                                task_id,
                                failure_context,
                                projection_details,
                            )
                    except Exception as exc:  # noqa: BLE001 - preserve READY fail-closed
                        print(
                            f"kanban-github-sync: failed to record claim projection "
                            f"failure (task {task_id}): {type(exc).__name__}",
                            file=sys.stderr,
                        )
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "working_label_projection_failed",
                "error": str(projection_details.get("error") or "unknown"),
                "lifecycle": dict(projection_details),
            }]

    # Resolve the workspace exactly like the core dispatcher does (the
    # rework worktree already exists from the first run; re-resolution
    # keeps parity if it was moved or recreated).
    try:
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch = kanban_db_workspace._resolve_worktree_workspace(
                claimed, board=board
            )
        else:
            workspace = kanban_db_workspace.resolve_workspace(claimed, board=board)
            resolved_branch = None
    except Exception as exc:
        auto_blocked = bool(kanban_db_dispatch._record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed",
            failure_limit=(failure_limit if failure_limit is not None
                           else kanban_db_dispatch.DEFAULT_FAILURE_LIMIT),
            release_claim=True, end_run=True,
        ))
        _reserve_rework_task(conn, task_id)
        if on_failure is not None:
            try:
                on_failure(claimed, "workspace_resolve_failed")
            except Exception as projection_exc:
                print(
                    f"kanban-github-sync: failed to clear rework execution labels after workspace failure: {type(projection_exc).__name__}",
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
    kanban_db_workspace.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        kanban_db_workspace.set_branch_name(
            conn, claimed.id,
            resolved_branch or (claimed.branch_name or "").strip() or f"wt/{claimed.id}",
        )

    spawn = spawn_fn if spawn_fn is not None else kanban_db_dispatch._default_spawn
    try:
        pid = spawn(claimed, str(workspace), board=board)
        if pid:
            kanban_db_dispatch._set_worker_pid(conn, claimed.id, int(pid))
        _append_rework_dispatch_provenance(
            conn,
            claimed,
            context=context if isinstance(context, Mapping) else None,
            phase="spawned",
            pid=int(pid) if pid else None,
        )
    except Exception as exc:
        auto_blocked = bool(kanban_db_dispatch._record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed",
            failure_limit=(failure_limit if failure_limit is not None
                           else kanban_db_dispatch.DEFAULT_FAILURE_LIMIT),
            release_claim=True, end_run=True,
        ))
        _reserve_rework_task(conn, task_id)
        if on_failure is not None:
            try:
                on_failure(claimed, "spawn_failed")
            except Exception as projection_exc:
                print(
                    f"kanban-github-sync: failed to clear rework execution labels after spawn failure: {type(projection_exc).__name__}",
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
        from hermes_cli import kanban_db_connect

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
    lock_factory = getattr(kanban_db_connect, "_dispatch_tick_lock", None)
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
    kanban_db_connect = _import_kanban_db_connect()

    # Self-healing workspace drift repair (Issue #76) — runs before any
    # reconciliation or dispatch so repaired bindings are what the rest of
    # the wake resolves. Deterministic, idempotent, and fail-closed on
    # unresolvable anchors (reported, never guessed). Dry-run wakes run the
    # strictly READ-ONLY preview twin instead: identical detection, zero
    # UPDATE/event writes, zero spawn. Entries are surfaced at the FRONT of
    # this wake's JSON result either way.
    selfheal_entries: list[dict[str, Any]] = []
    try:
        ws_admission = sys.modules.get("kanban_workspace_admission")
        if ws_admission is None:
            import kanban_workspace_admission as ws_admission  # pyright: ignore[reportImplicitRelativeImport]
        with kanban_db_connect.connect_closing(board=board) as heal_conn:
            if dry_run:
                selfheal_entries = ws_admission.preview_workspace_drift(
                    heal_conn, board
                )
            else:
                selfheal_entries = ws_admission.repair_workspace_drift(
                    heal_conn, kanban_db, board
                )
    except Exception as exc:
        selfheal_entries = [{
            "board": board, "reason": "selfheal_pass_failed",
            "error": f"{type(exc).__name__}: {exc}", "changed": False,
        }]

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
                attention_payload = _operator_attention_payload(conn, entry)
                if attention_payload is not None:
                    entry["operator_attention_predicted"] = {
                        "reason": attention_payload["reason"],
                        "attention_key": attention_payload["attention_key"],
                        "incident_provenance": attention_payload["incident_provenance"],
                    }
                    if attention_payload.get(
                        "incident_unresolved"
                    ):
                        entry["operator_attention_predicted"][
                            "incident_unresolved"
                        ] = True
            else:
                _record_operator_attention(conn, entry)
        return entry

    with kanban_db_connect.connect_closing(board=board) as conn:
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

        def _refresh_context_after_rework(
            task_id: str,
            ref: GithubTaskRef,
            decision: GithubCompletionDecision,
            entry: Mapping[str, Any],
        ) -> None:
            if not entry.get("changed") or entry.get("reason") not in {
                "agent_rework", "maintainer_retry_consumed",
            }:
                return
            lifecycle_contexts_by_task[task_id] = _fresh_rework_context(
                conn, client, ref, decision, task_id,
            )

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
                # Terminal merge convergence (Issue #73): a stale
                # blocked/unstarted rework chain under a canonical intake
                # root whose Issue is closed and whose required PR(s)
                # merged is converged in one pass BEFORE the classic gate
                # lane restores the root.  Any ambiguous shape, active
                # ownership, or non-authoritative GitHub read fails closed
                # and falls through to the classic lane unchanged.
                # Text sources are collected inside this pending branch
                # (the general collection below is only reached when the
                # gate is already satisfied).  A lookup failure is not
                # equivalent to an empty source set: handoff text may carry
                # another required PR, so stop this task's sync pass with an
                # explicit diagnostic rather than risking false merge
                # completion from incomplete evidence.
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
                                json.dumps(
                                    run.metadata, ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                except Exception as exc:  # run/comment API drift -> fail closed
                    results.append(_annotate({
                        "task_id": task_id,
                        "status": row["status"],
                        "changed": False,
                        "reason": "text_source_lookup_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }, row, ref))
                    continue
                try:
                    convergence = _attempt_terminal_merge_convergence(
                        conn,
                        client,
                        task_id,
                        row,
                        ref,
                        text_sources,
                        dry_run=dry_run,
                    )
                except (sqlite3.Error, SyncError, GithubCompletionError) as exc:
                    convergence = {
                        "task_id": task_id,
                        "status": row["status"],
                        "changed": False,
                        "reason": "terminal_convergence_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if convergence is not None:
                    results.append(_annotate(convergence, row, ref))
                    continue
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

            # P0 Invariant: Authoritative done never regresses to review on weak subsequent lookups
            if (
                row["status"] == "done"
                and decision.desired_status == "review"
                and decision.reason == "no_linked_pr"
                and _has_prior_authoritative_done(conn, task_id)
            ):
                decision = replace(
                    decision,
                    desired_status="done",
                    reason="authoritative_done_preserved",
                )

            # Explicit closed-unmerged PR supersession is the only path that
            # may re-intake a parked Issue while its historical PR remains
            # linked.  Evaluate the signal against the unfiltered fresh
            # decision first; then exclude the durable evidence from all later
            # completion decisions.
            if decision.authoritative:
                supersede_result = _consume_explicit_pr_supersede(
                    conn, client, ref, decision, task_id, row,
                    dry_run=dry_run,
                )
                if supersede_result is not None:
                    results.append(_annotate(supersede_result, row, ref))
                    continue
                superseded_prs = _explicit_superseded_pr_numbers(
                    conn, task_id, ref,
                )
                if superseded_prs:
                    decision = evaluate_completion(
                        ref,
                        decision.pull_requests,
                        linked_pr_numbers=decision.linked_pr_numbers,
                        superseded_pr_numbers=superseded_prs,
                    )

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
                    _refresh_context_after_rework(
                        task_id, ref, decision, lifecycle_entry,
                    )
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
                blocked_entry = _reconcile_blocked(
                    conn,
                    client,
                    ref,
                    decision,
                    row,
                    dry_run=dry_run,
                )
                _refresh_context_after_rework(
                    task_id, ref, decision, blocked_entry,
                )
                results.append(_annotate(blocked_entry, row, ref))
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
                    _refresh_context_after_rework(
                        task_id, ref, decision, lifecycle_entry,
                    )
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
                # The agent-rework label intentionally stays on the PR until
                # the edge dispatcher claims the Kanban task and atomically
                # swaps it for agent-working.  Refresh the in-memory context
                # first so this same wake cannot claim the prior round.
                _refresh_context_after_rework(task_id, ref, decision, result)
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
                    return {
                        "ok": False,
                        "error": str(exc),
                        "failure_class": exc.failure_class,
                        "http_status": exc.status,
                        "before_labels": exc.before_labels,
                        "desired_labels": exc.desired_labels,
                        "observed_labels": exc.observed_labels,
                    }
                return {"ok": True, "label_action": label_reason, "lifecycle": label_evidence}

            def _failure_projection(claimed: Any, stage: str) -> None:
                ctx = lifecycle_contexts_by_task.get(str(claimed.id))
                if not ctx or "_error" in ctx:
                    return
                try:
                    _clear_rework_execution_labels(client, ctx)
                except GithubCompletionError as exc:
                    print(
                        f"kanban-github-sync: failed to clear rework execution labels after {stage} (task {claimed.id}): {type(exc).__name__}",
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
                    "board": board,
                }]
            # Structured-result extension (intake Telegram observer): a
            # spawned rework worker is a READY -> RUNNING transition; every
            # dispatch entry carrying a task id gets its repository/issue
            # number attached via the task body ref.
            for entry in dispatch_entries:
                entry = cast(dict[str, Any], entry)
                entry.setdefault("board", board)
                if not entry.get("task_id"):
                    attention_payload = _operator_attention_payload(conn, entry)
                    if attention_payload is not None:
                        _attach_operator_attention(entry, attention_payload)
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
                _record_operator_attention(conn, entry)
            results.extend(dispatch_entries)
        # Surface the self-healing pass first so operators see what was
        # repaired (real runs) or predicted (dry-run) ahead of any
        # reconciliation transition in this wake.
        if selfheal_entries:
            results = selfheal_entries + results
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
