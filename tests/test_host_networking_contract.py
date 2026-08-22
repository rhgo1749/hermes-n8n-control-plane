#!/usr/bin/env python3
"""Verify the host-networking installer never accepts a fake mapped port."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE_INSTALLER = REPO / "automation" / "n8n" / "scripts" / "host-install.sh"
SOURCE_STATE_ROOT = REPO / "automation" / "n8n" / "scripts" / "state-root.sh"


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def run_installer(script: Path, root: Path, environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def read_fixture_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="n8n-host-networking-") as raw:
        root = Path(raw) / "repo"
        n8n = root / "automation" / "n8n"
        scripts = n8n / "scripts"
        binaries = Path(raw) / "bin"
        scripts.mkdir(parents=True)
        binaries.mkdir()
        shutil.copy2(SOURCE_INSTALLER, scripts / "host-install.sh")
        shutil.copy2(SOURCE_STATE_ROOT, scripts / "state-root.sh")
        (n8n / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        # The fixture value is deliberately non-secret so assertions never read
        # a generated credential.
        (n8n / ".env.example").write_text("N8N_ENCRYPTION_KEY=test-fixture-value\n", encoding="utf-8")

        calls = Path(raw) / "calls.log"
        write_executable(binaries / "docker", "#!/bin/sh\nprintf 'docker %s\\n' \"$*\" >> \"$CALL_LOG\"\n")
        write_executable(binaries / "curl", "#!/bin/sh\nexit 0\n")
        state_home = Path(raw) / "state-home"
        user_home = Path(raw) / "home"
        environment = os.environ | {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "CALL_LOG": str(calls),
            "HOME": str(user_home),
            "XDG_STATE_HOME": str(state_home),
        }

        rejected = run_installer(scripts / "host-install.sh", root, environment, "--port", "9119")
        assert rejected.returncode == 2, rejected.stdout + rejected.stderr
        assert not calls.exists() or not calls.read_text(encoding="utf-8").strip()
        assert not (n8n / ".env").exists()

        installed = run_installer(
            scripts / "host-install.sh",
            root,
            environment,
            "--timezone",
            "UTC",
            "--hermes-base-url",
            "http://100.107.12.90:9119",
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr
        values = read_fixture_env(n8n / ".env")
        assert values["N8N_PORT"] == "5678"
        assert values["N8N_EDITOR_BASE_URL"] == "http://127.0.0.1:5678"
        assert "LEASE_HERMES_BASE_URL" not in values
        assert "--hermes-base-url is deprecated and ignored" in installed.stderr
        assert "N8N_HOST_PORT" not in values
        assert calls.exists() and "docker compose" in calls.read_text(encoding="utf-8")
        expected_state_root = state_home / "hermes-n8n-control-plane"
        assert values["HERMES_N8N_STATE_ROOT"] == str(expected_state_root)
        assert (expected_state_root / "secrets").is_dir()
        assert not (n8n / "state").exists()

        # A main-era runtime .env may still contain the retired dashboard key.
        # A successful new installer run accepts it as input state but removes
        # it from the rewritten runtime environment.
        with (n8n / ".env").open("a", encoding="utf-8") as stream:
            stream.write("LEASE_HERMES_BASE_URL=legacy-dashboard-value\n")

        migrated = run_installer(
            scripts / "host-install.sh",
            root,
            environment,
        )
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        migrated_values = read_fixture_env(n8n / ".env")
        assert "LEASE_HERMES_BASE_URL" not in migrated_values
        assert migrated_values["HERMES_N8N_STATE_ROOT"] == str(expected_state_root)

        calls.unlink()
        (n8n / ".env").write_text(
            "N8N_PORT=9119\n"
            "N8N_ENCRYPTION_KEY=test-fixture-value\n"
            "LEASE_HERMES_BASE_URL=http://100.107.12.90:9119\n",
            encoding="utf-8",
        )
        invalid_runtime_port = run_installer(scripts / "host-install.sh", root, environment)
        assert invalid_runtime_port.returncode != 0
        assert "N8N_PORT must be 5678" in invalid_runtime_port.stderr
        assert not calls.exists() or not calls.read_text(encoding="utf-8").strip()

    print(
        '{"ok": true, "fixed_host_networking_port": 5678, '
        '"external_state_root": true, "arbitrary_port_rejected": true, '
        '"invalid_runtime_port_rejected_before_docker": true}'
    )
    return 0


def test_host_networking_contract() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
