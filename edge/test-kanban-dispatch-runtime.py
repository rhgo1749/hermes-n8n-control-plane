#!/usr/bin/env python3
"""Dispatcher split compatibility against real Hermes + isolated fake GitHub.

Never spawn real workers or mutate live boards. Patch only the current owning
modules, so a stale kanban_db private call cannot be hidden by the fixture.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location(
    "dispatch_runtime_harness", HERE / "test-kanban-github-sync-rework.py"
)
assert spec is not None and spec.loader is not None
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)

from hermes_cli import kanban_db_connect, kanban_db_dispatch, kanban_db_workspace  # noqa: E402
from kanban_workspace_admission import install_workspace_admission  # noqa: E402


class DispatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(h.isolated_test_environment())
        self.fake = h.fresh_env()
        self.tid = h._rework_ready_task(self.fake)
        self.workspace = h._scratch_workspace(self.tid, os.environ["HERMES_HOME"])
        h._make_profile_dir()
        self.spawn = self.enterContext(patch.object(
            kanban_db_dispatch, "_default_spawn", return_value=4242
        ))

    def dispatch(self, cfg=None, **kwargs):
        with h.connect_closing() as conn:
            return h.mod._dispatch_pending_rework(
                conn, h.kanban_db, "default", task_ids=[self.tid],
                cfg=({"max_in_progress": 1, "failure_limit": 2} if cfg is None else cfg),
                **kwargs,
            )

    def assert_spawned(self, result):
        self.assertEqual(result[0]["reason"], "rework_worker_spawned", result)
        self.spawn.assert_called_once()
        self.assertEqual(h.task_row(self.tid)["worker_pid"], 4242)
        with h.connect_closing() as conn:
            run = conn.execute(
                "SELECT worker_pid FROM task_runs WHERE id=?",
                (h.task_row(self.tid)["current_run_id"],),
            ).fetchone()
        self.assertEqual(run["worker_pid"], 4242)

    def test_default_spawn_records_task_and_run_pid(self):
        self.assert_spawned(self.dispatch())

    def test_workspace_overlay_resolves_current_default_spawn(self):
        with patch.object(h.mod, "_dispatch_pending_rework", h.mod._dispatch_pending_rework):
            install_workspace_admission(h.mod)
            self.assert_spawned(self.dispatch())

    def test_busy_core_lock_does_not_claim_or_spawn(self):
        before = h.task_row(self.tid)
        with kanban_db_connect._dispatch_tick_lock(
            h.kanban_db.kanban_db_path(board="default")
        ) as held:
            self.assertTrue(held)
            result = self.dispatch()
        self.assertEqual(result[0]["reason"], "dispatch_locked", result)
        self.spawn.assert_not_called()
        self.assertEqual(h.task_row(self.tid), before)

    def test_worktree_resolution_uses_current_owner(self):
        with h.connect_closing() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (self.tid,)
            )
            conn.commit()
        with patch.object(
            kanban_db_workspace, "_resolve_worktree_workspace",
            return_value=(Path(self.workspace), "wt/runtime-compat"),
        ) as resolve:
            self.assert_spawned(self.dispatch())
        resolve.assert_called_once()
        self.assertEqual(h.task_row(self.tid)["branch_name"], "wt/runtime-compat")

    def assert_failed_run_closed(self, expected_status, expected_outcome, failures):
        row = h.task_row(self.tid)
        self.assertEqual(row["status"], expected_status)
        self.assertIsNone(row["claim_lock"])
        self.assertIsNone(row["worker_pid"])
        self.assertEqual(row["consecutive_failures"], failures)
        with h.connect_closing() as conn:
            run = conn.execute(
                "SELECT outcome, ended_at FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (self.tid,),
            ).fetchone()
        self.assertEqual(run["outcome"], expected_outcome)
        self.assertIsNotNone(run["ended_at"])

    def test_workspace_failure_releases_claim_and_ends_run(self):
        with h.connect_closing() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (self.tid,)
            )
            conn.commit()
        with patch.object(
            kanban_db_workspace, "_resolve_worktree_workspace",
            side_effect=RuntimeError("workspace fixture failure"),
        ):
            result = self.dispatch()
        self.assertEqual(result[0]["reason"], "workspace_resolve_failed", result)
        self.spawn.assert_not_called()
        self.assert_failed_run_closed("ready", "spawn_failed", 1)

    def test_spawn_failure_preserves_retry_and_circuit_breaker(self):
        self.spawn.side_effect = RuntimeError("spawn fixture failure")
        for count, status, outcome in [(1, "ready", "spawn_failed"), (2, "blocked", "gave_up")]:
            result = self.dispatch()
            self.assertEqual(result[0]["reason"], "spawn_failed", result)
            self.assertEqual(result[0]["auto_blocked"], count == 2)
            self.assert_failed_run_closed(status, outcome, count)
        self.assertEqual(self.spawn.call_count, 2)

    def test_unset_failure_limit_uses_runtime_default(self):
        self.spawn.side_effect = RuntimeError("spawn fixture failure")
        with patch.object(kanban_db_dispatch, "DEFAULT_FAILURE_LIMIT", 3):
            for count in range(1, 4):
                result = self.dispatch(cfg={"max_in_progress": 1})
                self.assertEqual(result[0]["reason"], "spawn_failed", result)
                self.assertEqual(result[0]["auto_blocked"], count == 3)
                self.assert_failed_run_closed(
                    "blocked" if count == 3 else "ready",
                    "gave_up" if count == 3 else "spawn_failed", count,
                )

    def test_task_retry_override_keeps_precedence(self):
        with h.connect_closing() as conn:
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (self.tid,))
            conn.commit()
        self.spawn.side_effect = RuntimeError("spawn fixture failure")
        result = self.dispatch(cfg={"max_in_progress": 1, "failure_limit": 99})
        self.assertEqual(result[0]["reason"], "spawn_failed", result)
        self.assertTrue(result[0]["auto_blocked"])
        self.assert_failed_run_closed("blocked", "gave_up", 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
