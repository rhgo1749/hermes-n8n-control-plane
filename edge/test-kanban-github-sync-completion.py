#!/usr/bin/env python3
"""Focused completion/supersession regressions for kanban-github-sync.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, "/ws/hermes-agent")

SCRIPT = Path(__file__).resolve().parent / "kanban-github-sync.py"
spec = importlib.util.spec_from_file_location("kanban_github_sync_completion", SCRIPT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

REPO = "rhgo1749/H4V3-DJ"
ISSUE = 49
REF = mod.GithubTaskRef(REPO, ISSUE, "main", "talking-state")


def pr(
    number: int,
    *,
    state: str = "closed",
    merged: bool = False,
    head: str = "h4v3-dj/t_23c871a3-github-issue-intake-rhgo1749-h4v3-dj-49",
    base: str = "main",
):
    return mod.GithubPullRequest(
        number=number,
        state=state,
        merged=merged,
        base_branch=base,
        html_url=f"https://github.com/{REPO}/pull/{number}",
        title=f"PR {number}",
        head_sha=f"sha-{number}",
        head_ref=head,
        author="rhgo1749",
        body="",
        draft=False,
    )


def assert_decision(name, decision, status, reason):
    assert decision.desired_status == status, (
        name,
        decision.desired_status,
        decision.reason,
    )
    assert decision.reason == reason, (
        name,
        decision.desired_status,
        decision.reason,
    )
    print(f"PASS {name}: {status}/{reason}")


def payload(p):
    return {
        "number": p.number,
        "state": p.state,
        "merged": p.merged,
        "draft": p.draft,
        "base": {"ref": p.base_branch},
        "head": {"sha": p.head_sha, "ref": p.head_ref},
        "title": p.title,
        "user": {"login": p.author},
        "body": p.body,
        "html_url": p.html_url,
    }


class FakeGitHub:
    def __init__(self, issue_state: str, prs):
        self.issue_state = issue_state
        self.prs = {p.number: p for p in prs}
        self.issue_gets = 0

    def get_paginated(self, path, params=None, max_pages=10):
        assert path.endswith(f"/issues/{ISSUE}/timeline")
        return [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": number,
                        "pull_request": {
                            "url": f"https://api.github.com/repos/{REPO}/pulls/{number}"
                        },
                        "html_url": f"https://github.com/{REPO}/pull/{number}",
                        "repository": {"full_name": REPO},
                    }
                },
            }
            for number in sorted(self.prs)
        ]

    def get(self, path, params=None):
        if path.endswith(f"/issues/{ISSUE}"):
            self.issue_gets += 1
            return {"number": ISSUE, "state": self.issue_state}, {}

        marker = "/pulls/"
        if marker in path:
            number = int(path.rsplit("/", 1)[1])
            return payload(self.prs[number]), {}

        raise AssertionError(f"unexpected GET: {path}")


old = pr(78, merged=False)
replacement = pr(94, merged=True)

assert_decision(
    "closed issue accepts newer same-head merged replacement",
    mod.evaluate_completion(
        REF,
        (old, replacement),
        linked_pr_numbers=(78, 94),
        issue_state="closed",
    ),
    "done",
    "superseded_pr_merged",
)

assert_decision(
    "open issue does not accept supersession completion",
    mod.evaluate_completion(
        REF,
        (old, replacement),
        linked_pr_numbers=(78, 94),
        issue_state="open",
    ),
    "review",
    "linked_pr_closed_not_merged",
)

different_head = pr(94, merged=True, head="h4v3-dj/unrelated")
assert_decision(
    "different-head merge cannot supersede stale PR",
    mod.evaluate_completion(
        REF,
        (old, different_head),
        linked_pr_numbers=(78, 94),
        issue_state="closed",
    ),
    "review",
    "linked_pr_closed_not_merged",
)

extra_open = pr(95, state="open", merged=False, head="h4v3-dj/other")
assert_decision(
    "open linked PR keeps review authoritative",
    mod.evaluate_completion(
        REF,
        (old, replacement, extra_open),
        linked_pr_numbers=(78, 94, 95),
        issue_state="closed",
    ),
    "review",
    "linked_pr_open",
)

assert_decision(
    "existing all-linked-merged contract remains",
    mod.evaluate_completion(
        REF,
        (pr(78, merged=True), pr(94, merged=True)),
        linked_pr_numbers=(78, 94),
        issue_state="closed",
    ),
    "done",
    "all_linked_prs_merged",
)

assert_decision(
    "single closed-unmerged PR remains review",
    mod.evaluate_completion(
        REF,
        (old,),
        linked_pr_numbers=(78,),
        issue_state="closed",
    ),
    "review",
    "linked_pr_closed_not_merged",
)

fake_closed = FakeGitHub("closed", (old, replacement))
assert_decision(
    "verify_completion fresh-reads closed Issue for candidate",
    mod.verify_completion(fake_closed, REF),
    "done",
    "superseded_pr_merged",
)
assert fake_closed.issue_gets == 1
print("PASS verify_completion queried Issue exactly once for candidate")

fake_open = FakeGitHub("open", (old, replacement))
assert_decision(
    "verify_completion preserves review for open Issue",
    mod.verify_completion(fake_open, REF),
    "review",
    "linked_pr_closed_not_merged",
)
assert fake_open.issue_gets == 1
print("PASS open candidate queried Issue exactly once")

fake_unrelated = FakeGitHub("closed", (old, different_head))
assert_decision(
    "verify_completion avoids unnecessary Issue GET without supersession proof",
    mod.verify_completion(fake_unrelated, REF),
    "review",
    "linked_pr_closed_not_merged",
)
assert fake_unrelated.issue_gets == 0
print("PASS unrelated PR set added no Issue API call")

print("ALL COMPLETION REGRESSIONS PASS")
