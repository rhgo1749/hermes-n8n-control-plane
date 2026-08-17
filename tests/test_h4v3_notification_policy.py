"""Focused tests for the human-attention Telegram notification policy."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INT_A = Path(__file__).resolve().parents[1] / "automation/hermes/scripts/github-agent-ready-kanban-intake.py"
spec = importlib.util.spec_from_file_location("h4v3_intake_policy", INT_A)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)

EDGE = ROOT / "edge" / "kanban-github-sync.py"
edge_spec = importlib.util.spec_from_file_location("h4v3_edge_policy", EDGE)
assert edge_spec and edge_spec.loader
edge = importlib.util.module_from_spec(edge_spec)
sys.modules[edge_spec.name] = edge
edge_spec.loader.exec_module(edge)


def _entry(reason: str, **extra):
    base = {
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "issue_title": "Example",
    }
    base.update(extra)
    base["reason"] = reason
    return base


def test_suppress_normal_lifecycle_transitions() -> None:
    for from_state, to_state in (
        ("ready", "running"),
        ("running", "done"),
        ("done", "review"),
        ("review", "ready"),
    ):
        entry = _entry("linked_pr_open", changed=True, from_state=from_state, to_state=to_state)
        assert intake._should_notify_entry(entry) is False, (from_state, to_state)


def test_suppress_normal_rework_rounds() -> None:
    for round_no in (1, 2):
        entry = _entry(
            "agent_rework",
            changed=True,
            from_state="review",
            to_state="ready",
            rework={"reason": "agent_rework", "rework_round": round_no},
        )
        assert intake._should_notify_entry(entry) is False, round_no


def test_unknown_transition_suppressed_fail_closed() -> None:
    entry = _entry("some_future_reason", changed=True, from_state="todo", to_state="running")
    assert intake._should_notify_entry(entry) is False


def test_need_you_block_sends() -> None:
    entry = _entry("blocker_projection", status="blocked", block_kind="needs_input")
    assert intake._should_notify_entry(entry) is True
    assert intake._entry_attention_reason(entry) == "needs_input"


def test_human_attention_failure_sends() -> None:
    entry = _entry("rework_retry_blocked", error="needs maintainer decision", changed=True)
    assert intake._should_notify_entry(entry) is True


def test_rework_threshold_sends() -> None:
    entry = _entry("agent_rework", rework={"reason": "agent_rework", "rework_round": 3})
    assert intake._entry_attention_reason(entry) == "rework_threshold_exceeded"
    assert intake._should_notify_entry(entry) is True


def test_attention_line_is_short_and_actionable() -> None:
    entry = _entry("rework_retry_blocked", changed=True)
    line = intake._attention_notification_line("re-bound", "Re-Bound", 106, entry)
    assert line.startswith("⚠️ [re-bound] Re-Bound #106")
    assert "확인 필요" in line


def test_operator_attention_dedupe_and_resend_after_new_event() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, body TEXT);
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    conn.execute("INSERT INTO tasks (id, status, body) VALUES ('t1', 'blocked', 'x')")
    entry = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "rework_retry_blocked",
        "status": "blocked",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        assert edge._record_operator_attention(conn, entry) is False  # same incident: quiet
        rows = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'github_operator_attention'").fetchone()
        assert rows[0] == 1
        # A new ordinary lifecycle event changes the cursor -> recurrence may send again.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t1', 'claimed', '{}', 1)"
        )
        assert edge._record_operator_attention(conn, entry) is True
    finally:
        conn.close()


def test_send_dedup_skip_reports_skipped_and_never_invokes_hermes_send() -> None:
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="intake-policy-skip-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        state_dir = home / "state"
        state_dir.mkdir(parents=True)
        lines = ["⚠️ [re-bound] Re-Bound #106 · 확인 필요 — rework_context_failed — t"]
        text = "🤖 Hermes Kanban\n\n" + "\n".join(lines)
        (state_dir / "kanban-intake-last-sent.txt").write_text(text, encoding="utf-8")

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("hermes send must not run on a dedup skip")

        setattr(intake.subprocess, "run", fail_if_called)
        result = intake._send_telegram_batch(lines, ("123", ""))
        assert result == "skipped", result
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_delivery_reports_sent_and_writes_state() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-send-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        lines = ["line-one"]
        result = intake._send_telegram_batch(lines, ("123", "456"))
        assert result == "sent", result
        assert len(captured) == 1
        state = (home / "state" / "kanban-intake-last-sent.txt").read_text(encoding="utf-8")
        assert state == "🤖 Hermes Kanban\n\nline-one", state
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
