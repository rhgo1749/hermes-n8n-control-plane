#!/usr/bin/env python3
"""Focused regression for trusted closed/completed Issue terminal fallback."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EDGE = Path(__file__).resolve().parent
CORE = EDGE / "kanban-github-sync.py"
GUARD = EDGE / "kanban_retry_signal_guard.py"

core_spec = importlib.util.spec_from_file_location("kanban_closed_terminal_core", CORE)
assert core_spec is not None and core_spec.loader is not None
core = importlib.util.module_from_spec(core_spec)
sys.modules[core_spec.name] = core
core_spec.loader.exec_module(core)

guard_spec = importlib.util.spec_from_file_location("kanban_closed_terminal_guard", GUARD)
assert guard_spec is not None and guard_spec.loader is not None
guard = importlib.util.module_from_spec(guard_spec)
sys.modules[guard_spec.name] = guard
guard_spec.loader.exec_module(guard)

REF = core.GithubTaskRef("rhgo1749/hermes-n8n-control-plane", 106, "main", "incident")


class FakeGitHub:
    def __init__(self, payload=None, *, fail=False):
        self.payload = payload
        self.fail = fail
        self.issue_gets = 0

    def get(self, path, params=None):
        assert path == "/repos/rhgo1749/hermes-n8n-control-plane/issues/106"
        self.issue_gets += 1
        if self.fail:
            raise core.GithubCompletionError("simulated issue lookup failure")
        return self.payload, {}


def base_verify(client, ref, text_sources=()):
    assert ref == REF
    return core.GithubCompletionDecision(
        desired_status="review",
        reason="no_linked_pr",
        linked_pr_numbers=(107,),
        pull_requests=(),
    )


core.verify_completion = base_verify
guard.install_closed_completed_terminal_fallback(core)

trusted_closed = FakeGitHub({
    "number": 106,
    "state": "closed",
    "state_reason": "completed",
    "closed_by": {"login": "rhgo1749"},
})
result = core.verify_completion(trusted_closed, REF)
assert result.desired_status == "done", result
assert result.reason == "trusted_issue_completed", result
assert result.linked_pr_numbers == (107,), result
assert trusted_closed.issue_gets == 1
print("PASS trusted maintainer closed/completed no-linked-pr fallback -> done")

untrusted_closed = FakeGitHub({
    "number": 106,
    "state": "closed",
    "state_reason": "completed",
    "closed_by": {"login": "someone-else"},
})
result = core.verify_completion(untrusted_closed, REF)
assert result.desired_status == "review", result
assert result.reason == "no_linked_pr", result
print("PASS untrusted closer preserves review")

not_planned = FakeGitHub({
    "number": 106,
    "state": "closed",
    "state_reason": "not_planned",
    "closed_by": {"login": "rhgo1749"},
})
result = core.verify_completion(not_planned, REF)
assert result.desired_status == "review", result
assert result.reason == "no_linked_pr", result
print("PASS closed/not_planned preserves review")

open_issue = FakeGitHub({
    "number": 106,
    "state": "open",
    "state_reason": None,
    "closed_by": None,
})
result = core.verify_completion(open_issue, REF)
assert result.desired_status == "review", result
assert result.reason == "no_linked_pr", result
print("PASS open Issue preserves review")

lookup_failure = FakeGitHub(fail=True)
result = core.verify_completion(lookup_failure, REF)
assert result.desired_status is None, result
assert result.reason == "github_query_failed", result
assert "simulated issue lookup failure" in str(result.error)
print("PASS fallback lookup failure remains fail-closed")

# A real authoritative PR decision must never incur the Issue fallback lookup.
def merged_verify(client, ref, text_sources=()):
    return core.GithubCompletionDecision(
        desired_status="done",
        reason="all_linked_prs_merged",
        linked_pr_numbers=(107,),
        pull_requests=(),
    )

core.verify_completion = merged_verify
# Re-arm installer against the replaced verifier to validate wrapper scope.
core._closed_completed_terminal_fallback_installed = False
guard.install_closed_completed_terminal_fallback(core)
client = FakeGitHub({})
result = core.verify_completion(client, REF)
assert result.desired_status == "done", result
assert result.reason == "all_linked_prs_merged", result
assert client.issue_gets == 0
print("PASS existing authoritative completion bypasses fallback lookup")

print("ALL CLOSED-COMPLETED TERMINAL FALLBACK REGRESSIONS PASS")
