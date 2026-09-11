#!/usr/bin/env python3
"""Focused regression tests for review-attention delivery recovery."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace


ENTRYPOINT = Path(__file__).resolve().parent / "kanban-github-sync-entrypoint.py"


def load_installer():
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
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


class DummyRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class DummyConn:
    def __init__(self, event_rows=()):
        self._event_rows = list(event_rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _sql, _params=()):
        return DummyRows(self._event_rows)


class DummyClient:
    def __init__(self, timeline=()):
        self.timeline = list(timeline)

    def get_paginated(self, path, params=None, *, max_pages=10):
        del params, max_pages
        if path.endswith("/timeline"):
            return list(self.timeline)
        return []


def make_context():
    return {
        "labels": {"agent-rework"},
        "pr": SimpleNamespace(state="open"),
        "pr_number": 103,
        "event": ({
            "rework_round": 1,
            "pr_number": 103,
            "head_sha": "7d575644fef2ed40a6473e75a9c03ab2a991673a",
            "label_added_at": 90,
        }, 100, "github_pr_rework"),
    }


def make_core(*, original_reason="rework_retry_pending", delivered=True):
    calls = {
        "delivery": 0,
        "projection": 0,
        "events": [],
    }

    def original_reconcile(*args, **kwargs):
        if original_reason == "rework_retry_pending":
            return {
                "task_id": "t_ac34f08d",
                "status": "review",
                "changed": False,
                "reason": "rework_retry_pending",
                "retry_required": True,
                "attention_at": 200,
                "lifecycle": {"labels": ["agent-rework"]},
            }
        return {
            "task_id": "t_ac34f08d",
            "status": "ready",
            "changed": True,
            "reason": original_reason,
        }

    def delivery_evidence(*args, **kwargs):
        calls["delivery"] += 1
        if not delivered:
            return False, "delivery_run_missing", {"rework_at": 100}
        return True, "delivery_complete_verification_only", {
            "run_id": 577,
            "run_outcome": "completed",
            "head": "7d575644fef2ed40a6473e75a9c03ab2a991673a",
            "verification_only": True,
            "provenance": "specialist_reviewer_pass_same_head",
        }

    def project_labels(*args, **kwargs):
        calls["projection"] += 1
        assert kwargs["add"] == ("agent-review-ready",)
        assert kwargs["remove"] == ("agent-rework", "agent-working")
        return True, "labels_updated", {"labels": ["agent-review-ready"]}

    def append_event(conn, task_id, payload, *, kind):
        calls["events"].append((task_id, dict(payload), kind))

    core = SimpleNamespace(
        _reconcile_rework_lifecycle=original_reconcile,
        _rework_delivery_evidence=delivery_evidence,
        _project_pr_lifecycle_labels=project_labels,
        _latest_delivery_head=lambda conn, task_id: None,
        _append_sync_event=append_event,
        REWORK_LABEL="agent-rework",
        WORKING_LABEL="agent-working",
        REVIEW_READY_LABEL="agent-review-ready",
        TRUSTED_GITHUB_ACTORS={"rhgo1749"},
        _parse_iso_ts=lambda value: int(value) if value is not None else None,
        GithubCompletionError=RuntimeError,
    )
    install(core)
    return core, calls


def invoke(core, *, dry_run, timeline=(), event_rows=()):
    return core._reconcile_rework_lifecycle(
        DummyConn(event_rows),
        object(),
        DummyClient(timeline),
        SimpleNamespace(repository="rhgo1749/ctrl-hangul"),
        object(),
        {"id": "t_ac34f08d", "status": "review"},
        make_context(),
        dry_run=dry_run,
        failure_limit=None,
    )


def test_dry_run_predicts_review_ready_from_recovered_delivery():
    core, calls = make_core(delivered=True)
    result = invoke(core, dry_run=True)
    assert result["reason"] == "agent_review_ready_predicted"
    assert result["status"] == "review"
    assert result["attention_recovered"] is True
    assert result["evidence"]["run_id"] == 577
    assert calls["delivery"] == 1
    assert calls["projection"] == 0
    assert calls["events"] == []


def test_real_recovery_projects_label_and_records_delivery_event():
    core, calls = make_core(delivered=True)
    result = invoke(core, dry_run=False)
    assert result["reason"] == "agent_review_ready"
    assert result["status"] == "review"
    assert result["lifecycle"] == {"labels": ["agent-review-ready"]}
    assert calls["projection"] == 1
    assert len(calls["events"]) == 1
    task_id, payload, kind = calls["events"][0]
    assert task_id == "t_ac34f08d"
    assert kind == "github_pr_rework_delivery"
    assert payload["run_id"] == 577
    assert payload["attention_recovered"] is True


def test_invalid_delivery_keeps_retry_pending_fail_closed():
    core, calls = make_core(delivered=False)
    result = invoke(core, dry_run=True)
    assert result["reason"] == "rework_retry_pending"
    assert result["retry_required"] is True
    assert calls["delivery"] == 1
    assert calls["projection"] == 0
    assert calls["events"] == []


def test_fresh_trusted_rework_label_bypasses_attention_retry_comment():
    core, calls = make_core(delivered=True)
    result = invoke(
        core,
        dry_run=False,
        timeline=[{
            "event": "labeled",
            "label": {"name": "agent-rework"},
            "created_at": "300",
            "actor": {"login": "rhgo1749"},
        }],
    )
    assert result is None
    assert calls["delivery"] == 0
    assert calls["projection"] == 0
    assert calls["events"] == []


def test_untrusted_rework_label_does_not_bypass_attention():
    core, calls = make_core(delivered=True)
    result = invoke(
        core,
        dry_run=False,
        timeline=[{
            "event": "labeled",
            "label": {"name": "agent-rework"},
            "created_at": "300",
            "actor": {"login": "someone-else"},
        }],
    )
    assert result["reason"] == "agent_review_ready"
    assert calls["delivery"] == 1


def test_edge_owned_rework_projection_does_not_open_new_round():
    core, calls = make_core(delivered=True)
    timeline = [
        {
            "event": "unlabeled",
            "label": {"name": "agent-working"},
            "created_at": "300",
            "actor": {"login": "rhgo1749"},
        },
        {
            "event": "labeled",
            "label": {"name": "agent-rework"},
            "created_at": "300",
            "actor": {"login": "rhgo1749"},
        },
    ]
    event_rows = [{
        "kind": "github_pr_rework_attention",
        "payload": (
            '{"rework_round": 1, "pr_number": 103, '
            '"head_sha": "7d575644fef2ed40a6473e75a9c03ab2a991673a", '
            '"source": "github_edge_rework_reconciliation", '
            '"reason": "rework_human_attention"}'
        ),
        "created_at": 305,
    }]
    result = invoke(
        core,
        dry_run=False,
        timeline=timeline,
        event_rows=event_rows,
    )
    assert result["reason"] == "agent_review_ready"
    assert calls["delivery"] == 1


def test_explicit_retry_result_keeps_precedence():
    core, calls = make_core(original_reason="maintainer_retry_consumed", delivered=True)
    result = invoke(core, dry_run=False)
    assert result["reason"] == "maintainer_retry_consumed"
    assert calls["delivery"] == 0
    assert calls["projection"] == 0
    assert calls["events"] == []


def test_install_is_idempotent():
    core, _calls = make_core(delivered=True)
    first = core._reconcile_rework_lifecycle
    install(core)
    assert core._reconcile_rework_lifecycle is first


if __name__ == "__main__":
    tests = [
        test_dry_run_predicts_review_ready_from_recovered_delivery,
        test_real_recovery_projects_label_and_records_delivery_event,
        test_invalid_delivery_keeps_retry_pending_fail_closed,
        test_fresh_trusted_rework_label_bypasses_attention_retry_comment,
        test_untrusted_rework_label_does_not_bypass_attention,
        test_edge_owned_rework_projection_does_not_open_new_round,
        test_explicit_retry_result_keeps_precedence,
        test_install_is_idempotent,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
