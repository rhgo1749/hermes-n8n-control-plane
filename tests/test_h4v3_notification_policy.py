"""Focused tests for the human-attention Telegram notification policy."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

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

OVERVIEW = ROOT / "hermes-plugin" / "h4v3-overview" / "dashboard" / "plugin_api.py"
overview_spec = importlib.util.spec_from_file_location("h4v3_overview_policy", OVERVIEW)
assert overview_spec and overview_spec.loader
overview = importlib.util.module_from_spec(overview_spec)
sys.modules[overview_spec.name] = overview
overview_spec.loader.exec_module(overview)


def _entry(reason: str, **extra: Any) -> dict[str, Any]:
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


def test_attention_line_carries_semantic_identity_for_delivery_dedupe() -> None:
    first = _entry(
        "rework_retry_blocked",
        operator_attention={
            "reason": "rework_retry_blocked",
            "attention_key": "rework_retry_blocked:repo|106|123|1|7",
        },
    )
    second = _entry(
        "rework_retry_blocked",
        operator_attention={
            "reason": "rework_retry_blocked",
            "attention_key": "rework_retry_blocked:repo|106|123|2|8",
        },
    )
    first_line = intake._attention_notification_line(
        "re-bound", "Re-Bound", 106, first
    )
    second_line = intake._attention_notification_line(
        "re-bound", "Re-Bound", 106, second
    )
    assert "incident=rework_retry_blocked:repo|106|123|1|7" in first_line
    assert first_line != second_line


def test_attention_line_parser_preserves_delimiters_in_semantic_identity() -> None:
    key = "needs_input:needs_input|needs_input|operator — input|200|4"
    entry = _entry(
        "needs_input",
        issue_title="Title — with punctuation",
        operator_attention={"reason": "needs_input", "attention_key": key},
    )
    line = intake._attention_notification_line("re-bound", "Re-Bound", 106, entry)
    assert intake._telegram_attention_key(line) == key


def test_attention_line_parser_uses_trailing_marker_after_display_collision() -> None:
    key = "needs_input:needs_input|needs_input|operator|200|4"
    marker = intake._TELEGRAM_INCIDENT_MARKER
    entry = _entry(
        "needs_input",
        issue_title=f"Title{marker}display-decoy",
        operator_attention={
            "reason": f"needs_input{marker}reason-decoy",
            "attention_key": key,
        },
    )
    line = intake._attention_notification_line("re-bound", "Re-Bound", 106, entry)
    assert line.count(marker) == 3
    assert intake._telegram_attention_key(line) == key


def test_attention_line_marks_unresolved_before_trailing_marker() -> None:
    key = "dispatch_lock_unavailable:dispatch_lock_unavailable|board"
    marker = intake._TELEGRAM_INCIDENT_MARKER
    entry = _entry(
        "dispatch_lock_unavailable",
        issue_title=f"Title{marker}display-decoy",
        operator_attention={
            "reason": f"dispatch_lock_unavailable{marker}reason-decoy",
            "attention_key": key,
            "incident_unresolved": True,
        },
    )
    line = intake._attention_notification_line("re-bound", "Re-Bound", 106, entry)
    assert line.endswith(
        f"incident_unresolved=true{marker}{key}"
    ), line
    assert intake._telegram_attention_key(line) == key
    assert intake._telegram_attention_is_unresolved(line) is True


def test_attention_line_escapes_unresolved_marker_decoys_in_display_text() -> None:
    key = "needs_input:rhgo1749/re-bound|106|123|1|8"
    marker = intake._TELEGRAM_INCIDENT_UNRESOLVED_MARKER
    entry = _entry(
        "needs_input",
        issue_title=f"Title{marker}",
        operator_attention={
            "reason": f"needs_input{marker}",
            "attention_key": key,
        },
    )
    line = intake._attention_notification_line("re-bound", "Re-Bound", 106, entry)
    assert line.endswith(f"incident={key}")
    assert intake._telegram_attention_is_unresolved(line) is False
    assert marker not in line[: -len(f" · incident={key}")]


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
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "t1",
            "blocked",
            json.dumps({"kind": "needs_input", "reason": "first"}),
            10,
        ),
    )
    entry: dict[str, Any] = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "needs_input",
        "status": "blocked",
        "block_kind": "needs_input",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        first_payload = json.loads(
            conn.execute(
                "SELECT payload FROM task_events "
                "WHERE kind = 'github_operator_attention'"
            ).fetchone()[0]
        )
        assert first_payload["incident_provenance"]["source"] == "blocked_event"
        assert first_payload["incident_provenance"]["blocked_event_id"]
        assert edge._record_operator_attention(conn, entry) is False
        assert (
            entry["operator_attention"]["attention_key"]
            == first_payload["attention_key"]
        )
        # Ordinary event churn, including repeated projections, does not
        # create a new incident.
        for index, kind in enumerate(
            (
                "github_blocked_projection",
                "heartbeat",
                "commented",
                "respawn_guarded",
                "claimed",
                "spawned",
            ),
            start=11,
        ):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, '{}', ?)",
                ("t1", kind, index),
            )
            assert edge._record_operator_attention(conn, entry) is False
        # A resolved blocker followed by a new blocked event re-arms even with
        # the same reason and block_kind.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES ('t1', 'github_blocked_resolved', '{}', 20)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "t1",
                "blocked",
                json.dumps({"kind": "needs_input", "reason": "second"}),
                21,
            ),
        )
        assert edge._record_operator_attention(conn, entry) is True
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE kind = 'github_operator_attention' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert (
            json.loads(rows[0][0])["attention_key"]
            != json.loads(rows[1][0])["attention_key"]
        )
    finally:
        conn.close()


def test_blocked_generation_changes_when_resolved_and_reblocked_same_second() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    blocked_payload = json.dumps({"kind": "needs_input", "reason": "same"})
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('t1', 'blocked', ?, 200)",
        (blocked_payload,),
    )
    entry: dict[str, Any] = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "needs_input",
        "status": "blocked",
        "block_kind": "needs_input",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        first_payload = json.loads(
            conn.execute(
                "SELECT payload FROM task_events "
                "WHERE kind = 'github_operator_attention'"
            ).fetchone()[0]
        )
        first_provenance = first_payload["incident_provenance"]

        # The resolution and the new block intentionally share one timestamp.
        # The governing blocked row id must still open a new generation.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES ('t1', 'github_blocked_resolved', '{}', 200)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES ('t1', 'blocked', ?, 200)",
            (blocked_payload,),
        )
        assert edge._record_operator_attention(conn, entry) is True
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE kind = 'github_operator_attention' ORDER BY id"
        ).fetchall()
        second_payload = json.loads(rows[1][0])
        second_provenance = second_payload["incident_provenance"]
        assert first_provenance["blocked_event_created_at"] == 200
        assert second_provenance["blocked_event_created_at"] == 200
        assert first_provenance["blocked_event_id"] != second_provenance["blocked_event_id"]
        assert first_payload["attention_key"] != second_payload["attention_key"]
    finally:
        conn.close()


def test_operator_attention_rework_round_is_stable_across_retry_event() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    rework = {
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "pr_number": 123,
        "rework_round": 1,
        "request_comment_id": 7,
    }
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("t1", "github_pr_rework", json.dumps(rework), 10),
    )
    entry = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "rework_human_attention",
        "status": "review",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        assert edge._record_operator_attention(conn, entry) is False
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "github_pr_rework_retry", json.dumps(rework), 11),
        )
        assert edge._record_operator_attention(conn, entry) is False
        # A different governing request comment is a new rework incident even
        # when the round number and reason remain unchanged.
        rework["request_comment_id"] = 8
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "github_pr_rework_retry", json.dumps(rework), 12),
        )
        assert edge._record_operator_attention(conn, entry) is True
        rework["rework_round"] = 2
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "github_pr_rework", json.dumps(rework), 13),
        )
        assert edge._record_operator_attention(conn, entry) is True
        keys = [
            json.loads(row[0])["attention_key"]
            for row in conn.execute(
                "SELECT payload FROM task_events "
                "WHERE kind = 'github_operator_attention' ORDER BY id"
            )
        ]
        assert len(keys) == 3
        assert len(set(keys)) == 3
        assert "|106|123|1|7" in keys[0]
        assert "|106|123|1|8" in keys[1]
        assert "|106|123|2|8" in keys[2]
    finally:
        conn.close()


def test_lifecycle_conflict_binds_round_and_realerts_on_new_round() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    entry: dict[str, Any] = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "pr_number": 123,
        "reason": "lifecycle_label_conflict",
        "status": "review",
    }
    round_one = {
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "pr_number": 123,
        "rework_round": 1,
        "request_comment_id": 7,
    }
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'github_pr_rework', ?, 10)",
            ("t1", json.dumps(round_one)),
        )
        assert edge._record_operator_attention(conn, entry) is True
        first_payload = json.loads(
            conn.execute(
                "SELECT payload FROM task_events "
                "WHERE kind = 'github_operator_attention'"
            ).fetchone()[0]
        )
        first_key = first_payload["attention_key"]
        assert "|106|123|1|7" in first_key
        assert overview._semantic_attention_key(first_payload) == first_key
        first_line = intake._attention_notification_line(
            "re-bound", "Re-Bound", 106, entry
        )
        assert intake._telegram_attention_key(first_line) == first_key

        for kind, created_at in (("heartbeat", 11), ("commented", 12), ("spawned", 13)):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES ('t1', ?, '{}', ?)",
                (kind, created_at),
            )
            assert edge._record_operator_attention(conn, entry) is False

        round_two = dict(round_one, rework_round=2, request_comment_id=8)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'github_pr_rework', ?, 20)",
            ("t1", json.dumps(round_two)),
        )
        assert edge._record_operator_attention(conn, entry) is True
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE kind = 'github_operator_attention' ORDER BY id"
        ).fetchall()
        second_payload = json.loads(rows[1][0])
        second_key = second_payload["attention_key"]
        assert "|106|123|2|8" in second_key
        assert second_key != first_key
        assert overview._semantic_attention_key(second_payload) == second_key
    finally:
        conn.close()


def test_dispatch_lock_attention_ignores_task_pr_identity() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE task_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, "
        "kind TEXT, payload TEXT, created_at INTEGER)"
    )
    try:
        entry = {
            "task_id": "t1",
            "repository": "rhgo1749/re-bound",
            "issue_number": 106,
            "pr_number": 123,
            "reason": "dispatch_lock_failed",
            "board": "re-bound",
        }
        payload = edge._operator_attention_payload(conn, entry)
        assert payload is not None
        assert payload["attention_key"] == "dispatch_lock_failed"
        assert payload["incident_unresolved"] is True
        assert payload["repository"] is None
        assert payload["issue_number"] is None
        assert payload["incident_provenance"]["incident_ref"] is None
        assert edge._record_operator_attention(conn, entry) is False
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE kind = 'github_operator_attention'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_board_dispatch_lock_attention_is_keyed_for_telegram_observer() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        entry: dict[str, Any] = {
            "task_id": None,
            "board": "re-bound",
            "reason": "dispatch_lock_unavailable",
        }
        assert edge._record_operator_attention(conn, entry) is False
        payload = entry["operator_attention"]
        assert payload["attention_key"] == "dispatch_lock_unavailable"
        assert payload["incident_unresolved"] is True
        assert payload["incident_provenance"] == {
            "source": "board_context",
            "board": "re-bound",
            "reason": "dispatch_lock_unavailable",
            "incident_ref": None,
        }
        assert overview._semantic_attention_key(payload) is None
        assert intake._should_notify_entry(entry) is True
        assert intake._notification_context(entry, ()) == (
            "re-bound",
            "Re Bound",
            None,
        )
        predicted_entry = {
            "task_id": None,
            "board": "re-bound",
            "reason": "dispatch_lock_unavailable",
            "operator_attention_predicted": payload,
        }
        assert intake._should_notify_entry(predicted_entry) is True
        assert intake._notification_context(predicted_entry, ()) == (
            "re-bound",
            "Re Bound",
            None,
        )
        line = intake._attention_notification_line(
            "re-bound", "Re-Bound", None, entry
        )
        assert "board" in line
        assert intake._telegram_attention_key(line) == "dispatch_lock_unavailable"
    finally:
        conn.close()


def test_operator_attention_without_identity_fails_open_and_dedupes_exact_key() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    entry = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "lifecycle_label_conflict",
        "status": "review",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        payload = json.loads(
            conn.execute("SELECT payload FROM task_events").fetchone()[0]
        )
        assert payload["incident_unresolved"] is True
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "claimed", "{}", 10),
        )
        assert edge._record_operator_attention(conn, entry) is False
        # A legacy cursor row cannot suppress a semantic row, even if the
        # textual key happens to collide.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "t2",
                "github_operator_attention",
                json.dumps({"attention_key": "lifecycle_label_conflict:123"}),
                11,
            ),
        )
        new_entry = dict(entry)
        new_entry["task_id"] = "t2"
        new_entry["pr_number"] = 123
        assert edge._record_operator_attention(conn, new_entry) is True
    finally:
        conn.close()


def test_rework_attention_does_not_fallback_to_unrelated_block_identity() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('t1', 'blocked', ?, 10)",
        (json.dumps({"kind": "needs_input", "reason": "old blocker"}),),
    )
    entry: dict[str, Any] = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "reason": "rework_human_attention",
        "status": "blocked",
        "block_kind": "needs_input",
    }
    try:
        assert edge._record_operator_attention(conn, entry) is True
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM task_events "
                "WHERE kind = 'github_operator_attention'"
            ).fetchone()[0]
        )
        assert payload["incident_provenance"]["source"] == "entry_context"
        assert payload["incident_unresolved"] is True
        assert payload["attention_key"] == "rework_human_attention"
    finally:
        conn.close()


def test_incomplete_rework_identity_with_pr_only_fails_open_then_rearms() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
          kind TEXT, payload TEXT, created_at INTEGER
        );
        """
    )
    entry: dict[str, Any] = {
        "task_id": "t1",
        "repository": "rhgo1749/re-bound",
        "issue_number": 106,
        "pr_number": 123,
        "reason": "rework_human_attention",
        "status": "blocked",
    }
    try:
        # The PR number is useful context, but without a canonical rework
        # round it must not become a resolved incident identity.
        assert edge._record_operator_attention(conn, entry) is True
        first_payload = json.loads(
            conn.execute("SELECT payload FROM task_events").fetchone()[0]
        )
        assert first_payload["attention_key"] == "rework_human_attention"
        assert first_payload["incident_unresolved"] is True
        assert first_payload["incident_provenance"] == {
            "source": "entry_context",
            "pr_number": 123,
            "reason": "rework_human_attention",
            "incident_ref": None,
        }

        # A later canonical round on the same PR gets its own generation.
        rework = {
            "repository": "rhgo1749/re-bound",
            "issue_number": 106,
            "pr_number": 123,
            "rework_round": 1,
            "request_comment_id": 7,
        }
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "github_pr_rework", json.dumps(rework), 20),
        )
        assert edge._record_operator_attention(conn, entry) is True
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE kind = ? ORDER BY id",
            ("github_operator_attention",),
        ).fetchall()
        assert len(rows) == 2
        second_payload = json.loads(rows[1][0])
        assert second_payload["attention_key"] == (
            "rework_human_attention:rhgo1749/re-bound|106|123|1|7"
        )
        assert "incident_unresolved" not in second_payload
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
        key = "rework_context_failed:rhgo1749/re-bound|106|123|1|7"
        lines = [
            "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — rework_context_failed — t "
            f"· incident={key}"
        ]
        (state_dir / "kanban-intake-last-sent.txt").write_text(
            json.dumps({"version": 2, "attention_keys": [key]}),
            encoding="utf-8",
        )

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("hermes send must not run on a dedup skip")

        setattr(intake.subprocess, "run", fail_if_called)
        result = intake._send_telegram_batch(lines, ("123", ""))
        assert result == "skipped", result
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_corrupt_state_fails_open_and_replaces_it_after_delivery() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-corrupt-state-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        state_path = home / "state" / "kanban-intake-last-sent.txt"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("legacy non-json body", encoding="utf-8")
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        key = "needs_input:rhgo1749/re-bound|106|123|1|9"
        line = f"⚠️ board · incident={key}"
        assert intake._send_telegram_batch([line], ("123", "")) == "sent"
        assert len(captured) == 1
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state == {
            "active_unresolved_keys": [],
            "attention_keys": [key],
            "version": 3,
        }
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
        key = "rework_context_failed:rhgo1749/re-bound|106|123|1|7"
        lines = [
            "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — rework_context_failed — line-one "
            f"· incident={key}"
        ]
        result = intake._send_telegram_batch(lines, ("123", "456"))
        assert result == "sent", result
        assert len(captured) == 1
        state = json.loads(
            (home / "state" / "kanban-intake-last-sent.txt").read_text(encoding="utf-8")
        )
        assert state == {
            "active_unresolved_keys": [],
            "attention_keys": [key],
            "version": 3,
        }, state
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_dedup_sends_only_unseen_semantic_lines() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-semantic-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        line_a = "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — needs_input — A · incident=A"
        line_b = "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — needs_input — B · incident=B"
        assert intake._send_telegram_batch([line_a], ("123", "")) == "sent"
        assert intake._send_telegram_batch([line_a, line_b], ("123", "")) == "sent"
        assert captured[1] == "🤖 Hermes Kanban\n\n" + line_b
        assert "incident=A" not in captured[1]
        assert intake._send_telegram_batch([line_a, line_b], ("123", "")) == "skipped"
        assert len(captured) == 2
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_dedup_ignores_display_changes_for_same_semantic_key() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-display-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        first = "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — needs_input — old · incident=A"
        changed = "⚠️ [re-bound] Renamed #106 · 확인 필요 — needs_input — new · incident=A"
        assert intake._send_telegram_batch([first], ("123", "")) == "sent"
        assert intake._send_telegram_batch([changed], ("123", "")) == "skipped"
        assert len(captured) == 1
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_dedup_sends_genuinely_new_semantic_key_once() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-new-key-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        line_a = "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — needs_input — A · incident=A"
        line_b = "⚠️ [re-bound] Re-Bound #106 · 확인 필요 — needs_input — B · incident=B"
        assert intake._send_telegram_batch([line_a], ("123", "")) == "sent"
        assert intake._send_telegram_batch([line_b], ("123", "")) == "sent"
        assert intake._send_telegram_batch([line_b], ("123", "")) == "skipped"
        assert len(captured) == 2
        assert captured[1] == "🤖 Hermes Kanban\n\n" + line_b
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_unresolved_dedup_uses_active_snapshot_not_persistent_keys() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-unresolved-active-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        key = "dispatch_lock_failed"
        first = (
            "⚠️ [re-bound] Re-Bound board · 확인 필요 — dispatch_lock_failed — old "
            f"· incident_unresolved=true · incident={key}"
        )
        changed = (
            "⚠️ [re-bound] Renamed board · 확인 필요 — dispatch_lock_failed — new "
            f"· incident_unresolved=true · incident={key}"
        )
        assert intake._send_telegram_batch([first], ("123", "")) == "sent"
        state = json.loads(
            (home / "state" / "kanban-intake-last-sent.txt").read_text(encoding="utf-8")
        )
        assert state == {
            "active_unresolved_keys": [key],
            "attention_keys": [],
            "version": 3,
        }, state
        assert intake._send_telegram_batch([changed], ("123", "")) == "skipped"
        assert len(captured) == 1
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_unresolved_realerts_after_empty_configured_tick() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-unresolved-rearm-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        captured: list[str] = []

        def fake_run(_cmd, input, **_kwargs):
            captured.append(input)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        setattr(intake.subprocess, "run", fake_run)
        key = "dispatch_lock_unavailable"
        line = (
            "⚠️ [re-bound] Re-Bound board · 확인 필요 — dispatch_lock_unavailable "
            f"· incident_unresolved=true · incident={key}"
        )
        assert intake._send_telegram_batch([line], ("123", "")) == "sent"
        assert intake._send_telegram_batch([], ("123", "")) == "skipped"
        state = json.loads(
            (home / "state" / "kanban-intake-last-sent.txt").read_text(encoding="utf-8")
        )
        assert state["active_unresolved_keys"] == []
        assert state["attention_keys"] == []
        assert intake._send_telegram_batch([line], ("123", "")) == "sent"
        assert len(captured) == 2
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_empty_or_dedup_batch_never_invokes_hermes_send() -> None:
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="intake-policy-empty-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)
        state_path = home / "state" / "kanban-intake-last-sent.txt"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "attention_keys": ["resolved"],
                    "active_unresolved_keys": ["active"],
                }
            ),
            encoding="utf-8",
        )

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("hermes send must not run for empty/dedup batches")

        setattr(intake.subprocess, "run", fail_if_called)
        assert intake._send_telegram_batch(
            ["⚠️ board · incident_unresolved=true · incident=active"],
            ("123", ""),
        ) == "skipped"
        assert intake._send_telegram_batch([], ("123", "")) == "skipped"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["attention_keys"] == ["resolved"]
        assert state["active_unresolved_keys"] == []
    finally:
        setattr(intake, "_hermes_home", original_home)
        setattr(intake.subprocess, "run", original_run)
        shutil.rmtree(home, ignore_errors=True)


def test_send_failure_does_not_mark_new_delivery_state() -> None:
    import shutil
    import tempfile
    import types

    home = Path(tempfile.mkdtemp(prefix="intake-policy-send-failure-"))
    original_home = intake._hermes_home
    original_run = intake.subprocess.run
    try:
        setattr(intake, "_hermes_home", lambda: home)

        def failed_run(*_args, **_kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="send failed")

        setattr(intake.subprocess, "run", failed_run)
        line = "⚠️ board · incident_unresolved=true · incident=unresolved-new"
        assert intake._send_telegram_batch([line], ("123", "")) is False
        assert not (home / "state" / "kanban-intake-last-sent.txt").exists()
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
