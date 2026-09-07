#!/usr/bin/env python3
"""Isolated tests for the edge workspace self-healing pass (Issue #76).

No Hermes install required: a temporary board DB exercises deterministic
rebinding, anchor resolution, fail-closed behavior, and idempotence.

Round-2 (PR #77 rework) regressions included:
  * CAS rowcount honored: a two-connection interleaving that claims a row
    between scan and UPDATE yields changed=False, NO workspace_repaired
    event, and untouched claimed rows/event tables;
  * dry-run preview is strictly read-only: predicted entries, unchanged DB,
    no spawn;
  * durable guard/quarantine cards are excluded from repair while ordinary
    blocked implementation cards still get repaired;
  * created_at on workspace_repaired events is epoch INTEGER, and a
    list_events-style ordering consumer reads it correctly.
"""
from __future__ import annotations

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
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            ended_at INTEGER,
            outcome TEXT
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            author TEXT,
            body TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    return conn


def insert(conn, tid, title, status="ready", kind="worktree", path="/ws/projects/re-bound",
           branch="", key=None, claim=None, body="body"):
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, title, body, status, "kanban-developer", kind, path, branch, claim, key),
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="selfheal-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # fake repo root

    conn = make_db(tmp / "board.db")

    # Use the existing real sibling checkout as the anchor target.
    real_repo = Path("/ws/projects/hermes-n8n-control-plane")
    assert (real_repo / ".git").exists()

    # 1) Incident shape: implementation card bound to shared checkout with
    #    GitHub provenance key -> repairable via /ws/projects/<repo> anchor.
    insert(conn, "t_drift1", "구현: Issue #110 기능",
           key="github:rhgo1749/hermes-n8n-control-plane:issue:110")
    # 2) Drifted card WITHOUT resolvable anchor -> fail closed, report only.
    insert(conn, "t_noanchor", "REWORK round 2 fix", path=str(tmp / "nowhere"), key=None)
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
    ev = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id='t_drift1' "
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

    print("\n-- round-2 regressions --")

    # --- R2: guard/quarantine cards excluded from self-heal ---
    insert(conn, "t_guard", "SAFETY GUARD: 잘못된 공유 workspace 구현 카드 실행 금지",
           status="blocked", body="SAFETY GUARD ONLY — 실행/구현 금지.")
    out_g = admission.repair_workspace_drift(conn, None, "test-board")
    g_entries = [e for e in out_g if e["task_id"] == "t_guard"]
    check("guard card NOT repaired", not any(
        e.get("reason") == "workspace_selfhealed" for e in g_entries), str(g_entries))
    grow = conn.execute("SELECT workspace_kind, workspace_path FROM tasks "
                        "WHERE id='t_guard'").fetchone()
    check("guard binding preserved verbatim",
          grow["workspace_kind"] == "worktree"
          and grow["workspace_path"] == "/ws/projects/re-bound", str(dict(grow)))
    gev = conn.execute("SELECT COUNT(*) c FROM task_events WHERE task_id='t_guard'").fetchone()
    check("guard card has zero events", gev["c"] == 0)
    # Ordinary blocked implementation card IS repaired (control).
    insert(conn, "t_ordblocked", "IMPLEMENT ordinary blocked fix", status="blocked",
           key="github:rhgo1749/hermes-n8n-control-plane:issue:111")
    out_ob = admission.repair_workspace_drift(conn, None, "test-board")
    ob = {e["task_id"]: e for e in out_ob}.get("t_ordblocked", {})
    check("ordinary blocked impl card still repaired",
          ob.get("reason") == "workspace_selfhealed" and ob.get("changed") is True, str(ob))

    # --- R2: active-claim race honored via UPDATE rowcount ---
    db_race = tmp / "race.db"
    rc = make_db(db_race)
    insert(rc, "t_race", "REWORK race fix",
           key="github:rhgo1749/hermes-n8n-control-plane:issue:112")
    rc.commit()

    # Deterministic two-connection interleaving: A's repair pass scans and
    # sees an unclaimed drifted row; B claims the row AFTER the scan but
    # BEFORE A's CAS UPDATE (hooked into anchor resolution, the first write-
    # path read after the scan). A's UPDATE then matches zero rows.
    scan_conn = sqlite3.connect(str(db_race))
    scan_conn.row_factory = sqlite3.Row  # A's view (same file, fresh handle)
    other = sqlite3.connect(str(db_race))  # B: the competing writer
    other.row_factory = sqlite3.Row

    real_anchor = admission._repo_anchor_for_task

    def hooked_anchor(c, row):
        if not hooked_anchor.done:
            other.execute("UPDATE tasks SET status='running', "
                          "claim_lock='new-claim' WHERE id='t_race'")
            other.commit()
            hooked_anchor.done = True
        return real_anchor(c, row)

    hooked_anchor.done = False
    admission._repo_anchor_for_task = hooked_anchor
    try:
        before_row = dict(scan_conn.execute(
            "SELECT status, claim_lock, workspace_kind, workspace_path FROM tasks "
            "WHERE id='t_race'").fetchone())
        assert before_row["claim_lock"] is None  # scan saw an unclaimed row
        out_race = admission.repair_workspace_drift(scan_conn, None, "test-board")
    finally:
        admission._repo_anchor_for_task = real_anchor
    race_entry = {e["task_id"]: e for e in out_race}.get("t_race", {})
    check("race-lost row skipped with changed=False",
          race_entry.get("reason") == "selfheal_skipped_active_claim"
          and race_entry.get("changed") is False, str(race_entry))
    after_row = dict(other.execute(
        "SELECT status, claim_lock, workspace_kind, workspace_path FROM tasks "
        "WHERE id='t_race'").fetchone())
    check("claimed row untouched by repair pass",
          after_row["workspace_kind"] == before_row["workspace_kind"]
          and after_row["workspace_path"] == before_row["workspace_path"],
          f"{before_row} -> {after_row}")
    check("claim survives (B's state authoritative)",
          after_row["status"] == "running"
          and after_row["claim_lock"] == "new-claim", str(after_row))
    race_ev = other.execute(
        "SELECT COUNT(*) c FROM task_events WHERE task_id='t_race'").fetchone()
    check("no workspace_repaired event for claimed row", race_ev["c"] == 0)
    other.close()
    scan_conn.close()

    # --- R2: dry-run preview is strictly read-only ---
    db_prev = tmp / "preview.db"
    pc = make_db(db_prev)
    insert(pc, "t_pred1", "구현: Issue #113 예측",
           key="github:rhgo1749/hermes-n8n-control-plane:issue:113")
    insert(pc, "t_pred_na", "IMPLEMENT no anchor here", path=str(tmp / "void"))
    snapshot_tasks = pc.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    snapshot_events = pc.execute("SELECT * FROM task_events ORDER BY id").fetchall()
    prev = admission.preview_workspace_drift(pc, "test-board")
    p_by_id = {e["task_id"]: e for e in prev}
    p1 = p_by_id.get("t_pred1", {})
    check("preview predicts repair entry first-class",
          p1.get("reason") == "workspace_selfhealed" and p1.get("predicted") is True
          and p1.get("would_change") is True and p1.get("changed") is False, str(p1))
    check("preview carries from/to prediction",
          p1.get("from", {}).get("path") == "/ws/projects/re-bound"
          and p1.get("to", {}).get("path", "").endswith(".worktrees/t_pred1"), str(p1))
    pna = p_by_id.get("t_pred_na", {})
    check("preview predicts fail-closed anchor report",
          pna.get("reason") == "selfheal_anchor_unresolved"
          and pna.get("predicted") is True, str(pna))
    after_tasks = pc.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    after_events = pc.execute("SELECT * FROM task_events ORDER BY id").fetchall()
    check("preview left task rows byte-identical",
          [tuple(r) for r in snapshot_tasks] == [tuple(r) for r in after_tasks])
    check("preview wrote zero events", len(after_events) == 0 and len(snapshot_events) == 0)

    # --- R2: INTEGER timestamps + list_events/ordering consumer ---
    ts_row = conn.execute(
        "SELECT created_at, typeof(created_at) t FROM task_events "
        "WHERE kind='workspace_repaired' LIMIT 1").fetchone()
    check("workspace_repaired created_at is epoch INTEGER",
          ts_row is not None and ts_row["t"] == "integer"
          and isinstance(ts_row["created_at"], int),
          str(dict(ts_row) if ts_row else None))
    ordered = conn.execute(
        "SELECT created_at, id FROM task_events ORDER BY created_at ASC, id ASC"
    ).fetchall()
    vals = [(r["created_at"], r["id"]) for r in ordered]
    check("ordering consumer sees ascending (created_at, id)", vals == sorted(vals))

    # --- active_pr comment drift self-healing regressions ---
    db_c = tmp / "comments.db"
    cc = make_db(db_c)
    # 1. Bounded rework task with raw PR URL in comment (incident shape)
    insert(cc, "t_rework_c", "Issue #104 bounded rework: incomplete rework identity on PR #129",
           body="Bounded rework of existing PR #129 only")
    cc.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES ('t_rework_c', 'kanban-main', "
        "'Source Issue: #104. Existing PR #129: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129.', 100)"
    )
    # 2. Unrun implementation task with PR URL in contract comment
    insert(cc, "t_unrun_c", "구현: Issue #105 신규 기능")
    cc.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES ('t_unrun_c', 'kanban-main', "
        "'Spec reference: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/125', 100)"
    )
    # 3. Intake root card with PR URL (must NOT be touched)
    insert(cc, "t_intake_c", "GitHub Issue intake: rhgo1749/hermes-n8n-control-plane#104")
    cc.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES ('t_intake_c', 'github-issue-intake', "
        "'PR: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129', 100)"
    )
    # 4. Impl card that actually ran and opened a PR (must NOT be touched - genuine active_pr)
    insert(cc, "t_ran_c", "구현: Issue #106 일반 기능")
    cc.execute("INSERT INTO task_runs (task_id, ended_at, outcome) VALUES ('t_ran_c', 1000, 'completed')")
    cc.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES ('t_ran_c', 'kanban-developer', "
        "'Opened PR: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129', 100)"
    )

    # Dry-run preview observation
    prev_c = admission.preview_workspace_drift(cc, "test-board")
    prev_c_map = {e.get("task_id"): e for e in prev_c if e.get("reason") == "comment_active_pr_selfhealed"}
    check("preview predicts rework comment selfheal", "t_rework_c" in prev_c_map and prev_c_map["t_rework_c"]["predicted"] is True)
    check("preview predicts unrun impl comment selfheal", "t_unrun_c" in prev_c_map)
    check("preview never touches intake root", "t_intake_c" not in prev_c_map)
    check("preview never touches ran impl task", "t_ran_c" not in prev_c_map)

    # Real repair run
    repaired_c = admission.repair_workspace_drift(cc, None, "test-board")
    rep_c_map = {e.get("task_id"): e for e in repaired_c if e.get("reason") == "comment_active_pr_selfhealed"}
    check("rework task comment repaired", "t_rework_c" in rep_c_map and rep_c_map["t_rework_c"]["changed"] is True)
    check("unrun task comment repaired", "t_unrun_c" in rep_c_map and rep_c_map["t_unrun_c"]["changed"] is True)

    # Verify sanitized content
    row_rework = cc.execute("SELECT body FROM task_comments WHERE task_id = 't_rework_c'").fetchone()
    check("rework comment sanitized to owner/repo#pr", "rhgo1749/hermes-n8n-control-plane#129" in row_rework["body"] and "https://github.com" not in row_rework["body"])
    row_unrun = cc.execute("SELECT body FROM task_comments WHERE task_id = 't_unrun_c'").fetchone()
    check("unrun comment sanitized to owner/repo#pr", "rhgo1749/hermes-n8n-control-plane#125" in row_unrun["body"])

    # Verify intake and ran cards were NOT modified
    row_intake = cc.execute("SELECT body FROM task_comments WHERE task_id = 't_intake_c'").fetchone()
    check("intake comment kept raw PR URL", "https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129" in row_intake["body"])
    row_ran = cc.execute("SELECT body FROM task_comments WHERE task_id = 't_ran_c'").fetchone()
    check("ran impl comment kept raw PR URL", "https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129" in row_ran["body"])

    # Verify audit event
    ev_c = cc.execute("SELECT kind, payload FROM task_events WHERE task_id = 't_rework_c' AND kind = 'comment_repaired'").fetchone()
    check("comment_repaired audit event recorded", ev_c is not None and "active_pr_comment_selfheal" in ev_c["payload"])

    # Idempotence: second repair run produces 0 comment repairs
    repaired_c2 = admission.repair_workspace_drift(cc, None, "test-board")
    rep_c_map2 = {e.get("task_id"): e for e in repaired_c2 if e.get("reason") == "comment_active_pr_selfhealed"}
    check("second comment repair pass is idempotent", len(rep_c_map2) == 0)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
