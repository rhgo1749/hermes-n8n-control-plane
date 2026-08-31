from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "automation" / "n8n" / "scripts" / "diagnose-github-onboarding.sh"


def _runtime_home(tmp_path: Path, job_ids: list[str]) -> Path:
    home = tmp_path / "hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    jobs = [
        {
            # Real serializer shape: no object-level `profile` key (profile
            # identity is the store path), and the historically-observed
            # long job name for bf431b2a6ba6.
            "id": job_id,
            "name": "GitHub agent-ready Issue intake (CtrlHangul + Re-Bound + H4V3-DJ)",
            "script": "github-agent-ready-kanban-intake.py",
            "schedule": {
                "kind": "cron",
                "expr": "*/5 * * * *",
                "display": "*/5 * * * *",
            },
            "schedule_display": "*/5 * * * *",
            "origin": None,
            "base_url": None,
            "workdir": None,
            "no_agent": True,
            "deliver": "local",
            "enabled": True,
            "state": "scheduled",
            "repeat": {"times": None},
        }
        if job_id == "bf431b2a6ba6"
        else {"id": job_id}
        for job_id in job_ids
    ]
    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": jobs}) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        ROOT / "automation" / "hermes" / "scripts" / "github-agent-ready-kanban-intake-entrypoint.py",
        home / "scripts" / "github-agent-ready-kanban-intake.py",
    )
    shutil.copyfile(
        ROOT / "automation" / "hermes" / "scripts" / "github-agent-ready-kanban-intake.py",
        home / "scripts" / "github-agent-ready-kanban-intake-core.py",
    )
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



def test_diagnostic_rejects_placeholder_intake_script(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    (home / "scripts" / "github-agent-ready-kanban-intake-core.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "authoritative_intake_job_invalid" in completed.stderr

def test_diagnostic_rejects_malformed_authoritative_metadata(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    jobs = home / "cron" / "jobs.json"
    payload = json.loads(jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["no_agent"] = "true"
    jobs.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "authoritative_job_metadata_invalid" in completed.stderr


def test_diagnostic_rejects_symlinked_authoritative_store(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    store = home / "cron" / "jobs.json"
    target = tmp_path / "redirected-jobs.json"
    target.write_bytes(store.read_bytes())
    store.unlink()
    store.symlink_to(target)

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "authoritative_job_metadata_invalid" in completed.stderr


def test_diagnostic_rejects_non_string_cron_expr(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    jobs = home / "cron" / "jobs.json"
    payload = json.loads(jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["schedule"]["expr"] = ["*/5 * * * *"]
    jobs.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "authoritative_job_metadata_invalid" in completed.stderr


def test_diagnostic_still_accepts_interval_schedule_variant(tmp_path: Path):
    """The preserved 5-minute cadence may also serialize as an interval."""
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    jobs = home / "cron" / "jobs.json"
    payload = json.loads(jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["schedule"] = {
        "kind": "interval",
        "minutes": 5,
        "display": "every 5m",
    }
    payload["jobs"][0]["schedule_display"] = "every 5m"
    jobs.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "authoritative_intake_job=default:bf431b2a6ba6 unique=true" in (
        completed.stdout
    )
    assert "schedule=every 5m" in completed.stdout


def test_diagnostic_reports_unavailable_lease_trigger(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_ROUTER_URL": "http://127.0.0.1:1",
            "GITHUB_LEASE_URL": "http://127.0.0.1:1",
            "GITHUB_INTAKE_ACTUATOR_URL": "http://127.0.0.1:1",
        },
        check=False,
    )

    assert completed.returncode != 0
    assert "lease_trigger_contract=unavailable" in completed.stderr


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


def test_diagnostic_accepts_paused_job_between_wakes(tmp_path: Path):
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    jobs = home / "cron" / "jobs.json"
    payload = json.loads(jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["state"] = "paused"
    payload["jobs"][0]["enabled"] = False
    jobs.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "lifecycle=paused_between_wakes" in completed.stdout

def test_diagnostic_accepts_historical_job_name_and_real_serializer_shape(
    tmp_path: Path,
):
    """Historical durable job regression.

    The live ``bf431b2a6ba6`` job historically carried a longer name than
    the early short-name gate allowed, and the real Hermes cron serializer
    never persists an object-level ``profile`` key.  The diagnostic must
    bind identity to the canonical store + exact job id + script +
    preserved schedule/runtime fields only — so the realistic historical
    shape passes, and a job with a different id in the same store still
    fails closed.
    """
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "authoritative_intake_job=default:bf431b2a6ba6 unique=true" in (
        completed.stdout
    )
    assert "profile=default" in completed.stdout
    assert "schedule=*/5 * * * *" in completed.stdout


def test_diagnostic_identity_bound_to_exact_job_id_ignores_other_jobs(
    tmp_path: Path,
):
    """Identity is the exact job id in the canonical store.

    A second job in the same store with a different id must be ignored —
    the diagnostic neither rejects the store nor picks the wrong job.
    """
    home = _runtime_home(tmp_path, ["bf431b2a6ba6", "other-intake-job"])
    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "authoritative_intake_job=default:bf431b2a6ba6 unique=true" in (
        completed.stdout
    )


def test_diagnostic_rejects_wrong_script_for_authoritative_job_id(
    tmp_path: Path,
):
    """Same id, wrong script: still fail closed on the script field."""
    home = _runtime_home(tmp_path, ["bf431b2a6ba6"])
    jobs = home / "cron" / "jobs.json"
    payload = json.loads(jobs.read_text(encoding="utf-8"))
    payload["jobs"][0]["script"] = "github-agent-ready-kanban-intake-legacy.py"
    (home / "scripts" / "github-agent-ready-kanban-intake-legacy.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8"
    )
    jobs.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [str(SCRIPT), "--hermes-home", str(home), "--skip-network"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "script_mismatch" in completed.stderr
