#!/usr/bin/env python3
"""Executable regression checks for deploy-github-completion-edge-wake.sh.

The completion wake fires in the Kanban WORKER process, which the dispatcher
spawns with a profile-scoped HERMES_HOME. These checks pin the installer's
profile-coverage contract: every Kanban specialist profile plus the root home
is installed, non-specialist profiles are skipped, an explicit home list is
honored verbatim, and a root home without a deployed edge runtime fails that
home without silently continuing as success.
"""
from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "automation" / "hermes" / "scripts" / "install-github-completion-edge-wake.sh"
DEPLOYER = ROOT / "automation" / "hermes" / "scripts" / "deploy-github-completion-edge-wake.sh"

SPECIALISTS = ("kanban-designer", "kanban-developer", "kanban-main", "kanban-reviewer")


def make_fake_hermes_bin(root: Path, *, fail_enable_for: str = "") -> Path:
    """A fake `hermes` CLI whose `plugins enable` records calls per HERMES_HOME."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = root / "enable-calls.log"
    script = bin_dir / "hermes"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "plugins" && "$2" == "enable" ]]; then\n'
        f'  echo "$HERMES_HOME" >> "{log}"\n'
        f'  if [[ "$HERMES_HOME" == "{fail_enable_for}" ]]; then exit 3; fi\n'
        '  echo "fake enable ok"; exit 0;\n'
        "fi\n"
        "exit 0;\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir / "hermes"


def build_home(root: Path, name: str, *, with_scripts: bool) -> Path:
    home = root / name
    (home / "plugins").mkdir(parents=True)
    if with_scripts:
        (home / "scripts").mkdir()
        # install-github-completion-edge-wake.sh warns (not fails) when the
        # live edge path is absent; provide it so success output is unambiguous.
        (home / "scripts" / "kanban-github-sync.py").write_text("# live edge stub\n")
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
    return home


def run_deployer(env_home: Path | None, fake_bin: Path, *extra: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": f"{fake_bin.parent}:/usr/bin:/bin", "HOME": str(cwd or Path.home())}
    if env_home is not None:
        env["HERMES_HOME"] = str(env_home)
    return subprocess.run(
        ["bash", str(DEPLOYER), "--hermes-bin", str(fake_bin), *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        timeout=300,
    )


def main() -> int:
    for path in (INSTALLER, DEPLOYER):
        assert path.is_file(), f"missing script: {path}"

    with tempfile.TemporaryDirectory(prefix="wake-deploy-check-") as td:
        root = Path(td)
        fake_bin = make_fake_hermes_bin(root)

        # --- Check 1: default coverage = root + all specialist profiles ------
        # Root mirrors the real host: deploy-intake-edge.sh has run, so
        # <root>/scripts exists. Profiles legitimately lack scripts/.
        root_home = build_home(root, ".hermes", with_scripts=True)
        for name in SPECIALISTS:
            build_home(root_home / "profiles" / name, name, with_scripts=False)
        build_home(root_home / "profiles" / "eval", "eval", with_scripts=False)
        build_home(root_home / "profiles" / "dj-broadcast", "dj-broadcast", with_scripts=False)

        proc = run_deployer(None, fake_bin, cwd=root)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        # Profile homes get a benign "live edge path" WARNING (the observer
        # resolves the edge through the shared root at wake time); the
        # installer's own failure channel must stay clean otherwise.
        unexpected = [
            line for line in proc.stderr.splitlines()
            if line not in proc.stdout and "WARNING: fixed live edge path" not in line
            and "fail closed until edge deployment" not in line
        ]
        assert "skip: eval" in "\n".join(unexpected) or "skip: eval" in proc.stderr, proc.stderr
        assert not any(line.startswith("FAIL:") for line in unexpected), unexpected
        for name in SPECIALISTS:
            plugin = root_home / "profiles" / name / "plugins" / "github-completion-edge-wake"
            assert (plugin / "__init__.py").is_file(), f"{name}: plugin not installed"
        assert (root_home / "plugins" / "github-completion-edge-wake" / "__init__.py").is_file(), \
            "root home must be installed when its edge deployment exists"

        enables = (root / "enable-calls.log").read_text().splitlines()
        assert len(enables) == 5, enables
        assert str(root_home) in enables
        for name in SPECIALISTS:
            assert str(root_home / "profiles" / name) in enables, enables

        # --- Check 2: profile-shaped HERMES_HOME still covers root ----------
        (root / "enable-calls.log").unlink()
        proc = run_deployer(root_home / "profiles" / "kanban-developer", fake_bin, cwd=root)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        enables = (root / "enable-calls.log").read_text().splitlines()
        assert str(root_home) in enables, f"profile-shaped HERMES_HOME must resolve to root coverage: {enables}"

        # --- Check 3: explicit HOME list honored verbatim -------------------
        (root / "enable-calls.log").unlink()
        only = build_home(root, "custom-home", with_scripts=True)
        proc = run_deployer(None, fake_bin, str(only), cwd=root)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        enables = (root / "enable-calls.log").read_text().splitlines()
        assert enables == [str(only)], enables

        # --- Check 4: root home missing edge deployment fails that home -----
        bare_root = build_home(root, "bare-root", with_scripts=False)
        proc = run_deployer(bare_root, fake_bin, cwd=root)
        assert proc.returncode != 0, "deployer must fail when root scripts/ is absent"
        assert "run deploy-intake-edge.sh first" in proc.stderr, proc.stderr
        assert not (bare_root / "plugins" / "github-completion-edge-wake").exists(), \
            "failed root install must leave no plugin directory behind"

        # --- Check 5: --dry-run mutates nothing ------------------------------
        probe = build_home(root, "dry-root", with_scripts=True)
        build_home(probe / "profiles" / "kanban-main", "kanban-main", with_scripts=False)
        before = sorted(p.name for p in (probe / "plugins").iterdir())
        proc = run_deployer(probe, fake_bin, "--dry-run", cwd=root)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        after = sorted(p.name for p in (probe / "plugins").iterdir())
        assert before == after, (before, after)
        assert not (probe / "profiles" / "kanban-main" / "plugins" / "github-completion-edge-wake").exists()

    print("deploy-github-completion-edge-wake.sh regression checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
