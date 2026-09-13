from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "automation" / "n8n" / "scripts" / "diagnose-github-onboarding.sh"


def _runtime_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    (home / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT
        / "automation"
        / "hermes"
        / "scripts"
        / "github-agent-ready-kanban-intake-entrypoint.py",
        home / "scripts" / "github-agent-ready-kanban-intake.py",
    )
    shutil.copyfile(
        ROOT
        / "automation"
        / "hermes"
        / "scripts"
        / "github-agent-ready-kanban-intake.py",
        home / "scripts" / "github-agent-ready-kanban-intake-core.py",
    )
    return home


def _run(home: Path, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), *extra],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_diagnostic_accepts_direct_actuator_runtime_without_legacy_cron(
    tmp_path: Path,
) -> None:
    home = _runtime_home(tmp_path)

    completed = _run(home, "--skip-network")

    assert completed.returncode == 0
    assert "intake_execution=direct-actuator:5682 hermes_cron_required=false" in (
        completed.stdout
    )
    assert "intake_scripts_present=true contract=verified" in completed.stdout
    assert "network_probes=skipped" in completed.stdout
    assert not (home / "cron" / "jobs.json").exists()


def test_diagnostic_ignores_legacy_cron_metadata_and_does_not_mutate_it(
    tmp_path: Path,
) -> None:
    home = _runtime_home(tmp_path)
    (home / "cron").mkdir()
    jobs = home / "cron" / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "bf431b2a6ba6",
                        "script": "legacy-obsolete-script.py",
                        "enabled": True,
                        "state": "scheduled",
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = jobs.read_bytes()

    completed = _run(home, "--skip-network")

    assert completed.returncode == 0
    assert "hermes_cron_required=false" in completed.stdout
    assert jobs.read_bytes() == before


def test_diagnostic_rejects_placeholder_intake_core(tmp_path: Path) -> None:
    home = _runtime_home(tmp_path)
    (home / "scripts" / "github-agent-ready-kanban-intake-core.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )

    completed = _run(home, "--skip-network")

    assert completed.returncode != 0
    assert "authoritative_intake_runtime_invalid" in completed.stderr


def test_diagnostic_rejects_missing_intake_core(tmp_path: Path) -> None:
    home = _runtime_home(tmp_path)
    (home / "scripts" / "github-agent-ready-kanban-intake-core.py").unlink()

    completed = _run(home, "--skip-network")

    assert completed.returncode != 0
    assert "authoritative_intake_runtime_missing" in completed.stderr


def test_diagnostic_rejects_symlinked_intake_core(tmp_path: Path) -> None:
    home = _runtime_home(tmp_path)
    core = home / "scripts" / "github-agent-ready-kanban-intake-core.py"
    target = tmp_path / "core.py"
    target.write_bytes(core.read_bytes())
    core.unlink()
    core.symlink_to(target)

    completed = _run(home, "--skip-network")

    assert completed.returncode != 0
    assert "authoritative_intake_runtime_missing" in completed.stderr


def test_diagnostic_rejects_symlinked_runtime_path_component(tmp_path: Path) -> None:
    home = _runtime_home(tmp_path)
    scripts = home / "scripts"
    target = tmp_path / "real-scripts"
    scripts.rename(target)
    scripts.symlink_to(target, target_is_directory=True)

    completed = _run(home, "--skip-network")

    assert completed.returncode != 0
    assert "authoritative_intake_runtime_invalid reason=path_symlink" in completed.stderr


def test_diagnostic_reports_unavailable_direct_actuator_path(tmp_path: Path) -> None:
    home = _runtime_home(tmp_path)
    completed = _run(
        home,
        env={
            **os.environ,
            "GITHUB_ROUTER_URL": "http://127.0.0.1:1",
            "GITHUB_LEASE_URL": "http://127.0.0.1:1",
            "GITHUB_INTAKE_ACTUATOR_URL": "http://127.0.0.1:1",
        },
    )

    assert completed.returncode != 0
    assert "router_health=unavailable_or_invalid" in completed.stderr
    assert "lease-controller_health=unavailable_or_invalid" in completed.stderr
    assert "intake-actuator_health=unavailable_or_invalid" in completed.stderr
    assert "lease_trigger_contract=unavailable" in completed.stderr
