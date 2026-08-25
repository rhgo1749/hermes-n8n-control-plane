#!/usr/bin/env python3
"""Reviewed, reversible migration of intake boards to repository-derived identity.

User-directed rule change (2026-08-25): every registry-managed GitHub intake
board takes its canonical slug and display identity exclusively from GitHub
repository metadata (``repository.name.casefold()`` / ``repository.name``).
Static repository->board label maps, board shorthand maps, ``GitHub Intake``
suffix conventions, and per-board alias/allowlist exceptions are retired.

This tool is the ONLY authorized surface for the identity cutover. It is a
reviewed, tested, reversible maintenance operation — never ad-hoc SQLite or
filesystem mutation of production state. It never mutates GitHub Issues,
labels, PRs, or merge state, and it never hard-deletes a board (archival is
recoverable).

Cutover order is strictly ``migration -> transition``:

1. ``preflight`` (read-only): derives the repository identity, classifies
   live boards (canonical / legacy / unrelated), and fails closed on
   non-terminal legacy tasks, ambiguous or mixed-provenance boards,
   canonical conflicts, incomplete checkouts, or missing checkout evidence.
2. ``migrate`` (pre-transition, data-preserving): backs up every affected
   legacy board directory, creates the canonical board if it does not exist
   (display name = repository name; default_workdir = the legacy board's
   declared checkout, or ``--checkout-root/<slug>`` as the registry
   fallback), and re-verifies that the legacy board is byte-identical
   afterwards. Idempotency and task history stay in the legacy board; the
   registry keeps routing the repository to the legacy board (provenance
   wins) until the transition. Re-runs are safe (provision is idempotent;
   backups are content-stamped and never clobbered).
3. ``transition`` (only after migrate evidence exists): archives the legacy
   board (recoverable, never hard-deleted) and makes the canonical slug
   board the sole live intake route. After the transition a fresh registry
   dry-run sees only the canonical target with ``board_provisioning=[]``.
4. ``postcheck``: re-verifies the post-transition invariants and the
   integrity of the archived legacy board against the recorded checksums.
5. ``rollback``: restores the pre-transition state from the recorded
   backups (legacy board restored, canonical board removed only when it is
   still empty, evidence reverted).

Fail-closed gates (all of them):
- non-terminal (non done/review/archived) task on a legacy board;
- a live board whose GitHub provenance covers more than one repository
  (mixed/ambiguous), or the repository on two live boards
  (ambiguous_multiple_boards);
- a live canonical-slug board whose provenance is not exactly this
  repository (canonical conflict);
- incomplete checkout evidence (no --checkout and no resolvable
  --checkout-root) when the canonical board must be created;
- incomplete or stale migration evidence for transition/postcheck/rollback.

No live mutation happens in this repository task: mutation stages require
explicit confirm flags and run only when an operator invokes them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GITHUB_ISSUE_KEY = re.compile(r"^github:([^:]+/[^:]+):issue:\d+$", re.IGNORECASE)
TERMINAL_STATUSES = frozenset({"done", "review", "archived"})
EVIDENCE_SCHEMA_VERSION = 1
# Files that are volatile at runtime and must not gate integrity checks.
_VOLATILE_NAMES = frozenset(
    {
        "kanban.db-wal",
        "kanban.db-shm",
        "kanban.db.dispatch.lock",
        "kanban.db.init.lock",
        "kanban.db-shm.lock",
    }
)


class MigrationError(RuntimeError):
    """The migration failed closed; nothing was mutated past this point."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_map(directory: Path) -> dict[str, str]:
    """Content-addressed snapshot of a board directory (recoverable evidence)."""
    entries: dict[str, str] = {}
    if not directory.is_dir():
        return entries
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.name in _VOLATILE_NAMES:
            continue
        entries[str(path.relative_to(directory))] = _sha256(path)
    return entries


@dataclass(frozen=True)
class BoardFact:
    slug: str
    directory: Path
    name: str
    archived: bool
    default_workdir: str
    provenance: tuple[str, ...]
    task_count: int
    non_terminal: int


def _load_board_metadata(directory: Path) -> dict[str, Any]:
    metadata = directory / "board.json"
    if not metadata.is_file():
        return {}
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid board.json for {directory.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MigrationError(f"board.json for {directory.name} is not an object")
    return payload


def _read_board_db(board_dir: Path) -> tuple[int, int, tuple[str, ...]]:
    """Return (task_count, non_terminal_count, provenance) without mutation."""
    db = board_dir / "kanban.db"
    if not db.is_file():
        raise MigrationError(f"board {board_dir.name} has no kanban.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        rows = con.execute("SELECT status FROM tasks").fetchall()
        non_terminal = sum(
            1 for (status,) in rows if str(status) not in TERMINAL_STATUSES
        )
        keys = [
            row[0]
            for row in con.execute(
                "SELECT idempotency_key FROM tasks WHERE idempotency_key IS NOT NULL"
            ).fetchall()
        ]
    finally:
        con.close()
    repositories: set[str] = set()
    for raw in keys:
        match = GITHUB_ISSUE_KEY.match(str(raw or ""))
        if match:
            repositories.add(match.group(1).casefold())
    return total, non_terminal, tuple(sorted(repositories))


def _scan_boards(boards_root: Path) -> list[BoardFact]:
    if not boards_root.is_dir():
        raise MigrationError(f"boards root is not a directory: {boards_root}")
    facts: list[BoardFact] = []
    for child in sorted(boards_root.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        metadata = _load_board_metadata(child)
        declared_slug = str(metadata.get("slug") or "").strip()
        if declared_slug and declared_slug.casefold() != child.name.casefold():
            raise MigrationError(
                f"board metadata slug mismatch: directory={child.name} "
                f"metadata={declared_slug!r}"
            )
        total, non_terminal, provenance = _read_board_db(child)
        facts.append(
            BoardFact(
                slug=child.name,
                directory=child,
                name=str(metadata.get("name") or child.name),
                archived=bool(metadata.get("archived")),
                default_workdir=str(metadata.get("default_workdir") or "").strip(),
                provenance=provenance,
                task_count=total,
                non_terminal=non_terminal,
            )
        )
    return facts


def _classify(
    repository: str, facts: list[BoardFact]
) -> tuple[BoardFact | None, list[BoardFact], list[BoardFact], list[BoardFact]]:
    """Classify live boards for one repository.

    Returns ``(canonical, legacy, ambiguous, unrelated)``. ``canonical`` is
    a live board whose slug equals the repository canonical slug;
    ``legacy`` is a live board whose GitHub provenance is exactly this
    repository and whose slug differs; ``ambiguous`` collects every board
    that makes the identity unresolvable (mixed provenance, the repository
    on two live boards, or foreign/mixed provenance on the canonical slug);
    ``unrelated`` is live boards with no GitHub provenance at all (they are
    never touched).
    """
    canonical_slug = repository.split("/", 1)[-1].casefold()
    canonical: BoardFact | None = None
    legacy: list[BoardFact] = []
    ambiguous: list[BoardFact] = []
    unrelated: list[BoardFact] = []

    for fact in facts:
        if fact.archived:
            continue
        if fact.slug.casefold() == canonical_slug:
            if not fact.provenance:
                canonical = fact
            elif set(fact.provenance) == {repository.casefold()}:
                canonical = fact
            else:
                ambiguous.append(fact)
            continue
        if fact.provenance:
            provenance = set(fact.provenance)
            if provenance == {repository.casefold()}:
                legacy.append(fact)
            elif repository.casefold() in provenance:
                # This repository is mixed with another provenance on the
                # same board: identity is ambiguous — fail closed.
                ambiguous.append(fact)
            else:
                # Another repository's board: it is out of scope for this
                # repository's cutover and must not block it.
                unrelated.append(fact)
        else:
            unrelated.append(fact)
    return canonical, legacy, ambiguous, unrelated

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hermes(hermes_bin: str, *args: str, parse_json: bool = False) -> Any:
    proc = subprocess.run(
        [hermes_bin, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise MigrationError(
            f"hermes {' '.join(args[:2])} failed: "
            f"{proc.stderr.strip() or proc.stdout[-2000:]}"
        )
    if not parse_json:
        return proc.stdout
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"hermes CLI returned invalid JSON: {exc}") from exc


def _live_boards(hermes_bin: str) -> dict[str, str]:
    """slug -> display name for live boards, as the Hermes CLI sees them."""
    payload = _run_hermes(
        hermes_bin, "kanban", "boards", "list", "--json", parse_json=True
    )
    items = payload if isinstance(payload, list) else payload.get("boards", [])
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and item.get("slug"):
            result[str(item["slug"])] = str(item.get("name") or item["slug"])
    return result


def _require_repository(args: argparse.Namespace) -> str:
    repository = str(getattr(args, "repository", "") or "").strip()
    if not repository or repository.count("/") != 1:
        raise MigrationError("--repository must be owner/name")
    return repository


def _evidence_path(state_root: Path, repository: str) -> Path:
    slug = repository.split("/", 1)[-1].casefold()
    return state_root / f"{slug}.json"


def _load_evidence(state_root: Path, repository: str) -> dict[str, Any]:
    path = _evidence_path(state_root, repository)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(
            f"invalid migration evidence for {repository}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise MigrationError(
            f"migration evidence for {repository} is incomplete or stale"
        )
    return payload


def _save_evidence(state_root: Path, repository: str, evidence: dict[str, Any]) -> None:
    path = _evidence_path(state_root, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**evidence, "updated_at": int(time.time())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _task_count_excluding(board_dir: Path, task_ids: frozenset[str]) -> int:
    """Live task count of a board excluding the given task ids."""
    db = board_dir / "kanban.db"
    if not db.is_file():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total = int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        if not task_ids:
            return total
        placeholders = ",".join("?" for _ in task_ids)
        anchored = int(
            con.execute(
                f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders})",
                tuple(task_ids),
            ).fetchone()[0]
        )
    finally:
        con.close()
    return total - anchored


def _preflight(
    repository: str,
    facts: list[BoardFact],
    *,
    allow_recreate_canonical: bool,
    anchor_task_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Classify and fail closed before any mutation.

    ``anchor_task_ids`` are the carried idempotency anchors on the canonical
    board (see ``_carry_anchors``): they are expected evidence of an
    in-progress or partially-completed transition, not a conflict.
    """
    canonical, legacy, ambiguous, _unrelated = _classify(repository, facts)
    display_name = repository.split("/", 1)[-1]
    canonical_slug = display_name.casefold()

    errors: list[str] = []
    if ambiguous:
        errors.append(
            "ambiguous/mixed-provenance boards: "
            + ", ".join(
                f"{f.slug}({'+'.join(f.provenance) or 'no-provenance'})"
                for f in ambiguous
            )
        )
    if len(legacy) > 1:
        errors.append(
            "multiple legacy boards for one repository: "
            + ", ".join(f.slug for f in legacy)
        )
    if canonical is not None and legacy:
        if anchor_task_ids:
            foreign_count = _task_count_excluding(
                canonical.directory, frozenset(anchor_task_ids)
            )
        else:
            foreign_count = canonical.task_count
        if foreign_count > 0:
            errors.append(
                "canonical conflict: live canonical board "
                f"{canonical.slug} has {foreign_count} non-anchor task(s) "
                f"beside legacy {', '.join(f.slug for f in legacy)}"
            )

    for fact in legacy:
        if fact.non_terminal:
            errors.append(
                f"legacy board {fact.slug} has {fact.non_terminal} non-terminal "
                "task(s); fail closed"
            )

    if not legacy:
        if canonical is None:
            if not allow_recreate_canonical:
                errors.append(
                    "no live legacy board and no live canonical board; "
                    "creating a canonical board requires "
                    "--allow-recreate-canonical (reviewed migration only)"
                )
        elif not allow_recreate_canonical:
            # Already canonical (or an empty canonical duplicate beside a
            # populated legacy board that is now gone). Nothing to migrate.
            pass

    return {
        "repository": repository,
        "display_name": display_name,
        "canonical_slug": canonical_slug,
        "canonical_board": canonical.slug if canonical else None,
        "legacy_boards": [f.slug for f in legacy],
        "ambiguous_boards": [f.slug for f in ambiguous],
        "errors": errors,
    }


def _backup(board: BoardFact, backup_root: Path) -> tuple[Path, dict[str, str]]:
    """Copy a board directory to a stamped backup; never clobber existing."""
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    target = backup_root / f"{board.slug}-{stamp}"
    suffix = 1
    while target.exists():
        target = backup_root / f"{board.slug}-{stamp}-{suffix}"
        suffix += 1
    shutil.copytree(board.directory, target, symlinks=True)
    return target, _file_map(target)


def _ensure_canonical_board(
    hermes_bin: str,
    repository: str,
    display_name: str,
    checkout: str,
    *,
    dry_run: bool,
) -> str:
    """Idempotently provision/normalize the canonical board; verify it live."""
    slug = repository.split("/", 1)[-1].casefold()
    live = _live_boards(hermes_bin)
    if slug in live:
        if live[slug].casefold() != display_name.casefold():
            # Display-only normalization: the slug board exists with a
            # non-repository-derived display name (no static map involved).
            if not dry_run:
                _run_hermes(
                    hermes_bin, "kanban", "boards", "rename", slug, display_name
                )
            return "normalized-display"
        return "already-live"
    if dry_run:
        return "would-create"
    _run_hermes(
        hermes_bin,
        "kanban",
        "boards",
        "create",
        slug,
        "--name",
        display_name,
        "--description",
        f"GitHub agent-ready issue intake for {repository}",
        "--default-workdir",
        checkout,
        parse_json=False,
    )
    live = _live_boards(hermes_bin)
    if slug not in live:
        raise MigrationError(f"canonical board {slug} did not land live")
    if live[slug].casefold() != display_name.casefold():
        _run_hermes(
            hermes_bin, "kanban", "boards", "rename", slug, display_name
        )
        live = _live_boards(hermes_bin)
        if live[slug].casefold() != display_name.casefold():
            raise MigrationError(
                f"canonical board {slug} display name is not {display_name!r}"
            )
    return "created"

# ---------------------------------------------------------------------------
# Idempotency anchor carry-over (dedup boundary across the cutover)
# ---------------------------------------------------------------------------


def _repository_key_pattern(repository: str) -> "re.Pattern[str]":
    import re as _re

    owner, name = repository.split("/", 1)
    return _re.compile(
        "^github:" + _re.escape(owner) + "/" + _re.escape(name) + r":issue:\d+$",
        _re.IGNORECASE,
    )


def _terminal_anchor_keys(
    board_dir: Path, repository: str
) -> dict[str, str]:
    """Idempotency anchors to carry: terminal tasks with this repository's
    GitHub Issue keys, keyed by idempotency_key. Terminal (done/review/
    archived) only — non-terminal work must fail the preflight instead."""
    pattern = _repository_key_pattern(repository)
    con = sqlite3.connect(f"file:{board_dir / 'kanban.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT idempotency_key, status FROM tasks "
            "WHERE idempotency_key IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    anchors: dict[str, str] = {}
    for key, status in rows:
        if pattern.match(str(key or "")) and str(status) in TERMINAL_STATUSES:
            anchors[str(key)] = str(status)
    return anchors


def _carry_anchors(
    hermes_bin: str,
    repository: str,
    source_board_dir: Path,
    canonical_slug: str,
    display_name: str,
    *,
    dry_run: bool,
) -> dict[str, str]:
    """Carry terminal idempotency anchors to the canonical board.

    The intake dedup rule is board-scoped (``tasks.idempotency_key`` on the
    target board), so after the legacy board leaves the live set a re-intake
    of an already-imported issue (e.g. an ``agent-ready`` issue whose work is
    tracked on the legacy board) would otherwise create a duplicate root
    card. Carrying one terminal anchor task per GitHub Issue key preserves
    the dedup boundary: ``hermes kanban create`` with the same key returns
    the anchor instead of a new card. Idempotent and repeat-safe.
    """
    anchors = _terminal_anchor_keys(source_board_dir, repository)
    if not anchors:
        return {}
    task_ids: dict[str, str] = {}
    for key in sorted(anchors):
        number = key.rsplit(":", 1)[-1]
        title = f"GitHub Issue intake anchor: {repository}#{number}"
        body = "\n".join(
            [
                "# Board identity migration anchor",
                "",
                "This card is a carried idempotency anchor, not new intake work.",
                f"- source board: {source_board_dir.name} (archived during the "
                "repository-identity migration)",
                f"- repository: {repository}",
                f"- idempotency key: {key}",
                "- purpose: keep the board-scoped intake dedup boundary intact "
                "after the legacy board left the live set",
                "- no dispatch: no assignee; terminal status",
            ]
        )
        if dry_run:
            task_ids[key] = "would-carry"
            continue
        payload = _run_hermes(
            hermes_bin,
            "kanban",
            "--board",
            canonical_slug,
            "create",
            title,
            "--body",
            body,
            "--created-by",
            "board-identity-migration",
            "--idempotency-key",
            key,
            "--initial-status",
            "blocked",
            "--json",
            parse_json=True,
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise MigrationError(
                f"anchor create returned no task id for {key}: {payload!r}"
            )
        task_id = str(payload["id"])
        _run_hermes(
            hermes_bin,
            "kanban",
            "--board",
            canonical_slug,
            "complete",
            task_id,
            "--result",
            f"anchor for {key} carried from {source_board_dir.name}",
            parse_json=False,
        )
        task_ids[key] = task_id
    return task_ids


def _remove_anchors(
    hermes_bin: str,
    canonical_slug: str,
    task_ids: dict[str, str],
) -> None:
    """Purge carried anchors (archive then --rm) during rollback.

    Missing ids are tolerated (idempotent re-runs). A live canonical board
    without a ``kanban.db`` cannot hold anchors; nothing to remove.
    """
    for task_id in task_ids.values():
        if task_id in {"would-carry", ""} or not task_id:
            continue
        _run_hermes(
            hermes_bin, "kanban", "--board", canonical_slug, "archive", task_id
        )
        _run_hermes(
            hermes_bin,
            "kanban",
            "--board",
            canonical_slug,
            "archive",
            "--rm",
            task_id,
        )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _resolve_checkout(
    args: argparse.Namespace,
    repository: str,
    legacy: list[BoardFact],
) -> str:
    """Checkout for the canonical board's default_workdir.

    Order: explicit --checkout, then the legacy board's declared
    default_workdir, then --checkout-root/<canonical slug> (the registry's
    legacy fallback). A missing resolution fails closed.
    """
    checkout = str(getattr(args, "checkout", "") or "").strip()
    if not checkout:
        if legacy and legacy[0].default_workdir:
            checkout = legacy[0].default_workdir
        elif getattr(args, "checkout_root", None):
            canonical_slug = repository.split("/", 1)[-1].casefold()
            checkout = str(Path(str(args.checkout_root)) / canonical_slug)
    if not checkout:
        raise MigrationError(
            "cannot resolve checkout: pass --checkout or --checkout-root"
        )
    if not Path(checkout).is_absolute():
        raise MigrationError(f"checkout must be absolute: {checkout}")
    return checkout


def _stage_preflight(
    args: argparse.Namespace,
    facts: list[BoardFact],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    repository = _require_repository(args)
    report = _preflight(
        repository, facts, allow_recreate_canonical=args.allow_recreate_canonical
    )
    if report["errors"]:
        raise MigrationError("; ".join(report["errors"]))
    print(
        json.dumps(
            {"stage": "preflight", "dry_run": dry_run, **report},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return report


def _stage_migrate(
    args: argparse.Namespace,
    facts: list[BoardFact],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    repository = _require_repository(args)
    report = _preflight(
        repository, facts, allow_recreate_canonical=args.allow_recreate_canonical
    )
    if report["errors"]:
        raise MigrationError("; ".join(report["errors"]))

    canonical_slug = report["canonical_slug"]
    display_name = report["display_name"]
    canonical = next(
        (
            f
            for f in facts
            if f.slug.casefold() == canonical_slug and not f.archived
        ),
        None,
    )
    legacy = [f for f in facts if f.slug in report["legacy_boards"]]
    # A checkout is only needed when the canonical board must be created;
    # an already-live canonical board (display normalization) keeps its own
    # declared default_workdir.
    checkout = (
        _resolve_checkout(args, repository, legacy) if canonical is None else ""
    )

    evidence = _load_evidence(args.state_root, repository)
    action = _ensure_canonical_board(
        args.hermes_bin,
        repository,
        display_name,
        checkout,
        dry_run=dry_run,
    )

    backups: dict[str, dict[str, Any]] = {}
    if not dry_run:
        for board in legacy:
            target, checksums = _backup(board, args.backup_root)
            backups[board.slug] = {"path": str(target), "checksums": checksums}
            current = _file_map(board.directory)
            if current != checksums:
                raise MigrationError(
                    f"legacy board {board.slug} changed during backup; failing closed"
                )

    if not dry_run:
        evidence.update(
            {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "repository": repository,
                "display_name": display_name,
                "canonical_slug": canonical_slug,
                "canonical_action": action,
                "checkout": checkout,
                "legacy_boards": [b.slug for b in legacy],
                "legacy_backups": {
                    slug: info["path"] for slug, info in backups.items()
                },
                "legacy_checksums": {
                    slug: info["checksums"] for slug, info in backups.items()
                },
                "legacy_task_counts": {b.slug: b.task_count for b in legacy},
                "stage": "migrated",
            }
        )
        _save_evidence(args.state_root, repository, evidence)

    print(
        json.dumps(
            {
                "stage": "migrate",
                "dry_run": dry_run,
                "repository": repository,
                "checkout": checkout,
                "canonical_action": action,
                "legacy_boards": [b.slug for b in legacy],
                "backups": {slug: info["path"] for slug, info in backups.items()},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return {
        **report,
        "canonical_action": action,
        "checkout": checkout,
        "backups": {slug: info["path"] for slug, info in backups.items()},
    }


def _archived_variants(boards_root: Path, slug: str) -> list[Path]:
    """Recoverable archive records for one slug (<slug> or <slug>-<stamp>)."""
    archive_root = boards_root / "_archived"
    if not archive_root.is_dir():
        return []
    return [
        child
        for child in sorted(archive_root.iterdir(), key=lambda p: p.name)
        if child.is_dir() and (child.name == slug or child.name.startswith(f"{slug}-"))
    ]


def _stage_transition(
    args: argparse.Namespace,
    facts: list[BoardFact],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not args.require_provenance:
        raise MigrationError("transition requires --require-provenance")
    if not args.confirm_live_transition:
        raise MigrationError("transition requires --confirm-live-transition")
    repository = _require_repository(args)
    evidence = _load_evidence(args.state_root, repository)
    if evidence.get("stage") != "migrated" or not evidence.get("repository"):
        raise MigrationError(
            "migration evidence is incomplete; run preflight+migrate before transition"
        )
    if str(evidence.get("repository", "")).casefold() != repository.casefold():
        raise MigrationError("migration evidence belongs to a different repository")

    anchor_task_ids = frozenset(evidence.get("anchor_task_ids", {}).values())
    report = _preflight(
        repository,
        facts,
        allow_recreate_canonical=args.allow_recreate_canonical,
        anchor_task_ids=anchor_task_ids,
    )
    if report["errors"]:
        raise MigrationError("pre-transition re-check failed: " + "; ".join(report["errors"]))
    if not report["legacy_boards"]:
        raise MigrationError("no live legacy board left to transition")

    if args.require_provenance:
        # --require-provenance is a real re-verification, not just an
        # operator flag: the live legacy boards must still be exactly the
        # ones recorded in the migration evidence (same tasks, still
        # terminal-only) before the archive step runs.
        recorded_counts = evidence.get("legacy_task_counts", {})
        for slug in report["legacy_boards"]:
            fact = next(f for f in facts if f.slug == slug)
            if int(recorded_counts.get(slug, -1)) != fact.task_count:
                raise MigrationError(
                    f"legacy board {slug} task count changed since migrate "
                    f"({recorded_counts.get(slug)!r} -> {fact.task_count})"
                )
            if fact.non_terminal:
                raise MigrationError(
                    f"legacy board {slug} has non-terminal tasks since migrate"
                )

    canonical = next(
        (
            f
            for f in facts
            if f.slug.casefold() == report["canonical_slug"] and not f.archived
        ),
        None,
    )
    if canonical is None:
        raise MigrationError(
            "canonical board is not live; migration evidence is stale"
        )

    live = _live_boards(args.hermes_bin)
    if canonical.slug not in live:
        raise MigrationError("canonical board is not live in the Hermes CLI view")
    if live[canonical.slug].casefold() != report["display_name"].casefold():
        if not dry_run:
            _run_hermes(
                args.hermes_bin,
                "kanban",
                "boards",
                "rename",
                canonical.slug,
                report["display_name"],
            )

    legacy_slugs = report["legacy_boards"]
    carried: dict[str, str] = dict(evidence.get("anchor_task_ids", {}))
    if not dry_run:
        # Carry the idempotency anchors BEFORE the legacy board leaves the
        # live set: the source database must still be readable at its
        # original path, and a failed archive leaves the anchors on the
        # canonical board where rollback can purge them.
        carry_sources = {
            slug: next(f for f in facts if f.slug == slug) for slug in legacy_slugs
        }
        for slug in legacy_slugs:
            carried.update(
                _carry_anchors(
                    args.hermes_bin,
                    repository,
                    carry_sources[slug].directory,
                    report["canonical_slug"],
                    report["display_name"],
                    dry_run=dry_run,
                )
            )
        evidence["anchor_task_ids"] = carried
        for slug in legacy_slugs:
            # Recoverable archive (the CLI default); never --delete.
            _run_hermes(args.hermes_bin, "kanban", "boards", "rm", slug)
        evidence["stage"] = "transitioned"
        evidence["transitioned_boards"] = list(legacy_slugs)
        evidence["transitioned_at"] = int(time.time())
        _save_evidence(args.state_root, repository, evidence)

    print(
        json.dumps(
            {
                "stage": "transition",
                "dry_run": dry_run,
                "repository": repository,
                "canonical": canonical.slug,
                "archived_legacy": legacy_slugs,
                "carried_anchors": sorted(carried.keys()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return {**report, "archived_legacy": list(legacy_slugs)}


def _stage_postcheck(
    args: argparse.Namespace,
    facts: list[BoardFact],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not args.require_provenance:
        raise MigrationError("postcheck requires --require-provenance")
    repository = _require_repository(args)
    evidence = _load_evidence(args.state_root, repository)
    if evidence.get("stage") != "transitioned":
        raise MigrationError("postcheck requires transitioned evidence")

    canonical_slug = str(evidence["canonical_slug"])
    display_name = str(evidence["display_name"])
    live = _live_boards(args.hermes_bin)

    errors: list[str] = []
    if canonical_slug not in live:
        errors.append("canonical board is not live after transition")
    elif live[canonical_slug].casefold() != display_name.casefold():
        errors.append(
            f"canonical display name is {live[canonical_slug]!r}, "
            f"expected {display_name!r}"
        )
    for slug in evidence.get("legacy_boards", []):
        if slug in live:
            errors.append(f"legacy board {slug} is still live after transition")

    # The archived legacy board must match the recorded backup checksums.
    for slug, backup_path in evidence.get("legacy_backups", {}).items():
        matches = _archived_variants(args.boards_root, slug)
        expected = evidence.get("legacy_checksums", {}).get(slug, {})
        if not any(_file_map(candidate) == expected for candidate in matches):
            errors.append(
                f"archived legacy board {slug} does not match the recorded "
                f"backup {backup_path}"
            )

    if errors:
        raise MigrationError("postcheck failed: " + "; ".join(errors))
    print(
        json.dumps(
            {
                "stage": "postcheck",
                "dry_run": dry_run,
                "repository": repository,
                "canonical": canonical_slug,
                "display_name": display_name,
                "legacy_boards_archived": list(evidence.get("legacy_boards", [])),
                "ok": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return {"ok": True}


def _stage_rollback(
    args: argparse.Namespace,
    facts: list[BoardFact],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not args.confirm_rollback:
        raise MigrationError("rollback requires --confirm-rollback")
    repository = _require_repository(args)
    evidence = _load_evidence(args.state_root, repository)
    if not evidence:
        raise MigrationError("no migration evidence; nothing to roll back")

    live = _live_boards(args.hermes_bin)
    canonical_slug = str(evidence.get("canonical_slug", ""))
    errors: list[str] = []

    # The canonical board may only be removed while it is still empty: the
    # migration never creates tasks, so a populated canonical board means
    # post-transition work happened — fail closed instead of deleting data.
    canonical_fact = next(
        (
            f
            for f in facts
            if f.slug.casefold() == canonical_slug and not f.archived
        ),
        None,
    )
    anchor_ids = frozenset(evidence.get("anchor_task_ids", {}).values())
    foreign_count = (
        _task_count_excluding(canonical_fact.directory, anchor_ids)
        if canonical_fact is not None
        else 0
    )
    if foreign_count > 0:
        errors.append(
            f"canonical board {canonical_slug} has {foreign_count} non-anchor "
            "task(s); refusing to remove a populated canonical board"
        )

    restored: list[str] = []
    for slug in evidence.get("legacy_boards", []):
        if slug in live:
            continue  # already restored: idempotent re-run
        candidate: Path | None = None
        backup_path = str(evidence.get("legacy_backups", {}).get(slug, "")).strip()
        if backup_path:
            candidate = Path(backup_path)
            if not candidate.is_dir():
                candidate = None
        if candidate is None:
            matches = _archived_variants(args.boards_root, slug)
            if len(matches) != 1:
                errors.append(
                    f"cannot locate exactly one archived copy of legacy board {slug}"
                )
                continue
            candidate = matches[0]
        if not dry_run:
            target = args.boards_root / slug
            if target.exists():
                raise MigrationError(f"target {target} already exists; refusing")
            shutil.copytree(candidate, target, symlinks=True)
            restored.append(slug)

    if not dry_run and not errors:
        _remove_anchors(
            args.hermes_bin,
            canonical_slug,
            dict(evidence.get("anchor_task_ids", {})),
        )

    if canonical_fact is not None and not errors and not dry_run:
        _run_hermes(args.hermes_bin, "kanban", "boards", "rm", canonical_slug)

    if errors:
        raise MigrationError("rollback failed closed: " + "; ".join(errors))

    if not dry_run:
        # Revert to the migrated stage so a corrected retry can re-transition.
        evidence["stage"] = "migrated"
        evidence.pop("transitioned_boards", None)
        evidence.pop("transitioned_at", None)
        evidence.pop("anchor_task_ids", None)
        _save_evidence(args.state_root, repository, evidence)

    print(
        json.dumps(
            {
                "stage": "rollback",
                "dry_run": dry_run,
                "repository": repository,
                "restored_legacy": restored,
                "canonical_removed": canonical_fact is not None,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return {"restored_legacy": restored}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository", help="GitHub repository owner/name (required)"
    )
    parser.add_argument(
        "--boards-root",
        default=os.environ.get(
            "HERMES_KANBAN_BOARDS_ROOT", "/home/hermes/.hermes/kanban/boards"
        ),
        type=Path,
        help="Kanban boards root (default: $HERMES_KANBAN_BOARDS_ROOT or the "
        "production path)",
    )
    parser.add_argument(
        "--hermes-bin",
        default=os.environ.get("HERMES_BIN", "hermes"),
        help="Hermes CLI binary (default: $HERMES_BIN or 'hermes')",
    )
    parser.add_argument(
        "--state-root",
        default=os.environ.get(
            "HERMES_BOARD_IDENTITY_STATE_ROOT",
            str(
                Path(__file__).resolve().parents[1]
                / "state"
                / "board-identity-migration"
            ),
        ),
        type=Path,
        help="migration evidence root",
    )
    parser.add_argument(
        "--backup-root",
        default=os.environ.get(
            "HERMES_BOARD_IDENTITY_BACKUP_ROOT",
            str(
                Path(__file__).resolve().parents[1]
                / "backups"
                / "board-identity-migration"
            ),
        ),
        type=Path,
        help="legacy board backup root",
    )
    parser.add_argument(
        "--checkout",
        help="checkout path for the canonical board default_workdir",
    )
    parser.add_argument(
        "--checkout-root",
        help="fallback checkout root (board directory = <root>/<canonical slug>)",
    )
    parser.add_argument(
        "--allow-recreate-canonical",
        action="store_true",
        help="allow creating the canonical board when none is live (reviewed "
        "migration only)",
    )
    parser.add_argument(
        "--require-provenance",
        action="store_true",
        help="assert that transition/postcheck is backed by durable task "
        "provenance evidence",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    for name in ("preflight", "migrate", "transition", "postcheck", "rollback"):
        p = sub.add_parser(name)
        _add_common(p)
        p.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "read-only (preflight/migrate); refused for transition/rollback "
                "because those stages mutate"
            ),
        )
        if name == "transition":
            p.add_argument(
                "--confirm-live-transition",
                action="store_true",
                help="operator confirmation to perform the live archive",
            )
        if name == "rollback":
            p.add_argument(
                "--confirm-rollback",
                action="store_true",
                help="operator confirmation to restore the pre-transition state",
            )

    args = parser.parse_args(argv)
    dry_run = bool(getattr(args, "dry_run", False))

    # Transition and rollback always perform real mutations; --dry-run is
    # refused so the stage semantics stay unambiguous.
    if args.stage in ("transition", "rollback") and dry_run:
        parser.error(f"{args.stage} does not support --dry-run (it mutates)")

    facts = _scan_boards(args.boards_root)

    try:
        if args.stage == "preflight":
            _stage_preflight(args, facts, dry_run=dry_run)
        elif args.stage == "migrate":
            _stage_migrate(args, facts, dry_run=dry_run)
        elif args.stage == "transition":
            _stage_transition(args, facts, dry_run=False)
        elif args.stage == "postcheck":
            _stage_postcheck(args, facts, dry_run=dry_run)
        elif args.stage == "rollback":
            _stage_rollback(args, facts, dry_run=False)
    except MigrationError as exc:
        print(f"board-identity-migration: ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
