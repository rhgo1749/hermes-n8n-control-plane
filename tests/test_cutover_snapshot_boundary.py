#!/usr/bin/env python3
"""Preserve the historical one-job rollback boundary without reviving it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE_CUTOVER = REPO / "automation" / "n8n" / "scripts" / "cutover.sh"
INTAKE_ID = "bf431b2a6ba6"
EXCLUDED_ID = "27f6725028ff"


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def run_rollback(script: Path, root: Path, home: Path, hermes: Path, environment: dict[str, str]):
    return subprocess.run(
        [
            str(script),
            "rollback",
            "--confirm-n8n-workflows-deactivated",
            "--hermes-home",
            str(home),
            "--hermes-bin",
            str(hermes),
            "--n8n-port",
            "5678",
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cutover-snapshot-boundary-") as raw:
        root = Path(raw) / "repo"
        scripts = root / "automation" / "n8n" / "scripts"
        state = root / "automation" / "n8n" / "state" / "cutover"
        home = Path(raw) / "hermes-home"
        binaries = Path(raw) / "bin"
        scripts.mkdir(parents=True)
        state.mkdir(parents=True)
        (home / "cron").mkdir(parents=True)
        binaries.mkdir()
        shutil.copy2(SOURCE_CUTOVER, scripts / "cutover.sh")
        (root / "automation" / "n8n" / ".env").write_text("N8N_PORT=5678\n", encoding="utf-8")

        jobs = {
            "jobs": [
                {"id": INTAKE_ID, "enabled": True, "state": "scheduled"},
                {"id": EXCLUDED_ID, "enabled": True, "state": "scheduled"},
            ]
        }
        (home / "cron" / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")

        legacy_snapshot = state / "legacy-five-job-snapshot.json"
        legacy_snapshot.write_text(
            json.dumps(
                {
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "hermes_home": str(home.resolve()),
                    "jobs": [
                        {
                            "profile": "default",
                            "job": {"id": INTAKE_ID, "enabled": True, "state": "scheduled"},
                        },
                        {
                            "profile": "default",
                            "job": {"id": EXCLUDED_ID, "enabled": True, "state": "scheduled"},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "latest.json").write_text(
            json.dumps(
                {
                    "backup": str(legacy_snapshot),
                    "targets": [f"default:{INTAKE_ID}", f"default:{EXCLUDED_ID}"],
                }
            ),
            encoding="utf-8",
        )

        call_log = Path(raw) / "calls.log"
        fake_hermes = binaries / "hermes"
        write_executable(fake_hermes, "#!/bin/sh\nprintf 'hermes %s\\n' \"$*\" >> \"$CALL_LOG\"\n")
        write_executable(binaries / "docker", "#!/bin/sh\nprintf 'docker %s\\n' \"$*\" >> \"$CALL_LOG\"\n")
        environment = os.environ | {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "CALL_LOG": str(call_log),
        }
        result = run_rollback(scripts / "cutover.sh", root, home, fake_hermes, environment)
        assert result.returncode != 0, result.stdout + result.stderr
        assert not call_log.exists() or not call_log.read_text(encoding="utf-8").strip()

        row_mismatch_snapshot = state / "row-mismatch-snapshot.json"
        row_mismatch_snapshot.write_text(
            json.dumps(
                {
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "hermes_home": str(home.resolve()),
                    "targets": [f"default:{INTAKE_ID}"],
                    "jobs": [
                        {
                            "profile": "default",
                            "job": {"id": INTAKE_ID, "enabled": True, "state": "scheduled"},
                        },
                        {
                            "profile": "default",
                            "job": {"id": EXCLUDED_ID, "enabled": True, "state": "scheduled"},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "latest.json").write_text(
            json.dumps({"backup": str(row_mismatch_snapshot), "targets": [f"default:{INTAKE_ID}"]}),
            encoding="utf-8",
        )
        result = run_rollback(scripts / "cutover.sh", root, home, fake_hermes, environment)
        assert result.returncode != 0, result.stdout + result.stderr
        assert not call_log.exists() or not call_log.read_text(encoding="utf-8").strip()

        foreign_home_snapshot = state / "foreign-home-snapshot.json"
        foreign_home_snapshot.write_text(
            json.dumps(
                {
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "hermes_home": str((Path(raw) / "other-hermes-home").resolve()),
                    "targets": [f"default:{INTAKE_ID}"],
                    "jobs": [
                        {
                            "profile": "default",
                            "job": {"id": INTAKE_ID, "enabled": True, "state": "scheduled"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "latest.json").write_text(
            json.dumps({"backup": str(foreign_home_snapshot), "targets": [f"default:{INTAKE_ID}"]}),
            encoding="utf-8",
        )
        result = run_rollback(scripts / "cutover.sh", root, home, fake_hermes, environment)
        assert result.returncode != 0, result.stdout + result.stderr
        assert not call_log.exists() or not call_log.read_text(encoding="utf-8").strip()

        current_snapshot = state / "current-one-job-snapshot.json"
        current_snapshot.write_text(
            json.dumps(
                {
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "hermes_home": str(home.resolve()),
                    "targets": [f"default:{INTAKE_ID}"],
                    "jobs": [
                        {
                            "profile": "default",
                            "job": {"id": INTAKE_ID, "enabled": True, "state": "scheduled"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "latest.json").write_text(
            json.dumps({"backup": str(current_snapshot), "targets": [f"default:{INTAKE_ID}"]}),
            encoding="utf-8",
        )
        result = run_rollback(scripts / "cutover.sh", root, home, fake_hermes, environment)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = call_log.read_text(encoding="utf-8")
        assert "docker compose" in calls
        assert f"hermes -p default cron resume {INTAKE_ID}" in calls
        assert EXCLUDED_ID not in calls

    print(
        json.dumps(
            {
                "ok": True,
                "legacy_snapshot_rejected_before_side_effects": True,
                "row_mismatch_rejected_before_side_effects": True,
                "foreign_home_snapshot_rejected_before_side_effects": True,
                "current_snapshot_restored_only_intake": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
