#!/usr/bin/env python3
"""Focused regression tests for attention self-heal label recovery."""
from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType, SimpleNamespace


ENTRYPOINT = Path(__file__).resolve().parent / "kanban-github-sync-entrypoint.py"


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
    namespace = {"ModuleType": ModuleType}
    exec(compile(module, str(ENTRYPOINT), "exec"), namespace)
    return namespace["_install_rework_attention_delivery_recovery"]


install = load_installer()


class FakeClient:
    def __init__(self, label_times):
        self.label_times = list(label_times)

    def get_paginated(self, path, params=None, *, max_pages=10):
        return [
            {
                "event": "labeled",
                "label": {"name": "agent-rework"},
                "created_at": ts,
            }
            for ts in self.label_times
        ]


def make_core(*, strict_ok_after_filter=True):
    calls = []
    core = SimpleNamespace(
        REWORK_LABEL="agent-rework",
        WORKING_LABEL="agent-working",
        REVIEW_READY_LABEL="agent-review-ready",
        GithubCompletionError=RuntimeError,
        _parse_iso_ts=lambda value: int(value),
    )

    core._latest_rework_label_at = lambda client, ref, pr_number: max(
        [
            int(item["created_at"])
            for item in client.get_paginated(
                f"/repos/{ref.repository}/issues/{pr_number}/timeline"
            )
        ]
        or [0]
    )

    core._reconcile_rework_lifecycle = lambda *args, **kwargs: {
        "task_id": "t_ac34f08d",
        "status": "review",
        "changed": False,
        "reason": "rework_retry_pending",
        "retry_required": True,
        "attention_at": 201,
        "lifecycle": {"labels": ["agent-rework"]},
    }

    def delivery_evidence(conn, client, ref, task_id, pr, event):
        items = client.get_paginated(
            f"/repos/{ref.repository}/issues/103/timeline"
        )
        times = [
            int(item["created_at"])
            for item in items
            if item.get("event") == "labeled"
        ]
        calls.append(times)
        if max(times or [0]) > 100:
            return False, "delivery_superseded_by_new_rework", {"rework_at": 100}
        if strict_ok_after_filter:
            return True, "delivery_complete_verification_only", {
                "head": "a" * 40,
                "run_id": 578,
            }
        return False, "completion_handoff_missing", {"run_id": 578}

    core._rework_delivery_evidence = delivery_evidence
    install(core)
    return core, calls


def context(*, direct=False):
    return {
        "labels": {"agent-rework"},
        "pr": SimpleNamespace(state="open"),
        "pr_number": 103,
        "event": (
            {
                "source": "github" if direct else "github_edge_rework_recovery",
                "rework_round": 2 if direct else 1,
            },
            202 if direct else 200,
            "github_pr_rework" if direct else "github_pr_rework_retry",
        ),
    }


def invoke(core, client, ctx):
    return core._reconcile_rework_lifecycle(
        None,
        None,
        client,
        SimpleNamespace(repository="rhgo1749/ctrl-hangul"),
        None,
        {"id": "t_ac34f08d", "status": "review"},
        ctx,
        dry_run=True,
        failure_limit=None,
    )


def test_attention_selfheal_label_is_hidden_for_one_strict_retry():
    core, calls = make_core(strict_ok_after_filter=True)
    result = invoke(core, FakeClient([99, 200]), context())
    assert result["reason"] == "agent_review_ready_predicted"
    assert result["evidence"]["run_id"] == 578
    assert result["evidence"]["attention_selfheal_label_recovered"] is True
    assert result["evidence"]["attention_at"] == 201
    assert result["evidence"]["selfheal_label_at"] == 200
    assert calls == [[99, 200], [99]]


def test_label_added_after_attention_is_never_hidden():
    core, calls = make_core(strict_ok_after_filter=True)
    result = invoke(core, FakeClient([99, 202]), context())
    assert result["reason"] == "rework_retry_pending"
    assert calls == [[99, 202]]


def test_direct_new_rework_event_never_uses_attention_bypass():
    core, calls = make_core(strict_ok_after_filter=True)
    result = invoke(core, FakeClient([99, 200]), context(direct=True))
    assert result["reason"] == "rework_retry_pending"
    assert calls == [[99, 200]]


def test_downstream_strict_delivery_failure_remains_fail_closed():
    core, calls = make_core(strict_ok_after_filter=False)
    result = invoke(core, FakeClient([99, 200]), context())
    assert result["reason"] == "rework_retry_pending"
    assert calls == [[99, 200], [99]]


if __name__ == "__main__":
    tests = [
        test_attention_selfheal_label_is_hidden_for_one_strict_retry,
        test_label_added_after_attention_is_never_hidden,
        test_direct_new_rework_event_never_uses_attention_bypass,
        test_downstream_strict_delivery_failure_remains_fail_closed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
