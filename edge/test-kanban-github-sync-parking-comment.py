#!/usr/bin/env python3
"""Parking-marker regressions for the DONE -> REVIEW edge transition.

Covers the GitHub-backed review parking UX contract:

(a) every authoritative done->review parking (apply_decision with a review
    decision) appends exactly one structured ``[parked: ...]`` comment that
    names the reason, the linked PRs, and the next automatic trigger;
(b) repeated syncs over an unchanged parked situation never duplicate the
    marker (idempotent by exact task+reason+pr body);
(c) a changed situation (different reason/PR set) renders a new line once;
(d) non-review decisions (REVIEW -> DONE) and non-authoritative decisions
    never append parking comments.

Run directly:

    python3 edge/test-kanban-github-sync-parking-comment.py

Every test builds a fresh temp HERMES_HOME; the real Kanban DB layer is used
(no mocks) and no real GitHub call is ever made.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/ws/hermes-agent")

SCRIPT = Path(__file__).resolve().parent / "kanban-github-sync.py"
spec = importlib.util.spec_from_file_location("kanban_github_sync_parking", SCRIPT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

from hermes_cli.kanban_db import create_task  # type: ignore  # noqa: E402
from hermes_cli.kanban_db_connect import connect_closing, init_db  # type: ignore  # noqa: E402


def kanban_db_create(title: str, body: str, idem: str) -> str:
    with connect_closing() as conn:
        return create_task(
            conn,
            title=title,
            body=body,
            assignee="kanban-main",
            created_by="parking-test",
            workspace_kind="scratch",
            idempotency_key=idem,
        )

PASS: list[str] = []
FAIL: list[str] = []

_KANBAN_PATH_ENV_KEYS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_ROOT",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
    "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_WORKSPACE",
)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def github_backed_body() -> str:
    return (
        "# GitHub Issue intake\n\n"
        "## Provenance\n\n"
        "- source: github-issue\n"
        "- repository: rhgo1749/H4V3-DJ\n"
        "- issue number: 49\n"
        "- issue URL: https://github.com/rhgo1749/H4V3-DJ/issues/49\n"
        "- issue title: Test issue title\n"
        "- idempotency key: github:rhgo1749/H4V3-DJ:issue:49\n"
        "- completion contract: github-pr\n\n"
    )


def make_pr(number: int, *, state: str = "open", merged: bool = False):
    return mod.GithubPullRequest(
        number=number,
        state=state,
        merged=merged,
        base_branch="main",
        html_url=f"https://github.com/rhgo1749/H4V3-DJ/pull/{number}",
        title=f"PR {number}",
        head_sha=f"sha-{number}",
        head_ref=f"h4v3-dj/t-test-{number}",
        author="rhgo1749",
        body="",
        draft=False,
    )


def new_done_task(tag: str) -> str:
    init_db()
    with connect_closing() as conn:
        tid = kanban_db_create(
            f"parking test {tag}", github_backed_body(), f"parking:{tag}"
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = strftime('%s','now'), "
            "assignee = 'kanban-developer' WHERE id = ?",
            (tid,),
        )
        conn.commit()
    return tid


def task_comments(tid: str) -> list[dict]:
    with connect_closing() as conn:
        rows = conn.execute(
            "SELECT author, body, created_at FROM task_comments "
            "WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        return [dict(r) for r in rows]


@contextlib.contextmanager
def isolated_test_environment():
    previous_home = os.environ.get("HERMES_HOME")
    previous_paths = {k: os.environ.get(k) for k in _KANBAN_PATH_ENV_KEYS}
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="parking-test-")
    for key in _KANBAN_PATH_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
        for key, value in previous_paths.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_1_open_pr_parks_with_single_marker():
    print("1. done + linked_pr_open -> one [parked: awaiting-merge] comment")
    tid = new_done_task("open")
    decision = mod.GithubCompletionDecision(
        desired_status="review",
        reason="linked_pr_open",
        linked_pr_numbers=(74,),
        pull_requests=(make_pr(74),),
    )
    with connect_closing() as conn:
        result = mod.apply_decision(conn, tid, decision)
    check("transition applied", result.get("changed") is True, str(result))
    comments = task_comments(tid)
    markers = [c for c in comments if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)]
    check("exactly one marker", len(markers) == 1, json.dumps(comments, ensure_ascii=False))
    if markers:
        body = markers[0]["body"]
        check(
            "marker format",
            body == "[parked: awaiting-merge] reason=linked_pr_open pr=#74 "
            "next=github-edge(merge 감지 시 자동 해제). 사람 행동 불필요.",
            repr(body),
        )
        check("marker author", markers[0]["author"] == "github-edge")


def test_2_no_linked_pr_marker_format():
    print("2. done + no_linked_pr -> [parked: awaiting-pr] marker")
    tid = new_done_task("nopr")
    decision = mod.GithubCompletionDecision(
        desired_status="review",
        reason="no_linked_pr",
        linked_pr_numbers=(),
        pull_requests=(),
    )
    with connect_closing() as conn:
        result = mod.apply_decision(conn, tid, decision)
    check("transition applied", result.get("changed") is True, str(result))
    comments = task_comments(tid)
    markers = [c for c in comments if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)]
    check("exactly one marker", len(markers) == 1, json.dumps(comments, ensure_ascii=False))
    if markers:
        body = markers[0]["body"]
        check(
            "awaiting-pr format",
            body == "[parked: awaiting-pr] reason=no_linked_pr "
            "next=github-edge(PR 링크 감지 시 자동 재평가). 사람 행동 불필요.",
            repr(body),
        )


def test_3_repeated_sync_never_duplicates():
    print("3. repeated identical transitions append no second marker")
    tid = new_done_task("repeat")
    decision = mod.GithubCompletionDecision(
        desired_status="review",
        reason="linked_pr_open",
        linked_pr_numbers=(74,),
        pull_requests=(make_pr(74),),
    )
    with connect_closing() as conn:
        first = mod.apply_decision(conn, tid, decision)
        # Card is now review; an unchanged re-evaluation is idempotent at the
        # apply_decision level (same desired status -> no change, no append).
        second = mod.apply_decision(conn, tid, decision)
    check("first applied", first.get("changed") is True)
    check("second idempotent", second.get("changed") is False, str(second))
    # A worker re-completes the card (core provisional done again) and the
    # edge parks it once more over the same unchanged PR situation.
    with connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = strftime('%s','now') "
            "WHERE id = ?",
            (tid,),
        )
        conn.commit()
        third = mod.apply_decision(conn, tid, decision)
    check("re-park applied", third.get("changed") is True, str(third))
    markers = [
        c for c in task_comments(tid)
        if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)
    ]
    check("still exactly one marker", len(markers) == 1, json.dumps(markers, ensure_ascii=False))


def test_4_changed_situation_records_new_line_once():
    print("4. changed reason/pr set renders exactly one additional line")
    tid = new_done_task("changed")
    open_decision = mod.GithubCompletionDecision(
        desired_status="review",
        reason="linked_pr_open",
        linked_pr_numbers=(74,),
        pull_requests=(make_pr(74),),
    )
    closed_decision = mod.GithubCompletionDecision(
        desired_status="review",
        reason="linked_pr_closed_not_merged",
        linked_pr_numbers=(74,),
        pull_requests=(make_pr(74, state="closed"),),
    )
    with connect_closing() as conn:
        mod.apply_decision(conn, tid, open_decision)
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = strftime('%s','now') "
            "WHERE id = ?",
            (tid,),
        )
        mod.apply_decision(conn, tid, closed_decision)
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = strftime('%s','now') "
            "WHERE id = ?",
            (tid,),
        )
        mod.apply_decision(conn, tid, closed_decision)
    bodies = [
        c["body"] for c in task_comments(tid)
        if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)
    ]
    check(
        "two distinct situation lines",
        len([b for b in bodies if "reason=linked_pr_open" in b]) == 1
        and len([b for b in bodies if "reason=linked_pr_closed_not_merged" in b]) == 1,
        json.dumps(bodies, ensure_ascii=False),
    )


def test_5_done_transition_appends_no_marker():
    print("5. REVIEW -> DONE appends no parking comment")
    tid = new_done_task("done-side")
    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
    decision = mod.GithubCompletionDecision(
        desired_status="done",
        reason="all_linked_prs_merged",
        linked_pr_numbers=(74,),
        pull_requests=(make_pr(74, state="closed", merged=True),),
    )
    with connect_closing() as conn:
        result = mod.apply_decision(conn, tid, decision)
    check("merged -> done", result.get("status") == "done" and result.get("changed") is True, str(result))
    markers = [
        c for c in task_comments(tid)
        if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)
    ]
    check("no marker on done", not markers, json.dumps(task_comments(tid), ensure_ascii=False))


def test_6_non_authoritative_and_preserved_states_stay_clean():
    print("6. non-authoritative / preserved-state paths never mark")
    tid = new_done_task("guarded")
    stale = mod.GithubCompletionDecision(
        desired_status=None,
        reason="github_query_failed",
        error="boom",
    )
    with connect_closing() as conn:
        r1 = mod.apply_decision(conn, tid, stale)
    check("non-authoritative refused", r1.get("changed") is False, str(r1))
    check("stays done", _status_of(tid) == "done")
    markers = [
        c for c in task_comments(tid)
        if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)
    ]
    check("no marker from failed query", not markers)
    # Preserved state (ready) refuses the park entirely.
    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        r2 = mod.apply_decision(
            conn,
            tid,
            mod.GithubCompletionDecision(
                desired_status="review",
                reason="linked_pr_open",
                linked_pr_numbers=(74,),
                pull_requests=(make_pr(74),),
            ),
        )
    check("preserved state untouched", r2.get("reason") == "state_preserved", str(r2))
    check("still ready", _status_of(tid) == "ready")
    check("still no marker", not [
        c for c in task_comments(tid)
        if c["body"].startswith(mod._PARKING_COMMENT_PREFIX)
    ])


def _status_of(tid: str) -> str:
    with connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        return str(row["status"])


TESTS = [
    test_1_open_pr_parks_with_single_marker,
    test_2_no_linked_pr_marker_format,
    test_3_repeated_sync_never_duplicates,
    test_4_changed_situation_records_new_line_once,
    test_5_done_transition_appends_no_marker,
    test_6_non_authoritative_and_preserved_states_stay_clean,
]


def main() -> int:
    for test in TESTS:
        print(f"\n=== {test.__name__} ===")
        with isolated_test_environment():
            test()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
