#!/usr/bin/env python3
"""Prepare private n8n import artifacts for the host deployment command.

The credential output is deliberately decrypted because n8n's server CLI
encrypts plain credential ``data`` while importing it into the instance
credential store.  All output paths are supplied by the caller and are
expected to be inside a private, temporary state directory.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from render_workflows import (
    EDGE_SYNC_CREDENTIAL_ID,
    EDGE_SYNC_CREDENTIAL_NAME,
    EDGE_SYNC_WEBHOOK_PATH,
    EDGE_SYNC_WORKFLOW_ID,
    EDGE_SYNC_WORKFLOW_NAME,
    bind_runtime_credential,
    build_runtime_credential,
)


def _read_control_token(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError("control token file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("control token file is not regular")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("control token file permissions are too open")
    if info.st_size <= 0 or info.st_size > 256:
        raise ValueError("control token file size is invalid")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("control token file cannot be read") from exc
    if not token or any(character.isspace() for character in token):
        raise ValueError("control token must be one non-empty value")
    return token


def _read_workflow(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("workflow template is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("workflow template must be a JSON object")
    return value


def _has_managed_webhook_path(workflow: dict[str, Any]) -> bool:
    raw_nodes = workflow.get("nodes")
    if not isinstance(raw_nodes, list):
        return False
    return any(
        isinstance(node, dict)
        and node.get("type") == "n8n-nodes-base.webhook"
        and isinstance(node.get("parameters"), dict)
        and node["parameters"].get("path") == EDGE_SYNC_WEBHOOK_PATH
        for node in raw_nodes
    )


def select_managed_workflow_id(inventory: Any) -> str:
    """Select the exact managed workflow ID from an n8n ``--all`` export.

    A single legacy record is intentionally reused so a corrected import
    overwrites the record that n8n may have created with a random ID.  Multiple
    exact records are unsafe because importing either one could leave duplicate
    webhook registrations, so the deployment fails before any import occurs.
    """
    if not isinstance(inventory, list):
        raise ValueError("n8n workflow inventory must be a JSON array")
    records: list[dict[str, Any]] = []
    for index, record in enumerate(inventory):
        if not isinstance(record, dict):
            raise ValueError(f"n8n workflow inventory record {index} is not an object")
        if (
            record.get("name") == EDGE_SYNC_WORKFLOW_NAME
            and _has_managed_webhook_path(record)
        ):
            records.append(record)

    if len(records) > 1:
        identifiers = ", ".join(
            repr(record.get("id", "<missing-id>")) for record in records
        )
        raise ValueError(
            "found multiple managed workflows matching exact name "
            f"{EDGE_SYNC_WORKFLOW_NAME!r} and webhook path "
            f"{EDGE_SYNC_WEBHOOK_PATH!r} (IDs: {identifiers}); "
            "refusing to import; resolve the duplicate records through n8n "
            "workflow management, then rerun"
        )
    if not records:
        return EDGE_SYNC_WORKFLOW_ID

    workflow_id = records[0].get("id")
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or workflow_id != workflow_id.strip()
        or any(character.isspace() for character in workflow_id)
    ):
        raise ValueError(
            "the one exact managed workflow record has no usable ID; refusing "
            "to import until it is repaired through n8n workflow management"
        )
    return workflow_id


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(path, 0o600)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _write_curl_config(path: Path, token: str) -> None:
    """Write only the canary Authorization header to a private curl config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    config = f"header = {json.dumps(f'Authorization: Bearer {token}')}\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(config)
        os.chmod(path, 0o600)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--workflow-template", type=Path, required=True)
    parser.add_argument("--credential-output", type=Path, required=True)
    parser.add_argument("--workflow-output", type=Path, required=True)
    parser.add_argument("--workflow-id", default=EDGE_SYNC_WORKFLOW_ID)
    parser.add_argument("--curl-config-output", type=Path)
    parser.add_argument("--credential-id", default=EDGE_SYNC_CREDENTIAL_ID)
    parser.add_argument("--credential-name", default=EDGE_SYNC_CREDENTIAL_NAME)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        token = _read_control_token(args.token_file)
        workflow = _read_workflow(args.workflow_template)
        credential = build_runtime_credential(
            token,
            credential_id=args.credential_id,
            credential_name=args.credential_name,
        )
        runtime_workflow = bind_runtime_credential(
            workflow,
            credential_id=args.credential_id,
            credential_name=args.credential_name,
            workflow_id=args.workflow_id,
        )
        _write_json(args.credential_output, credential)
        _write_json(args.workflow_output, runtime_workflow)
        if args.curl_config_output is not None:
            _write_curl_config(args.curl_config_output, token)
    except (OSError, ValueError, TypeError) as exc:
        print(f"prepare-edge-sync-runtime: ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "workflow_id": runtime_workflow.get("id", ""),
                "credential_id": args.credential_id,
                "credential_nodes": [
                    "GitHub edge sync webhook",
                    "Run edge sync actuator",
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
