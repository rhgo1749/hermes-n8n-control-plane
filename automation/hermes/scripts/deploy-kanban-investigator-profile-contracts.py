#!/usr/bin/env python3
"""Deploy the H4V3 Kanban Investigator role boundary into Hermes profiles.

Existing Main/Developer/Reviewer/Designer SOUL files are preserved and receive
one idempotent managed marker block.  The Investigator SOUL is repository-owned
and replaced as a whole because that profile is expected to be bootstrapped
from another profile and must not retain the copied role identity.

The deployer fails closed during preflight: all five profile directories,
SOUL.md files, and repository contract templates must exist before any write.
Every changed SOUL gets a timestamped backup and is replaced atomically.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MARKER_BEGIN = "<!-- BEGIN H4V3 KANBAN INVESTIGATOR CONTRACT -->"
MARKER_END = "<!-- END H4V3 KANBAN INVESTIGATOR CONTRACT -->"

PROFILE_CONTRACTS = {
    "kanban-main": "kanban-main-investigator.md",
    "kanban-developer": "kanban-developer-investigator.md",
    "kanban-reviewer": "kanban-reviewer-investigator.md",
    "kanban-designer": "kanban-designer-investigator.md",
}
INVESTIGATOR_PROFILE = "kanban-investigator"
INVESTIGATOR_SOUL = "kanban-investigator-SOUL.md"


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedWrite:
    profile: str
    soul_path: Path
    content: str
    changed: bool


def _default_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def _contract_root() -> Path:
    return Path(__file__).resolve().parents[1] / "profile-contracts"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeployError(f"cannot read required file: {path}: {exc}") from exc


def _managed_block(contract: str) -> str:
    body = contract.strip()
    return f"{MARKER_BEGIN}\n{body}\n{MARKER_END}"


def _apply_marker(existing: str, contract: str) -> str:
    block = _managed_block(contract)
    start = existing.find(MARKER_BEGIN)
    end = existing.find(MARKER_END)

    if (start == -1) != (end == -1):
        raise DeployError("SOUL contains only one investigator contract marker")
    if start != -1:
        if existing.find(MARKER_BEGIN, start + len(MARKER_BEGIN)) != -1:
            raise DeployError("SOUL contains duplicate investigator begin markers")
        if existing.find(MARKER_END, end + len(MARKER_END)) != -1:
            raise DeployError("SOUL contains duplicate investigator end markers")
        if end < start:
            raise DeployError("SOUL investigator markers are reversed")
        end += len(MARKER_END)
        return existing[:start] + block + existing[end:]

    prefix = existing.rstrip()
    if prefix:
        return prefix + "\n\n" + block + "\n"
    return block + "\n"


def _preflight(hermes_home: Path, contract_root: Path) -> list[PlannedWrite]:
    profiles_root = hermes_home / "profiles"
    if not profiles_root.is_dir():
        raise DeployError(f"Hermes profiles directory not found: {profiles_root}")

    required_templates = [
        contract_root / name for name in PROFILE_CONTRACTS.values()
    ] + [contract_root / INVESTIGATOR_SOUL]
    missing_templates = [path for path in required_templates if not path.is_file()]
    if missing_templates:
        raise DeployError(
            "missing repository profile contract template(s): "
            + ", ".join(str(path) for path in missing_templates)
        )

    required_profiles = [*PROFILE_CONTRACTS.keys(), INVESTIGATOR_PROFILE]
    missing_profiles = [
        name for name in required_profiles if not (profiles_root / name).is_dir()
    ]
    if missing_profiles:
        raise DeployError(
            "missing Hermes profile(s): "
            + ", ".join(missing_profiles)
            + ". Create/copy the profile first; this deployer never clones profile state."
        )

    soul_paths = {name: profiles_root / name / "SOUL.md" for name in required_profiles}
    missing_souls = [name for name, path in soul_paths.items() if not path.is_file()]
    if missing_souls:
        raise DeployError("missing SOUL.md for profile(s): " + ", ".join(missing_souls))

    plans: list[PlannedWrite] = []
    for profile, template_name in PROFILE_CONTRACTS.items():
        current = _read_text(soul_paths[profile])
        contract = _read_text(contract_root / template_name)
        updated = _apply_marker(current, contract)
        plans.append(
            PlannedWrite(
                profile=profile,
                soul_path=soul_paths[profile],
                content=updated,
                changed=updated != current,
            )
        )

    investigator_current = _read_text(soul_paths[INVESTIGATOR_PROFILE])
    investigator_target = _read_text(contract_root / INVESTIGATOR_SOUL).rstrip() + "\n"
    plans.append(
        PlannedWrite(
            profile=INVESTIGATOR_PROFILE,
            soul_path=soul_paths[INVESTIGATOR_PROFILE],
            content=investigator_target,
            changed=investigator_target != investigator_current,
        )
    )
    return plans


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        mode = path.stat().st_mode & 0o777
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def deploy(hermes_home: Path, *, dry_run: bool = False) -> list[PlannedWrite]:
    contract_root = _contract_root()
    plans = _preflight(hermes_home, contract_root)
    changed = [plan for plan in plans if plan.changed]

    print(f"hermes_home={hermes_home}")
    print(f"contract_root={contract_root}")
    for plan in plans:
        print(f"profile={plan.profile} changed={'yes' if plan.changed else 'no'} soul={plan.soul_path}")

    if dry_run or not changed:
        if dry_run:
            print("dry_run=true; no files changed")
        else:
            print("already_converged=true")
        return plans

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups: list[tuple[Path, Path]] = []
    try:
        for plan in changed:
            backup_dir = plan.soul_path.parent / ".h4v3-backups" / f"investigator-contract-{stamp}"
            backup_dir.mkdir(parents=True, exist_ok=False)
            backup = backup_dir / "SOUL.md"
            shutil.copy2(plan.soul_path, backup)
            backups.append((plan.soul_path, backup))

        for plan in changed:
            _atomic_write(plan.soul_path, plan.content)
    except Exception as exc:
        rollback_errors: list[str] = []
        for soul, backup in reversed(backups):
            try:
                if backup.is_file():
                    shutil.copy2(backup, soul)
            except OSError as rollback_exc:
                rollback_errors.append(f"{soul}: {rollback_exc}")
        suffix = ""
        if rollback_errors:
            suffix = "; rollback errors: " + "; ".join(rollback_errors)
        raise DeployError(f"profile deployment failed and rollback was attempted: {exc}{suffix}") from exc

    for soul, backup in backups:
        print(f"backup={backup} source={soul}")
    print(f"changed_profiles={len(changed)}")
    return plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=_default_hermes_home())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        deploy(args.hermes_home.expanduser().resolve(), dry_run=args.dry_run)
    except DeployError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
