#!/usr/bin/env python3
"""Regression tests for dynamic backend-aware Kanban resource admission."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

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
        items = [{"slug": "default"}]
        root = self.boards_root()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if (child / "kanban.db").is_file():
                    items.append({"slug": child.name})
        return items

    def get_current_board(self):
        return "default"


def cfg():
    return {
        "default_assignee": "kanban-main",
        "worker_resources": {
            "local-serial-llm": {
                "capacity": 1,
                "backend": "local",
                "assignees": [
                    "kanban-main",
                    "kanban-developer",
                    "kanban-reviewer",
                    "kanban-designer",
                ],
                "stale_worker_grace_seconds": 0,
            }
        },
    }


def test_same_profile_changes_resource_with_backend() -> None:
    old = dynamic.resolve_profile_backend
    dynamic.install_dynamic_resource_policy(admission)
    try:
        dynamic.resolve_profile_backend = lambda name: dynamic.ProfileBackend(
            name, "openai-codex", "gpt-5", "", "cloud"
        )
        resource = admission.resource_for_assignee(cfg(), "kanban-main")
        check("main on Codex is not local-gated", resource is None, str(resource))

        dynamic.resolve_profile_backend = lambda name: dynamic.ProfileBackend(
            name, "custom", "qwen", "http://127.0.0.1:8080/v1", "local"
        )
        resource = admission.resource_for_assignee(cfg(), "kanban-main")
        check(
            "same main profile becomes local-gated after switch",
            resource is not None and resource.name == "local-serial-llm",
            str(resource),
        )
    finally:
        dynamic.resolve_profile_backend = old


def test_legacy_policy_remains_assignee_only() -> None:
    old = dynamic.resolve_profile_backend
    try:
        dynamic.resolve_profile_backend = lambda name: dynamic.ProfileBackend(
            name, "openai-codex", "gpt-5", "", "cloud"
        )
        legacy = {
            "worker_resources": {
                "serial": {
                    "capacity": 1,
                    "assignees": ["kanban-main"],
                }
            }
        }
        resource = admission.resource_for_assignee(legacy, "kanban-main")
        check(
            "policy without backend preserves legacy matching",
            resource is not None and resource.name == "serial",
            str(resource),
        )
    finally:
        dynamic.resolve_profile_backend = old


def test_default_and_named_board_discovery() -> None:
    root = Path(tempfile.mkdtemp(prefix="dynamic-resource-paths-"))
    kb = FakeKanban(root)
    default_conn = make_db(kb.kanban_db_path(board="default"))
    named_conn = make_db(kb.kanban_db_path(board="alpha"))
    try:
        paths = set(dynamic._all_board_db_paths(kb, "default"))
        check(
            "default board scan includes legacy root kanban.db",
            kb.kanban_db_path(board="default").resolve() in paths,
            str(paths),
        )
        check(
            "default board scan includes named board db",
            kb.kanban_db_path(board="alpha").resolve() in paths,
            str(paths),
        )
    finally:
        default_conn.close()
        named_conn.close()


def _install_fake_claims(kb: FakeKanban):
    def claim(conn, task_id, *args, **kwargs):
        cur = conn.execute(
            "UPDATE tasks SET status='running' WHERE id=? AND status='ready'",
            (task_id,),
        )
        conn.commit()
        return {"id": task_id} if cur.rowcount == 1 else None

    kb.claim_task = claim
    kb.claim_review_task = claim


def test_core_claim_gate_separates_local_from_cloud() -> None:
    root = Path(tempfile.mkdtemp(prefix="dynamic-resource-core-"))
    kb = FakeKanban(root)
    _install_fake_claims(kb)
    conn_a = make_db(kb.kanban_db_path(board="alpha"))
    conn_b = make_db(kb.kanban_db_path(board="beta"))

    # beta occupies the only local inference slot without a PID yet; this is
    # the claim->spawn reservation window the cross-board gate must observe.
    conn_b.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-local-live", "kanban-developer", "running", None, 7),
    )
    conn_b.commit()

    conn_a.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-local-wait", "kanban-developer", "ready", None, None),
    )
    conn_a.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-cloud", "kanban-main", "ready", None, None),
    )
    conn_a.commit()

    old_backend = dynamic.resolve_profile_backend
    old_cfg = dynamic._load_kanban_cfg

    def backend(name: str):
        if name == "kanban-main":
            return dynamic.ProfileBackend(name, "openai-codex", "gpt-5", "", "cloud")
        return dynamic.ProfileBackend(
            name, "custom", "qwen", "http://127.0.0.1:8080/v1", "local"
        )

    dynamic.resolve_profile_backend = backend
    dynamic._load_kanban_cfg = cfg
    try:
        dynamic.install_core_claim_admission(kb, admission)

        blocked = kb.claim_task(conn_a, "t-local-wait")
        check(
            "second local claim waits while sibling board owns slot",
            blocked is None,
            str(blocked),
        )

        cloud = kb.claim_task(conn_a, "t-cloud")
        check(
            "Codex-backed main claim bypasses local slot",
            cloud is not None,
            str(cloud),
        )
        status = conn_a.execute(
            "SELECT status FROM tasks WHERE id='t-cloud'"
        ).fetchone()[0]
        check("cloud task reached running", status == "running", status)
    finally:
        dynamic.resolve_profile_backend = old_backend
        dynamic._load_kanban_cfg = old_cfg
        conn_a.close()
        conn_b.close()


def test_local_claim_becomes_cross_board_reservation() -> None:
    root = Path(tempfile.mkdtemp(prefix="dynamic-resource-reservation-"))
    kb = FakeKanban(root)
    _install_fake_claims(kb)
    conn_a = make_db(kb.kanban_db_path(board="alpha"))
    conn_b = make_db(kb.kanban_db_path(board="beta"))
    for conn, task_id in ((conn_a, "t-a"), (conn_b, "t-b")):
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            (task_id, "kanban-developer", "ready", None, None),
        )
        conn.commit()

    old_backend = dynamic.resolve_profile_backend
    old_cfg = dynamic._load_kanban_cfg
    dynamic.resolve_profile_backend = lambda name: dynamic.ProfileBackend(
        name, "custom", "qwen", "http://127.0.0.1:8080/v1", "local"
    )
    dynamic._load_kanban_cfg = cfg
    try:
        dynamic.install_core_claim_admission(kb, admission)
        first = kb.claim_task(conn_a, "t-a")
        second = kb.claim_task(conn_b, "t-b")
        check("first local task is admitted", first is not None, str(first))
        check(
            "RUNNING no-PID claim reserves slot for sibling board",
            second is None,
            str(second),
        )
    finally:
        dynamic.resolve_profile_backend = old_backend
        dynamic._load_kanban_cfg = old_cfg
        conn_a.close()
        conn_b.close()


def test_stray_archived_db_does_not_poison_admission() -> None:
    """Regression: a stray zero-byte ``kanban.db`` left directly inside the
    archive container (``boards/_archived/kanban.db``) must not poison
    cross-board enumeration or the worker inspection.

    A read-only diagnostic that opened a write-mode ``sqlite3.connect`` on the
    ``_archived/`` directory had created that empty file; because the board
    glob swept it in, ``_active_resource_workers`` raised
    ``ResourceAdmissionError`` (``no such table: task_runs``) and refused every
    claim on every board.
    """
    root = Path(tempfile.mkdtemp(prefix="dynamic-resource-stray-"))
    kb = FakeKanban(root)
    default_conn = make_db(kb.kanban_db_path(board="default"))
    named_conn = make_db(kb.kanban_db_path(board="alpha"))
    boards_root = kb.boards_root()
    stray = boards_root / "_archived" / "kanban.db"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"")
    # A non-empty schema-bearing DB also inside the archive container, to prove
    # the whole internal container is excluded, not just empty files.
    archived_board = boards_root / "_archived" / "old-1234"
    archived_conn = make_db(archived_board / "kanban.db")
    dynamic.install_cross_board_helpers(admission)
    try:
        paths = set(dynamic._all_board_db_paths(kb, "default"))
        check(
            "stray _archived/kanban.db is not enumerated",
            stray.resolve() not in paths,
            str(paths),
        )
        check(
            "archived board DB is not enumerated",
            (archived_board / "kanban.db").resolve() not in paths,
            str(paths),
        )
        check(
            "real default + named boards remain enumerated",
            kb.kanban_db_path(board="default").resolve() in paths
            and kb.kanban_db_path(board="alpha").resolve() in paths,
            str(paths),
        )
        resource = admission.WorkerResource("serial", 1, ("kanban-main",))
        try:
            active = admission._active_resource_workers(kb, "default", resource)
        except admission.ResourceAdmissionError as exc:
            active = f"RAISED {type(exc).__name__}: {exc}"
        check(
            "worker inspection resolves despite stray file",
            isinstance(active, list),
            str(active),
        )
    finally:
        default_conn.close()
        named_conn.close()
        archived_conn.close()


def test_respawn_guard_overlay_waives_rework_active_pr() -> None:
    """Regression: respawn guard overlay must waive active_pr for rework/unrun tasks,
    while keeping active_pr intact for intake roots and previously-run impl tasks."""
    from hermes_cli import kanban_db_dispatch as kbd

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, last_failure_error TEXT)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at INTEGER)")
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, created_at INTEGER, body TEXT)")

    # 1. Intake card with PR URL
    conn.execute("INSERT INTO tasks VALUES ('t_intake', 'GitHub Issue intake: test#104', 'body', NULL)")
    conn.execute("INSERT INTO task_comments VALUES (1, 't_intake', 2000000000, 'PR: https://github.com/foo/bar/pull/1')")

    # 2. Impl card that ran outside window and opened a PR
    conn.execute("INSERT INTO tasks VALUES ('t_impl', 'Implement feature X', 'body', NULL)")
    conn.execute("INSERT INTO task_runs VALUES (1, 't_impl', 1000, 'completed')")
    conn.execute("INSERT INTO task_comments VALUES (2, 't_impl', 2000000000, 'PR: https://github.com/foo/bar/pull/1')")

    # 3. Bounded rework card with PR URL (incident shape)
    conn.execute("INSERT INTO tasks VALUES ('t_rework', 'Issue #104 bounded rework: fix PR #129', 'Bounded rework of existing PR #129', NULL)")
    conn.execute("INSERT INTO task_comments VALUES (3, 't_rework', 2000000000, 'Existing PR: https://github.com/foo/bar/pull/129')")

    # 4. Unrun impl card with PR URL in spec
    conn.execute("INSERT INTO tasks VALUES ('t_unrun', '구현: Issue #105 spec', 'body', NULL)")
    conn.execute("INSERT INTO task_comments VALUES (4, 't_unrun', 2000000000, 'Spec PR: https://github.com/foo/bar/pull/125')")

    dynamic._install_respawn_guard_overlay()

    check("intake root active_pr remains intact", kbd.check_respawn_guard(conn, "t_intake") == "active_pr")
    check("ran impl task active_pr remains intact", kbd.check_respawn_guard(conn, "t_impl") == "active_pr")
    check("rework task active_pr is waived", kbd.check_respawn_guard(conn, "t_rework") is None)
    check("unrun impl task active_pr is waived", kbd.check_respawn_guard(conn, "t_unrun") is None)


def main() -> int:
    test_same_profile_changes_resource_with_backend()
    test_legacy_policy_remains_assignee_only()
    test_default_and_named_board_discovery()
    test_core_claim_gate_separates_local_from_cloud()
    test_local_claim_becomes_cross_board_reservation()
    test_stray_archived_db_does_not_poison_admission()
    test_respawn_guard_overlay_waives_rework_active_pr()
    print(f"\n{len(PASS)} passed; {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
