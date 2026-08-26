#!/usr/bin/env python3
"""Regression checks for the GitHub intake lifecycle-contract wrapper."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake-entrypoint.py"
)

spec = importlib.util.spec_from_file_location("github_intake_entrypoint_test", ENTRYPOINT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _render_body() -> str:
    config = mod.RepositoryConfig(
        name="rhgo1749/example",
        board="example",
        checkout="/ws/projects/example",
        default_branch="main",
        contract_paths=("AGENTS.md",),
        display_name="example",
    )
    snapshot = mod.RepoSnapshot(
        origin_sha="0123456789abcdef",
        remote="https://github.com/rhgo1749/example.git",
        contract_paths=("AGENTS.md",),
    )
    issue = {
        "number": 42,
        "title": "Example issue",
        "body": "Implement the example.",
        "html_url": "https://github.com/rhgo1749/example/issues/42",
        "labels": [{"name": "agent-ready"}],
    }
    return mod._task_body(
        config,
        snapshot,
        issue,
        "github:rhgo1749/example:issue:42",
        "2026-08-19T00:00:00Z",
    )


def test_worker_completion_uses_core_terminal_action() -> None:
    body = _render_body()
    assert "must finish the worker run with core `kanban_complete`" in body
    assert "Do not call `kanban_request_review`" in body
    assert "core `done` transition is provisional" in body
    assert "projects the card to parked `review`" in body
    assert "Authoritative `done` requires a fresh GitHub API read" in body


def test_external_wait_never_keeps_worker_running() -> None:
    body = _render_body()
    assert "may remain RUNNING solely to wait for future GitHub Actions/checks" in body
    assert "never use `sleep` or repeated polling" in body
    assert "Pending future CI/checks, human review, merge, or comments" in body
    assert "Future PR lifecycle belongs to GitHub + edge reconciliation" in body


def test_required_acceptance_is_attempted_or_explained() -> None:
    body = _render_body()
    assert "must attempt every required gate that is executable" in body
    assert "browser acceptance must be performed before delivery" in body
    assert "`HUMAN_VALIDATION_REQUIRED` is not permission to skip an executable gate" in body
    assert "whether the required browser/tool/runtime was available" in body
    assert "what step was attempted" in body
    assert "bare `human validation required`, `not run`" in body
    assert "is REWORK" in body


def test_main_and_controller_ownership_are_separated() -> None:
    body = _render_body()
    assert "## Kanban lead orchestration contract" in body
    assert "Main is the planner/router/judge, not the default implementer" in body
    assert "Encode real dependencies before downstream work runs" in body
    assert "Reviewer does not create child rework tasks or manipulate dependencies" in body
    assert "without parent-linking to non-terminal reviewer cards" in body
    assert "That core done is provisional; authoritative done is owned by the edge" in body
    assert "Deterministic Controller/edge logic owns event intake" in body
    assert "Do not recreate controller behavior through agent reasoning loops" in body
    assert "## Luna lead execution contract" not in body


def test_legacy_review_handoff_contract_is_removed() -> None:
    body = _render_body()
    assert "status must be `review`, never `done`" not in body
    assert "Worker implementation completion is a review handoff" not in body
    assert "completion contract: github-pr" in body


def test_contract_drift_fails_closed() -> None:
    fake = ModuleType("fake_intake")
    fake._task_body = lambda *args, **kwargs: "unexpected contract"
    mod._install_completion_contract_overlay(fake)
    try:
        fake._task_body()
    except RuntimeError as exc:
        assert "completion contract drifted" in str(exc)
    else:
        raise AssertionError("contract drift must fail closed")


def test_lead_contract_drift_fails_closed() -> None:
    fake = ModuleType("fake_intake_lead")
    fake._task_body = lambda *args, **kwargs: mod._OLD_COMPLETION_CONTRACT
    mod._install_completion_contract_overlay(fake)
    config = SimpleNamespace(default_branch="main")
    try:
        fake._task_body(config)
    except RuntimeError as exc:
        assert "Kanban lead contract drifted" in str(exc)
    else:
        raise AssertionError("lead contract drift must fail closed")


# ---------------------------------------------------------------------------
# Issue #79: GitHub PR closing-reference authoring and verification contract.
#
# GitHub only closes an Issue when a merged PR body carries a closing keyword
# (Closes/Fixes/Resolves) adjacent to the Issue number as VISIBLE plain text.
# A backticked/fenced ``Closes #N`` line, or a word-adjacent mention such as
# ``Issue #N의``, never creates the ``PullRequest.closingIssuesReferences``
# relationship (ctrl-hangul#70 / PR #82 hotfix).
# ---------------------------------------------------------------------------


def test_rendered_body_carries_closing_reference_contract() -> None:
    body = _render_body()
    assert "## GitHub PR closing-reference contract (pre-handoff)" in body
    # The standalone plain-text example uses the actual source-Issue shape:
    # the fixture Issue is #42, so the rendered body must contain `Closes #42.`
    assert "Closes #42." in body
    assert "Closes #<issue-number>." in body
    # The termination rule is named explicitly.
    assert "whitespace or punctuation" in body
    # Word-adjacent mentions are explicitly NOT closing references.
    assert "Issue #N의" in body
    assert "NOT GitHub closing references" in body
    # Backticked/fenced forms are explicitly NOT accepted.
    assert "backticked or fenced" in body
    # Existing-PR-only REST read -> GraphQL verification -> PATCH -> read-back.
    assert "existing PR" in body
    assert "closingIssuesReferences" in body
    assert "REST JSON PATCH" in body
    assert "SAME PR" in body
    assert "fresh-read" in body
    # Forbidden operations are named.
    assert "No second PR" in body
    assert "no new PR" in body
    assert "no forced" in body
    assert "auto-merge" in body
    assert "post-merge Issue close" in body
    # Fail-closed wording.
    assert "fail-closed" in body
    for failure in (
        "Source Issue or PR identity ambiguity",
        "GraphQL lookup failure",
        "REST lookup failure",
        "PATCH failure",
        "failed post-PATCH read-back",
    ):
        assert failure in body


def test_closing_reference_line_is_visible_plain_text() -> None:
    """The rendered example lines must survive fence/inline-span stripping:
    they are visible plain text, not code."""
    body = _render_body()
    visible = mod._closing_visible_lines(body)
    text = "\n".join(visible)
    assert "Closes #42." in text
    assert "Closes #<issue-number>." in text
    assert mod.body_has_valid_closing_reference(body, 42) is True


def test_closing_reference_helper_accepts_only_plain_text() -> None:
    check = mod.body_has_valid_closing_reference
    # Valid: visible plain-text keyword lines (any case), any termination.
    assert check("Closes #42.", 42) is True
    assert check("closes #42", 42) is True
    assert check("Fixes #42\n", 42) is True
    assert check("Resolves #42, and more", 42) is True
    assert check("\n  Closes #42.\n", 42) is True
    # Word-adjacent mentions are ordinary mentions, never closing references.
    assert check("Issue #42의 계정", 42) is False
    assert check("follow-up #42", 42) is False
    # Extra digits after the number name a DIFFERENT Issue.
    assert check("Closes #420", 42) is False
    assert check("Closes #420", 420) is True
    # A word character directly after the number is not termination.
    assert check("Closes #42abc", 42) is False
    assert check("Closes #42의 계정", 42) is False
    assert check("Closes #42_foo", 42) is False
    # No `#` is not a closing reference.
    assert check("Closes 42", 42) is False
    # Backticked / fenced forms are never accepted.
    assert check("`Closes #42`", 42) is False
    assert check("```text\nCloses #42.\n```", 42) is False
    assert check("~~~\nCloses #42.\n~~~", 42) is False
    assert check("````\nCloses #42.\n```\nCloses #42.\n````", 42) is False
    assert check("```\n```not-a-close\nCloses #42.\n```", 42) is False


def test_overlay_and_canonical_closing_reference_wording_synced() -> None:
    """The overlay validates and emits the canonical core's one contract.

    The wording intentionally lives only in the canonical implementation;
    duplicating it in the deployed wrapper would create a second source of
    truth.
    """
    canonical = mod._core._CLOSING_REFERENCE_CONTRACT
    assert isinstance(canonical, str) and canonical
    assert canonical in _render_body()


def test_closing_reference_drift_fails_closed() -> None:
    """A core body that carries the completion + lead contracts but is
    missing the closing-reference contract must be refused."""
    fake = ModuleType("fake_intake_closing")
    rendered = (
        mod._OLD_COMPLETION_CONTRACT
        + "\n"
        + mod._OLD_LEAD_CONTRACT_TEMPLATE.format(default_branch="main")
    )
    setattr(fake, "_task_body", lambda *args, **kwargs: rendered)
    setattr(fake, "_CLOSING_REFERENCE_CONTRACT", mod._core._CLOSING_REFERENCE_CONTRACT)
    mod._install_completion_contract_overlay(fake)
    config = SimpleNamespace(default_branch="main")
    try:
        fake._task_body(config)
    except RuntimeError as exc:
        assert "closing-reference contract drifted" in str(exc)
    else:
        raise AssertionError("closing-reference contract drift must fail closed")


def test_closing_reference_missing_from_core_fails_closed() -> None:
    """A legacy core without the canonical contract cannot be deployed via
    the live entrypoint, even if its other contract blocks still exist."""
    fake = ModuleType("fake_intake_closing_missing")
    rendered = (
        mod._OLD_COMPLETION_CONTRACT
        + "\n"
        + mod._OLD_LEAD_CONTRACT_TEMPLATE.format(default_branch="main")
    )
    setattr(fake, "_task_body", lambda *args, **kwargs: rendered)
    mod._install_completion_contract_overlay(fake)
    config = SimpleNamespace(default_branch="main")
    try:
        fake._task_body(config)
    except RuntimeError as exc:
        assert "canonical PR closing-reference contract is missing" in str(exc)
    else:
        raise AssertionError("missing closing-reference contract must fail closed")


def test_issue_body_contract_text_cannot_satisfy_overlay_guard() -> None:
    """A copied contract inside the untrusted Issue body cannot satisfy the
    guard when the generated canonical section omits the closing reference."""
    fake = ModuleType("fake_intake_closing_injection")
    rendered = (
        mod._OLD_COMPLETION_CONTRACT
        + "\n"
        + mod._core._CLOSING_REFERENCE_CONTRACT
        + "\n--- END GITHUB ISSUE BODY ---\n\n"
        + mod._OLD_COMPLETION_CONTRACT
        + "\n"
        + mod._OLD_LEAD_CONTRACT_TEMPLATE.format(default_branch="main")
    )
    setattr(fake, "_task_body", lambda *args, **kwargs: rendered)
    setattr(fake, "_CLOSING_REFERENCE_CONTRACT", mod._core._CLOSING_REFERENCE_CONTRACT)
    mod._install_completion_contract_overlay(fake)
    config = SimpleNamespace(default_branch="main")
    try:
        fake._task_body(config)
    except RuntimeError as exc:
        assert "closing-reference contract drifted" in str(exc)
    else:
        raise AssertionError("Issue-body contract text must not satisfy the overlay guard")


if __name__ == "__main__":
    tests = (
        test_worker_completion_uses_core_terminal_action,
        test_external_wait_never_keeps_worker_running,
        test_required_acceptance_is_attempted_or_explained,
        test_main_and_controller_ownership_are_separated,
        test_legacy_review_handoff_contract_is_removed,
        test_contract_drift_fails_closed,
        test_lead_contract_drift_fails_closed,
        test_rendered_body_carries_closing_reference_contract,
        test_closing_reference_line_is_visible_plain_text,
        test_closing_reference_helper_accepts_only_plain_text,
        test_overlay_and_canonical_closing_reference_wording_synced,
        test_closing_reference_drift_fails_closed,
        test_closing_reference_missing_from_core_fails_closed,
        test_issue_body_contract_text_cannot_satisfy_overlay_guard,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
