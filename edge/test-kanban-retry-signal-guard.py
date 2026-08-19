#!/usr/bin/env python3
"""Focused regression tests for the explicit retry whole-comment guard."""
from __future__ import annotations

from types import SimpleNamespace

from kanban_retry_signal_guard import install_retry_signal_guard


class FakeClient:
    def __init__(self, comments):
        self.comments = comments

    def get_paginated(self, path, params=None, max_pages=10):
        return list(self.comments)


def make_comment(body: str, *, n: int, author: str = "rhgo1749", ts: str = "new"):
    return {
        "id": n,
        "user": {"login": author},
        "body": body,
        "created_at": ts,
    }


def make_core():
    core = SimpleNamespace(
        REWORK_RETRY_MARKER="AGENT_REWORK_RETRY",
        TRUSTED_GITHUB_ACTORS={"rhgo1749"},
        _parse_iso_ts=lambda value: {"old": 10, "new": 30}.get(value),
        _find_rework_retry_signal=lambda *args, **kwargs: {"old": True},
    )
    install_retry_signal_guard(core)
    return core


def find(core, comments, *, task_id="t_aff9017c", consumed=None):
    return core._find_rework_retry_signal(
        FakeClient(comments),
        SimpleNamespace(repository="rhgo1749/ctrl-hangul"),
        74,
        task_id,
        baseline_at=20,
        consumed_ids=set(consumed or ()),
    )


def test_edge_attention_help_text_is_not_retry():
    core = make_core()
    body = "\n".join([
        "HERMES_KANBAN_REWORK_ATTENTION task=t_aff9017c reason=delivery_run_failed",
        "",
        "This rework round's delivery could not be accepted automatically.",
        "",
        "To open a NEW rework round instead, a trusted maintainer posts:",
        "",
        "AGENT_REWORK_RETRY",
        "task=t_aff9017c",
        "",
        "Each signal is consumed exactly once.",
    ])
    assert find(core, [make_comment(body, n=5327810201)]) is None


def test_exact_two_line_retry_is_accepted():
    core = make_core()
    result = find(core, [make_comment("AGENT_REWORK_RETRY\ntask=t_aff9017c", n=9001)])
    assert result == {
        "comment_id": 9001,
        "author": "rhgo1749",
        "created_at": 30,
    }


def test_extra_prose_or_wrong_task_is_rejected():
    core = make_core()
    prose = "please retry\nAGENT_REWORK_RETRY\ntask=t_aff9017c"
    wrong = "AGENT_REWORK_RETRY\ntask=t_other"
    assert find(core, [make_comment(prose, n=9002)]) is None
    assert find(core, [make_comment(wrong, n=9003)]) is None


def test_existing_trust_time_and_one_shot_guards_remain():
    core = make_core()
    exact = "AGENT_REWORK_RETRY\ntask=t_aff9017c"
    assert find(core, [make_comment(exact, n=9004, author="someone-else")]) is None
    assert find(core, [make_comment(exact, n=9005, ts="old")]) is None
    assert find(core, [make_comment(exact, n=9006)], consumed={9006}) is None


def test_install_is_idempotent():
    core = make_core()
    first = core._find_rework_retry_signal
    install_retry_signal_guard(core)
    assert core._find_rework_retry_signal is first


if __name__ == "__main__":
    tests = [
        test_edge_attention_help_text_is_not_retry,
        test_exact_two_line_retry_is_accepted,
        test_extra_prose_or_wrong_task_is_rejected,
        test_existing_trust_time_and_one_shot_guards_remain,
        test_install_is_idempotent,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
