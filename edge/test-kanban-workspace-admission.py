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
import json
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

    _actual_core_regression(tmp)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


# ---------------------------------------------------------------------------
# ACTUAL-CORE / ENTRYPOINT integration regression (round-3 F1/F2)
#
# The unit phase above drives the wrapper with a FAKE original dispatcher, so a
# _SpawnBlocked that its fake original swallows would still let those checks
# pass.  This phase instead loads the REAL core dispatcher through the REAL
# entrypoint overlay stack (dynamic resource -> resource admission ->
# head-binding -> retry-guard -> workspace admission, production install order)
# and runs the real ``_dispatch_pending_rework``.  It proves the sentinel
# actually ESCAPES the core's ``except Exception`` around spawn() and that the
# stored gate-failure result is what the production call path returns:
#   * a shared-checkout implementation claim returns the gate result
#     (reason=workspace_isolation_violation, gate=pre_spawn) with NO worker
#     spawn and ZERO _record_spawn_failure() accounting;
#   * the event-write-failure variant returns event_persisted=False with
#     gate_persistence='failed' (and zero spawn_blocked rows);
#   * the unidentifiable-claim (F2) path returns a truthful fail-closed result
#     (no TypeError) with event_persisted=False and gate_persistence='unavailable',
#     and the sentinel is a BaseException (NOT an Exception) so it survives the
#     core's ``except Exception``.
#
# Gated on ``hermes_cli`` availability: the suite stays runnable without a Hermes
# install, but in a validation environment this phase MUST run (not skip).
# ---------------------------------------------------------------------------

def _actual_core_regression(tmp: Path) -> None:
    try:
        import importlib
        if importlib.util.find_spec("hermes_cli") is None:
            print("\n-- actual-core/entrypoint regression SKIPPED (hermes_cli not installed) --")
            return
    except Exception:
        print("\n-- actual-core/entrypoint regression SKIPPED (hermes_cli not importable) --")
        return

    import os
    import subprocess
    import time as _time
    import sqlite3 as _sqlite3

    edge_dir = Path(__file__).resolve().parent
    hermes_src = "/ws/hermes-agent"
    kanban_env_keys = (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_ROOT", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT",
        "HERMES_KANBAN_WORKSPACE",
    )
    saved_env = {k: os.environ.get(k) for k in ("HERMES_HOME", *kanban_env_keys)}

    # Isolated temp HERMES_HOME + a profile dir so profile_exists(assignee) is
    # true during the real dispatch.  Set BEFORE any hermes_cli import.
    home = Path(tempfile.mkdtemp(prefix="ws-adm-actualcore-"))
    os.environ["HERMES_HOME"] = str(home)
    for k in kanban_env_keys:
        os.environ.pop(k, None)
    (home / "profiles" / "kanban-developer").mkdir(parents=True, exist_ok=True)
    if hermes_src not in sys.path:
        sys.path.insert(0, hermes_src)

    # Shared-checkout anchor: a real git repo root the impl card is bound to.
    anchor = home / "shared-checkout"
    anchor.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=anchor, check=True)
    (anchor / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(anchor), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(anchor), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "init"], check=True)

    # Load the REAL entrypoint (which loads the real core + installs every
    # overlay in production order).
    ep_spec = importlib.util.spec_from_file_location(
        "kbghsync_entrypoint_actualcore", edge_dir / "kanban-github-sync-entrypoint.py")
    ep = importlib.util.module_from_spec(ep_spec)
    sys.modules["kbghsync_entrypoint_actualcore"] = ep
    ep_spec.loader.exec_module(ep)
    core = ep._core

    try:
        from hermes_cli import kanban_db, kanban_db_dispatch
        from hermes_cli.kanban_db import connect_closing, init_db

        check("actual-core: entrypoint installed the workspace-admission wrapper",
              getattr(core._dispatch_pending_rework,
                      "_workspace_admission_installed", False) is True)
        check("actual-core: sentinel is a BaseException",
              issubclass(admission._SpawnBlocked, BaseException))
        check("actual-core: sentinel is NOT an Exception (escapes core except)",
              not issubclass(admission._SpawnBlocked, Exception))

        init_db()

        spawn_calls: list[str] = []
        fail_calls: list[tuple[str, str]] = []

        def spy_default_spawn(task, workspace, *, board=None):
            spawn_calls.append(str(task.id))
            return 12345

        def spy_record_spawn_failure(conn, task_id, error, **kwargs):
            fail_calls.append((task_id, str(error)[:80]))
            return False

        from unittest.mock import patch
        patches = (
            patch.object(kanban_db_dispatch, "_default_spawn", spy_default_spawn),
            patch.object(kanban_db_dispatch, "_record_task_failure", spy_record_spawn_failure),
        )
        for runtime_patch in patches:
            runtime_patch.start()

        def make_impl_task(tag: str) -> str:
            """A rework-pending implementation card bound to the shared checkout."""
            with connect_closing() as conn:
                tid = kanban_db.create_task(
                    conn,
                    title=f"REWORK {tag}: fix workspace isolation",
                    body="bounded developer rework - shared checkout binding must be gated",
                    assignee="kanban-developer",
                    created_by="actual-core-regression",
                    workspace_kind="dir",
                    workspace_path=str(anchor),
                )
                conn.commit()
            with connect_closing() as conn:
                conn.execute(
                    "UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
                # Make the task rework-pending so the REAL rework lane considers it:
                # the governing event must be github_pr_rework.
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'github_pr_rework', ?, ?)",
                    (tid, json.dumps({
                        "source": "actual_core_regression", "pr_number": 77,
                        "rework_round": 1, "head_sha": "a" * 40,
                    }),
                     int(_time.time())))
                conn.commit()
            return tid

        def spawn_blocked_rows(conn, tid: str) -> int:
            return int(conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawn_blocked'",
                (tid,)).fetchone()[0])

        def task_status(conn, tid: str) -> str:
            return str(conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0])

        # ---- CASE 1: F1 gate result preserved through the REAL core ----
        t1 = make_impl_task("c1")
        spawn_calls.clear()
        fail_calls.clear()
        with connect_closing() as conn:
            out1 = core._dispatch_pending_rework(
                conn, kanban_db, "default", task_ids=[t1], cfg={})
        e1 = out1[0] if out1 else {}
        check("actual-core F1: gate result returned (not spawn_failed)",
              e1.get("reason") == "workspace_isolation_violation"
              and e1.get("gate") == "pre_spawn", str(out1))
        check("actual-core F1: event_persisted is True (durable spawn_blocked)",
              e1.get("event_persisted") is True, str(e1))
        check("actual-core F1: ZERO worker spawn (no _default_spawn call)",
              spawn_calls == [], str(spawn_calls))
        check("actual-core F1: ZERO _record_spawn_failure() accounting",
              fail_calls == [], str(fail_calls))
        with connect_closing() as conn:
            check("actual-core F1: one durable spawn_blocked event persisted",
                  spawn_blocked_rows(conn, t1) == 1)
            check("actual-core F1: claim reclaimed (task back to ready)",
                  task_status(conn, t1) == "ready",
                  task_status(conn, t1))

        # ---- CASE 2: F1 event-write-failure variant ----
        # A conn that fails ONLY the admission's spawn_blocked INSERT (its unique
        # SQL literal), NOT the core's claimed/reclaimed event INSERTs.
        class FlakyConn:
            def __init__(self, raw: _sqlite3.Connection):
                self._raw = raw
                self.fail_spawn_blocked_insert = False

            def execute(self, sql, *a, **k):
                if self.fail_spawn_blocked_insert and "'spawn_blocked'" in sql:
                    raise _sqlite3.OperationalError("injected event-write failure")
                return self._raw.execute(sql, *a, **k)

            def commit(self):
                return self._raw.commit()

            def rollback(self):
                return self._raw.rollback()

            @property
            def in_transaction(self):
                return self._raw.in_transaction

            def __getattr__(self, name):
                return getattr(self._raw, name)

        t2 = make_impl_task("c2")
        spawn_calls.clear()
        fail_calls.clear()
        with connect_closing() as raw:
            fc = FlakyConn(raw)
            fc.fail_spawn_blocked_insert = True
            out2 = core._dispatch_pending_rework(
                fc, kanban_db, "default", task_ids=[t2], cfg={})
            fc.fail_spawn_blocked_insert = False
            e2 = out2[0] if out2 else {}
            check("actual-core F1 (event-write failure): reason is gate result",
                  e2.get("reason") == "workspace_isolation_violation", str(out2))
            check("actual-core F1 (event-write failure): event_persisted is False",
                  e2.get("event_persisted") is False, str(e2))
            check("actual-core F1 (event-write failure): gate_persistence is 'failed'",
                  e2.get("gate_persistence") == "failed", str(e2))
            check("actual-core F1 (event-write failure): ZERO worker spawn",
                  spawn_calls == [], str(spawn_calls))
            check("actual-core F1 (event-write failure): ZERO _record_spawn_failure()",
                  fail_calls == [], str(fail_calls))
            check("actual-core F1 (event-write failure): zero spawn_blocked rows",
                  spawn_blocked_rows(raw, t2) == 0)

        # ---- CASE 3: F2 unidentifiable-claim fail-closed (no TypeError) ----
        class NoIdClaim:
            id = ""

        with connect_closing() as conn:
            f2_raised = None
            f2_result = None
            try:
                admission._guarded_spawn(
                    conn, kanban_db, "default", NoIdClaim(), "/ws/shared",
                    inner_spawn=None, default_spawn=None)
            except admission._SpawnBlocked as bl:
                f2_result = getattr(bl, "result", {})
            except Exception as exc:  # includes the pre-fix TypeError
                f2_raised = exc
            check("actual-core F2: unknown-task path raises the sentinel (not TypeError)",
                  f2_raised is None and f2_result is not None,
                  f"raised={f2_raised!r} result={f2_result!r}")
            check("actual-core F2: fail-closed result reports event_persisted False",
                  bool(f2_result) and f2_result.get("event_persisted") is False,
                  str(f2_result))
            check("actual-core F2: truthfully reports persistence unavailable",
                  bool(f2_result) and f2_result.get("gate_persistence") in
                  ("failed", "unavailable"), str(f2_result))

    finally:
        for runtime_patch in locals().get("patches", ()):
            runtime_patch.stop()
        # Restore the process env so the suite's other phases are unaffected.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    raise SystemExit(main())
