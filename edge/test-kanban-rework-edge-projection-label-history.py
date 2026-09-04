#!/usr/bin/env python3
"""Regression tests for repeated same-round edge-owned rework label projections."""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace


ENTRYPOINT = Path(__file__).resolve().parent / "kanban-github-sync-entrypoint.py"
HEAD = "7d575644fef2ed40a6473e75a9c03ab2a991673a"
TASK = "t_ac34f08d"


def load_installer():
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_install_rework_attention_delivery_recovery"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            target,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {"ModuleType": ModuleType, "json": json}
    exec(compile(module, str(ENTRYPOINT), "exec"), namespace)
    return namespace["_install_rework_attention_delivery_recovery"]


install = load_installer()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        );
        """
    )
    retry = {
        "source": "github_edge_rework_recovery",
        "rework_round": 1,
        "pr_number": 103,
        "head_sha": HEAD,
    }
    attention = {
        "source": "github_edge_rework_reconciliation",
        "reason": "rework_human_attention",
        "rework_round": 1,
        "pr_number": 103,
        "head_sha": HEAD,
    }
    unrelated = {
        "source": "github_edge_rework_recovery",
        "rework_round": 2,
        "pr_number": 103,
        "head_sha": "b" * 40,
    }
    conn.executemany(
        "INSERT INTO task_events(id, task_id, kind, payload, created_at) "
        "VALUES(?,?,?,?,?)",
        [
            (1, TASK, "github_pr_rework_retry", json.dumps(retry), 152),
            (2, TASK, "github_pr_rework_attention", json.dumps(attention), 201),
            (3, TASK, "github_pr_rework_retry", json.dumps(unrelated), 999),
        ],
    )
    return conn


class FakeClient:
    def __init__(self, items):
        self.items = list(items)

    def get_paginated(self, path, params=None, *, max_pages=10):
        return list(self.items)


def labeled(ts, name="agent-rework"):
    return {"event": "labeled", "label": {"name": name}, "created_at": ts}


def unlabeled(ts, name="agent-working"):
    return {"event": "unlabeled", "label": {"name": name}, "created_at": ts}


def live_shape(*, human_after_attention=False, missing_working_pair=False):
    items = [
        labeled(90),  # trusted maintainer label that opened the original round
        unlabeled(150),
        labeled(150),  # edge same-round recovery projection
        unlabeled(200),
        labeled(200),  # edge attention self-heal projection
    ]
    if missing_working_pair:
        items = [item for item in items if not (
            item["event"] == "unlabeled" and item["created_at"] == 150
        )]
    if human_after_attention:
        items.append(labeled(202))
    return items


def make_core(*, strict_ok=True):
    calls = []
    latest_event = (
        {
            "source": "github_edge_rework_recovery",
            "rework_round": 1,
            "pr_number": 103,
            "head_sha": HEAD,
        },
        152,
        "github_pr_rework_retry",
    )
    origin_event = (
        {
            "source": "github",
            "rework_round": 1,
            "pr_number": 103,
            "head_sha": HEAD,
        },
        100,
        "github_pr_rework",
    )

    core = SimpleNamespace(
        REWORK_LABEL="agent-rework",
        WORKING_LABEL="agent-working",
        REVIEW_READY_LABEL="agent-review-ready",
        GithubCompletionError=RuntimeError,
        _parse_iso_ts=lambda value: int(value),
        _latest_rework_event=lambda conn, task_id: latest_event,
        _rework_round_origin_event=lambda conn, task_id, event: origin_event,
        _latest_delivery_head=lambda conn, task_id: None,
        _append_sync_event=lambda *args, **kwargs: None,
        _project_pr_lifecycle_labels=lambda *args, **kwargs: (
            True,
            "labels_updated",
            {"labels": ["agent-review-ready"]},
        ),
    )

    core._reconcile_rework_lifecycle = lambda *args, **kwargs: {
        "task_id": TASK,
        "status": "review",
        "changed": False,
        "reason": "rework_retry_pending",
        "retry_required": True,
        "attention_at": 201,
        "lifecycle": {"labels": ["agent-rework"]},
    }

    def delivery_evidence(conn, client, ref, task_id, pr, event):
        items = client.get_paginated(
            f"/repos/{ref.repository}/issues/103/timeline",
            {"per_page": 100},
        )
        rework_times = [
            int(item["created_at"])
            for item in items
            if item.get("event") == "labeled"
            and (item.get("label") or {}).get("name") == "agent-rework"
        ]
        calls.append(rework_times)
        if max(rework_times or [0]) > 100:
            return False, "delivery_superseded_by_new_rework", {"rework_at": 100}
        if strict_ok:
            return True, "delivery_complete_verification_only", {
                "head": HEAD,
                "run_id": 578,
                "reviewer_run_id": 576,
                "provenance": "specialist_reviewer_pass_same_head",
            }
        return False, "completion_handoff_missing", {"run_id": 578}

    core._rework_delivery_evidence = delivery_evidence
    install(core)
    return core, calls


def context_with_origin_only():
    # Deliberately provide the origin event here. Recovery must be derived from
    # the durable DB/latest-event reader, not from this context representation.
    return {
        "labels": {"agent-rework"},
        "pr": SimpleNamespace(state="open"),
        "pr_number": 103,
        "event": (
            {
                "source": "github",
                "rework_round": 1,
                "pr_number": 103,
                "head_sha": HEAD,
            },
            100,
            "github_pr_rework",
        ),
    }


def invoke(core, conn, client):
    return core._reconcile_rework_lifecycle(
        conn,
        None,
        client,
        SimpleNamespace(repository="rhgo1749/ctrl-hangul"),
        None,
        {"id": TASK, "status": "review"},
        context_with_origin_only(),
        dry_run=True,
        failure_limit=None,
    )


def test_repeated_edge_projection_labels_are_both_filtered():
    conn = make_db()
    core, calls = make_core(strict_ok=True)
    result = invoke(core, conn, FakeClient(live_shape()))

    assert result["reason"] == "agent_review_ready_predicted"
    assert result["evidence"]["run_id"] == 578
    assert result["evidence"]["reviewer_run_id"] == 576
    assert result["evidence"]["edge_projection_labels_recovered"] is True
    assert result["evidence"]["filtered_rework_label_times"] == [150, 200]
    assert calls == [[90, 150, 200], [90]]


def test_fresh_human_label_after_attention_remains_visible_fail_closed():
    conn = make_db()
    core, calls = make_core(strict_ok=True)
    result = invoke(core, conn, FakeClient(live_shape(human_after_attention=True)))

    assert result["reason"] == "rework_retry_pending"
    assert calls == [[90, 150, 200, 202], [90, 202]]


def test_label_without_working_to_rework_projection_pair_is_not_hidden():
    conn = make_db()
    core, calls = make_core(strict_ok=True)
    result = invoke(core, conn, FakeClient(live_shape(missing_working_pair=True)))

    assert result["reason"] == "rework_retry_pending"
    assert calls == [[90, 150, 200], [90, 150]]


def test_downstream_strict_delivery_failure_remains_fail_closed():
    conn = make_db()
    core, calls = make_core(strict_ok=False)
    result = invoke(core, conn, FakeClient(live_shape()))

    assert result["reason"] == "rework_retry_pending"
    assert calls == [[90, 150, 200], [90]]


if __name__ == "__main__":
    tests = [
        test_repeated_edge_projection_labels_are_both_filtered,
        test_fresh_human_label_after_attention_remains_visible_fail_closed,
        test_label_without_working_to_rework_projection_pair_is_not_hidden,
        test_downstream_strict_delivery_failure_remains_fail_closed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
