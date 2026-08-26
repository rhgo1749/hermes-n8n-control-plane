#!/usr/bin/env python3
"""Deterministic terminal-convergence regressions (Issue #73).

Models the H4V3-DJ #88 stranded rework graph:

    t_dba6e389 (done implementation)
      -> t_fbd3f0b9 (blocked capability; rework actually delivered)
         -> t_9a6395f0 (todo reviewer, never run)
            -> t_7e4e4523 (todo GitHub intake root)

and proves that ONLY a freshly-proven GitHub state (source Issue closed,
every linked PR closed+merged into the target branch, no active claim/run/
worker ownership anywhere in the chain) converges the graph to
``done / archived / done`` in one pass, idempotently, without ever
promoting, claiming, spawning, or re-running a worker.  Every absent
authority (open Issue, open PR, closed-unmerged PR, GitHub error, active
ownership, ambiguous/unconvergeable node) fails closed and preserves the
graph.

Run with the Hermes venv python:

    /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-terminal-convergence.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, "/ws/hermes-agent")

REPO = "rhgo1749/H4V3-DJ"
ISSUE_N = 88
PR_N = 144
TARGET = "main"

# The exact stranded #88 topology (node ids are stable across the fixture).
DONE_IMPL = "t_dba6e389"
BLOCKED_REWORK = "t_fbd3f0b9"
TODO_REVIEWER = "t_9a6395f0"
ROOT = "t_7e4e4523"

SCRIPT = Path(__file__).resolve().parent / "kanban-github-sync.py"
spec = importlib.util.spec_from_file_location("kanban_github_sync_tc", SCRIPT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["kanban_github_sync_tc"] = mod
spec.loader.exec_module(mod)

from hermes_cli import kanban_db  # type: ignore  # noqa: E402
from hermes_cli.kanban_db import connect_closing, init_db  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Isolated environment (real Kanban DB layer, no real GitHub call)
# ---------------------------------------------------------------------------
_KANBAN_PATH_ENV_KEYS = (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_ROOT", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_WORKSPACE",
)
_TEST_ENV_KEYS = ("HERMES_HOME", *_KANBAN_PATH_ENV_KEYS)


def _prepare_isolated_environment() -> None:
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="tc73-test-")
    for key in _KANBAN_PATH_ENV_KEYS:
        os.environ.pop(key, None)


@contextlib.contextmanager
def isolated_test_environment():
    previous = {key: os.environ.get(key) for key in _TEST_ENV_KEYS}
    try:
        _prepare_isolated_environment()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# Fake GitHub client (routing fake; unknown endpoints raise)
# ---------------------------------------------------------------------------
def pr_payload(number: int, *, state: str = "closed", merged: bool = True,
               base: str = "main", head_sha: str = "6669d5c8b75d758c5557b941997fd9040849d966") -> dict:
    return {
        "number": number, "state": state, "merged": merged, "draft": False,
        "base": {"ref": base},
        "head": {"sha": head_sha, "ref": "h4v3-dj/issue-88-rework"},
        "title": f"PR {number}",
        "user": {"login": "rhgo1749"},
        "body": "",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


class FakeGitHub:
    def __init__(self, *, issue_state: str = "closed",
                 pr: dict | None = pr_payload(PR_N),
                 extra_pr: dict | None = None,
                 include_timeline_pr: bool = True,
                 fail_pr_fetch: bool = False,
                 on_issue_get: Any = None):
        self.issue_state = issue_state
        self.pr = pr
        self.extra_pr = extra_pr
        self.include_timeline_pr = include_timeline_pr
        self.fail_pr_fetch = fail_pr_fetch
        self.on_issue_get = on_issue_get
        self.issue_gets = 0

    def get(self, path: str, params: Any = None):
        if self.fail_pr_fetch and "/pulls/" in path:
            raise mod.GithubCompletionError("simulated PR fetch failure")
        if path.endswith(f"/issues/{ISSUE_N}"):
            self.issue_gets += 1
            if self.on_issue_get is not None:
                self.on_issue_get()
            return {"number": ISSUE_N, "state": self.issue_state}, {}
        marker = "/pulls/"
        if marker in path:
            number = int(path.rsplit("/", 1)[1])
            if number == PR_N:
                return self.pr, {}
            if self.extra_pr is not None and number == int(self.extra_pr["number"]):
                return self.extra_pr, {}
            raise mod.GithubCompletionError(f"unknown PR {number}")
        raise mod.GithubCompletionError(f"unexpected GET: {path}")

    def get_paginated(self, path: str, params: Any = None, max_pages: int = 10):
        if path.endswith(f"/issues/{ISSUE_N}/timeline"):
            if not self.include_timeline_pr:
                return []
            return [{
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": PR_N,
                        "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{PR_N}"},
                        "html_url": f"https://github.com/{REPO}/pull/{PR_N}",
                        "repository": {"full_name": REPO},
                    }
                },
            }]
        raise mod.GithubCompletionError(f"unexpected paginated GET: {path}")


# ---------------------------------------------------------------------------
# Graph fixtures (real Kanban DB layer)
# ---------------------------------------------------------------------------
def intake_body() -> str:
    return (
        "# GitHub Issue intake\n\n"
        "## Provenance\n\n"
        f"- source: github-issue\n"
        f"- repository: {REPO}\n"
        f"- issue number: {ISSUE_N}\n"
        f"- issue title: merged Issue stale rework graph\n"
        f"- target branch: {TARGET}\n"
        f"- completion contract: github-pr\n\n"
        "## Canonical Issue body\n\n"
        "--- BEGIN GITHUB ISSUE BODY ---\n"
        "Converge the stranded #88 rework graph.\n"
        "--- END GITHUB ISSUE BODY ---\n"
    )


def link_body() -> str:
    return "# Rework / reviewer handoff card (no intake provenance).\n"


def _create_task(status: str, body: str, idempotency_key: str, *,
                 assignee: str = "kanban-developer",
                 parents: list[str] | None = None) -> str:
    with connect_closing() as conn:
        tid = kanban_db.create_task(
            conn,
            title=f"tc73 {idempotency_key}",
            body=body,
            assignee=assignee,
            created_by="tc73-fixture",
            workspace_kind="scratch",
            idempotency_key=idempotency_key,
            skills=["github"],
            parents=parents or [],
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()
    return tid


def build_graph(root_status: str = "todo",
                blocked_status: str = "blocked",
                reviewer_status: str = "todo",
                impl_status: str = "done",
                blocked_claim: str | None = None,
                extra_ancestor: tuple[str, str] | None = None,
                rework_evidence: bool = True) -> dict:
    """Create the #88-shaped chain and return the node ids used.

    Links (parent -> child): impl -> blocked -> reviewer -> root.
    ``extra_ancestor`` injects an additional parent above ``impl`` to
    model an unrelated/ambiguous node.
    """
    impl = _create_task(impl_status, link_body(), "impl-88")
    blocked = _create_task(
        blocked_status, link_body(), "rework-88", parents=[impl]
    )
    reviewer = _create_task(
        reviewer_status,
        link_body(),
        "reviewer-88",
        assignee="kanban-reviewer",
        parents=[blocked],
    )
    root = _create_task(root_status, intake_body(), "root-88", parents=[reviewer])
    with connect_closing() as conn:
        # Keep the synthetic blocked-node lifecycle ordered like production:
        # creation precedes the rework event, while later regressions can
        # exercise equal-second event ids deterministically.
        conn.execute(
            "UPDATE task_events SET created_at = 1 "
            "WHERE task_id = ? AND kind = 'created'",
            (blocked,),
        )
        if rework_evidence:
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (
                    blocked,
                    "github_pr_rework",
                    json.dumps({
                        "previous_status": "blocked",
                        "new_status": "ready",
                        "repository": REPO,
                        "issue_number": ISSUE_N,
                        "pr_number": PR_N,
                        "head_sha": pr_payload(PR_N)["head"]["sha"],
                        "reason": "agent_rework",
                        "rework_round": 1,
                        "source": "github",
                        "merge_authority": "human",
                        "auto_merge": False,
                    }, sort_keys=True),
                    2,
                ),
            )
        if blocked_claim is not None:
            conn.execute(
                "UPDATE tasks SET claim_lock = ? WHERE id = ?",
                (blocked_claim, blocked),
            )
        if extra_ancestor is not None:
            ancestor_status, ancestor_key = extra_ancestor
            ancestor = _create_task(ancestor_status, link_body(), f"extra-88-{ancestor_key}")
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, impl))
            conn.commit()
        else:
            conn.commit()
    return {"impl": impl, "blocked": blocked, "reviewer": reviewer, "root": root}


def statuses(ids: dict) -> dict:
    with connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, status, claim_lock, worker_pid, current_run_id, block_kind, completed_at "
            "FROM tasks WHERE id IN ({})".format(",".join("?" for _ in ids.values())),
            tuple(ids.values()),
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}


def events_for(task_id: str) -> list[dict]:
    with connect_closing() as conn:
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [{"kind": r["kind"], "payload": json.loads(r["payload"] or "{}")} for r in rows]


def event_snapshot(ids: dict) -> dict[str, list[dict]]:
    return {tid: events_for(tid) for tid in ids.values()}


def run_sync(fake: FakeGitHub) -> list[dict]:
    return mod.sync_board("default", client=fake)


def run_counts() -> int:
    with connect_closing() as conn:
        return conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]


def assert_no_workers(ids: dict) -> None:
    rows = statuses(ids)
    for tid, row in rows.items():
        assert row["claim_lock"] is None, (tid, row)
        assert row["worker_pid"] is None, (tid, row)
        assert row["current_run_id"] is None, (tid, row)
    assert run_counts() == 0, run_counts()


# ---------------------------------------------------------------------------
# 1. Qualifying #88-shaped graph converges to done/archived/done
# ---------------------------------------------------------------------------
def test_1_qualifying_convergence():
    print("1. #88-shaped graph + closed Issue + merged PR -> done/archived/done")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        before = statuses(ids)
        results = run_sync(fake)
        after = statuses(ids)

        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("root -> done (changed)", root_entry["status"] == "done" and root_entry["changed"] is True, str(root_entry))
        check("root reason is terminal_merge_convergence", root_entry.get("reason") == "terminal_merge_convergence", str(root_entry.get("reason")))
        check("blocked rework node -> done", after[ids["blocked"]]["status"] == "done", str(after[ids["blocked"]]))
        check("reviewer node -> archived", after[ids["reviewer"]]["status"] == "archived", str(after[ids["reviewer"]]))
        check("done impl node unchanged (done)", after[ids["impl"]]["status"] == "done", str(after[ids["impl"]]))
        check("before blocked was blocked", before[ids["blocked"]]["status"] == "blocked")
        check("before reviewer was todo", before[ids["reviewer"]]["status"] == "todo")
        check("root completed_at set", after[ids["root"]]["completed_at"] is not None)
        check("blocked completed_at set", after[ids["blocked"]]["completed_at"] is not None)

        root_events = events_for(ids["root"])
        conv = [e for e in root_events if e["kind"] == "github_pr_sync"
                and e["payload"].get("reason") == "terminal_merge_convergence"]
        check("root durable github_pr_sync event", len(conv) == 1, str(root_events))
        check("event carries merged PR provenance",
              bool(conv) and conv[0]["payload"].get("merged_prs") == [
                  {"number": PR_N, "head_sha": pr_payload(PR_N)["head"]["sha"], "base_branch": "main"}
              ], str(conv))
        check("event merge_authority=human / auto_merge=False",
              bool(conv) and conv[0]["payload"].get("merge_authority") == "human"
              and conv[0]["payload"].get("auto_merge") is False)
        blocked_events = [e for e in events_for(ids["blocked"])
                          if e["kind"] == "github_pr_sync"]
        check("blocked node durable event", len(blocked_events) == 1
              and blocked_events[0]["payload"].get("new_status") == "done", str(blocked_events))
        reviewer_events = [e for e in events_for(ids["reviewer"])
                           if e["kind"] == "github_pr_sync"]
        check("reviewer node durable event (archived, not fabricated PASS)",
              len(reviewer_events) == 1
              and reviewer_events[0]["payload"].get("new_status") == "archived"
              and "review" not in str(reviewer_events[0]["payload"].get("new_status")), str(reviewer_events))
        assert_no_workers(ids)
        check("no worker promoted/claimed/spawned; no runs", True)


# ---------------------------------------------------------------------------
# 2. Repeat pass is idempotent, no claim/spawn
# ---------------------------------------------------------------------------
def test_2_idempotent_repeat():
    print("2. repeat pass is a no-op, no claim/spawn")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        run_sync(fake)
        snapshot1 = statuses(ids)
        events1 = {tid: len(events_for(tid)) for tid in ids.values()}
        runs1 = run_counts()

        results = run_sync(fake)
        snapshot2 = statuses(ids)
        events2 = {tid: len(events_for(tid)) for tid in ids.values()}
        runs2 = run_counts()

        check("statuses unchanged on 2nd pass", snapshot1 == snapshot2,
              f"{snapshot1} -> {snapshot2}")
        check("no new events on 2nd pass", events1 == events2,
              f"{events1} -> {events2}")
        check("no new runs on 2nd pass", runs1 == runs2 == 0, f"{runs1} -> {runs2}")
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("root 2nd pass idempotent (not changed)",
              root_entry["changed"] is False and root_entry["status"] == "done", str(root_entry))
        assert_no_workers(ids)


# ---------------------------------------------------------------------------
# 3. Fail-closed: open Issue / open PR / closed-unmerged PR / GitHub error
# ---------------------------------------------------------------------------
def _preserve_graph(name, fake, ids, root_reason="internal_dependency_pending"):
    before = statuses(ids)
    events_before = event_snapshot(ids)
    results = run_sync(fake)
    after = statuses(ids)
    events_after = event_snapshot(ids)
    check(f"{name}: graph preserved", before == after, f"{before} -> {after}")
    check(f"{name}: events preserved", events_before == events_after,
          f"{events_before} -> {events_after}")
    root_entry = next(r for r in results if r["task_id"] == ids["root"])
    check(f"{name}: root still todo via classic gate",
          root_entry["status"] == "todo" and root_entry["changed"] is False
          and root_entry.get("reason") == root_reason, str(root_entry))
    assert_no_workers(ids)


def test_3_fail_closed():
    print("3. fail-closed preserves the graph")
    # 3a. Issue OPEN.
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="open", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        _preserve_graph("issue-open", fake, ids)
    # 3b. Required PR OPEN.
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="open", merged=False))
        ids = build_graph()
        _preserve_graph("pr-open", fake, ids)
    # 3c. Required PR closed-unmerged.
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=False))
        ids = build_graph()
        _preserve_graph("pr-closed-unmerged", fake, ids)
    # 3d. GitHub lookup failure.
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True),
                          fail_pr_fetch=True)
        ids = build_graph()
        _preserve_graph("github-error", fake, ids)


# ---------------------------------------------------------------------------
# 4. Active claim/run/worker ownership refuses convergence
# ---------------------------------------------------------------------------
def test_4_active_ownership():
    print("4. active ownership blocks automatic terminal cleanup")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph(blocked_claim="lock-88")
        before = statuses(ids)
        results = run_sync(fake)
        after = statuses(ids)
        check("graph preserved under active claim", before == after, f"{before} -> {after}")
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("root reports active-ownership refusal",
              root_entry.get("reason") == "terminal_convergence_active_ownership"
              and root_entry["changed"] is False, str(root_entry))
        check("blocked node still blocked", after[ids["blocked"]]["status"] == "blocked")
        assert_no_workers({k: v for k, v in ids.items() if k != "blocked"})


# ---------------------------------------------------------------------------
# 5. Unrelated / ambiguous ancestor refuses convergence
# ---------------------------------------------------------------------------
def test_5_ambiguous_ancestor():
    print("5. unrelated/ambiguous ancestor is not changed")
    # A `triage` ancestor is a valid status but not convergible -> fail closed.
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph(extra_ancestor=("triage", "unrelated"))
        before = statuses(ids)
        results = run_sync(fake)
        after = statuses(ids)
        check("graph preserved with triage ancestor", before == after, f"{before} -> {after}")
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("root reports unconvergeable ancestor",
              root_entry.get("reason") == "terminal_convergence_node_unconvergeable"
              and root_entry["changed"] is False, str(root_entry))
        assert_no_workers(ids)


def _unrelated_ancestor_case(status: str, key: str) -> None:
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph(extra_ancestor=(status, key))
        before = statuses(ids)
        events_before = event_snapshot(ids)
        results = run_sync(fake)
        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(f"unrelated {status} ancestor preserves graph", before == after,
              f"{before} -> {after}")
        check(f"unrelated {status} ancestor preserves events",
              events_before == events_after,
              f"{events_before} -> {events_after}")
        check(f"unrelated {status} ancestor fails closed",
              root_entry["changed"] is False
              and root_entry.get("reason") == "terminal_convergence_node_unconvergeable",
              str(root_entry))
        assert_no_workers(ids)


def test_5b_unrelated_allowed_status_ancestors():
    print("5b. unrelated blocked and allowed-status ancestors are preserved")
    _unrelated_ancestor_case("blocked", "unrelated-blocked")
    _unrelated_ancestor_case("ready", "unrelated-ready")


def test_5c_missing_rework_provenance():
    print("5c. missing stale rework provenance refuses convergence")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph(rework_evidence=False)
        before = statuses(ids)
        events_before = event_snapshot(ids)
        results = run_sync(fake)
        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("missing rework provenance preserves graph", before == after,
              f"{before} -> {after}")
        check("missing rework provenance preserves events",
              events_before == events_after,
              f"{events_before} -> {events_after}")
        check("missing rework provenance fails closed",
              root_entry["changed"] is False
              and root_entry.get("reason") == "terminal_convergence_node_unconvergeable",
              str(root_entry))
        assert_no_workers(ids)


def _later_human_hold_case(name: str, event_kind: str, payload: dict[str, Any]) -> None:
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        with connect_closing() as conn:
            conn.execute(
                "UPDATE tasks SET block_kind='needs_input' WHERE id=?",
                (ids["blocked"],),
            )
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (ids["blocked"], event_kind, json.dumps(payload, sort_keys=True), 3),
            )
            conn.commit()
        before = statuses(ids)
        events_before = event_snapshot(ids)
        results = run_sync(fake)
        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(f"{name}: graph preserved", before == after, f"{before} -> {after}")
        check(f"{name}: events preserved", events_before == events_after,
              f"{events_before} -> {events_after}")
        check(f"{name}: root remains dependency-pending",
              root_entry["status"] == "todo"
              and root_entry["changed"] is False
              and root_entry.get("reason") == "terminal_convergence_node_unconvergeable",
              str(root_entry))
        check(f"{name}: blocked metadata preserved",
              after[ids["blocked"]]["status"] == "blocked"
              and after[ids["blocked"]]["block_kind"] == "needs_input",
              str(after[ids["blocked"]]))
        assert_no_workers(ids)


def test_5f_later_human_block_preserves_graph():
    print("5f. later durable human block prevents stale rework convergence")
    _later_human_hold_case(
        "later human block",
        "blocked",
        {
            "reason": "needs human decision",
            "kind": "needs_input",
            "recurrences": 1,
            "source_status": "running",
        },
    )


def test_5g_later_current_round_attention_preserves_graph():
    print("5g. later current-round rework attention prevents convergence")
    _later_human_hold_case(
        "later current-round attention",
        "github_pr_rework_attention",
        {
            "repository": REPO,
            "issue_number": ISSUE_N,
            "pr_number": PR_N,
            "head_sha": pr_payload(PR_N)["head"]["sha"],
            "rework_round": 1,
            "reason": "rework_human_attention",
            "diagnostic": "completion_handoff_missing",
            "source": "github_edge_rework_reconciliation",
        },
    )


def test_5h_later_governing_transition_preserves_graph():
    print("5h. later canonical governing transition prevents stale convergence")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        with connect_closing() as conn:
            # Same-second insertion exercises the durable (created_at, id)
            # ordering used by the canonical governing-event contract.
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (
                    ids["blocked"],
                    "status",
                    json.dumps({"new_status": "ready", "source": "test"}, sort_keys=True),
                    2,
                ),
            )
            conn.commit()
        before = statuses(ids)
        events_before = event_snapshot(ids)
        results = run_sync(fake)
        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("later governing transition preserves graph", before == after,
              f"{before} -> {after}")
        check("later governing transition preserves events",
              events_before == events_after,
              f"{events_before} -> {events_after}")
        check("later governing transition fails closed",
              root_entry["changed"] is False
              and root_entry.get("reason") == "terminal_convergence_node_unconvergeable",
              str(root_entry))
        assert_no_workers(ids)


def _later_attention_ambiguity_case(name: str, payload: Any) -> None:
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        with connect_closing() as conn:
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (ids["blocked"], "github_pr_rework_attention", json.dumps(payload, sort_keys=True), 3),
            )
            conn.commit()
        before = statuses(ids)
        events_before = event_snapshot(ids)
        results = run_sync(fake)
        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(f"{name}: graph preserved", before == after, f"{before} -> {after}")
        check(f"{name}: events preserved", events_before == events_after,
              f"{events_before} -> {events_after}")
        check(f"{name}: ambiguous attention fails closed",
              root_entry["changed"] is False
              and root_entry.get("reason") == "terminal_convergence_node_unconvergeable",
              str(root_entry))
        assert_no_workers(ids)


def test_5i_later_malformed_or_mismatched_attention_preserves_graph():
    print("5i. later malformed or mismatched attention preserves graph")
    _later_attention_ambiguity_case("malformed attention", ["not-a-mapping"])
    _later_attention_ambiguity_case(
        "mismatched attention",
        {
            "repository": REPO,
            "issue_number": ISSUE_N,
            "pr_number": PR_N + 1,
            "rework_round": 1,
        },
    )


def test_5j_earlier_attention_before_newer_rework_allows_convergence():
    print("5j. earlier-round attention before newer rework remains eligible")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        with connect_closing() as conn:
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (
                    ids["blocked"],
                    "github_pr_rework_attention",
                    json.dumps({
                        "repository": REPO,
                        "issue_number": ISSUE_N,
                        "pr_number": PR_N,
                        "rework_round": 1,
                    }, sort_keys=True),
                    3,
                ),
            )
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (
                    ids["blocked"],
                    "github_pr_rework_retry",
                    json.dumps({
                        "previous_status": "blocked",
                        "new_status": "ready",
                        "repository": REPO,
                        "issue_number": ISSUE_N,
                        "pr_number": PR_N,
                        "head_sha": pr_payload(PR_N)["head"]["sha"],
                        "reason": "agent_rework",
                        "rework_round": 2,
                        "source": "github_edge_rework_recovery",
                        "merge_authority": "human",
                        "auto_merge": False,
                    }, sort_keys=True),
                    4,
                ),
            )
            conn.commit()
        results = run_sync(fake)
        after = statuses(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("earlier attention does not block newer rework",
              root_entry["changed"] is True
              and root_entry.get("reason") == "terminal_merge_convergence",
              str(root_entry))
        check("newer rework graph converges",
              after[ids["blocked"]]["status"] == "done"
              and after[ids["reviewer"]]["status"] == "archived"
              and after[ids["root"]]["status"] == "done",
              str(after))
        assert_no_workers(ids)


def _text_source_lookup_failure_case(name: str, failing_attr: str) -> None:
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        extra_pr_number = PR_N + 1
        fake = FakeGitHub(
            issue_state="closed",
            pr=pr_payload(PR_N, state="closed", merged=True),
            extra_pr=pr_payload(extra_pr_number, state="open", merged=False),
        )
        ids = build_graph()
        with connect_closing() as conn:
            handoff_url = f"https://github.com/{REPO}/pull/{extra_pr_number}"
            if failing_attr == "list_comments":
                conn.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (ids["root"], "kanban-main", f"required handoff: {handoff_url}", 3),
                )
            else:
                kanban_db._synthesize_ended_run(  # type: ignore[attr-defined]
                    conn,
                    ids["root"],
                    outcome="completed",
                    summary=f"required handoff: {handoff_url}",
                )
            conn.commit()
        before = statuses(ids)
        events_before = event_snapshot(ids)
        runs_before = run_counts()
        original = getattr(kanban_db, failing_attr)

        def fail_lookup(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"simulated {failing_attr} failure")

        setattr(kanban_db, failing_attr, fail_lookup)
        try:
            results = run_sync(fake)
        finally:
            setattr(kanban_db, failing_attr, original)

        after = statuses(ids)
        events_after = event_snapshot(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(f"{name}: graph preserved", before == after,
              f"{before} -> {after}")
        check(f"{name}: events preserved", events_before == events_after,
              f"{events_before} -> {events_after}")
        check(f"{name}: lookup failure is fail-closed",
              root_entry["changed"] is False
              and root_entry.get("reason") == "text_source_lookup_failed",
              str(root_entry))
        check(f"{name}: no worker run spawned", run_counts() == runs_before,
              f"{runs_before} -> {run_counts()}")
        for tid, row in after.items():
            check(f"{name}: no ownership for {tid}",
                  row["claim_lock"] is None
                  and row["worker_pid"] is None
                  and row["current_run_id"] is None,
                  str(row))


def test_5d_comments_lookup_failure():
    print("5d. pending comments lookup failure preserves graph and events")
    _text_source_lookup_failure_case("comments lookup failure", "list_comments")


def test_5e_runs_lookup_failure():
    print("5e. pending runs lookup failure preserves graph and events")
    _text_source_lookup_failure_case("runs lookup failure", "list_runs")


def test_6_dry_run_predicts_without_mutation():
    print("6. dry-run predicts convergence without mutating the graph")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        fake = FakeGitHub(issue_state="closed", pr=pr_payload(PR_N, state="closed", merged=True))
        ids = build_graph()
        before = statuses(ids)
        events_before = {tid: len(events_for(tid)) for tid in ids.values()}
        results = mod.sync_board("default", client=fake, dry_run=True)
        after = statuses(ids)
        events_after = {tid: len(events_for(tid)) for tid in ids.values()}
        check("dry-run: no status change", before == after, f"{before} -> {after}")
        check("dry-run: no events written", events_before == events_after)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check("dry-run: predicted convergence entry",
              root_entry.get("reason") == "terminal_merge_convergence_predicted"
              and root_entry["changed"] is False, str(root_entry))
        check("dry-run: prediction names the converged set",
              root_entry["evidence"].get("converged") is None
              and set(root_entry["evidence"].get("terminalize", [])) |
              set(root_entry["evidence"].get("archive", [])) ==
              {ids["blocked"], ids["reviewer"]}, str(root_entry.get("evidence")))
        assert_no_workers(ids)


def test_7_late_ancestor_activation_is_refused():
    print("7. late ancestor activation during fresh GitHub read is refused")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        ids = build_graph()

        def activate_ancestor() -> None:
            with connect_closing() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'running' WHERE id = ?",
                    (ids["impl"],),
                )

        fake = FakeGitHub(
            issue_state="closed",
            pr=pr_payload(PR_N, state="closed", merged=True),
            on_issue_get=activate_ancestor,
        )
        events_before = sum(len(events_for(tid)) for tid in ids.values())
        results = run_sync(fake)
        after = statuses(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(
            "late active ancestor refuses convergence",
            root_entry["changed"] is False
            and root_entry["status"] == "todo"
            and root_entry.get("reason") == "terminal_convergence_state_changed",
            str(root_entry),
        )
        check(
            "late ancestor remains active while root is preserved",
            after[ids["impl"]]["status"] == "running"
            and after[ids["root"]]["status"] == "todo",
            str(after),
        )
        check(
            "late ancestor drift writes no events",
            sum(len(events_for(tid)) for tid in ids.values()) == events_before,
        )
        assert_no_workers(ids)


def test_8_late_active_parent_is_refused():
    print("8. late active parent insertion during fresh GitHub read is refused")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        ids = build_graph()
        late_parent: dict[str, str] = {}

        def insert_active_parent() -> None:
            parent_id = _create_task("running", link_body(), "late-parent-88")
            late_parent["id"] = parent_id
            with connect_closing() as conn:
                conn.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, ids["root"]),
                )

        fake = FakeGitHub(
            issue_state="closed",
            pr=pr_payload(PR_N, state="closed", merged=True),
            on_issue_get=insert_active_parent,
        )
        events_before = sum(len(events_for(tid)) for tid in ids.values())
        results = run_sync(fake)
        after = statuses({**ids, "late_parent": late_parent["id"]})
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        parent_id = late_parent["id"]
        check(
            "late active parent refuses convergence",
            root_entry["changed"] is False
            and root_entry["status"] == "todo"
            and root_entry.get("reason") == "terminal_convergence_state_changed",
            str(root_entry),
        )
        check(
            "late parent remains active and linked",
            after[ids["root"]]["status"] == "todo"
            and after[parent_id]["status"] == "running",
            str(after),
        )
        check(
            "late parent drift writes no events",
            sum(len(events_for(tid)) for tid in ids.values()) == events_before,
        )
        assert_no_workers(ids)


def test_9_cycle_is_bounded_and_preserved():
    print("9. cyclic task links return an ambiguous-graph refusal")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        ids = build_graph()
        with connect_closing() as conn:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (ids["root"], ids["impl"]),
            )
            before_events = sum(
                conn.execute(
                    "SELECT count(*) FROM task_events WHERE task_id = ?", (tid,)
                ).fetchone()[0]
                for tid in ids.values()
            )
            nodes, error = mod._terminal_chain_ancestors(conn, ids["root"])
        check(
            "cycle returns no ancestor closure",
            nodes is None and bool(error),
            f"nodes={nodes!r} error={error!r}",
        )
        check(
            "cycle leaves graph events unchanged",
            sum(len(events_for(tid)) for tid in ids.values()) == before_events,
        )
        fake = FakeGitHub(
            issue_state="closed",
            pr=pr_payload(PR_N, state="closed", merged=True),
        )
        results = run_sync(fake)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(
            "cycle sync fails closed",
            root_entry["changed"] is False
            and root_entry.get("reason") == "terminal_convergence_ambiguous_graph",
            str(root_entry),
        )
        assert_no_workers(ids)


def test_10_diamond_shared_ancestor_converges():
    print("10. diamond graph with a shared ancestor still converges")
    with isolated_test_environment():
        _prepare_isolated_environment()
        init_db()
        ids = build_graph()
        second_reviewer = _create_task(
            "todo",
            link_body(),
            "reviewer-88-second",
            assignee="kanban-reviewer",
            parents=[ids["blocked"]],
        )
        with connect_closing() as conn:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (second_reviewer, ids["root"]),
            )
        ids["second_reviewer"] = second_reviewer
        fake = FakeGitHub(
            issue_state="closed",
            pr=pr_payload(PR_N, state="closed", merged=True),
        )
        results = run_sync(fake)
        after = statuses(ids)
        root_entry = next(r for r in results if r["task_id"] == ids["root"])
        check(
            "diamond root converges",
            root_entry["changed"] is True
            and root_entry.get("reason") == "terminal_merge_convergence",
            str(root_entry),
        )
        check(
            "diamond shared blocked ancestor terminalized once",
            after[ids["blocked"]]["status"] == "done"
            and after[ids["reviewer"]]["status"] == "archived"
            and after[second_reviewer]["status"] == "archived",
            str(after),
        )
        assert_no_workers(ids)


def main() -> int:
    tests = [
        test_1_qualifying_convergence,
        test_2_idempotent_repeat,
        test_3_fail_closed,
        test_4_active_ownership,
        test_5_ambiguous_ancestor,
        test_5b_unrelated_allowed_status_ancestors,
        test_5c_missing_rework_provenance,
        test_5f_later_human_block_preserves_graph,
        test_5g_later_current_round_attention_preserves_graph,
        test_5h_later_governing_transition_preserves_graph,
        test_5i_later_malformed_or_mismatched_attention_preserves_graph,
        test_5j_earlier_attention_before_newer_rework_allows_convergence,
        test_5d_comments_lookup_failure,
        test_5e_runs_lookup_failure,
        test_6_dry_run_predicts_without_mutation,
        test_7_late_ancestor_activation_is_refused,
        test_8_late_active_parent_is_refused,
        test_9_cycle_is_bounded_and_preserved,
        test_10_diamond_shared_ancestor_converges,
    ]
    for t in tests:
        t()
    print()
    print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print("ALL TERMINAL-CONVERGENCE REGRESSIONS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
