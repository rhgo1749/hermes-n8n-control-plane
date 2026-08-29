#!/usr/bin/env python3
"""Deployment entrypoint for GitHub Issue -> Kanban intake.

The canonical intake implementation remains
``github-agent-ready-kanban-intake.py`` in the repository. During deployment
it is installed beside this wrapper as
``github-agent-ready-kanban-intake-core.py`` while this file is installed under
the historical live name ``github-agent-ready-kanban-intake.py``.

This small overlay keeps Hermes core lifecycle semantics intact. GitHub-backed
workers finish their implementation run with core ``kanban_complete``; the
edge reconciler remains the only owner of GitHub ``done`` <-> ``review``
projection. The overlay also makes the lead/specialist stop boundary explicit:
Kanban dependencies are the waiting mechanism, and no worker remains alive
solely to poll future CI, human review, merge, or comments.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


_OLD_COMPLETION_CONTRACT = """## GitHub completion contract (authoritative)

- Worker implementation completion is a review handoff: the Kanban status must be `review`, never `done`.
- `done` is allowed only after a fresh GitHub API read proves every PR linked to this Issue is merged into the target branch.
- An OPEN PR, CI success, pushed commit, PR creation, review handoff, or `Closes #N` text is not merge evidence.
- A CLOSED PR with `merged=false` is not completion evidence; keep the card in `review` (or preserve an existing human `blocked` state).
- GitHub API failure is fail-closed: preserve the current Kanban status and do not infer completion from local metadata or worker output.
- Linked PR discovery uses GitHub Issue links plus handoff references; all discovered required PRs must be merged."""

_NEW_COMPLETION_CONTRACT = """## GitHub completion contract (authoritative)

- Worker implementation completion must finish the worker run with core `kanban_complete`. That core `done` transition is provisional for a GitHub-backed card and is not merge evidence.
- Do not call `kanban_request_review` on this GitHub-backed intake card. Review waiting is projected by the edge reconciler, not by spawning a second core review worker.
- No implementation, review, or lead worker may remain RUNNING solely to wait for future GitHub Actions/checks, human review, merge, or comments. Record the current PR/head and any pending external/manual gate once, then hand off.
- Main/lead waiting for specialist work must use real Kanban dependencies; never use `sleep` or repeated polling to keep an active worker slot occupied.
- If any required PR is OPEN or CLOSED with `merged=false`, the edge reconciler projects the card to parked `review`, clears worker ownership/claim metadata, and leaves it non-runnable. Only an explicit trusted rework signal may return it to work.
- Authoritative `done` requires a fresh GitHub API read proving every PR linked to this Issue is merged into the target branch.
- An OPEN PR, CI success, pushed commit, PR creation, worker completion, or `Closes #N` text is not merge evidence.
- GitHub API failure is fail-closed: preserve the current Kanban status and do not infer completion from local metadata or worker output.
- Linked PR discovery uses GitHub Issue links plus handoff references; all discovered required PRs must be merged."""

_OLD_LEAD_CONTRACT_TEMPLATE = """## Luna lead execution contract

1. Read the complete GitHub Issue thread (body and comments) from the canonical URL before making implementation decisions.
2. Read the repository's `AGENTS.md`, the applicable router (`AGENTS_PROJECT.md` / `Docs/AGENTS.md` where present), canonical docs, and every repository contract path listed in Provenance from the current `origin/{default_branch}`.
3. Inspect the current fetched `origin/{default_branch}`, relevant source/tests, and open or overlapping PRs. Do not modify the shared checkout directly; use the Kanban worktree/branch contract.
4. Instantiate the repository-specific request using the naming/path contract defined by `AGENTS.md` and the detected repository template; do not invent a request identifier or path.
5. Implement only the Issue's PR-sized scope. Delegate only bounded research, implementation, or test work to Luna workers when useful; delegation does not transfer lead ownership.
6. Independently review every delegated diff/evidence, run applicable deterministic repository gates, and keep HUMAN_VALIDATION_REQUIRED / HOST_VALIDATION_REQUIRED / BLOCKED states honest. Required UI/browser/device/manual acceptance must be attempted whenever the worker has the necessary execution surface; if it cannot be run, record the exact gate, attempted step, concrete blocker or missing prerequisite, and the smallest human follow-up. A bare `human validation required` note is not sufficient evidence.
7. Create a GitHub PR only after the executable gates pass. Never merge or enable auto-merge."""
_ISSUE_BODY_END_MARKER = "--- END GITHUB ISSUE BODY ---\n\n"

_NEW_LEAD_CONTRACT = """## Kanban lead orchestration contract

1. Read the complete GitHub Issue thread (body and comments) from the canonical URL before routing work.
2. Read the repository's `AGENTS.md`, applicable router (`AGENTS_PROJECT.md` / `Docs/AGENTS.md` where present), canonical docs, and every repository contract path listed in Provenance from the current default branch recorded above.
3. Inspect only enough current default-branch source/tests and overlapping PR state to recover scope and route safely. Do not modify the shared checkout directly.
4. Instantiate the repository-specific request using the naming/path contract defined by `AGENTS.md`; preserve the source Issue identity.
5. Build the smallest correct specialist graph. Main is the planner/router/judge, not the default implementer. Route repository changes to a verified `kanban-developer`; use `kanban-reviewer` for independent technical review; use `kanban-designer` only when a material product/UX decision or design review is actually required.
6. Encode real dependencies before downstream work runs. While a dependency is running, step back: do not `sleep`, poll worker status, duplicate specialist validation, or consume an active worker slot merely to observe progress. Resume from durable Kanban dependency transitions.
7. Developer delivery is implementation + required repository-local deterministic validation + PR create/update + exact evidence. When the Issue/request requires UI, browser, Dashboard, device, or other manual acceptance, the developer must attempt every required gate that is executable with the worker's available tools/runtime. In particular, if browser/computer-use is available and the required authenticated/exact-head surface can be reached, browser acceptance must be performed before delivery; `HUMAN_VALIDATION_REQUIRED` is not permission to skip an executable gate. Pending future CI/checks, human review, merge, or comments are recorded as external/manual gates and are not reasons to keep that developer RUNNING.
8. If a required acceptance gate cannot be executed, the handoff must state the exact gate, whether the required browser/tool/runtime was available, what step was attempted, the concrete blocking error or missing prerequisite (for example auth, exact-head deployment, device, permission), any useful evidence, and the smallest human follow-up. A bare `human validation required`, `not run`, or equivalent without a reason is invalid handoff evidence.
9. Reviewer verifies the current PR/head/diff/evidence and returns PASS or REWORK. An executable required acceptance gate that was skipped, or a NOT RUN gate without the required blocker evidence, is REWORK. Reviewer does not create child rework tasks or manipulate dependencies. When REWORK is reported, Main creates the bounded developer rework without parent-linking to non-terminal reviewer cards.
10. When all required internal specialist dependencies are satisfied, inspect their durable handoffs and finish the GitHub-backed root run with core `kanban_complete`. That core done is provisional; authoritative done is owned by the edge after GitHub proves merge. Never merge or enable auto-merge. Future PR lifecycle belongs to GitHub + edge reconciliation.
11. Deterministic Controller/edge logic owns event intake, READY/resource/lease/stale recovery, dependency readiness, and external GitHub projection. Do not recreate controller behavior through agent reasoning loops."""


def _core_path() -> Path:
    here = Path(__file__).resolve()
    deployed = here.with_name("github-agent-ready-kanban-intake-core.py")
    if deployed.is_file():
        return deployed

    # Repository checkout mode: the wrapper sits beside the canonical source.
    source = here.with_name("github-agent-ready-kanban-intake.py")
    if source != here and source.is_file():
        return source
    raise RuntimeError("canonical GitHub intake implementation is missing")


def _install_completion_contract_overlay(module: ModuleType) -> None:
    original = getattr(module, "_task_body", None)
    if not callable(original):
        raise RuntimeError("intake core has no _task_body")
    if getattr(original, "_github_completion_overlay_installed", False):
        return

    def patched_task_body(*args: Any, **kwargs: Any) -> str:
        rendered = str(original(*args, **kwargs))
        if _OLD_COMPLETION_CONTRACT not in rendered:
            raise RuntimeError(
                "GitHub completion contract drifted; refusing to emit an "
                "unverified worker lifecycle contract"
            )
        config = args[0] if args else kwargs.get("config")
        default_branch = str(getattr(config, "default_branch", "")).strip()
        if not default_branch:
            raise RuntimeError(
                "Kanban lead contract cannot resolve the repository default branch"
            )
        old_lead_contract = _OLD_LEAD_CONTRACT_TEMPLATE.format(
            default_branch=default_branch
        )
        if old_lead_contract not in rendered:
            raise RuntimeError(
                "Kanban lead contract drifted; refusing to emit an unverified "
                "orchestration lifecycle contract"
            )
        closing_contract = getattr(module, "_CLOSING_REFERENCE_CONTRACT", None)
        if not isinstance(closing_contract, str) or not closing_contract:
            raise RuntimeError(
                "canonical PR closing-reference contract is missing; refusing to emit an unverified handoff contract"
            )
        issue_body_end = rendered.rfind(_ISSUE_BODY_END_MARKER)
        completion_search_start = (
            issue_body_end + len(_ISSUE_BODY_END_MARKER)
            if issue_body_end >= 0
            else 0
        )
        completion_start = rendered.find(
            _OLD_COMPLETION_CONTRACT,
            completion_search_start,
        )
        if completion_start < 0:
            raise RuntimeError(
                "GitHub completion contract drifted; refusing to emit an unverified worker lifecycle contract"
            )
        completion_end = completion_start + len(_OLD_COMPLETION_CONTRACT)
        if rendered.find(closing_contract, completion_end) < 0:
            raise RuntimeError(
                "PR closing-reference contract drifted; refusing to emit an unverified handoff contract"
            )
        rendered = rendered.replace(
            _OLD_COMPLETION_CONTRACT,
            _NEW_COMPLETION_CONTRACT,
            1,
        )
        return rendered.replace(
            old_lead_contract,
            _NEW_LEAD_CONTRACT,
            1,
        )

    setattr(patched_task_body, "_github_completion_overlay_installed", True)
    setattr(patched_task_body, "_github_completion_overlay_original", original)
    module.__dict__["_task_body"] = patched_task_body


def _load_core() -> ModuleType:
    path = _core_path()
    name = "github_agent_ready_kanban_intake_core"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load intake core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _install_completion_contract_overlay(module)
    return module


_core = _load_core()


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    return int(_core.main())


if __name__ == "__main__":
    raise SystemExit(main())
