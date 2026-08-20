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


def main() -> int:
    test_same_profile_changes_resource_with_backend()
    test_legacy_policy_remains_assignee_only()
    test_default_and_named_board_discovery()
    test_core_claim_gate_separates_local_from_cloud()
    test_local_claim_becomes_cross_board_reservation()
    print(f"\n{len(PASS)} passed; {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
