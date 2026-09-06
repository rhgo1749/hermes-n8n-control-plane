#!/usr/bin/env python3
"""Regression tests for specialist-graph rework delivery provenance."""
from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

from kanban_retry_signal_guard import install_rework_delivery_provenance_guard

HEAD = "7d575644fef2ed40a6473e75a9c03ab2a991673a"
TASK = "t_ac34f08d"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT,
            assignee TEXT,
            title TEXT,
            completed_at INTEGER
        );
        CREATE TABLE task_links (
            parent_id TEXT,
            child_id TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            status TEXT,
            outcome TEXT,
            summary TEXT,
            error TEXT,
            metadata TEXT,
            started_at INTEGER,
            ended_at INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            run_id INTEGER,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        );
        """
    )

    tasks = [
        (TASK, "done", "kanban-main", "Issue #100 lead", 190),
        ("old-review", "done", "kanban-reviewer", "R1 reviewer", 50),
        ("r2-review", "done", "kanban-reviewer", "R2 reviewer", 150),
        ("r4-review", "done", "kanban-reviewer", "R4 reviewer", 180),
        ("current-developer", "done", "kanban-developer", "Current developer", 160),
    ]
    conn.executemany(
        "INSERT INTO tasks(id,status,assignee,title,completed_at) VALUES(?,?,?,?,?)",
        tasks,
    )
    conn.executemany(
        "INSERT INTO task_links(parent_id,child_id) VALUES(?,?)",
        [
            ("old-review", TASK),
            ("r2-review", TASK),
            ("r4-review", TASK),
            ("current-developer", "r4-review"),
        ],
    )

    runs = [
        (568, TASK, "blocked", "blocked", f"R2 dependency wait head {HEAD}", None, None, 101, 120),
        (577, TASK, "done", "completed", "terminal provisional handoff", None, json.dumps({
            "validation": {"result": "passed", "head_sha": HEAD},
            "handoff": {"head_sha": HEAD},
        }), 181, 190),
        (700, "old-review", "done", "completed", f"PASS exact head {HEAD}", None, None, 40, 50),
        (701, "r2-review", "done", "completed", f"REWORK exact head {HEAD}", None, None, 140, 150),
        (702, "r4-review", "done", "completed", f"independent PASS exact head {HEAD}", None, None, 170, 180),
        (703, "current-developer", "done", "completed", "developer validation passed", None, json.dumps({
            "validation": "passed",
            "head_sha": HEAD,
        }), 120, 130),
    ]
    conn.executemany(
        "INSERT INTO task_runs(id,task_id,status,outcome,summary,error,metadata,started_at,ended_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        runs,
    )

    origin = {
        "source": "github",
        "repository": "rhgo1749/ctrl-hangul",
        "issue_number": 100,
        "pr_number": 103,
        "rework_round": 1,
        "head_sha": HEAD,
        "request_comment_id": None,
    }
    retry = {
        **origin,
        "source": "github_edge_rework_recovery",
        "retry": True,
        "retry_reason": "rework_head_unchanged",
    }
    dispatch = {
        "source": "github_edge_rework_dispatch",
        "phase": "claimed",
        "task_id": TASK,
        "run_id": 568,
        "rework_event_at": 100,
        "rework_round": 1,
        "head_sha": HEAD,
    }
    conn.executemany(
        "INSERT INTO task_events(id,task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?,?)",
        [
            (1, TASK, None, "github_pr_rework", json.dumps(origin), 100),
            (2, TASK, 568, "github_pr_rework_dispatch", json.dumps(dispatch), 101),
            (3, TASK, None, "github_pr_rework_retry", json.dumps(retry), 200),
        ],
    )
    return conn


def make_core(*, canonical_returns_root=False):
    core = SimpleNamespace(
        REWORK_DISPATCH_PROVENANCE_KIND="github_pr_rework_dispatch",
        REWORK_DISPATCH_PROVENANCE_SOURCE="github_edge_rework_dispatch",
    )

    def head_candidates(run):
        text = "\n".join(
            str(run[key] or "") for key in ("summary", "error", "metadata")
        )
        return {item.casefold() for item in re.findall(r"\b[0-9a-fA-F]{40}\b", text)}

    def original_task_run_after_rework(conn, task_id, rework_at, *, rework_round=None):
        # Canonical strict reader may see the edge-provenanced bootstrap first
        # or its completed root provisional fallback.
        if task_id != TASK or rework_at != 100 or rework_round != 1:
            return None
        run_id = 577 if canonical_returns_root else 568
        return conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()

    observed_events = []

    def original_rework_delivery_evidence(conn, client, ref, task_id, pr, event):
        observed_events.append(event)
        payload, rework_at, _kind = event
        run = core._task_run_after_rework(
            conn,
            task_id,
            rework_at,
            rework_round=payload.get("rework_round"),
        )
        if run is None or run["ended_at"] is None:
            return False, "delivery_run_missing", {"rework_at": rework_at}
        # The trusted completion marker/head checks have already succeeded by
        # the time canonical code reaches this diagnostic.  The head is the
        # same because this is a verification-only specialist round.
        return False, "rework_head_unchanged", {
            "requested_head": payload["head_sha"],
            "head": payload["head_sha"],
            "run_id": run["id"],
        }

    core._rework_head_candidates = head_candidates
    core._task_run_after_rework = original_task_run_after_rework
    core._rework_delivery_evidence = original_rework_delivery_evidence
    core._observed_delivery_events = observed_events
    install_rework_delivery_provenance_guard(core)
    return core


def retry_event(conn):
    row = conn.execute(
        "SELECT payload, created_at, kind FROM task_events "
        "WHERE kind='github_pr_rework_retry'"
    ).fetchone()
    return json.loads(row["payload"]), row["created_at"], row["kind"]


def call_delivery(core, conn):
    return core._rework_delivery_evidence(
        conn,
        object(),
        SimpleNamespace(repository="rhgo1749/ctrl-hangul", issue_number=100),
        TASK,
        SimpleNamespace(number=103, head_sha=HEAD),
        retry_event(conn),
    )


def test_same_round_retry_keeps_original_delivery_origin_and_specialist_run():
    conn = make_db()
    core = make_core()

    delivered, reason, evidence = call_delivery(core, conn)

    assert delivered is True
    assert reason == "delivery_complete_verification_only"
    assert evidence["run_id"] == 703
    assert evidence["developer_task_id"] == "current-developer"
    assert evidence["developer_run_id"] == 703
    assert evidence["lead_run_id"] == 577
    assert evidence["reviewer_task_id"] == "r4-review"
    assert evidence["reviewer_run_id"] == 702
    assert evidence["provenance"] == "specialist_reviewer_pass_same_head"

    # The canonical delivery evaluator must see the immutable original round,
    # not the internal retry timestamp 200.
    seen_payload, seen_at, seen_kind = core._observed_delivery_events[-1]
    assert seen_at == 100
    assert seen_kind == "github_pr_rework"
    assert seen_payload["rework_round"] == 1

    # The pre-round append-only parent is intentionally still attached; it
    # must not prevent the newest current-round reviewer PASS from binding.
    assert conn.execute(
        "SELECT COUNT(*) FROM task_links WHERE child_id = ?", (TASK,)
    ).fetchone()[0] == 3


def test_current_specialists_can_advance_beyond_origin_head():
    conn = make_db()
    new_head = "8" * 40
    conn.execute(
        "UPDATE task_runs SET summary = ? WHERE id = 702",
        (f"PASS exact head {new_head}",),
    )
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = 703",
        (json.dumps({"validation": "passed", "head_sha": new_head}),),
    )
    core = make_core()

    run, evidence = core._rework_specialist_delivery_candidate(
        conn, TASK, 100, 1, HEAD
    )

    assert run["id"] == 703
    assert evidence["head"] == new_head
    assert evidence["reviewer_run_id"] == 702


def test_completed_root_provisional_run_does_not_mask_developer_attestation():
    conn = make_db()
    core = make_core(canonical_returns_root=True)

    run = core._task_run_after_rework(conn, TASK, 100, rework_round=1)

    assert run["id"] == 703
    assert json.loads(run["metadata"])["validation"] == "passed"


def test_missing_developer_validation_never_uses_root_provisional_run():
    conn = make_db()
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = 703",
        (json.dumps({"validation": "failed", "head_sha": HEAD}),),
    )
    core = make_core()

    delivered, reason, _evidence = call_delivery(core, conn)

    assert delivered is False
    assert reason == "rework_head_unchanged"


def test_missing_edge_bootstrap_never_inherits_ordinary_completed_run():
    conn = make_db()
    conn.execute("DELETE FROM task_events WHERE kind='github_pr_rework_dispatch'")
    core = make_core()

    delivered, reason, _evidence = call_delivery(core, conn)

    assert delivered is False
    assert reason == "rework_head_unchanged"


def test_latest_current_round_reviewer_must_pass_exact_head():
    conn = make_db()
    conn.execute(
        "UPDATE task_runs SET summary = ? WHERE id = 702",
        (f"REWORK exact head {HEAD}",),
    )
    core = make_core()

    delivered, reason, _evidence = call_delivery(core, conn)

    assert delivered is False
    assert reason == "rework_head_unchanged"


def test_retry_identity_mismatch_does_not_rebind_to_old_round():
    conn = make_db()
    row = conn.execute(
        "SELECT id, payload FROM task_events WHERE kind='github_pr_rework_retry'"
    ).fetchone()
    payload = json.loads(row["payload"])
    payload["head_sha"] = "a" * 40
    conn.execute(
        "UPDATE task_events SET payload=? WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    core = make_core()

    delivered, reason, evidence = call_delivery(core, conn)

    assert delivered is False
    assert reason == "delivery_run_missing"
    assert evidence["rework_at"] == 200


def test_install_is_idempotent():
    core = make_core()
    first_task_reader = core._task_run_after_rework
    first_delivery = core._rework_delivery_evidence
    install_rework_delivery_provenance_guard(core)
    assert core._task_run_after_rework is first_task_reader
    assert core._rework_delivery_evidence is first_delivery


if __name__ == "__main__":
    tests = [
        test_same_round_retry_keeps_original_delivery_origin_and_specialist_run,
        test_current_specialists_can_advance_beyond_origin_head,
        test_completed_root_provisional_run_does_not_mask_developer_attestation,
        test_missing_developer_validation_never_uses_root_provisional_run,
        test_missing_edge_bootstrap_never_inherits_ordinary_completed_run,
        test_latest_current_round_reviewer_must_pass_exact_head,
        test_retry_identity_mismatch_does_not_rebind_to_old_round,
        test_install_is_idempotent,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
