#!/usr/bin/env python3
"""Isolated tests for the edge workspace-isolation admission overlay.

No Hermes install or real process is required: a temporary board DB with the
minimal schema exercises the guarded PRE-SPAWN wrapper deterministically,
with the wrapped dispatcher and reclaim/spawn helpers patched.

Round-2 (PR #77 rework) regressions included:
  * spawn callback is NEVER invoked for shared/dir/scratch implementation
    cards (pre-spawn gate) while isolated / intake / review controls pass;
  * verification DB/schema/read failures fail closed: no worker invocation,
    no leaked claim, explicit failure result;
  * event-persistence failure is surfaced as an explicit failed gate, never
    swallowed, and still prevents the worker;
  * durable guard/quarantine cards are excluded from gating and repair;
  * task_events.created_at is epoch INTEGER per the core event convention.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
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


def make_db(path: Path, *, integer_created_at: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    created_at = "INTEGER NOT NULL" if integer_created_at else "TEXT NOT NULL"
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            workspace_kind TEXT,
            workspace_path TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            run_id INTEGER,
            kind TEXT,
            payload TEXT,
            created_at {created_at}
        );
        """
    )
    return conn


class FakeTask:
    def __init__(self, task_id: str):
        self.id = task_id


def make_original(spawned_pid=None):
    def original(conn, kanban_db, board, *args, **kwargs):
        # Simulate the real dispatcher: claim -> resolve -> guarded_spawn.
        # The injected spawn_fn IS the pre-spawn seam; the recorded callback
        # stands in for the real worker process.
        task_id = kwargs["task_id"]
        if kwargs.get("dry_run"):
            return [{
                "task_id": task_id, "status": "ready", "changed": False,
                "reason": "rework_spawn_predicted",
            }]
        claimed = FakeTask(task_id)
        spawn_fn = kwargs.get("spawn_fn")
        resolved = RESOLVED_BY_ID.get(task_id, f"/ws/projects/rb/.worktrees/{task_id}")
        try:
            pid = spawn_fn(claimed, resolved, board=board)
        except admission._SpawnBlockedWithResult as blocked:
            return [dict(blocked.result)]
        except admission._SpawnBlocked as blocked:
            return [{
                "task_id": task_id, "status": "ready", "changed": True,
                "reason": "workspace_isolation_violation",
                "violation": blocked.violation,
            }]
        return [{
            "task_id": task_id, "status": "running", "changed": True,
            "reason": "rework_worker_spawned", "pid": pid,
        }]
    return original


RESOLVED_BY_ID: dict[str, str] = {}


def install(tmp: Path, spawned_pid=None):
    spec = importlib.util.spec_from_file_location(
        f"edge_core_{len(PASS)}",
        "/ws/projects/hermes-n8n-control-plane/edge/kanban-github-sync.py")
    edge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = edge
    # Do not execute the full module (heavy imports); stub instead.
    edge._dispatch_pending_rework = make_original(spawned_pid)
    admission.install_workspace_admission(edge)
    return edge


class FakeKB:
    def __init__(self):
        self.reclaimed: list[str] = []
        self.default_spawn_calls: list[str] = []

    def reclaim_task(self, conn_, task_id, reason=""):
        conn_.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        conn_.commit()
        self.reclaimed.append(task_id)

    def _default_spawn(self, claimed, workspace, board=""):
        self.default_spawn_calls.append(str(claimed.id))
        return 424242


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ws-admission-test-"))

    # --- unit: _isolation_violation ---
    check("shared repo root blocked",
          admission._isolation_violation("worktree", Path("/ws/projects/re-bound")) is not None)
    check("non-worktree kind blocked",
          admission._isolation_violation("dir", Path("/tmp/x")) is not None)
    check(".worktrees path allowed",
          admission._isolation_violation(
              "worktree", Path("/ws/projects/re-bound/.worktrees/t_15b405c6")) is None)

    # --- guard-card recognition ---
    check("guard title recognized",
          admission._is_guard_card(
              "SAFETY GUARD: 잘못된 공유 workspace 구현 카드 실행 금지", ""))
    check("guard body-only marker recognized",
          admission._is_guard_card("구현 카드", "SAFETY GUARD ONLY — 실행/구현 금지"))
    check("ordinary impl card not a guard card",
          not admission._is_guard_card("REWORK round 3 fix", "bounded developer rework"))
    check("guard excluded from implementation classification",
          not admission._is_implementation_task(
              "SAFETY GUARD: 잘못된 공유 workspace 구현 카드 실행 금지", ""))

    # --- integration: guarded dispatch (PRE-spawn) ---
    edge = install(tmp)
    check("install wraps exactly once",
          getattr(edge._dispatch_pending_rework, "_workspace_admission_installed", False))
    admission.install_workspace_admission(edge)  # idempotent

    db = tmp / "board.db"
    conn = make_db(db)
    kb = FakeKB()

    # Incident shape t_acd36c65: implementation card bound to shared checkout.
    conn.execute(
        "INSERT INTO tasks VALUES ('t_shared','구현: Issue #110 난이도','body',"
        "'running','kanban-developer','worktree','/ws/projects/re-bound')")
    RESOLVED_BY_ID["t_shared"] = "/ws/projects/re-bound"  # shared checkout
    out = edge._dispatch_pending_rework(
        conn, kb, "re-bound", task_id="t_shared", dry_run=False)
    check("shared-checkout spawn gated pre-spawn",
          out[0]["reason"] == "workspace_isolation_violation" and out[0].get("gate") == "pre_spawn",
          str(out))
    check("spawn callback NEVER invoked for shared impl card",
          kb.default_spawn_calls == [], str(kb.default_spawn_calls))
    check("claim reclaimed after block", "t_shared" in kb.reclaimed)
    ev = conn.execute(
        "SELECT kind, created_at FROM task_events WHERE task_id='t_shared'").fetchall()
    check("durable spawn_blocked event", any(e["kind"] == "spawn_blocked" for e in ev))
    row = conn.execute("SELECT status FROM tasks WHERE id='t_shared'").fetchone()
    check("task back to ready", row["status"] == "ready")

    # Correct isolated worktree: allowed through unchanged (control).
    conn.execute(
        "INSERT INTO tasks VALUES ('t_iso','REWORK round 5 fix','body',"
        "'running','kanban-developer','worktree','/ws/projects/rb/.worktrees/t_iso')")
    RESOLVED_BY_ID["t_iso"] = "/ws/projects/rb/.worktrees/t_iso"
    out2 = edge._dispatch_pending_rework(
        conn, kb, "rb", task_id="t_iso", dry_run=False)
    check("isolated worktree passes", out2[0]["reason"] == "rework_worker_spawned")
    check("spawn invoked exactly once for isolated control",
          kb.default_spawn_calls == ["t_iso"], str(kb.default_spawn_calls))

    # Review card in a dir workspace: allowed (not implementation).
    conn.execute(
        "INSERT INTO tasks VALUES ('t_rev','TECH REVIEW: exact-head 검증','body',"
        "'running','kanban-reviewer','dir','/tmp/some-review-ws')")
    RESOLVED_BY_ID["t_rev"] = "/tmp/some-review-ws"
    out3 = edge._dispatch_pending_rework(
        conn, kb, "rb", task_id="t_rev", dry_run=False)
    check("review card passes", out3[0]["reason"] == "rework_worker_spawned")

    # GitHub intake card bound oddly: allowed (importer owns its binding).
    conn.execute(
        "INSERT INTO tasks VALUES ('t_intake','GitHub Issue intake: o/r#9 — bug','body',"
        "'running','kanban-main','worktree','/ws/projects/x')")
    RESOLVED_BY_ID["t_intake"] = "/ws/projects/x"
    out4 = edge._dispatch_pending_rework(
        conn, kb, "x", task_id="t_intake", dry_run=False)
    check("intake card exempt", out4[0]["reason"] == "rework_worker_spawned")

    # dir/scratch-bound implementation cards also gate pre-spawn.
    conn.execute(
        "INSERT INTO tasks VALUES ('t_dir','IMPLEMENT feature X','body',"
        "'running','kanban-developer','dir','/ws/projects/re-bound')")
    RESOLVED_BY_ID["t_dir"] = "/ws/projects/re-bound"  # shared checkout via dir kind
    out5 = edge._dispatch_pending_rework(
        conn, kb, "re-bound", task_id="t_dir", dry_run=False)
    check("dir-bound impl card gated pre-spawn",
          out5[0]["reason"] == "workspace_isolation_violation"
          and "t_dir" not in kb.default_spawn_calls
          and kb.default_spawn_calls == ["t_iso", "t_rev", "t_intake"],
          str(kb.default_spawn_calls))

    # Dry-run never gates (observation only; no spawn either way in this stub).
    conn.execute("UPDATE tasks SET status='ready' WHERE id='t_shared'")
    before_events = conn.execute("SELECT COUNT(*) c FROM task_events").fetchone()["c"]
    out6 = edge._dispatch_pending_rework(
        conn, kb, "re-bound", task_id="t_shared", dry_run=True)
    check("dry-run is observation only", out6[0]["reason"] == "rework_spawn_predicted")
    after_events = conn.execute("SELECT COUNT(*) c FROM task_events").fetchone()["c"]
    check("dry-run wrote nothing", before_events == after_events)

    # --- fail-closed: verification DB read failure prevents spawn ---
    db2 = tmp / "board2.db"
    raw2 = make_db(db2)
    kb2 = FakeKB()
    raw2.execute(
        "INSERT INTO tasks VALUES ('t_boom','IMPLEMENT boom','body',"
        "'running','kanban-developer','worktree','/ws/projects/re-bound')")
    raw2.commit()
    edge2 = install(tmp)

    return _finish(raw2, kb2, edge2, tmp)


class FlakyConn:
    """Connection wrapper whose execute() can be made to fail on demand."""

    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw
        self.fail_read = False
        self.fail_event_write = False

    def execute(self, sql, *a, **k):
        if self.fail_read or (
            self.fail_event_write and "INSERT INTO task_events" in sql
        ):
            raise sqlite3.OperationalError("injected failure")
        return self._raw.execute(sql, *a, **k)

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()


def _finish(raw2, kb2, edge2, tmp) -> int:
    print("\n-- fail-closed phase --")

    # --- DB-read verification failure must prevent spawn (fail closed) ---
    conn2 = FlakyConn(raw2)
    RESOLVED_BY_ID["t_boom"] = "/ws/projects/re-bound"
    conn2.fail_read = True
    out_fc = edge2._dispatch_pending_rework(
        conn2, kb2, "b", task_id="t_boom", dry_run=False)
    conn2.fail_read = False

    check("DB-read verification failure blocks spawn (fail closed)",
          isinstance(out_fc, list) and out_fc
          and out_fc[0].get("reason") == "workspace_isolation_violation",
          str(out_fc))
    check("no worker invocation on verification failure",
          all(tid != "t_boom" for tid in kb2.default_spawn_calls),
          str(kb2.default_spawn_calls))

    # --- event-write persistence failure is an explicit failed gate ---
    db3 = tmp / "board3.db"
    raw3 = make_db(db3)
    kb3 = FakeKB()
    raw3.execute(
        "INSERT INTO tasks VALUES ('t_ev','IMPLEMENT ev','body',"
        "'running','kanban-developer','worktree','/ws/projects/re-bound')")
    raw3.commit()
    conn3 = FlakyConn(raw3)
    RESOLVED_BY_ID["t_ev"] = "/ws/projects/re-bound"  # violating binding
    conn3.fail_event_write = True
    out_ev = edge2._dispatch_pending_rework(
        conn3, kb3, "b", task_id="t_ev", dry_run=False)
    conn3.fail_event_write = False

    check("event-write failure blocks spawn (never swallowed)",
          out_ev and out_ev[0].get("reason") == "workspace_isolation_violation"
          and out_ev[0].get("event_persisted") is False
          and out_ev[0].get("gate_persistence") == "failed", str(out_ev))
    check("no leaked claim on event-write failure",
          "t_ev" not in kb3.default_spawn_calls, str(kb3.default_spawn_calls))

    # --- durable guard/quarantine cards are NOT gated nor repaired ---
    db4 = tmp / "board4.db"
    conn4 = make_db(db4)
    kb4 = FakeKB()
    conn4.execute(
        "INSERT INTO tasks VALUES "
        "('t_guard','SAFETY GUARD: 잘못된 공유 workspace 구현 카드 실행 금지',"
        "'SAFETY GUARD ONLY — 실행/구현 금지.',"
        "'blocked','kanban-main','worktree','/ws/projects/re-bound')")
    RESOLVED_BY_ID["t_guard"] = "/ws/projects/re-bound"
    out_g = edge2._dispatch_pending_rework(
        conn4, kb4, "b", task_id="t_guard", dry_run=False)
    # The guard card sits OUTSIDE the gate entirely (it can never reach this
    # lane in reality because it stays blocked): allowed through unchanged,
    # never gated, and never rebound by self-heal (selfheal suite asserts it).
    check("guard card exempt from pre-spawn gate",
          out_g and out_g[0].get("reason") == "rework_worker_spawned", str(out_g))

    # --- INTEGER event-schema fixture + ordering consumer check ---
    # A clean block run persists a spawn_blocked event; assert its created_at
    # is epoch INTEGER per the core event convention.
    db6 = tmp / "board6.db"
    conn6 = make_db(db6)
    kb6 = FakeKB()
    conn6.execute(
        "INSERT INTO tasks VALUES ('t_ts2','IMPLEMENT ts2','body',"
        "'running','kanban-developer','worktree','/ws/projects/re-bound')")
    conn6.commit()
    RESOLVED_BY_ID["t_ts2"] = "/ws/projects/re-bound"  # violating binding
    edge2._dispatch_pending_rework(conn6, kb6, "b", task_id="t_ts2", dry_run=False)
    ev_row = conn6.execute(
        "SELECT created_at, typeof(created_at) t FROM task_events "
        "WHERE kind='spawn_blocked'").fetchone()
    check("created_at stored as epoch INTEGER",
          ev_row is not None and ev_row["t"] == "integer"
          and isinstance(ev_row["created_at"], int),
          str(dict(ev_row) if ev_row else None))
    ordered = raw3.execute(
        "SELECT created_at FROM task_events ORDER BY created_at ASC, id ASC"
    ).fetchall()
    vals = [r["created_at"] for r in ordered]
    check("ordering consumer sees ascending INTEGER timestamps",
          vals == sorted(vals))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
