#!/usr/bin/env python3
"""Focused completion/supersession regressions for kanban-github-sync.py."""

from __future__ import annotations

import importlib.util
import json
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
    closes_issue: bool = True,
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
        closing_issue_numbers=frozenset({ISSUE}) if closes_issue else frozenset(),
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
    def __init__(self, issue_state: str, prs, *, relationship_error=False,
                 relationship_response=None):
        self.issue_state = issue_state
        self.prs = {p.number: p for p in prs}
        self.issue_gets = 0
        self.relationship_calls = 0
        self.relationship_error = relationship_error
        self.relationship_response = relationship_response

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

    def graphql(self, query, variables):
        assert "closingIssuesReferences" in query
        self.relationship_calls += 1
        if self.relationship_error:
            raise mod.GithubCompletionError("simulated relationship lookup failure")
        if self.relationship_response is not None:
            return self.relationship_response
        number = int(variables["number"])
        relation = self.prs[number].closing_issue_numbers
        assert relation is not None
        return {
            "repository": {
                "pullRequest": {
                    "number": number,
                    "closingIssuesReferences": {
                        "nodes": [{"number": item} for item in sorted(relation)],
                        "pageInfo": {"hasNextPage": False},
                    },
                },
            },
        }


class _GraphQLResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


graphql_calls = []
original_urlopen = mod.urlopen

def fake_urlopen(request, timeout):
    graphql_calls.append((request, timeout))
    return _GraphQLResponse({"data": {"repository": {"pullRequest": {}}}})

mod.__dict__["urlopen"] = fake_urlopen
try:
    graphql_client = mod.GithubApiClient("test-token", timeout=7)
    graphql_client.graphql("query { repository { pullRequest { number } } }", {
        "owner": "rhgo1749",
        "name": "H4V3-DJ",
        "number": 14,
    })
finally:
    mod.__dict__["urlopen"] = original_urlopen
assert len(graphql_calls) == 1
request, request_timeout = graphql_calls[0]
assert request.full_url == "https://api.github.com/graphql"
assert request.get_method() == "POST"
assert request.get_header("Authorization") == "Bearer test-token"
assert request_timeout == 7
assert json.loads(request.data.decode("utf-8"))["variables"]["number"] == 14
print("PASS GraphQL client posts bounded closing-reference request")

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

merged_closer = pr(14, merged=True, closes_issue=True)
unrelated_open_mention = pr(
    15,
    state="open",
    merged=False,
    head="h4v3-dj/issue-12",
    closes_issue=False,
)
fake_mention = FakeGitHub("closed", (merged_closer, unrelated_open_mention))
assert_decision(
    "closed issue ignores unrelated open cross-reference mention",
    mod.verify_completion(fake_mention, REF),
    "done",
    "all_linked_prs_merged",
)
assert fake_mention.relationship_calls == 2
print("PASS relationship evidence filtered unrelated OPEN mention")

unrelated_closed_mention = pr(
    15,
    state="closed",
    merged=False,
    head="h4v3-dj/issue-12",
    closes_issue=False,
)
fake_supersession_with_mention = FakeGitHub(
    "closed", (old, replacement, unrelated_closed_mention)
)
assert_decision(
    "closed supersession ignores unrelated closed cross-reference mention",
    mod.verify_completion(fake_supersession_with_mention, REF),
    "done",
    "superseded_pr_merged",
)
assert fake_supersession_with_mention.issue_gets == 1
print("PASS supersession filters unrelated closed mention before Issue lookup")

actual_open_closer = pr(15, state="open", merged=False, closes_issue=True)
fake_open_closer = FakeGitHub("closed", (merged_closer, actual_open_closer))
assert_decision(
    "actual open closing PR remains review authority",
    mod.verify_completion(fake_open_closer, REF),
    "review",
    "linked_pr_open",
)
assert fake_open_closer.relationship_calls == 2
print("PASS authoritative OPEN closer remains blocking")

fake_relationship_failure = FakeGitHub(
    "closed", (merged_closer, unrelated_open_mention), relationship_error=True
)
failed_relationship = mod.verify_completion(fake_relationship_failure, REF)
assert failed_relationship.desired_status is None
assert failed_relationship.reason == "github_query_failed"
assert fake_relationship_failure.relationship_calls == 1
print("PASS relationship lookup failure remains github_query_failed")

fake_relationship_malformed = FakeGitHub(
    "closed", (merged_closer,),
    relationship_response={"repository": {"pullRequest": {"number": 14}}},
)
malformed_relationship = mod.verify_completion(fake_relationship_malformed, REF)
assert malformed_relationship.desired_status is None
assert malformed_relationship.reason == "github_query_failed"
print("PASS malformed relationship response remains github_query_failed")

fake_relationship_paged = FakeGitHub(
    "closed", (merged_closer,),
    relationship_response={
        "repository": {
            "pullRequest": {
                "number": 14,
                "closingIssuesReferences": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True},
                },
            },
        },
    },
)
paged_relationship = mod.verify_completion(fake_relationship_paged, REF)
assert paged_relationship.desired_status is None
assert paged_relationship.reason == "github_query_failed"
print("PASS unbounded relationship response remains github_query_failed")

print("ALL COMPLETION REGRESSIONS PASS")
