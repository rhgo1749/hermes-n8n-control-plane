#!/usr/bin/env python3
"""Focused Issue #85 regressions for core resource admission telemetry."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import kanban_dynamic_resource as dynamic
import kanban_resource_admission as admission

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
            assignee TEXT,
            status TEXT,
            worker_pid INTEGER,
            current_run_id INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            profile TEXT,
            status TEXT,
            outcome TEXT,
            worker_pid INTEGER,
            ended_at INTEGER
        );
        """
    )
    conn.commit()
    return conn


class FakeKanban:
    claim_task: Any
    claim_review_task: Any
    dispatch_once: Any

    def __init__(self, root: Path):
        self.root = root

    def kanban_home(self):
        return self.root

    def boards_root(self):
        return self.root / "kanban" / "boards"

    def kanban_db_path(self, *, board: str):
        if board == "default":
            return self.root / "kanban.db"
        return self.boards_root() / board / "kanban.db"

    def list_boards(self, include_archived=False):
        del include_archived
        items = [{"slug": "default"}]
        root = self.boards_root()
        if root.is_dir():
            items.extend(
                {"slug": child.name}
                for child in sorted(root.iterdir())
                if (child / "kanban.db").is_file()
            )
        return items

    def get_current_board(self):
        return "default"

    def has_spawnable_ready(self, conn):
        del conn
        return True

    def has_spawnable_review(self, conn):
        del conn
        return True


def install_fake_claims(kb: FakeKanban):
    def claim(conn, task_id, *args, **kwargs):
        del args, kwargs
        cur = conn.execute(
            "UPDATE tasks SET status='running' "
            "WHERE id=? AND status IN ('ready', 'review')",
            (task_id,),
        )
        conn.commit()
        return {"id": task_id} if cur.rowcount == 1 else None

    kb.claim_task = claim
    kb.claim_review_task = claim


def config():
    return {
        "default_assignee": "kanban-main",
        "worker_resources": {
            "local-serial-llm": {
                "capacity": 1,
                "backend": "local",
                "assignees": ["kanban-main", "kanban-developer", "kanban-reviewer"],
                "stale_worker_grace_seconds": 0,
            }
        },
    }


def local_backend(name: str):
    return dynamic.ProfileBackend(
        name, "custom", "qwen", "http://127.0.0.1:8080/v1", "local"
    )


def install_test_seams():
    old_backend = dynamic.resolve_profile_backend
    old_cfg = dynamic._load_kanban_cfg
    old_profile_exists = getattr(dynamic, "_profile_exists", None)
    dynamic.resolve_profile_backend = local_backend
    dynamic._load_kanban_cfg = config
    dynamic._profile_exists = lambda name: bool(name)
    return old_backend, old_cfg, old_profile_exists


def restore_test_seams(old):
    old_backend, old_cfg, old_profile_exists = old
    dynamic.resolve_profile_backend = old_backend
    dynamic._load_kanban_cfg = old_cfg
    if old_profile_exists is None:
        del dynamic._profile_exists
    else:
        dynamic._profile_exists = old_profile_exists


def clear_outcomes() -> None:
    clear = getattr(admission, "clear_resource_admission_outcomes", None)
    if callable(clear):
        clear()


def test_ready_and_review_busy_then_release() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-health-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn_wait = make_db(kb.kanban_db_path(board="wait"))
    conn_holder = make_db(kb.kanban_db_path(board="holder"))
    conn_holder.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-holder", "kanban-developer", "running", 7101, 1),
    )
    conn_wait.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        [
            ("t-ready", "kanban-developer", "ready", None, None),
            ("t-review", "kanban-reviewer", "review", None, None),
        ],
    )
    conn_holder.commit()
    conn_wait.commit()

    old_seams = install_test_seams()
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    admission._pid_alive = lambda pid: pid is not None and int(pid) == 7101
    admission._worker_identity = lambda pid, task_id: True
    clear_outcomes()
    try:
        dynamic.install_core_claim_admission(kb, admission)
        ready_claim = kb.claim_task(conn_wait, "t-ready")
        review_claim = kb.claim_review_task(conn_wait, "t-review")
        check("READY claim returns resource busy", ready_claim is None, str(ready_claim))
        check("REVIEW claim returns resource busy", review_claim is None, str(review_claim))
        check(
            "busy READY remains READY",
            conn_wait.execute("SELECT status FROM tasks WHERE id='t-ready'").fetchone()[0]
            == "ready",
        )
        check(
            "busy REVIEW remains REVIEW",
            conn_wait.execute("SELECT status FROM tasks WHERE id='t-review'").fetchone()[0]
            == "review",
        )
        check("busy READY is absent from health probe", kb.has_spawnable_ready(conn_wait) is False)
        check("busy REVIEW is absent from health probe", kb.has_spawnable_review(conn_wait) is False)
        conn_wait.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            ("t-unmatched", "cloud-worker", "ready", None, None),
        )
        conn_wait.commit()
        check(
            "mixed queue keeps health spawnable",
            kb.has_spawnable_ready(conn_wait) is True,
        )
        conn_wait.execute("UPDATE tasks SET status='done' WHERE id='t-unmatched'")
        conn_wait.commit()
        busy = admission.resource_admission_outcomes(reason="resource_busy")
        check(
            "bounded busy telemetry covers both lanes",
            {item.get("lane") for item in busy} >= {"ready", "review"},
            str(busy),
        )

        # A dead holder is ignored by the next admission scan. The waiting
        # candidates can now create durable RUNNING reservations.
        admission._pid_alive = lambda pid: False
        ready_claim = kb.claim_task(conn_wait, "t-ready")
        check("dead holder releases READY", ready_claim is not None, str(ready_claim))
        check(
            "released READY reaches RUNNING",
            conn_wait.execute("SELECT status FROM tasks WHERE id='t-ready'").fetchone()[0]
            == "running",
        )
        # The fake claim function has no terminal-run API. Remove its synthetic
        # reservation before checking the independent REVIEW boundary.
        conn_wait.execute("UPDATE tasks SET status='done' WHERE id='t-ready'")
        conn_wait.commit()
        review_claim = kb.claim_review_task(conn_wait, "t-review")
        check("dead holder releases REVIEW", review_claim is not None, str(review_claim))
        check(
            "released REVIEW reaches RUNNING",
            conn_wait.execute("SELECT status FROM tasks WHERE id='t-review'").fetchone()[0]
            == "running",
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        clear_outcomes()
        restore_test_seams(old_seams)
        conn_wait.close()
        conn_holder.close()


def test_no_resource_config_preserves_legacy_probe() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-no-config-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="default"))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-legacy", "kanban-main", "ready", None, None),
    )
    conn.commit()
    old_cfg = dynamic._load_kanban_cfg
    dynamic._load_kanban_cfg = dict
    try:
        dynamic.install_core_claim_admission(kb, admission)
        check("no-resource health delegates", kb.has_spawnable_ready(conn) is True)
        check("no-resource claim delegates", kb.claim_task(conn, "t-legacy") is not None)
    finally:
        dynamic._load_kanban_cfg = old_cfg
        conn.close()


def test_dry_run_filters_busy_without_claim() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-dry-run-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn_wait = make_db(kb.kanban_db_path(board="wait"))
    conn_holder = make_db(kb.kanban_db_path(board="holder"))
    conn_holder.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-holder", "kanban-developer", "running", 7102, 1),
    )
    conn_wait.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-ready", "kanban-developer", "ready", None, None),
    )
    conn_holder.commit()
    conn_wait.commit()

    claim_calls: list[str] = []
    original_claim = kb.claim_task

    def counting_claim(conn, task_id, *args, **kwargs):
        claim_calls.append(task_id)
        return original_claim(conn, task_id, *args, **kwargs)

    kb.claim_task = counting_claim

    def dispatch_once(conn, **kwargs):
        del conn, kwargs
        return types.SimpleNamespace(
            spawned=[("t-ready", "kanban-developer", "")],
        )

    kb.dispatch_once = dispatch_once
    old_seams = install_test_seams()
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    admission._pid_alive = lambda pid: pid is not None and int(pid) == 7102
    admission._worker_identity = lambda pid, task_id: True
    try:
        dynamic.install_core_claim_admission(kb, admission)
        result = kb.dispatch_once(conn_wait, dry_run=True)
        busy = getattr(result, "resource_busy", [])
        busy_ids = {item.get("task_id") for item in busy}
        check("dry-run reports resource busy", "t-ready" in busy_ids, str(result))
        check("dry-run removes busy from spawn list", result.spawned == [], str(result))
        check("dry-run does not invoke claim", claim_calls == [], str(claim_calls))
        check(
            "dry-run leaves READY unchanged",
            conn_wait.execute("SELECT status FROM tasks WHERE id='t-ready'").fetchone()[0]
            == "ready",
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        restore_test_seams(old_seams)
        conn_wait.close()
        conn_holder.close()


def test_dry_run_respects_core_review_selection() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-dry-run-review-selection-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="default"))
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        [
            ("t-ready", "kanban-developer", "ready", None, None),
            ("t-review", "kanban-reviewer", "review", None, None),
        ],
    )
    conn.commit()

    call_order: list[str] = []

    def dispatch_once(conn, **kwargs):
        call_order.append("core")
        check("core receives max_spawn=1", kwargs.get("max_spawn") == 1, str(kwargs))
        del conn
        return types.SimpleNamespace(
            # This is the native core result for READY+REVIEW with max_spawn=1:
            # the reserved REVIEW slot selects t-review.
            spawned=[("t-review", "kanban-reviewer", "")],
        )

    kb.dispatch_once = dispatch_once
    old_seams = install_test_seams()
    old_active_workers = admission._active_resource_workers

    def active_workers(*args, **kwargs):
        call_order.append("overlay")
        return old_active_workers(*args, **kwargs)

    admission._active_resource_workers = active_workers
    try:
        dynamic.install_core_claim_admission(kb, admission)
        result = kb.dispatch_once(conn, dry_run=True, max_spawn=1)
        busy_ids = {
            item.get("task_id")
            for item in getattr(result, "resource_busy", [])
        }
        check(
            "dry-run runs core before resource replay",
            call_order == ["core", "overlay"],
            str(call_order),
        )
        check(
            "core REVIEW candidate remains predicted spawn",
            result.spawned == [("t-review", "kanban-reviewer", "")],
            str(result),
        )
        check(
            "READY peer is reported busy after REVIEW reservation",
            busy_ids == {"t-ready"},
            str(getattr(result, "resource_busy", [])),
        )
        check(
            "core-first dry-run leaves both lanes unchanged",
            [tuple(row) for row in conn.execute(
                "SELECT id, status FROM tasks ORDER BY id"
            ).fetchall()]
            == [("t-ready", "ready"), ("t-review", "review")],
        )
    finally:
        admission._active_resource_workers = old_active_workers
        restore_test_seams(old_seams)
        conn.close()


def test_dry_run_consumes_virtual_reservations() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-dry-run-reservation-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="default"))
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        [
            ("t-first", "kanban-developer", "ready", None, None),
            ("t-second", "kanban-developer", "ready", None, None),
        ],
    )
    conn.commit()

    def dispatch_once(conn, **kwargs):
        del conn, kwargs
        return types.SimpleNamespace(
            spawned=[
                ("t-first", "kanban-developer", ""),
                ("t-second", "kanban-developer", ""),
            ],
        )

    kb.dispatch_once = dispatch_once
    old_seams = install_test_seams()
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    admission._pid_alive = lambda pid: False
    admission._worker_identity = lambda pid, task_id: True
    clear_outcomes()
    try:
        dynamic.install_core_claim_admission(kb, admission)
        result = kb.dispatch_once(conn, dry_run=True)
        busy_ids = {
            item.get("task_id")
            for item in getattr(result, "resource_busy", [])
        }
        check(
            "dry-run reserves first same-resource candidate",
            result.spawned == [("t-first", "kanban-developer", "")],
            str(result),
        )
        check(
            "dry-run marks later same-resource candidate busy",
            busy_ids == {"t-second"},
            str(getattr(result, "resource_busy", [])),
        )
        check(
            "virtual reservation keeps both tasks READY",
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='ready'"
            ).fetchone()[0]
            == 2,
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        clear_outcomes()
        restore_test_seams(old_seams)
        conn.close()


def test_health_failure_stays_visible() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-health-failure-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="default"))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-failure", "kanban-developer", "ready", None, None),
    )
    conn.commit()

    old_seams = install_test_seams()
    old_resource_for_assignee = admission.resource_for_assignee
    old_active_workers = admission._active_resource_workers
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    admission._pid_alive = lambda pid: False
    admission._worker_identity = lambda pid, task_id: True
    clear_outcomes()
    try:
        dynamic.install_core_claim_admission(kb, admission)

        def fail_policy_resolution(cfg, assignee):
            del cfg, assignee
            raise RuntimeError("injected policy-resolution failure")

        admission.resource_for_assignee = fail_policy_resolution
        check(
            "policy failure remains visible to health",
            kb.has_spawnable_ready(conn) is True,
            str(admission.resource_admission_outcomes()),
        )

        admission.resource_for_assignee = old_resource_for_assignee

        def fail_worker_inspection(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("injected active-worker inspection failure")

        admission._active_resource_workers = fail_worker_inspection
        check(
            "active-worker inspection failure remains visible to health",
            kb.has_spawnable_ready(conn) is True,
            str(admission.resource_admission_outcomes()),
        )
        failures = admission.resource_admission_outcomes()
        check(
            "health failure diagnostics are explicit",
            {item.get("reason") for item in failures}
            >= {"resource_config_invalid", "resource_admission_failed"},
            str(failures),
        )
    finally:
        admission.resource_for_assignee = old_resource_for_assignee
        admission._active_resource_workers = old_active_workers
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        clear_outcomes()
        restore_test_seams(old_seams)
        conn.close()


def test_dispatch_normalizes_reaped_busy_spawn() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-reaped-spawn-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="default"))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-requeued", "kanban-developer", "ready", None, None),
    )
    conn.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "t-requeued", "kanban-developer", "done", "completed", 7301, 100),
    )
    conn.commit()

    def dispatch_once(conn, **kwargs):
        del kwargs
        claimed = kb.claim_task(conn, "t-requeued")
        return types.SimpleNamespace(
            spawned=[("t-requeued", "kanban-developer", "")] if claimed else [],
        )

    kb.dispatch_once = dispatch_once
    old_seams = install_test_seams()
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    old_terminate = admission._terminate_verified_worker
    live = {7301}
    killed: list[int] = []
    admission._pid_alive = lambda pid: pid is not None and int(pid) in live
    admission._worker_identity = lambda pid, task_id: True

    def terminate(pid, grace):
        del grace
        killed.append(int(pid))
        live.discard(int(pid))
        return True

    admission._terminate_verified_worker = terminate
    clear_outcomes()
    try:
        dynamic.install_core_claim_admission(kb, admission)
        result = kb.dispatch_once(conn, dry_run=False)
        check(
            "same-task terminal worker is reaped and spawned",
            result.spawned == [("t-requeued", "kanban-developer", "")]
            and killed == [7301],
            str((result, killed)),
        )
        check(
            "reaped spawn has no stale resource busy evidence",
            getattr(result, "resource_busy", []) == [],
            str(getattr(result, "resource_busy", [])),
        )
        check(
            "reaped spawn excludes busy from diagnostics",
            not any(
                item.get("reason") == "resource_busy"
                for item in getattr(result, "resource_admission", [])
            ),
            str(getattr(result, "resource_admission", [])),
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        admission._terminate_verified_worker = old_terminate
        clear_outcomes()
        restore_test_seams(old_seams)
        conn.close()


def test_cli_dry_run_formatter_reports_busy() -> None:
    from argparse import Namespace

    hermes_root = "/ws/hermes-agent"
    if hermes_root not in sys.path:
        sys.path.insert(0, hermes_root)
    from hermes_cli import config as config_module
    from hermes_cli import kanban as cli_module
    from hermes_cli import kanban_db as core_db

    dynamic._install_cli_dispatch_overlay()
    old_connect = core_db.connect_closing
    old_dispatch = core_db.dispatch_once
    old_load_config = config_module.load_config

    @contextlib.contextmanager
    def fake_connect():
        yield object()

    def fake_dispatch(conn, **kwargs):
        del conn, kwargs
        dynamic._last_resource_diagnostics = [{
            "task_id": "t-ready",
            "lane": "ready",
            "reason": "resource_busy",
            "resource_group": "local-serial-llm",
            "resource_active": 1,
            "resource_capacity": 1,
        }]
        return types.SimpleNamespace(
            reclaimed=0,
            crashed=[],
            timed_out=[],
            stale=[],
            auto_blocked=[],
            promoted=0,
            spawned=[("t-ready", "kanban-main", "")],
            skipped_unassigned=[],
            skipped_nonspawnable=[],
            skipped_per_profile_capped=[],
            auto_assigned_default=[],
        )

    core_db.connect_closing = fake_connect
    core_db.dispatch_once = fake_dispatch
    config_module.load_config = lambda: {"kanban": {}}
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_module._cmd_dispatch(
                Namespace(dry_run=True, max=None, failure_limit=2, json=True)
            )
        payload = json.loads(output.getvalue())
        check("CLI dry-run returns success", code == 0, str(code))
        check(
            "CLI dry-run exposes resource_busy",
            payload.get("skipped_resource_busy") == ["t-ready"],
            str(payload),
        )
        check("CLI dry-run does not list busy as spawned", payload.get("spawned") == [], str(payload))
    finally:
        core_db.connect_closing = old_connect
        core_db.dispatch_once = old_dispatch
        config_module.load_config = old_load_config


def test_same_task_terminal_pid_safety() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-busy-same-task-"))
    kb = FakeKanban(root)
    install_fake_claims(kb)
    conn = make_db(kb.kanban_db_path(board="worker"))
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        [
            ("t-terminal", "kanban-developer", "ready", None, None),
            ("t-active", "kanban-developer", "ready", None, None),
            ("t-reused", "kanban-developer", "ready", None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "t-terminal", "kanban-developer", "done", "completed", 7201, 100),
            (2, "t-active", "kanban-developer", "running", None, 7202, None),
            (3, "t-reused", "kanban-developer", "done", "completed", 7203, 100),
        ],
    )
    conn.commit()

    old_seams = install_test_seams()
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    old_terminate = admission._terminate_verified_worker
    live = {7201, 7203}
    killed: list[int] = []
    admission._pid_alive = lambda pid: pid is not None and int(pid) in live
    admission._worker_identity = lambda pid, task_id: (
        int(pid) != 7203
    )

    def terminate(pid, grace):
        del grace
        killed.append(int(pid))
        live.discard(int(pid))
        return True

    admission._terminate_verified_worker = terminate
    try:
        dynamic.install_core_claim_admission(kb, admission)
        terminal_claim = kb.claim_task(conn, "t-terminal")
        check(
            "terminal same-task worker is reaped before claim",
            terminal_claim is not None and killed == [7201],
            str((terminal_claim, killed)),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id='t-terminal'")
        conn.commit()
        live.add(7202)

        active_claim = kb.claim_task(conn, "t-active")
        check("active same-task run blocks claim", active_claim is None, str(active_claim))
        check("active same-task run is never killed", killed == [7201], str(killed))
        live.discard(7202)

        reused_claim = kb.claim_task(conn, "t-reused")
        check(
            "PID reuse is ignored without signalling unrelated process",
            reused_claim is not None and killed == [7201],
            str((reused_claim, killed)),
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        admission._terminate_verified_worker = old_terminate
        clear_outcomes()
        restore_test_seams(old_seams)
        conn.close()


def test_dynamic_backend_provider_matching_ornith_and_cloud() -> None:
    """Verify endpoint/provider resolution classifies custom local/ornith and cloud routes correctly."""
    import kanban_dynamic_resource as kdr

    check(
        "custom:local-llamacpp-(8080) matches local",
        kdr._provider_is_local("custom:local-llamacpp-(8080)"),
    )
    check(
        "custom:local-ornith-1.5-35b-(8082) matches local",
        kdr._provider_is_local("custom:local-ornith-1.5-35b-(8082)"),
    )
    check(
        "openai-codex does not match local provider",
        not kdr._provider_is_local("openai-codex"),
    )
    check(
        "cloud codex backend resolves to cloud kind",
        kdr.resolve_profile_backend("kanban-developer").kind == "cloud",
    )


def test_diagnostics_are_bounded() -> None:
    clear_outcomes()
    for index in range(256):
        admission.record_resource_admission_outcome(
            task_id=f"t-{index}",
            board="test",
            resource_group="local-serial-llm",
            reason="resource_busy",
            lane="ready",
            resource_active=1,
            resource_capacity=1,
        )
    outcomes = admission.resource_admission_outcomes()
    check(
        "resource diagnostics stay bounded",
        len(outcomes) == admission.RESOURCE_OUTCOME_LIMIT,
        str(len(outcomes)),
    )
    clear_outcomes()


def main() -> int:
    test_dynamic_backend_provider_matching_ornith_and_cloud()
    test_ready_and_review_busy_then_release()
    test_no_resource_config_preserves_legacy_probe()
    test_dry_run_filters_busy_without_claim()
    test_dry_run_respects_core_review_selection()
    test_dry_run_consumes_virtual_reservations()
    test_health_failure_stays_visible()
    test_dispatch_normalizes_reaped_busy_spawn()
    test_cli_dry_run_formatter_reports_busy()
    test_same_task_terminal_pid_safety()
    test_diagnostics_are_bounded()
    print(f"\n{len(PASS)} passed; {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
