from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "automation" / "n8n" / "scripts" / "diagnose-github-onboarding.sh"


def _runtime_home(tmp_path: Path, job_ids: list[str]) -> Path:
    home = tmp_path / "hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": job_id} for job_id in job_ids]}) + "\n",
        encoding="utf-8",
    )
    (home / "scripts" / "github-agent-ready-kanban-intake.py").touch()
    (home / "scripts" / "github-agent-ready-kanban-intake-core.py").touch()
    return home


def test_diagnostic_confirms_unique_authoritative_job_without_network(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    before = (home / "cron" / "jobs.json").read_bytes()

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "authoritative_intake_job=default:bf431b2a6ba6 unique=true" in completed.stdout
    assert "intake_scripts_present=true" in completed.stdout
    assert "network_probes=skipped" in completed.stdout
    assert (home / "cron" / "jobs.json").read_bytes() == before


def test_diagnostic_reports_missing_job_and_never_creates_one(tmp_path: Path):
    home = _runtime_home(tmp_path, [])
    jobs = home / "cron" / "jobs.json"
    before = jobs.read_bytes()

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "authoritative_intake_job_missing" in completed.stderr
    assert jobs.read_bytes() == before
