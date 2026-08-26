#!/usr/bin/env python3
"""Repository-derived intake board identity migration — fixture/integration tests.

Acceptance matrix:
- canonical-only, legacy-only, canonical+legacy, unrelated board,
  non-terminal legacy, mixed/ambiguous provenance, canonical conflict,
  rerun/rollback;
- display identity derived from repository metadata without any static map;
- post-transition registry routing (provenance) and intake provisioning
  (``board_provisioning=[]``);
- the #72-class churn fixture: a re-intake of an already-imported issue
  after the cutover hits the carried idempotency anchor instead of a
  duplicate root card.

Runs as a standalone script: ``python3 tests/test_board_identity_migration.py``
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "automation" / "n8n" / "scripts" / "board_identity_migration.py"
REGISTRY = REPO_ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py"
INTAKE = REPO_ROOT / "automation" / "hermes" / "scripts" / "github-agent-ready-kanban-intake.py"
DEPLOY = REPO_ROOT / "automation" / "hermes" / "scripts" / "deploy-intake-edge.sh"
FAKE_HERMES = Path(__file__).resolve().with_name("fake_hermes.py")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


registry = _load_module("mig_test_registry", REGISTRY)
intake = _load_module("mig_test_intake", INTAKE)
migration = _load_module("mig_test_migration", MIGRATION)


class Sandbox:
    """Temp-directory harness: fake boards root + fake hermes CLI runner."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.boards_root = tmp / "boards"
        self.boards_root.mkdir(parents=True)
        self.state_root = tmp / "state"
        self.backup_root = tmp / "backups"
        self.checkout_root = tmp / "checkouts"
        self.fake_hermes = tmp / "fake_hermes.py"
        shutil.copy2(FAKE_HERMES, self.fake_hermes)
        os.chmod(self.fake_hermes, 0o755)
        self.env = dict(os.environ)
        self.env["FAKE_KANBAN_BOARDS_ROOT"] = str(self.boards_root)
        for key in (
            "HERMES_KANBAN_BOARDS_ROOT",
            "HERMES_BOARD_IDENTITY_STATE_ROOT",
            "HERMES_BOARD_IDENTITY_BACKUP_ROOT",
            "HERMES_BIN",
        ):
            self.env.pop(key, None)

    @staticmethod
    def _ensure_checkout(path: Path, repository: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            subprocess.run(
                ["git", "init", "--quiet", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        remote = f"https://github.com/{repository}.git"
        current = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
        )
        if current.returncode != 0:
            subprocess.run(
                ["git", "-C", str(path), "remote", "add", "origin", remote],
                check=True,
                capture_output=True,
                text=True,
            )
        elif current.stdout.strip() != remote:
            subprocess.run(
                ["git", "-C", str(path), "remote", "set-url", "origin", remote],
                check=True,
                capture_output=True,
                text=True,
            )

    def make_board(
        self,
        slug: str,
        name: str | None = None,
        workdir: str | None = None,
        tasks: list[tuple[str, str | None]] | None = None,
    ) -> Path:
        if workdir:
            repository = "rhgo1749/ctrl-hangul"
            for _status, key in tasks or []:
                if key and key.startswith("github:") and ":issue:" in key:
                    repository = key[len("github:") :].rsplit(":issue:", 1)[0]
                    break
            self._ensure_checkout(Path(workdir), repository)
        board_dir = self.boards_root / slug
        board_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(board_dir / "kanban.db")
        con.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, "
            "title TEXT, status TEXT, idempotency_key TEXT)"
        )
        for index, (status, key) in enumerate(tasks or []):
            con.execute(
                "INSERT INTO tasks VALUES (?,?,?,?)",
                (f"t_{slug}_{index}", f"task {index}", status, key),
            )
        con.commit()
        con.close()
        (board_dir / "board.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "name": name or slug,
                    "default_workdir": workdir or "",
                    "archived": False,
                }
            ),
            encoding="utf-8",
        )
        return board_dir

    def archive_board_dir(
        self, slug: str, tasks: list[tuple[str, str | None]] | None = None
    ) -> Path:
        """Simulate a previously archived board (recoverable copy)."""
        board_dir = self.make_board(slug, tasks=tasks)
        arch = self.boards_root / "_archived"
        arch.mkdir(parents=True, exist_ok=True)
        target = arch / f"{slug}-1787657316"
        suffix = 1
        while target.exists():
            target = arch / f"{slug}-1787657316-{suffix}"
            suffix += 1
        board_dir.rename(target)
        return target

    def live_slugs(self) -> set[str]:
        proc = subprocess.run(
            [
                sys.executable,
                str(self.fake_hermes),
                "kanban",
                "boards",
                "list",
                "--json",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return {item["slug"] for item in json.loads(proc.stdout)}

    def board_meta(self, slug: str) -> dict:
        return json.loads(
            (self.boards_root / slug / "board.json").read_text(encoding="utf-8")
        )

    def board_tasks(self, slug: str) -> list[tuple[str, str, str]]:
        con = sqlite3.connect(self.boards_root / slug / "kanban.db")
        rows = con.execute(
            "SELECT id, status, idempotency_key FROM tasks"
        ).fetchall()
        con.close()
        return rows

    def archived_dirs(self, slug: str) -> list[Path]:
        arch = self.boards_root / "_archived"
        if not arch.is_dir():
            return []
        return [
            child
            for child in arch.iterdir()
            if child.is_dir()
            and (child.name == slug or child.name.startswith(f"{slug}-"))
        ]

    def run(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(MIGRATION),
            *args,
            "--boards-root",
            str(self.boards_root),
            "--hermes-bin",
            str(self.fake_hermes),
            "--state-root",
            str(self.state_root),
            "--backup-root",
            str(self.backup_root),
        ]
        return subprocess.run(
            cmd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_with_checkout(self, *args: str) -> subprocess.CompletedProcess:
        self._ensure_checkout(
            self.checkout_root / "ctrl-hangul",
            "rhgo1749/ctrl-hangul",
        )
        cmd = [
            sys.executable,
            str(MIGRATION),
            *args,
            "--boards-root",
            str(self.boards_root),
            "--hermes-bin",
            str(self.fake_hermes),
            "--state-root",
            str(self.state_root),
            "--backup-root",
            str(self.backup_root),
            "--checkout-root",
            str(self.checkout_root),
        ]
        return subprocess.run(
            cmd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )


def _fixture_repos() -> list[dict]:
    """The five registry-managed repositories (live snapshot shape)."""
    repos = [
        "rhgo1749/H4V3-DJ",
        "rhgo1749/H4V3-Meowcore",
        "rhgo1749/ctrl-hangul",
        "rhgo1749/hermes-n8n-control-plane",
        "rhgo1749/re-bound",
    ]
    return [
        {
            "id": index,
            "full_name": full_name,
            "default_branch": "main",
            "archived": False,
            "owner": {"login": full_name.split("/")[0]},
            "contract_paths": ["AGENTS.md"],
        }
        for index, full_name in enumerate(repos, start=1)
    ]


def test_registry_derives_identity_without_static_maps() -> None:
    """Acceptance: 5-repo snapshot; slug/name derivation is deterministic
    from repository metadata, no static mapping involved."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repos = _fixture_repos()
        for repo in repos:
            (root / repo["full_name"].split("/")[-1].casefold()).mkdir()

        origin_by_name = {
            repo["full_name"].split("/")[-1].casefold(): (
                "https://github.com/" + repo["full_name"]
            )
            for repo in repos
        }
        snapshot = registry.registry_snapshot(
            repos,
            root,
            contract_reader=lambda _r, _b: ("AGENTS.md",),
            board_resolver=lambda _r: (None, "not_found_task_provenance"),
            origin_reader=lambda p: origin_by_name[p.name.casefold()],
        )
        assert snapshot["board_authority"] == "tasks.idempotency_key"
        by_repo = {entry["repository"]: entry for entry in snapshot["repositories"]}
        for repo in repos:
            entry = by_repo[repo["full_name"]]
            name = repo["full_name"].split("/")[-1]
            # Repository metadata is the ONLY identity authority.
            assert entry["canonical_slug"] == name.casefold()
            assert entry["display_name"] == name
            # No live board yet -> first-intake bootstrap intent, generic.
            assert entry["bootstrap"] is not None
            assert entry["bootstrap"]["board"] == name.casefold()


def test_intake_display_identity_is_repository_derived(tmp: Path) -> None:
    """No static board->label map: display identity comes from the config
    (validated as repository-derived at load)."""
    fixture = tmp / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "repository": "rhgo1749/ctrl-hangul",
                "repository_config": {
                    "board": "ctrl-hangul",
                    "checkout": str(tmp / "checkout"),
                    "default_branch": "main",
                    "contract_paths": ["AGENTS.md"],
                },
            }
        ),
        encoding="utf-8",
    )
    configs = intake._fixture_repository_configs(fixture)
    assert configs[0].display_name == "ctrl-hangul"
    assert (
        intake._board_display_name("ctrl-hangul", "rhgo1749/ctrl-hangul", configs)
        == "ctrl-hangul"
    )
    # A fixture with a non-repository display name fails closed.
    bad = tmp / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "repository": "rhgo1749/ctrl-hangul",
                "repository_config": {
                    "board": "ctrl-hangul",
                    "checkout": str(tmp / "checkout"),
                    "default_branch": "main",
                    "contract_paths": [],
                    "display_name": "CtrlHangul GitHub Intake",
                },
            }
        ),
        encoding="utf-8",
    )
    try:
        intake._fixture_repository_configs(bad)
        raise AssertionError("non-repository display_name must fail closed")
    except intake.IntakeError:
        pass


def test_no_static_maps_remain_in_sources() -> None:
    intake_source = INTAKE.read_text(encoding="utf-8")
    assert "_BOARD_SHORT_NAMES" not in intake_source
    assert "Avatar-Lab" not in intake_source
    assert "CtrlHangul" not in intake_source
    assert "def _board_display_name" in intake_source
    registry_source = REGISTRY.read_text(encoding="utf-8")
    assert "display_name" in registry_source
    migration_source = MIGRATION.read_text(encoding="utf-8")
    # No repository-specific alias/allowlist tables in the migration tool:
    # identity is derived from repository metadata + task provenance only.
    assert "ctrlhangul" not in migration_source.lower()
    assert "_BOARD_SHORT_NAMES" not in migration_source
    assert "ALIAS" not in migration_source


def test_preflight_migrate_transition_postcheck_cycle(tmp: Path) -> None:
    """Full reviewed cycle on a sandbox mimicking the live precondition:
    populated legacy ``ctrlhangul`` (terminal tasks incl. #72) is live; the
    empty canonical duplicate is already archived; the canonical target is
    discovered/created only by the migration."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[
            ("done", "github:rhgo1749/ctrl-hangul:issue:72"),
            ("done", "github:rhgo1749/ctrl-hangul:issue:50"),
            ("archived", "github:rhgo1749/ctrl-hangul:issue:49"),
            ("done", "canary:local:1"),
        ],
    )
    # Current live precondition: the empty canonical duplicate is archived.
    sandbox.archive_board_dir("ctrl-hangul")
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert "ctrlhangul" in sandbox.live_slugs()

    common = ["--repository", repo]

    # 1) preflight: legacy-only classification, no errors.
    proc = sandbox.run_with_checkout("preflight", "--dry-run", *common)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["canonical_board"] is None
    assert report["legacy_boards"] == ["ctrlhangul"]
    assert report["ambiguous_boards"] == []
    assert report["errors"] == []

    # 2) migrate dry-run: would create the canonical board; legacy untouched.
    proc = sandbox.run_with_checkout("migrate", "--dry-run", *common)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["canonical_action"] == "would-create"
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert "ctrlhangul" in sandbox.live_slugs()

    # 3) migrate (real): canonical created with the repository-derived
    #    display name; legacy untouched (data-preserving); evidence saved.
    proc = sandbox.run_with_checkout("migrate", *common)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["canonical_action"] == "created"
    assert sandbox.board_meta("ctrl-hangul")["name"] == "ctrl-hangul"
    assert (
        sandbox.board_meta("ctrl-hangul")["default_workdir"] == str(tmp / "checkout")
    )
    assert "ctrl-hangul" in sandbox.live_slugs()
    assert "ctrlhangul" in sandbox.live_slugs()
    assert len(sandbox.board_tasks("ctrlhangul")) == 4
    assert len(list(sandbox.backup_root.glob("ctrlhangul-*"))) == 1
    evidence = json.loads((sandbox.state_root / "ctrl-hangul.json").read_text())
    assert evidence["stage"] == "migrated"
    assert evidence["legacy_boards"] == ["ctrlhangul"]

    # 4) rerun safety: migrate again is idempotent.
    proc = sandbox.run_with_checkout("migrate", *common)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["canonical_action"] == "already-live"
    assert sandbox.board_meta("ctrl-hangul")["name"] == "ctrl-hangul"

    # 5) transition without the gates is rejected.
    proc = sandbox.run_with_checkout("transition", *common)
    assert proc.returncode == 1
    assert "--require-provenance" in proc.stderr
    proc = sandbox.run_with_checkout("transition", "--require-provenance", *common)
    assert proc.returncode == 1
    assert "--confirm-live-transition" in proc.stderr

    # 6) reviewed transition: legacy archived (recoverable), anchors carried,
    #    evidence transitioned.
    proc = sandbox.run_with_checkout(
        "transition",
        "--require-provenance",
        "--confirm-live-transition",
        *common,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ctrlhangul" not in sandbox.live_slugs()
    assert "ctrl-hangul" in sandbox.live_slugs()
    assert sandbox.archived_dirs("ctrlhangul")
    evidence = json.loads((sandbox.state_root / "ctrl-hangul.json").read_text())
    assert evidence["stage"] == "transitioned"
    assert evidence["anchor_task_ids"]
    anchor_key = "github:rhgo1749/ctrl-hangul:issue:72"
    anchored = [row for row in sandbox.board_tasks("ctrl-hangul") if row[2] == anchor_key]
    assert len(anchored) == 1
    assert anchored[0][1] == "done"

    # 7) post-transition registry routing: provenance resolves the repository
    #    to the canonical board; provisioning is empty.
    board_evidence = registry._kanban_board_repository_evidence(sandbox.boards_root)
    board, status = registry._resolve_board(repo, board_evidence)
    assert board == "ctrl-hangul"
    assert status == "resolved_task_provenance"

    # 8) postcheck passes.
    proc = sandbox.run_with_checkout("postcheck", "--require-provenance", *common)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_churn_prevention_after_cutover(tmp: Path) -> None:
    """#72-class churn: after the cutover, a re-intake of an already-imported
    issue must hit the carried idempotency anchor; no duplicate root card.
    Acceptance: a fresh dry-run sees only the canonical target with
    ``board_provisioning=[]``."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    sandbox.run_with_checkout("migrate", *common)
    sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    key = "github:rhgo1749/ctrl-hangul:issue:72"
    before = [row for row in sandbox.board_tasks("ctrl-hangul") if row[2] == key]
    assert len(before) == 1

    # Intake create on the canonical board with the same key returns the
    # anchor (core idempotency rule) instead of a new card.
    proc = subprocess.run(
        [
            sys.executable,
            str(sandbox.fake_hermes),
            "kanban",
            "--board",
            "ctrl-hangul",
            "create",
            "GitHub Issue intake: rhgo1749/ctrl-hangul#72 re-intake",
            "--idempotency-key",
            key,
            "--json",
        ],
        env=sandbox.env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["id"] == before[0][0]
    after = [row for row in sandbox.board_tasks("ctrl-hangul") if row[2] == key]
    assert len(after) == 1

    # Registry-level provisioning after the cutover is empty.
    board_evidence = registry._kanban_board_repository_evidence(sandbox.boards_root)
    repos = [
        {
            "id": 7,
            "full_name": repo,
            "default_branch": "main",
            "archived": False,
            "owner": {"login": "rhgo1749"},
        }
    ]
    with tempfile.TemporaryDirectory() as td:
        snapshot = registry.registry_snapshot(
            repos,
            Path(td),
            contract_reader=lambda _r, _b: (),
            board_resolver=lambda _r: registry._resolve_board(_r, board_evidence),
        )
        entry = snapshot["repositories"][0]
        assert entry["board"] == "ctrl-hangul"
        assert entry["bootstrap"] is None
    intake._board_slugs = lambda: sandbox.live_slugs()
    provisioning = intake._provision_bootstrap_boards(snapshot, dry_run=True)
    assert provisioning == []


def test_non_terminal_legacy_fails_closed(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72"), ("todo", None)],
    )
    proc = sandbox.run_with_checkout("preflight", "--repository", repo)
    assert proc.returncode == 1
    assert "non-terminal" in proc.stderr


def test_mixed_provenance_fails_closed(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[
            ("done", "github:rhgo1749/ctrl-hangul:issue:72"),
            ("done", "github:rhgo1749/re-bound:issue:10"),
        ],
    )
    proc = sandbox.run_with_checkout("preflight", "--repository", repo)
    assert proc.returncode == 1
    assert "ambiguous" in proc.stderr


def test_canonical_conflict_fails_closed(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    # A live board named like the canonical slug but carrying foreign
    # provenance is a conflict, not a canonical target.
    sandbox.make_board(
        "ctrl-hangul-foreign",
        name="something-else",
        workdir=str(tmp / "other"),
        tasks=[("done", "github:rhgo1749/re-bound:issue:10")],
    )
    # And a canonical-slug board with a different repository's provenance.
    sandbox.make_board(
        "re-bound",
        name="re-bound",
        workdir=str(tmp / "other2"),
        tasks=[("done", "github:rhgo1749/re-bound:issue:10")],
    )
    proc = sandbox.run_with_checkout("preflight", "--repository", repo)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    # Boards owned by another repository (foreign provenance only) are out
    # of scope for this cutover and do not block it; the repository itself
    # still has no live canonical board.
    assert report["ambiguous_boards"] == []
    assert report["canonical_board"] is None

    # Now the real canonical conflict: a board named ctrl-hangul carrying a
    # different repository's provenance.
    sandbox2 = Sandbox(tmp / "c2")
    sandbox2.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    sandbox2.make_board(
        "ctrl-hangul",
        name="cloned",
        workdir=str(tmp / "other"),
        tasks=[("done", "github:rhgo1749/re-bound:issue:10")],
    )
    proc = sandbox2.run_with_checkout("preflight", "--repository", repo)
    assert proc.returncode == 1
    assert "ambiguous" in proc.stderr


def test_unrelated_board_untouched(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    sandbox.make_board("default", name="Default", workdir="")
    sandbox.make_board("test_board", name="Test", workdir="")
    common = ["--repository", repo]
    sandbox.run_with_checkout("migrate", *common)
    sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    live = sandbox.live_slugs()
    assert "default" in live
    assert "test_board" in live
    assert "ctrlhangul" not in live
    assert "ctrl-hangul" in live


def test_canonical_only_display_normalization(tmp: Path) -> None:
    """A board already on the canonical slug with a stale display name is
    normalized to the repository-derived value; no data migration."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrl-hangul",
        name="CtrlHangul GitHub Intake",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    proc = sandbox.run_with_checkout("preflight", *common)
    assert proc.returncode == 0, proc.stderr
    proc = sandbox.run_with_checkout("migrate", *common)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["canonical_action"] == "normalized-display"
    assert sandbox.board_meta("ctrl-hangul")["name"] == "ctrl-hangul"


def test_missing_checkout_fails_closed(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    # Legacy board without a declared workdir and no fallback root: fail.
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=None,
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    proc = sandbox.run("migrate", "--repository", repo)
    assert proc.returncode == 1
    assert "checkout" in proc.stderr.lower()

    # With a fallback checkout root the migration proceeds.
    sandbox2 = Sandbox(tmp / "c2")
    sandbox2.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=None,
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    proc = sandbox2.run_with_checkout("migrate", "--repository", repo)
    assert proc.returncode == 0, proc.stderr
    assert (
        json.loads(proc.stdout)["checkout"]
        == str(sandbox2.checkout_root / "ctrl-hangul")
    )


def test_missing_checkout_cannot_create_canonical(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    metadata_path = sandbox.boards_root / "ctrlhangul" / "board.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["default_workdir"] = str(tmp / "missing-checkout")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    proc = sandbox.run("migrate", "--repository", repo)
    assert proc.returncode == 1
    assert "does not exist" in proc.stderr
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert not sandbox.backup_root.exists()


def test_non_git_checkout_cannot_create_canonical(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    checkout = tmp / "non-git-checkout"
    checkout.mkdir()
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    metadata_path = sandbox.boards_root / "ctrlhangul" / "board.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["default_workdir"] = str(checkout)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    proc = sandbox.run("migrate", "--repository", repo)
    assert proc.returncode == 1
    assert "verified Git root" in proc.stderr
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert not sandbox.backup_root.exists()


def test_wrong_origin_checkout_cannot_create_canonical(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    checkout = tmp / "wrong-origin-checkout"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(checkout),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "set-url", "origin", "https://github.com/rhgo1749/re-bound.git"],
        check=True,
        capture_output=True,
        text=True,
    )

    proc = sandbox.run("migrate", "--repository", repo)
    assert proc.returncode == 1
    assert "origin mismatch" in proc.stderr
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert not sandbox.backup_root.exists()


def test_rollback_restores_and_reruns(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[
            ("done", "github:rhgo1749/ctrl-hangul:issue:72"),
            ("archived", "github:rhgo1749/ctrl-hangul:issue:49"),
        ],
    )
    common = ["--repository", repo]
    sandbox.run_with_checkout("migrate", *common)
    sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    # Rollback without confirmation is refused.
    proc = sandbox.run_with_checkout("rollback", *common)
    assert proc.returncode == 1
    assert "--confirm-rollback" in proc.stderr

    # Confirmed rollback: legacy restored, canonical removed, anchors purged,
    # evidence back to migrated.
    proc = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert proc.returncode == 0, proc.stderr
    live = sandbox.live_slugs()
    assert "ctrlhangul" in live
    assert "ctrl-hangul" not in live
    assert [
        row
        for row in sandbox.board_tasks("ctrlhangul")
        if row[2] and row[2].startswith("github:")
    ]
    evidence = json.loads((sandbox.state_root / "ctrl-hangul.json").read_text())
    assert evidence["stage"] == "migrated"
    assert "anchor_task_ids" not in evidence

    # Rerun safety: the full cycle can be repeated after rollback.
    sandbox.run_with_checkout("migrate", *common)
    sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    assert "ctrlhangul" not in sandbox.live_slugs()
    assert "ctrl-hangul" in sandbox.live_slugs()


def test_rollback_refuses_populated_canonical(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    sandbox.run_with_checkout("migrate", *common)
    sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    # Post-transition work landed on the canonical board: rollback must fail
    # closed instead of deleting live data.
    con = sqlite3.connect(sandbox.boards_root / "ctrl-hangul" / "kanban.db")
    con.execute(
        "INSERT INTO tasks (id, title, status) VALUES ('t_live', 'post cutover', 'todo')"
    )
    con.commit()
    con.close()
    canonical_before = sandbox.board_tasks("ctrl-hangul")
    proc = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert proc.returncode == 1
    assert "populated canonical" in proc.stderr
    assert "ctrl-hangul" in sandbox.live_slugs()
    # Rollback preconditions are all-or-nothing: the legacy board must not be
    # restored and canonical task/removal state must remain untouched.
    assert "ctrlhangul" not in sandbox.live_slugs()
    assert sandbox.board_tasks("ctrl-hangul") == canonical_before


def test_transition_rejects_missing_or_stale_evidence(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    # No evidence at all.
    proc = sandbox.run_with_checkout(
        "transition",
        "--require-provenance",
        "--confirm-live-transition",
        "--repository",
        repo,
    )
    assert proc.returncode == 1
    assert "incomplete" in proc.stderr

    # Evidence for a different repository: its evidence file belongs to
    # re-bound, not to this repository.
    sandbox.run_with_checkout("migrate", "--repository", repo)
    other = sandbox.state_root / "re-bound.json"
    other.write_text(
        json.dumps(
            {
                "schema_version": migration.EVIDENCE_SCHEMA_VERSION,
                "repository": "rhgo1749/re-bound",
                "stage": "migrated",
            }
        ),
        encoding="utf-8",
    )
    # A second sandbox: evidence for re-bound exists but the live boards
    # have no re-bound legacy board -> transition fails closed.
    sandbox2 = Sandbox(tmp / "c2")
    repo2 = "rhgo1749/re-bound"
    sandbox2.state_root.mkdir(parents=True, exist_ok=True)
    (sandbox2.state_root / "re-bound.json").write_text(
        json.dumps(
            {
                "schema_version": migration.EVIDENCE_SCHEMA_VERSION,
                "repository": repo2,
                "stage": "migrated",
                "display_name": "re-bound",
                "canonical_slug": "re-bound",
            }
        ),
        encoding="utf-8",
    )
    sandbox2.make_board("rebound-legacy", name="re-bound", workdir=str(tmp / "checkout"), tasks=[])
    sandbox2.make_board("ctrl-hangul", name="ctrl-hangul", workdir=str(tmp / "other"), tasks=[])
    proc = sandbox2.run_with_checkout(
        "transition",
        "--require-provenance",
        "--confirm-live-transition",
        "--repository",
        repo2,
    )
    assert proc.returncode == 1
    assert "no live legacy board" in proc.stderr

    # Evidence belonging to a different repository is rejected outright:
    # the re-bound evidence file names another repository inside.
    sandbox3 = Sandbox(tmp / "c3")
    sandbox3.state_root.mkdir(parents=True, exist_ok=True)
    (sandbox3.state_root / "re-bound.json").write_text(
        json.dumps(
            {
                "schema_version": migration.EVIDENCE_SCHEMA_VERSION,
                "repository": "rhgo1749/ctrl-hangul",
                "stage": "migrated",
            }
        ),
        encoding="utf-8",
    )
    proc = sandbox3.run_with_checkout(
        "transition",
        "--require-provenance",
        "--confirm-live-transition",
        "--repository",
        repo2,
    )
    assert proc.returncode == 1
    assert "different repository" in proc.stderr


def test_partial_anchor_carry_checkpoints_rerun_and_rollback(tmp: Path) -> None:
    """A failure on the Nth anchor leaves a recoverable checkpoint only."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    keys = [
        "github:rhgo1749/ctrl-hangul:issue:72",
        "github:rhgo1749/ctrl-hangul:issue:73",
    ]
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", key) for key in keys],
    )
    sandbox.env["FAKE_KANBAN_FAIL_TASK_CREATE_N"] = "2"
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0

    failed = sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    assert failed.returncode == 1
    assert "ctrlhangul" in sandbox.live_slugs()
    evidence = json.loads((sandbox.state_root / "ctrl-hangul.json").read_text())
    assert evidence["stage"] == "migrated"
    assert len(evidence["anchor_task_ids"]) == 1
    canonical_keys = {
        row[2]
        for row in sandbox.board_tasks("ctrl-hangul")
        if row[2]
    }
    assert len(canonical_keys) == 1

    retried = sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    )
    assert retried.returncode == 0, retried.stderr
    assert {
        row[2]
        for row in sandbox.board_tasks("ctrl-hangul")
        if row[2]
    } == set(keys)

    rolled_back = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert "ctrlhangul" in sandbox.live_slugs()
    assert "ctrl-hangul" not in sandbox.live_slugs()
    assert not (sandbox.boards_root / "ctrl-hangul").exists()


def test_rollback_rejects_tampered_recorded_anchor_ids(tmp: Path) -> None:
    """A forged/mismatched anchor checkpoint must fail closed.

    Rewriting the evidence's recorded key->task-id pair so it points at a
    task id that the canonical row for that idempotency key does not carry
    must refuse rollback entirely: both live board paths and every canonical
    task row remain unchanged (no anchor purge, no board removal).
    """
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0
    assert sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    ).returncode == 0

    evidence_path = sandbox.state_root / "ctrl-hangul.json"
    evidence = json.loads(evidence_path.read_text())
    assert evidence["stage"] == "transitioned"
    assert set(evidence["anchor_task_ids"]) == {
        "github:rhgo1749/ctrl-hangul:issue:72"
    }
    # Tamper: point the recorded anchor at a task id that is NOT what the
    # canonical row for this idempotency key carries.
    real_id = evidence["anchor_task_ids"]["github:rhgo1749/ctrl-hangul:issue:72"]
    forged_id = (
        "t_forged_0001" if real_id != "t_forged_0001" else "t_forged_0002"
    )
    evidence["anchor_task_ids"]["github:rhgo1749/ctrl-hangul:issue:72"] = forged_id
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    tasks_before = sandbox.board_tasks("ctrl-hangul")
    proc = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert proc.returncode == 1, proc.stdout
    assert "does not match the canonical task row" in proc.stderr

    # Fail-closed: nothing was mutated.
    assert "ctrl-hangul" in sandbox.live_slugs()
    assert "ctrlhangul" not in sandbox.live_slugs()
    assert sandbox.board_tasks("ctrl-hangul") == tasks_before


def test_rollback_purges_only_verified_anchor_pairs(tmp: Path) -> None:
    """Verified anchors are purged by the canonical row's own task id.

    Even when the recorded checkpoint ids are stale relative to a
    reconciled-by-key canonical carry, rollback removes the canonical rows'
    actual ids and leaves no other content behind.
    """
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    keys = [
        "github:rhgo1749/ctrl-hangul:issue:72",
        "github:rhgo1749/ctrl-hangul:issue:73",
    ]
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", key) for key in keys],
    )
    sandbox.env["FAKE_KANBAN_FAIL_TASK_CREATE_N"] = "2"
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0
    assert sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    ).returncode == 1
    # The retried transition reconciles by idempotency key; its checkpoint
    # then records verified pairs only.
    assert sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    ).returncode == 0

    rolled_back = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert "ctrlhangul" in sandbox.live_slugs()
    assert "ctrl-hangul" not in sandbox.live_slugs()
    legacy_keys = {
        row[2] for row in sandbox.board_tasks("ctrlhangul") if row[2]
    }
    assert legacy_keys == set(keys)


def test_rollback_rejects_distinct_content_backup_corruption(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0
    assert sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    ).returncode == 0
    backup = next(sandbox.backup_root.glob("ctrlhangul-*")) / "board.json"
    backup.write_text('{"slug":"ctrlhangul","name":"tampered"}\n', encoding="utf-8")

    rollback = sandbox.run_with_checkout("rollback", "--confirm-rollback", *common)
    assert rollback.returncode == 1
    assert "backup integrity mismatch" in rollback.stderr
    assert "ctrl-hangul" in sandbox.live_slugs()
    assert "ctrlhangul" not in sandbox.live_slugs()


def test_postcheck_rejects_second_live_target_provenance_board(tmp: Path) -> None:
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0
    assert sandbox.run_with_checkout(
        "transition", "--require-provenance", "--confirm-live-transition", *common
    ).returncode == 0
    sandbox.make_board(
        "shadow-board",
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:99")],
    )

    postcheck = sandbox.run_with_checkout("postcheck", "--require-provenance", *common)
    assert postcheck.returncode == 1
    assert "target-provenance" in postcheck.stderr or "ambiguous" in postcheck.stderr


def test_transition_lease_blocks_interleaved_intake_writer(tmp: Path) -> None:
    """The migration lease rejects a writer at the archive boundary."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0

    blocked: list[str] = []
    intake_config = intake.RepositoryConfig(
        name=repo,
        board="ctrlhangul",
        checkout=str(tmp / "checkout"),
        default_branch="main",
        contract_paths=(),
        display_name="ctrl-hangul",
    )
    intake_snapshot = intake.RepoSnapshot(
        origin_sha="fixture",
        remote=f"https://github.com/{repo}.git",
        contract_paths=(),
    )
    issue = {"number": 99, "title": "interleaved", "body": "", "labels": []}
    original_migration_run = migration._run_hermes
    original_intake_run = intake._run_hermes
    old_lease = os.environ.get("HERMES_INTAKE_MIGRATION_LEASE")
    old_fake_root = os.environ.get("FAKE_KANBAN_BOARDS_ROOT")
    os.environ["FAKE_KANBAN_BOARDS_ROOT"] = str(sandbox.boards_root)
    os.environ["HERMES_INTAKE_MIGRATION_LEASE"] = str(
        sandbox.boards_root / ".intake-migration.lock"
    )

    def forbidden_intake_run(*args: str, **kwargs: object) -> object:
        raise AssertionError(f"intake writer bypassed the lease: {args}")

    def interleaving_run(hermes_bin: str, *args: str, **kwargs: object) -> object:
        if args[:4] == ("kanban", "boards", "rm", "ctrlhangul"):
            try:
                intake._create_task(
                    intake_config,
                    issue,
                    intake_snapshot,
                    "2026-08-25T00:00:00Z",
                    tick_started=0,
                )
            except intake.IntakeError as exc:
                blocked.append(str(exc))
            else:
                raise AssertionError("interleaved intake writer unexpectedly succeeded")
        return original_migration_run(hermes_bin, *args, **kwargs)

    intake.__dict__["_run_hermes"] = forbidden_intake_run
    migration.__dict__["_run_hermes"] = interleaving_run
    try:
        args = argparse.Namespace(
            repository=repo,
            boards_root=sandbox.boards_root,
            hermes_bin=str(sandbox.fake_hermes),
            state_root=sandbox.state_root,
            backup_root=sandbox.backup_root,
            lease_path=sandbox.boards_root / ".intake-migration.lock",
            allow_recreate_canonical=False,
            require_provenance=True,
            confirm_live_transition=True,
        )
        migration._stage_transition(args, [], dry_run=False)
    finally:
        migration.__dict__["_run_hermes"] = original_migration_run
        intake.__dict__["_run_hermes"] = original_intake_run
        if old_fake_root is None:
            os.environ.pop("FAKE_KANBAN_BOARDS_ROOT", None)
        else:
            os.environ["FAKE_KANBAN_BOARDS_ROOT"] = old_fake_root
        if old_lease is None:
            os.environ.pop("HERMES_INTAKE_MIGRATION_LEASE", None)
        else:
            os.environ["HERMES_INTAKE_MIGRATION_LEASE"] = old_lease

    assert blocked and "lease is held" in blocked[0]
    assert "ctrlhangul" not in sandbox.live_slugs()
    assert not any(
        row[2] == "github:rhgo1749/ctrl-hangul:issue:99"
        for row in sandbox.board_tasks("ctrl-hangul")
    )


def test_transition_rescan_rejects_out_of_band_source_change(tmp: Path) -> None:
    """A writer outside the cooperating intake still cannot cross archive."""
    sandbox = Sandbox(tmp)
    repo = "rhgo1749/ctrl-hangul"
    sandbox.make_board(
        "ctrlhangul",
        name="ctrl-hangul",
        workdir=str(tmp / "checkout"),
        tasks=[("done", "github:rhgo1749/ctrl-hangul:issue:72")],
    )
    common = ["--repository", repo]
    assert sandbox.run_with_checkout("migrate", *common).returncode == 0

    original_carry = migration._carry_anchors
    old_fake_root = os.environ.get("FAKE_KANBAN_BOARDS_ROOT")
    os.environ["FAKE_KANBAN_BOARDS_ROOT"] = str(sandbox.boards_root)

    def inject_after_carry(*args: object, **kwargs: object) -> dict[str, str]:
        result = original_carry(*args, **kwargs)
        db = sandbox.boards_root / "ctrlhangul" / "kanban.db"
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?)",
            (
                "t_injected",
                "out-of-band",
                "done",
                "github:rhgo1749/ctrl-hangul:issue:99",
            ),
        )
        con.commit()
        con.close()
        return result

    migration.__dict__["_carry_anchors"] = inject_after_carry
    try:
        args = argparse.Namespace(
            repository=repo,
            boards_root=sandbox.boards_root,
            hermes_bin=str(sandbox.fake_hermes),
            state_root=sandbox.state_root,
            backup_root=sandbox.backup_root,
            lease_path=sandbox.boards_root / ".intake-migration.lock",
            allow_recreate_canonical=False,
            require_provenance=True,
            confirm_live_transition=True,
        )
        try:
            migration._stage_transition(args, [], dry_run=False)
        except migration.MigrationError as exc:
            assert "changed during transition" in str(exc)
        else:
            raise AssertionError("out-of-band source mutation must fail closed")
    finally:
        migration.__dict__["_carry_anchors"] = original_carry
        if old_fake_root is None:
            os.environ.pop("FAKE_KANBAN_BOARDS_ROOT", None)
        else:
            os.environ["FAKE_KANBAN_BOARDS_ROOT"] = old_fake_root

    assert "ctrlhangul" in sandbox.live_slugs()
    assert "ctrl-hangul" in sandbox.live_slugs()
    assert any(
        row[2] == "github:rhgo1749/ctrl-hangul:issue:99"
        for row in sandbox.board_tasks("ctrlhangul")
    )


def test_deploy_script_carrying() -> None:
    """The reviewed tool ships with the runtime deployment."""
    assert DEPLOY.is_file()
    source = DEPLOY.read_text(encoding="utf-8")
    assert "board_identity_migration.py" in source
    assert "MIGRATION_SOURCE" in source
    assert subprocess.run(
        ["bash", "-n", str(DEPLOY)], capture_output=True
    ).returncode == 0


def main() -> int:
    failures = 0
    tests = sorted(
        (name, fn)
        for name, fn in list(globals().items())
        if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        try:
            if "tmp" in params:
                with tempfile.TemporaryDirectory(prefix="mig-test-") as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    if failures:
        print(f"{failures} test(s) failed")
        return 1
    print(f"all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
