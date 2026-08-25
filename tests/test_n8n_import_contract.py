# pyright: reportMissingImports=false

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
SCRIPTS = N8N / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prepare_edge_sync_runtime import (  # noqa: E402
    main as prepare_runtime,
    select_managed_workflow_id,
)
from render_workflows import (  # noqa: E402
    EDGE_SYNC_CREDENTIAL_ID,
    EDGE_SYNC_CREDENTIAL_NAME,
    EDGE_SYNC_WORKFLOW_ID,
    bind_runtime_credential,
    build_runtime_credential,
)


def _workflow() -> dict:
    return json.loads(
        (N8N / "workflows" / "github-pr-edge-sync.json").read_text(
            encoding="utf-8"
        )
    )


def test_runtime_credential_helper_binds_both_nodes_without_mutating_template() -> None:
    template = _workflow()
    original = json.loads(json.dumps(template))
    runtime = bind_runtime_credential(template)

    assert template == original
    assert runtime["id"] == EDGE_SYNC_WORKFLOW_ID
    assert runtime["active"] is False
    assert runtime["meta"]["templateCredsSetupCompleted"] is True

    references = {
        node["name"]: node["credentials"]["httpHeaderAuth"]
        for node in runtime["nodes"]
        if node["name"]
        in {"GitHub edge sync webhook", "Run edge sync actuator"}
    }
    assert references == {
        "GitHub edge sync webhook": {
            "id": EDGE_SYNC_CREDENTIAL_ID,
            "name": EDGE_SYNC_CREDENTIAL_NAME,
        },
        "Run edge sync actuator": {
            "id": EDGE_SYNC_CREDENTIAL_ID,
            "name": EDGE_SYNC_CREDENTIAL_NAME,
        },
    }


def test_runtime_credential_helper_can_reuse_legacy_workflow_id() -> None:
    template = _workflow()

    runtime = bind_runtime_credential(
        template,
        workflow_id="legacy-random-workflow-id",
    )

    assert runtime["id"] == "legacy-random-workflow-id"


def test_workflow_inventory_reuses_one_exact_legacy_record() -> None:
    legacy = _workflow()
    legacy["id"] = "legacy-random-workflow-id"

    assert select_managed_workflow_id([legacy]) == "legacy-random-workflow-id"
    assert select_managed_workflow_id([]) == EDGE_SYNC_WORKFLOW_ID


def test_workflow_inventory_fails_closed_on_duplicate_exact_records() -> None:
    first = _workflow()
    first["id"] = "legacy-random-workflow-id"
    second = _workflow()
    second["id"] = "another-random-workflow-id"

    with pytest.raises(ValueError, match="multiple managed workflows") as error:
        select_managed_workflow_id([first, second])

    message = str(error.value)
    assert "legacy-random-workflow-id" in message
    assert "another-random-workflow-id" in message
    assert "refusing to import" in message


def test_workflow_inventory_requires_exact_managed_name_and_path() -> None:
    wrong_name = _workflow()
    wrong_name["id"] = "wrong-name-id"
    wrong_name["name"] = "Hermes Webhook · Another workflow"

    wrong_path = _workflow()
    wrong_path["id"] = "wrong-path-id"
    for node in wrong_path["nodes"]:
        if node["name"] == "GitHub edge sync webhook":
            node["parameters"]["path"] = "another-webhook-path"

    assert select_managed_workflow_id([wrong_name, wrong_path]) == EDGE_SYNC_WORKFLOW_ID


def test_runtime_credential_envelope_is_plain_http_header_auth() -> None:
    synthetic_value = "synthetic-test-value"
    records = build_runtime_credential(synthetic_value)

    assert records == [
        {
            "id": EDGE_SYNC_CREDENTIAL_ID,
            "name": EDGE_SYNC_CREDENTIAL_NAME,
            "type": "httpHeaderAuth",
            "data": {
                "name": "Authorization",
                "value": f"Bearer {synthetic_value}",
            },
        }
    ]


def test_prepare_runtime_writes_private_artifacts_and_curl_config(tmp_path: Path) -> None:
    synthetic_value = "synthetic-test-value"
    token_file = tmp_path / "control-token"
    token_file.write_text(f"{synthetic_value}\n", encoding="utf-8")
    token_file.chmod(0o600)
    credential_path = tmp_path / "runtime" / "credential.json"
    workflow_path = tmp_path / "runtime" / "workflow.json"
    curl_config_path = tmp_path / "runtime" / "canary.curlrc"
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(_workflow()), encoding="utf-8")

    result = prepare_runtime(
        [
            "--token-file",
            str(token_file),
            "--workflow-template",
            str(template_path),
            "--credential-output",
            str(credential_path),
            "--workflow-output",
            str(workflow_path),
            "--workflow-id",
            "legacy-random-workflow-id",
            "--curl-config-output",
            str(curl_config_path),
        ]
    )

    assert result == 0
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(workflow_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(curl_config_path.stat().st_mode) == 0o600

    credential = json.loads(credential_path.read_text(encoding="utf-8"))
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    curl_config = curl_config_path.read_text(encoding="utf-8")
    assert credential[0]["type"] == "httpHeaderAuth"
    assert credential[0]["data"] == {
        "name": "Authorization",
        "value": f"Bearer {synthetic_value}",
    }
    assert synthetic_value in curl_config
    assert synthetic_value not in json.dumps(workflow)
    assert workflow["id"] == "legacy-random-workflow-id"
    assert all(
        node["credentials"]["httpHeaderAuth"]
        == {"id": EDGE_SYNC_CREDENTIAL_ID, "name": EDGE_SYNC_CREDENTIAL_NAME}
        for node in workflow["nodes"]
        if node["name"]
        in {"GitHub edge sync webhook", "Run edge sync actuator"}
    )


def test_import_command_is_fail_closed_and_canaries_production_noop() -> None:
    source = (SCRIPTS / "import-workflows.sh").read_text(encoding="utf-8")

    required_fragments = (
        'TOKEN_FILE="$STATE_ROOT/secrets/hermes-intake-control-token"',
        "edge_sync_runtime_ready",
        "import:credentials",
        "import:workflow",
        "publish:workflow",
        "export:workflow",
        "export:workflow \\\n  --all \\\n  --output=\"$INVENTORY_CONTAINER_PATH\"",
        '"$INVENTORY_JSON"',
        '--workflow-id "$MANAGED_WORKFLOW_ID"',
        "select_managed_workflow_id",
        "managed workflow inventory selection failed; no workflow was imported",
        '"${compose[@]}" restart n8n',
        "run_canary_once",
        "wait_for_canary 30",
        'n8n_cli unpublish:workflow --id="$MANAGED_WORKFLOW_ID"',
        'n8n_cli publish:workflow --id="$MANAGED_WORKFLOW_ID"',
        "republish fallback",
        'EDGE_SYNC_WEBHOOK_URL="http://127.0.0.1:5678/webhook/hermes-github-edge-sync"',
        '"$RUNTIME_DIR"',
        'rm -rf -- "$RUNTIME_DIR"',
        '--config "$CURL_CONFIG"',
        "unsupported_pull_request_action",
    )
    for fragment in required_fragments:
        assert fragment in source

    assert source.index(
        "n8n_cli export:workflow \\\n  --all \\\n  --output=\"$INVENTORY_CONTAINER_PATH\""
    ) < source.index("n8n_cli import:credentials")
    assert "After import, bind" not in source
    assert "manual UI" not in source
    assert "scheduleTrigger" not in source
    assert "/fallback" not in source
    assert "bf431b2a6ba6" in source


def test_runtime_helper_rejects_whitespace_in_control_value() -> None:
    with pytest.raises(ValueError, match="single non-empty value"):
        build_runtime_credential("value with whitespace")
