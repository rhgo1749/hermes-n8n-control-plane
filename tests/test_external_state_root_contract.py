from __future__ import annotations

from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
SCRIPTS = N8N / "scripts"
ENV_EXPR = "${HERMES_N8N_ENV_FILE:?run host-install.sh first}"
STATE_EXPR = "${HERMES_N8N_STATE_ROOT:?run host-install.sh first}"


def _bind(service: dict, target: str) -> dict:
    for volume in service["volumes"]:
        if isinstance(volume, dict) and volume.get("target") == target:
            return volume
    raise AssertionError(f"missing bind target: {target}")


def test_compose_uses_external_config_and_state_only() -> None:
    compose = yaml.safe_load((N8N / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["n8n"]["env_file"] == [ENV_EXPR]
    assert services["github-router"]["env_file"] == [ENV_EXPR]
    assert _bind(services["n8n"], "/files") == {
        "type": "bind",
        "source": STATE_EXPR,
        "target": "/files",
    }
    assert _bind(services["lease-controller"], "/state") == {
        "type": "bind",
        "source": STATE_EXPR,
        "target": "/state",
    }
    assert _bind(services["github-router"], "/state") == {
        "type": "bind",
        "source": STATE_EXPR,
        "target": "/state",
    }
    assert _bind(services["github-router"], "/run/secrets") == {
        "type": "bind",
        "source": f"{STATE_EXPR}/secrets",
        "target": "/run/secrets",
        "read_only": True,
    }

    serialized = (N8N / "compose.yaml").read_text(encoding="utf-8")
    assert "./state" not in serialized
    assert "- .env" not in serialized


def test_operational_helpers_share_state_root_resolver() -> None:
    state_users = [
        "configure-github-router-secrets.sh",
        "cutover.sh",
        "export-workflows.sh",
        "host-install.sh",
        "import-workflows.sh",
        "install-intake-actuator.sh",
        "reconcile-github-router.sh",
    ]

    for name in state_users:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert 'source=state-root.sh' in source, name
        assert '"$SCRIPT_DIR/state-root.sh"' in source, name
        assert "h4v3_n8n_state_root" in source, name
        assert "N8N_DIR/state" not in source, name
        assert "automation/n8n/state" not in source, name

    compose_users = ["cutover.sh", "export-workflows.sh", "host-install.sh", "import-workflows.sh"]
    for name in compose_users:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "h4v3_n8n_env_file" in source, name
        assert 'export HERMES_N8N_ENV_FILE="$ENV_FILE"' in source, name
        assert 'export HERMES_N8N_STATE_ROOT="$STATE_ROOT"' in source, name
        assert 'ENV_FILE="$N8N_DIR/.env"' not in source, name


def test_installer_persists_single_runtime_path_contract() -> None:
    source = (SCRIPTS / "host-install.sh").read_text(encoding="utf-8")
    assert '"HERMES_N8N_ENV_FILE": str(path)' in source
    assert 'values["HERMES_N8N_ENV_FILE"] = str(path)' in source
    assert '"HERMES_N8N_STATE_ROOT": state_root' in source
    assert 'values["HERMES_N8N_STATE_ROOT"] = state_root' in source
    assert 'install -d -m 700 "$(dirname "$ENV_FILE")"' in source
    assert 'install -d -m 700 "$STATE_ROOT"' in source


def test_runtime_path_resolver_has_external_xdg_defaults() -> None:
    source = (SCRIPTS / "state-root.sh").read_text(encoding="utf-8")
    assert "h4v3_n8n_env_file" in source
    assert "XDG_CONFIG_HOME" in source
    assert ".config/hermes-n8n-control-plane/n8n.env" in source
    assert "XDG_STATE_HOME" in source
    assert ".local/state/hermes-n8n-control-plane" in source
    assert "SUDO_USER" in source
    assert 'HERMES_N8N_ENV_FILE must be an absolute host path' in source
    assert 'HERMES_N8N_STATE_ROOT must be an absolute host path' in source


def test_state_root_shell_scripts_parse() -> None:
    names = [
        "state-root.sh",
        "configure-github-router-secrets.sh",
        "cutover.sh",
        "export-workflows.sh",
        "host-install.sh",
        "import-workflows.sh",
        "install-intake-actuator.sh",
        "reconcile-github-router.sh",
    ]
    for name in names:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPTS / name)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"