#!/usr/bin/env python3
"""Regression tests for the merged-linked-PR intake guard.

An OPEN ``agent-ready`` Issue is completed ONLY when a merged PR actually
closes it (GitHub's own closing relationship via
``PullRequest.closingIssuesReferences``). A bare ``cross-referenced`` timeline
event — a mention like "follow-up #72" — is NEVER completion evidence.

Regression cases (ctrl-hangul#72 hotfix):
  CASE 1  merged PR merely mentions the issue -> agent-ready preserved, card created
  CASE 2  merged PR truly closes the issue      -> skip + label clear
  CASE 3  closing ref but PR unmerged           -> card created
  CASE 4  merged PR closes a DIFFERENT issue    -> this card created
  plus   GraphQL failure fails closed without touching labels.

Run with:

    /ws/hermes-agent/venv/bin/python3 tests/test_intake_merged_pr_guard.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake.py"
)

spec = importlib.util.spec_from_file_location(
    "github_agent_ready_kanban_intake_guard",
    MODULE_PATH,
)
assert spec and spec.loader
intake: Any = importlib.util.module_from_spec(spec)
assert isinstance(spec.loader, object)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)

REPO = "rhgo1749/ctrl-hangul"

PASS: list[str] = []
FAIL: list[str] = []

_ORIG = {
    name: getattr(intake, name)
    for name in (
        "_github_json", "_github_patch_json", "_repo_snapshot", "_board_slugs",
        "_sync_board", "_create_task", "_issue_candidates",
    )
}


def restore_all() -> None:
    for name, fn in _ORIG.items():
        setattr(intake, name, fn)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _config() -> Any:
    return intake.RepositoryConfig(
        name=REPO,
        board="ctrlhangul",
        checkout="/tmp/repo",
        default_branch="main",
        contract_paths=(),
        display_name=REPO.split("/", 1)[1],
    )


def _open_issue(number: int) -> dict:
    return {
        "number": number,
        "state": "open",
        "title": f"task {number}",
        "labels": [{"name": "agent-ready"}],
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }


class FakeGitHub:
    """Patches module-level GitHub helpers; routes GraphQL by fixture."""

    def __init__(self, *, graphql_data=None, graphql_error=False):
        self.graphql_data = graphql_data if graphql_data is not None else {"repository": {"issue": None}}
        self.graphql_error = graphql_error
        self.patch_calls: list[tuple[str, dict]] = []
        self.created: list[int] = []
        self.queries: list[dict] = []
        self.candidates = [(_config(), _open_issue(30))]

    def get(self, token, path, params):
        # closed-issue cleanup GET etc. -> empty
        return [], {}

    def graphql(self, token, query, variables):
        self.queries.append(dict(variables))
        if self.graphql_error:
            raise intake.IntakeError("GraphQL down")
        return self.graphql_data

    def patch(self, token, path, payload):
        self.patch_calls.append((path, dict(payload)))
        return 200, None

    def install(self) -> FakeGitHub:
        intake._github_json = self.get
        intake._github_graphql = self.graphql
        intake._github_patch_json = self.patch
        intake._issue_candidates = lambda token, fixture_path, configs: list(self.candidates)
        intake._repo_snapshot = lambda config: intake.RepoSnapshot("sha", "remote", ())
        intake._board_slugs = lambda: {"ctrlhangul"}
        intake._sync_board = lambda config, token, **kw: []
        return self


def _pr_node(number: int, merged: bool, closes_issue: int | None) -> dict:
    refs = {"nodes": [{"number": closes_issue}]} if closes_issue else {"nodes": []}
    return {
        "source": {"number": number, "merged": merged, "closingIssuesReferences": refs}
    }


def _args(*, dry_run: bool = False) -> Any:
    return SimpleNamespace(
        dry_run=dry_run,
        fixture_json=None,
        repository=None,
    )


def _stub_provisioning() -> None:
    intake._repository_configs_from_registry = lambda snapshot, repository: (
        ((_config(),), [])
    )
    intake._select_repositories = lambda configs, repository: configs
    intake._load_registry_snapshot = lambda token: {"repositories": []}
    intake._provision_bootstrap_boards = lambda snapshot, **kwargs: []
    intake._claim_wake_scope = lambda: None


def test_case1_plain_merged_crossref_never_clears_agent_ready() -> None:
    """CASE 1 (ctrl-hangul#72): PR #74 mentions Issue #72 ("follow-up") and is
    merged, but does NOT close it. GitHub proves no closing relationship ->
    agent-ready MUST be preserved and the card created."""
    _stub_provisioning()
    print("CASE 1: plain merged cross-reference -> agent-ready preserved, card created")
    restore_all()
    fake = FakeGitHub(graphql_data={"repository": {"issue": {"timelineItems": {
        "nodes": [_pr_node(74, merged=True, closes_issue=None)],
    }}}}).install()

    def fake_create(config, issue_, snapshot, imported_at, *, tick_started):
        fake.created.append(issue_["number"])
        return {}

    intake._create_task = fake_create
    intake._run(_args())

    check("card created (still an intake candidate)", fake.created == [30], str(fake.created))
    check("no label PATCH at all", fake.patch_calls == [], str(fake.patch_calls))


def test_case2_true_closing_relation_skips_and_clears() -> None:
    """CASE 2: a merged PR really closes the issue (closingIssuesReferences
    contains it) -> re-intake guard applies: skip + label clear."""
    _stub_provisioning()
    print("CASE 2: true closing relation + merged -> skip card, clear labels")
    restore_all()
    fake = FakeGitHub(graphql_data={"repository": {"issue": {"timelineItems": {
        "nodes": [_pr_node(88, merged=True, closes_issue=30)],
    }}}}).install()
    intake._create_task = lambda *a, **kw: fake.created.append(0) or {}
    intake._run(_args())

    check("no card created", fake.created == [], str(fake.created))
    check("labels cleared atomically",
          fake.patch_calls == [(f"/repos/{REPO}/issues/30", {"labels": []})],
          str(fake.patch_calls))


def test_case3_unmerged_closing_ref_still_creates_card() -> None:
    """CASE 3: closing relationship exists but the PR is NOT merged yet ->
    work remains; card must still be created."""
    _stub_provisioning()
    print("CASE 3: unmerged closing-ref PR -> card created, labels untouched")
    restore_all()
    fake = FakeGitHub(graphql_data={"repository": {"issue": {"timelineItems": {
        "nodes": [_pr_node(90, merged=False, closes_issue=30)],
    }}}}).install()

    def fake_create(config, issue_, snapshot, imported_at, *, tick_started):
        fake.created.append(issue_["number"])
        return {}

    intake._create_task = fake_create
    intake._run(_args())

    check("card created for unmerged closer", fake.created == [30], str(fake.created))
    check("labels untouched", fake.patch_calls == [], str(fake.patch_calls))


def test_case4_multi_mention_pr_only_closes_declared_issue() -> None:
    """CASE 4: 'Implements #71, follow-up #72' — one PR cross-references both
    issues but its closingIssuesReferences only contains #71-equivalent (the
    other task). For THIS task (#30) nothing is proven closed -> card stays."""
    _stub_provisioning()
    print("CASE 4: multi-mention PR closing a DIFFERENT issue -> this card created")
    restore_all()
    fake = FakeGitHub(graphql_data={"repository": {"issue": {"timelineItems": {
        "nodes": [_pr_node(92, merged=True, closes_issue=71)],
    }}}}).install()

    def fake_create(config, issue_, snapshot, imported_at, *, tick_started):
        fake.created.append(issue_["number"])
        return {}

    intake._create_task = fake_create
    intake._run(_args())

    check("card created (other issue was closed, not this one)",
          fake.created == [30], str(fake.created))
    check("labels untouched", fake.patch_calls == [], str(fake.patch_calls))


def test_lookup_failure_fails_closed_without_clearing_labels() -> None:
    """GraphQL failure must fail-closed: no labels cleared, error raised."""
    _stub_provisioning()
    print("GraphQL lookup failure -> fail-closed, labels never cleared")
    restore_all()
    fake = FakeGitHub(graphql_error=True).install()
    intake._create_task = lambda *a, **kw: fake.created.append(0) or {}

    raised = False
    try:
        intake._run(_args())
    except intake.IntakeError as exc:
        raised = True
        assert "closing-relationship lookup failed" in str(exc)

    check("IntakeError raised", raised)
    check("no label PATCH on failure", fake.patch_calls == [], str(fake.patch_calls))
    check("no card created on failure", fake.created == [], str(fake.created))


def main() -> int:
    tests = [
        test_case1_plain_merged_crossref_never_clears_agent_ready,
        test_case2_true_closing_relation_skips_and_clears,
        test_case3_unmerged_closing_ref_still_creates_card,
        test_case4_multi_mention_pr_only_closes_declared_issue,
        test_lookup_failure_fails_closed_without_clearing_labels,
    ]
    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
