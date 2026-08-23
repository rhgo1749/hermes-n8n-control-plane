#!/usr/bin/env python3
"""Regression tests for the merged-linked-PR intake guard.

An OPEN ``agent-ready`` Issue whose every linked PR is already merged has no
remaining automated work. The intake must not create a card for it; it must
clear the ``agent-ready`` label (same atomic label-clear contract as the
closed-Issue cleanup) so the verify-only re-intake cycle cannot recur.

Run with:

    /ws/hermes-agent/venv/bin/python3 tests/test_intake_merged_pr_guard.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    """Patches module-level GitHub helpers with timeline/pull routing."""

    def __init__(self, *, linked_pr: dict | None):
        self.linked_pr = linked_pr
        self.patch_calls: list[tuple[str, dict]] = []
        self.created: list[int] = []
        self.candidates = [(_config(), _open_issue(30))]

    def get(self, token, path, params):
        if "/timeline" in path:
            if self.linked_pr is None:
                return [], {}
            return [{
                "event": "cross-referenced",
                "source": {"issue": {
                    "number": self.linked_pr["number"],
                    "pull_request": {"url": "x"},
                }},
            }], {}
        if path.startswith(f"/repos/{REPO}/pulls/") and self.linked_pr:
            number = int(path.rsplit("/", 1)[1])
            if number == self.linked_pr["number"]:
                return dict(self.linked_pr), {}
        # closed-issue cleanup GET: state=closed -> empty
        return [], {}

    def patch(self, token, path, payload):
        self.patch_calls.append((path, dict(payload)))
        return 200, None

    def install(self) -> "FakeGitHub":
        intake._github_json = self.get
        intake._github_patch_json = self.patch
        intake._issue_candidates = lambda token, fixture_path, configs: list(self.candidates)
        intake._repo_snapshot = lambda config: intake.RepoSnapshot("sha", "remote", ())
        # snapshots[config.name] must resolve for every candidate
        intake._board_slugs = lambda: {"ctrlhangul"}
        intake._sync_board = lambda config, token, **kw: []
        return self


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
    intake._provision_bootstrap_boards = lambda snapshot, *, dry_run, scope=None: []
    intake._claim_wake_scope = lambda: None


def test_merged_linked_pr_skips_card_and_clears_labels() -> None:
    _stub_provisioning()
    print("merged linked PR -> no card created, agent-ready label cleared")
    restore_all()
    fake = FakeGitHub(linked_pr={
        "number": 77, "merged": True, "state": "closed",
        "base": {"ref": "main"},
    }).install()
    intake._create_task = lambda *a, **kw: fake.created.append(0) or {}

    intake._run(_args())

    check("no card created", fake.created == [], str(fake.created))
    label_patches = [
        (p, payload) for p, payload in fake.patch_calls
        if p == f"/repos/{REPO}/issues/30"
    ]
    check("labels cleared atomically",
          label_patches == [(f"/repos/{REPO}/issues/30", {"labels": []})],
          str(fake.patch_calls))


def test_open_linked_pr_still_creates_card() -> None:
    _stub_provisioning()
    print("OPEN (unmerged) linked PR -> card still created, labels untouched")
    restore_all()
    fake = FakeGitHub(linked_pr={
        "number": 78, "merged": False, "state": "open",
        "base": {"ref": "main"},
    }).install()

    def fake_create(config, issue_, snapshot, imported_at, *, tick_started):
        fake.created.append(issue_["number"])
        return {"key": f"github:{config.name}:issue:{issue_['number']}"}

    intake._create_task = fake_create
    intake._run(_args())

    check("card created for open-PR issue", fake.created == [30], str(fake.created))
    label_patches = [p for p, _ in fake.patch_calls]
    check("labels untouched", label_patches == [], str(fake.patch_calls))


def test_no_linked_pr_still_creates_card() -> None:
    _stub_provisioning()
    print("no cross-referenced PRs -> normal creation path unchanged")
    restore_all()
    fake = FakeGitHub(linked_pr=None).install()

    def fake_create(config, issue_, snapshot, imported_at, *, tick_started):
        fake.created.append(issue_["number"])
        return {"key": f"github:{config.name}:issue:{issue_['number']}"}

    intake._create_task = fake_create
    intake._run(_args())

    check("card created without linked PRs", fake.created == [30], str(fake.created))
    check("no PATCH calls", fake.patch_calls == [], str(fake.patch_calls))


def main() -> int:
    tests = [
        test_merged_linked_pr_skips_card_and_clears_labels,
        test_open_linked_pr_still_creates_card,
        test_no_linked_pr_still_creates_card,
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
