#!/usr/bin/env python3
"""Isolated tests for the edge workspace self-healing pass (Issue #76).

No Hermes install required: a temporary board DB exercises deterministic
rebinding, anchor resolution, fail-closed behavior, and idempotence.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

import kanban_workspace_admission as admission


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            workspace_kind TEXT,
            workspace_path TEXT,
            branch_name TEXT,
            claim_lock TEXT,
            idempotency_key TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            run_id INTEGER,
            kind TEXT,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    return conn


def insert(conn, tid, title, status="ready", kind="worktree", path="/ws/projects/re-bound",
           branch="", key=None, claim=None):
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, title, "body", status, "kanban-developer", kind, path, branch, claim, key),
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="selfheal-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # fake repo root

    conn = make_db(tmp / "board.db")

    # 1) Incident shape: implementation card bound to shared checkout with
    #    GitHub provenance key -> repairable via /ws/projects/<repo> anchor.
    #    Use the existing real sibling checkout as the anchor target.
    real_repo = Path("/ws/projects/hermes-n8n-control-plane")
    assert (real_repo / ".git").exists()
    insert(conn, "t_drift1", "구현: Issue #110 기능",
           key="github:rhgo1749/hermes-n8n-control-plane:issue:110")
    # 2) Drifted card WITHOUT resolvable anchor -> fail closed, report only.
    insert(conn, "t_noanchor", "REWORK round 2 fix",
           path=str(tmp / "nowhere"), key=None)
    # 3) Review card in shared dir -> exempt.
    insert(conn, "t_review", "TECH REVIEW: 검증")
    conn.execute("UPDATE tasks SET title='TECH REVIEW: 검증', workspace_kind='dir',"
                 "workspace_path='/tmp/rev-ws' WHERE id='t_review'")
    # 4) Running card with drift -> must NOT be touched.
    insert(conn, "t_running", "IMPLEMENT feature", status="running")
    # 5) Claimed ready card -> must NOT be touched.
    insert(conn, "t_claimed", "REWORK fix", claim="lock-1")

    out = admission.repair_workspace_drift(conn, None, "test-board")

    by_id = {e["task_id"]: e for e in out}
    r1 = by_id.get("t_drift1", {})
    check("drifted implementation card repaired", r1.get("reason") == "workspace_selfhealed",
          str(r1))
    row = conn.execute("SELECT * FROM tasks WHERE id='t_drift1'").fetchone()
    check("rebound to .worktrees/<id>", row["workspace_path"].endswith(".worktrees/t_drift1"),
          row["workspace_path"])
    check("branch set wt/<id>", row["branch_name"] == "wt/t_drift1")
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id='t_drift1' "
                      "AND kind='workspace_repaired'").fetchall()
    check("audit event with before/after", len(ev) == 1 and "before" in ev[0]["payload"])
    payload = json.loads(ev[0]["payload"])
    check("event carries violation+anchor",
          bool(payload["violation"]) and payload["anchor"].endswith("hermes-n8n-control-plane"))

    na = by_id.get("t_noanchor", {})
    check("unresolvable anchor fails closed",
          na.get("reason") == "selfheal_anchor_unresolved" and not na.get("changed"), str(na))
    row = conn.execute("SELECT workspace_path FROM tasks WHERE id='t_noanchor'").fetchone()
    check("fail-closed leaves binding untouched", row["workspace_path"] == str(tmp / "nowhere"))

    check("review card exempt", "t_review" not in by_id)
    check("running untouched", "t_running" not in by_id)
    check("claimed untouched", "t_claimed" not in by_id)

    # Idempotence: second pass repairs nothing new.
    out2 = admission.repair_workspace_drift(conn, None, "test-board")
    check("second pass has no new repairs",
          all(e["reason"] != "workspace_selfhealed" for e in out2), str(out2))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
