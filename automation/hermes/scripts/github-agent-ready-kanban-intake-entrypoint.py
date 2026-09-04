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
from contextlib import nullcontext
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


def _registry_entries_by_repository(
    module: ModuleType,
    snapshot: object,
) -> dict[str, dict[str, Any]]:
    entries = snapshot.get("repositories") if isinstance(snapshot, dict) else None
    if not isinstance(entries, list):
        raise module.IntakeError("registry_unavailable")

    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise module.IntakeError("registry_unavailable")
        raw_repository = entry.get("repository")
        if (
            not isinstance(raw_repository, str)
            or not raw_repository
            or raw_repository != raw_repository.strip()
        ):
            raise module.IntakeError("registry_unavailable")
        key = raw_repository.casefold()
        if key in by_name:
            raise module.IntakeError("registry_unavailable")
        by_name[key] = entry
    return by_name


def _missing_checkout_repositories_from_registry(
    module: ModuleType,
    snapshot: object,
) -> tuple[str, ...]:
    entries = _registry_entries_by_repository(module, snapshot)
    repositories: list[str] = []
    for entry in entries.values():
        checkout_status = entry.get("checkout_status")
        reason = entry.get("reason")
        if checkout_status != "missing" and reason != "checkout_missing":
            continue
        repositories.append(str(entry["repository"]))
    return tuple(sorted(repositories, key=str.casefold))


def _requeue_claimed_scope(module: ModuleType, scope: object, reason: str) -> None:
    try:
        module._requeue_wake_scope(scope, reason)
    except Exception:
        pass


def _install_full_scope_onboarding_overlay(module: ModuleType) -> None:
    original = getattr(module, "_claim_wake_scope", None)
    if not callable(original):
        raise RuntimeError("intake core has no _claim_wake_scope")
    if getattr(original, "_full_scope_onboarding_overlay_installed", False):
        return

    def patched_claim_wake_scope():
        scope = original()
        if scope is None or getattr(scope, "mode", None) != "full":
            return scope

        try:
            token = module._github_token()
            snapshot = module._load_registry_snapshot(token)
            repositories = _missing_checkout_repositories_from_registry(
                module,
                snapshot,
            )
        except Exception:
            _requeue_claimed_scope(module, scope, "registry_unavailable")
            raise

        if not repositories:
            return scope

        try:
            results, skipped, _ = module._provision_scoped_checkouts(
                token,
                repositories,
                snapshot,
                dry_run=bool(getattr(module, "_intake_overlay_dry_run", False)),
            )
            progress = getattr(module, "_active_scope_progress", {})
            if isinstance(progress, dict):
                progress["checkout_provisioning"] = results
                progress["scope_skipped"] = skipped
        except Exception:
            _requeue_claimed_scope(module, scope, "onboarding_retryable")
            raise

        return scope

    setattr(patched_claim_wake_scope, "_full_scope_onboarding_overlay_installed", True)
    setattr(patched_claim_wake_scope, "_full_scope_onboarding_overlay_original", original)
    module.__dict__["_claim_wake_scope"] = patched_claim_wake_scope


def _install_existing_ready_scope_overlay(module: ModuleType) -> None:
    original = getattr(module, "_provision_scoped_checkouts", None)
    if not callable(original):
        raise RuntimeError("intake core has no _provision_scoped_checkouts")
    if getattr(original, "_existing_ready_scope_overlay_installed", False):
        return

    def patched_provision_scoped_checkouts(
        token: str,
        repositories: Any,
        snapshot: object,
        *,
        dry_run: bool,
    ):
        if bool(getattr(module, "_intake_overlay_manual_repository", False)):
            return original(token, repositories, snapshot, dry_run=dry_run)

        registry_snapshot = module._load_registry_snapshot(token)
        entries = _registry_entries_by_repository(module, registry_snapshot)
        reused: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        strict: list[Any] = []
        seen: set[str] = set()

        for raw_repository in repositories:
            if not isinstance(raw_repository, str):
                strict.append(raw_repository)
                continue
            key = raw_repository.casefold()
            if key in seen:
                continue
            seen.add(key)
            entry = entries.get(key)
            if not (
                isinstance(entry, dict)
                and entry.get("ready") is True
                and entry.get("checkout_status") == "verified"
                and isinstance(entry.get("checkout"), str)
                and str(entry.get("checkout")).startswith("/")
            ):
                strict.append(raw_repository)
                continue

            self_heal = getattr(module, "_self_heal_stale_checkout", None)
            refresh_action = "noop"
            refresh_reason = "checkout_reused"
            try:
                if callable(self_heal):
                    lock_factory: Any = getattr(
                        module,
                        "_repository_onboarding_lock",
                        None,
                    )
                    lock: Any = (
                        lock_factory(raw_repository)
                        if callable(lock_factory)
                        else nullcontext()
                    )
                    with lock:
                        metadata = module._onboarding_repository_metadata(
                            token,
                            raw_repository,
                        )
                        if (
                            metadata.repository.casefold() != key
                            or metadata.default_branch != entry.get("default_branch")
                        ):
                            raise module.IntakeError("repository_metadata_invalid")
                        if dry_run:
                            module._validate_onboarding_checkout(
                                metadata,
                                Path(str(entry["checkout"])),
                            )
                            refresh_action = "noop"
                        else:
                            refresh_action = str(
                                self_heal(
                                    token,
                                    metadata,
                                    Path(str(entry["checkout"])),
                                )
                            )
                        refresh_reason = (
                            "checkout_self_healed"
                            if refresh_action == "healed"
                            else "self_heal_noop_same_sha"
                        )
                else:
                    # Compatibility for operator probes that load an older
                    # core module; deployed cores always expose the shared
                    # locked self-heal contract above.
                    metadata = module._onboarding_repository_metadata(
                        token,
                        raw_repository,
                    )
                    if (
                        metadata.repository.casefold() != key
                        or metadata.default_branch != entry.get("default_branch")
                    ):
                        raise module.IntakeError("repository_metadata_invalid")
            except module.IntakeError as exc:
                reason = module._onboarding_error_code(exc)
                skipped.append(
                    {
                        "repository": raw_repository,
                        "reason": reason,
                    }
                )
                recorder = getattr(module, "_record_repository_outcome", None)
                permanent = getattr(module, "_is_permanent_scope_reason", None)
                if callable(recorder):
                    recorder(
                        raw_repository,
                        "skipped" if callable(permanent) and permanent(reason) else "failed",
                        reason,
                    )
                continue
            except Exception:
                skipped.append(
                    {
                        "repository": raw_repository,
                        "reason": "intake_failed",
                    }
                )
                recorder = getattr(module, "_record_repository_outcome", None)
                if callable(recorder):
                    recorder(raw_repository, "failed", "intake_failed")
                continue

            reused.append(
                {
                    "repository": metadata.repository,
                    "checkout": str(entry["checkout"]),
                    "action": "healed" if refresh_action == "healed" else "reused",
                }
            )
            recorder = getattr(module, "_record_repository_outcome", None)
            if callable(recorder):
                recorder(metadata.repository, reused[-1]["action"], refresh_reason)

        strict_results: list[dict[str, str]] = []
        strict_skipped: list[dict[str, str]] = []
        reload_required = False
        if strict:
            strict_results, strict_skipped, reload_required = original(
                token,
                strict,
                registry_snapshot,
                dry_run=dry_run,
            )

        results = reused + strict_results
        all_skipped = skipped + strict_skipped
        progress = getattr(module, "_active_scope_progress", {})
        if isinstance(progress, dict):
            progress["checkout_provisioning"] = results
            progress["scope_skipped"] = all_skipped
        return results, all_skipped, reload_required

    setattr(
        patched_provision_scoped_checkouts,
        "_existing_ready_scope_overlay_installed",
        True,
    )
    setattr(
        patched_provision_scoped_checkouts,
        "_existing_ready_scope_overlay_original",
        original,
    )
    module.__dict__["_provision_scoped_checkouts"] = patched_provision_scoped_checkouts


def _origin_snapshot_unlocked(module: ModuleType, config: Any):
    checkout = Path(str(config.checkout))
    if (
        not checkout.is_absolute()
        or module._path_has_symlink_component(checkout)
        or checkout.is_symlink()
        or not checkout.is_dir()
    ):
        raise module.IntakeError(f"checkout missing or unsafe: {config.checkout}")

    code, root, _ = module._run_git(
        str(config.checkout),
        "rev-parse",
        "--show-toplevel",
    )
    if code != 0 or not root or Path(root).resolve() != checkout.resolve():
        raise module.IntakeError(
            f"checkout is not the expected Git root: {config.checkout}"
        )

    code, remote, _ = module._run_git(
        str(config.checkout),
        "remote",
        "get-url",
        "origin",
    )
    expected = module._normalise_remote(f"https://github.com/{config.name}.git")
    if code != 0 or module._normalise_remote(remote) != expected:
        raise module.IntakeError(f"origin mismatch for {config.name}")

    token = module._github_token()
    metadata = module._onboarding_repository_metadata(
        token,
        config.name,
    )
    if metadata.default_branch != config.default_branch:
        raise module.IntakeError(f"default branch drift for {config.name}")

    refresh_action = "noop"
    refresh_reason = "checkout_reused"
    self_heal = getattr(module, "_self_heal_stale_checkout", None)
    if callable(self_heal) and not bool(
        getattr(module, "_intake_overlay_dry_run", False)
    ):
        refresh_action = str(
            self_heal(
                token,
                metadata,
                checkout,
            )
        )
        refresh_reason = (
            "checkout_self_healed"
            if refresh_action == "healed"
            else "self_heal_noop_same_sha"
        )

    remote_ref = f"origin/{config.default_branch}"
    code, sha, _ = module._run_git(
        str(config.checkout),
        "rev-parse",
        "--verify",
        f"{remote_ref}^{{commit}}",
    )
    if (
        code != 0
        or not isinstance(sha, str)
        or module._ONBOARDING_SHA.fullmatch(sha) is None
    ):
        raise module.IntakeError(f"{remote_ref} unavailable for {config.name}")
    if (
        not isinstance(metadata.default_branch_sha, str)
        or sha.casefold() != metadata.default_branch_sha.casefold()
    ):
        raise module.IntakeError(f"{remote_ref} is stale for {config.name}")

    contract_paths = tuple(metadata.contract_paths)
    if (
        not contract_paths
        or len(set(contract_paths)) != len(contract_paths)
        or any(
            path not in module.ONBOARDING_CONTRACT_CANDIDATES
            for path in contract_paths
        )
    ):
        raise module.IntakeError(f"contract paths are invalid for {config.name}")

    missing: list[str] = []
    for contract_path in contract_paths:
        code, _, _ = module._run_git(
            str(config.checkout),
            "cat-file",
            "-e",
            f"{remote_ref}:{contract_path}",
        )
        if code != 0:
            missing.append(contract_path)
    if missing:
        raise module.IntakeError(
            f"{remote_ref} contract missing for {config.name}: {', '.join(missing)}"
        )

    return module.RepoSnapshot(
        origin_sha=sha,
        remote=remote,
        contract_paths=contract_paths,
        refresh_action="healed" if refresh_action == "healed" else "reused",
        refresh_reason=refresh_reason,
    )


def _install_origin_snapshot_overlay(module: ModuleType) -> None:
    original = getattr(module, "_repo_snapshot", None)
    if not callable(original):
        raise RuntimeError("intake core has no _repo_snapshot")
    if getattr(original, "_origin_snapshot_overlay_installed", False):
        return

    def patched_repo_snapshot(config: Any):
        with module._repository_onboarding_lock(config.name):
            return _origin_snapshot_unlocked(module, config)

    setattr(patched_repo_snapshot, "_origin_snapshot_overlay_installed", True)
    setattr(patched_repo_snapshot, "_origin_snapshot_overlay_original", original)
    module.__dict__["_repo_snapshot"] = patched_repo_snapshot


def _install_run_context_overlay(module: ModuleType) -> None:
    original = getattr(module, "_run_once", None)
    if not callable(original):
        raise RuntimeError("intake core has no _run_once")
    if getattr(original, "_run_context_overlay_installed", False):
        return

    def patched_run_once(args: Any) -> int:
        previous_dry_run = module.__dict__.get("_intake_overlay_dry_run")
        previous_manual = module.__dict__.get("_intake_overlay_manual_repository")
        module.__dict__["_intake_overlay_dry_run"] = bool(
            getattr(args, "dry_run", False)
        )
        module.__dict__["_intake_overlay_manual_repository"] = bool(
            getattr(args, "repository", None)
        )
        try:
            return int(original(args))
        finally:
            if previous_dry_run is None:
                module.__dict__.pop("_intake_overlay_dry_run", None)
            else:
                module.__dict__["_intake_overlay_dry_run"] = previous_dry_run
            if previous_manual is None:
                module.__dict__.pop("_intake_overlay_manual_repository", None)
            else:
                module.__dict__["_intake_overlay_manual_repository"] = previous_manual

    setattr(patched_run_once, "_run_context_overlay_installed", True)
    setattr(patched_run_once, "_run_context_overlay_original", original)
    module.__dict__["_run_once"] = patched_run_once


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
    _install_full_scope_onboarding_overlay(module)
    _install_existing_ready_scope_overlay(module)
    _install_origin_snapshot_overlay(module)
    _install_run_context_overlay(module)
    return module


_core = _load_core()


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    return int(_core.main())


if __name__ == "__main__":
    raise SystemExit(main())
