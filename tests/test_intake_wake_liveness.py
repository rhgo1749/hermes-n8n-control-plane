from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
ROUTER_ENTRYPOINT = (
    ROOT / "automation" / "n8n" / "github-router" / "router_entrypoint.py"
)
LEASE_CONTROLLER = (
    ROOT / "automation" / "n8n" / "lease-controller" / "controller.py"
)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _entrypoint() -> ModuleType:
    return _load(
        ROUTER_ENTRYPOINT,
        f"test_intake_wake_router_entrypoint_{uuid.uuid4().hex}",
    )


def _fresh_core(entrypoint: ModuleType, *, edge_failure: bool = False) -> ModuleType:
    core = entrypoint._load_core()
    if edge_failure:
        def fail_edge(_event):
            raise core.RouterError("direct edge busy")
        core._n8n_edge_sync = fail_edge
    entrypoint.install(core)
    return core


def _configure_router_state(core: ModuleType, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    core.STATE_PATH = root / "router.json"
    core.INTAKE_TOKEN_FILE = root / "intake-token"
    core.INTAKE_TOKEN_FILE.write_text("test-token\n", encoding="utf-8")


def _wake_recorder(core: ModuleType):
    calls: list[str] = []

    def wake():
        calls.append("wake")
        return {"lease": f"lease-{len(calls)}", "upstream_status": 202}

    core._wake = wake
    return calls


def test_ack_chains_exactly_one_wake_for_next_eligible_scope() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint)
        _configure_router_state(core, Path(td))
        wakes = _wake_recorder(core)

        core._enqueue_scope(full=True, delivery_id="scope-a")
        core._enqueue_scope(
            full=False,
            repository="rhgo1749/ctrl-hangul",
            delivery_id="scope-b",
        )
        first = core._claim_scope()

        result = core._ack_scope(first["id"], first["claim_token"])

        assert result["status"] == "acknowledged"
        assert result["next_wake"]["accepted"] is True
        assert wakes == ["wake"]
        assert core._queue_status()["queued_scopes"] == 1


def test_ack_does_not_wake_when_only_backoff_scope_remains() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint)
        _configure_router_state(core, Path(td))
        core.SCOPE_RETRY_BACKOFF_SECONDS = (60, 60)
        wakes = _wake_recorder(core)

        core._enqueue_scope(full=True, delivery_id="scope-a")
        core._enqueue_scope(
            full=False,
            repository="rhgo1749/ctrl-hangul",
            delivery_id="scope-b",
        )
        first = core._claim_scope()
        second = core._claim_scope()

        requeued = core._requeue_scope(
            second["id"],
            "scope_retryable",
            second["claim_token"],
        )
        acknowledged = core._ack_scope(first["id"], first["claim_token"])

        assert requeued["status"] == "requeued"
        assert requeued["next_wake"]["reason"] == "no_eligible_scope"
        assert acknowledged["status"] == "acknowledged"
        assert acknowledged["next_wake"]["reason"] == "no_eligible_scope"
        assert wakes == []


def test_requeue_chains_wake_for_other_eligible_scope() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint)
        _configure_router_state(core, Path(td))
        core.SCOPE_RETRY_BACKOFF_SECONDS = (60, 60)
        wakes = _wake_recorder(core)

        core._enqueue_scope(full=True, delivery_id="scope-a")
        core._enqueue_scope(
            full=False,
            repository="rhgo1749/ctrl-hangul",
            delivery_id="scope-b",
        )
        first = core._claim_scope()

        result = core._requeue_scope(
            first["id"],
            "scope_retryable",
            first["claim_token"],
        )

        assert result["status"] == "requeued"
        assert result["next_wake"]["accepted"] is True
        assert wakes == ["wake"]


def _rework_event(delivery: str) -> dict[str, object]:
    return {
        "repository": "rhgo1749/ctrl-hangul",
        "event": "pull_request",
        "action": "labeled",
        "merged": False,
        "label": "agent-rework",
        "delivery": delivery,
    }


def test_supported_direct_edge_failure_defers_to_durable_scope() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint, edge_failure=True)
        _configure_router_state(core, Path(td))
        wakes = _wake_recorder(core)

        result = core._n8n_edge_sync(_rework_event("delivery-rework"))

        assert result["status"] == 202
        assert result["body"]["deferred"] is True
        assert result["body"]["reason"] == "edge_sync_deferred"
        assert wakes == ["wake"]
        state = core._load_state_unlocked()
        assert len(state["scope_queue"]) == 1
        queued = state["scope_queue"][0]
        assert queued["repositories"] == ["rhgo1749/ctrl-hangul"]
        assert queued["delivery"] == "delivery-rework"


def test_deferred_wake_failure_reuses_same_durable_scope_on_retry() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint, edge_failure=True)
        _configure_router_state(core, Path(td))
        wake_calls = 0

        def wake():
            nonlocal wake_calls
            wake_calls += 1
            if wake_calls == 1:
                raise core.RouterError("lease unavailable")
            return {"lease": "lease-retry", "upstream_status": 202}

        core._wake = wake
        event = _rework_event("delivery-retry")

        try:
            core._n8n_edge_sync(event)
        except core.RouterError as exc:
            assert str(exc) == "edge_sync_defer_failed"
        else:
            raise AssertionError("first deferred wake must fail")

        first_state = core._load_state_unlocked()
        assert len(first_state["scope_queue"]) == 1
        first_id = first_state["scope_queue"][0]["id"]

        result = core._n8n_edge_sync(event)
        second_state = core._load_state_unlocked()

        assert result["status"] == 202
        assert result["body"]["deferred"] is True
        assert wake_calls == 2
        assert len(second_state["scope_queue"]) == 1
        assert second_state["scope_queue"][0]["id"] == first_id


def test_startup_recovery_wakes_existing_eligible_scope_without_full_enqueue() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = entrypoint._load_core()
        _configure_router_state(core, Path(td))
        wakes = _wake_recorder(core)
        queued = core._enqueue_scope(
            full=False,
            repository="rhgo1749/ctrl-hangul",
            delivery_id="startup-existing",
        )

        result = entrypoint._startup_queue_recovery(
            core,
            attempts=1,
            delay_seconds=0,
        )

        assert result["accepted"] is True
        assert wakes == ["wake"]
        state = core._load_state_unlocked()
        assert len(state["scope_queue"]) == 1
        assert state["scope_queue"][0]["id"] == queued["id"]
        assert all(item.get("mode") != "full" for item in state["scope_queue"])


def test_startup_recovery_skips_empty_queue() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = entrypoint._load_core()
        _configure_router_state(core, Path(td))
        wakes = _wake_recorder(core)

        result = entrypoint._startup_queue_recovery(
            core,
            attempts=1,
            delay_seconds=0,
        )

        assert result == {"skipped": True, "reason": "no_eligible_scope"}
        assert wakes == []


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for background transition")


def _configure_lease_state(controller: ModuleType, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    controller.STATE_PATH = root / "lease.json"
    controller.TOKEN_FILE = root / "intake-token"
    controller.TOKEN_FILE.write_text("test-token\n", encoding="utf-8")


def test_lease_startup_resumes_only_persisted_pending_trigger() -> None:
    controller = _load(
        LEASE_CONTROLLER,
        f"test_intake_wake_lease_controller_{uuid.uuid4().hex}",
    )
    with tempfile.TemporaryDirectory() as td:
        _configure_lease_state(controller, Path(td))
        lease = str(uuid.uuid4())
        controller._write_state({"lease": lease, "status": "pending"})
        calls: list[str] = []

        def call_actuator(authorization: str):
            calls.append(authorization)
            return 200, b'{"ok":true}'

        controller._call_actuator = call_actuator

        assert controller._resume_pending_trigger_on_startup() is True
        _wait_for(lambda: controller._load_state().get("status") == "active")

        assert calls == ["Bearer test-token"]
        assert controller._load_state() == {
            "lease": lease,
            "status": "active",
            "upstream_status": 200,
        }


def test_lease_startup_does_not_replay_non_pending_state() -> None:
    controller = _load(
        LEASE_CONTROLLER,
        f"test_intake_wake_lease_controller_{uuid.uuid4().hex}",
    )
    with tempfile.TemporaryDirectory() as td:
        _configure_lease_state(controller, Path(td))
        calls: list[str] = []

        def call_actuator(authorization: str):
            calls.append(authorization)
            return 200, b"{}"

        controller._call_actuator = call_actuator
        for status in ("active", "paused", "failed"):
            controller._write_state(
                {"lease": str(uuid.uuid4()), "status": status}
            )
            assert controller._resume_pending_trigger_on_startup() is False

        assert calls == []


def test_invalid_pending_lease_identity_is_not_replayed() -> None:
    controller = _load(
        LEASE_CONTROLLER,
        f"test_intake_wake_lease_controller_{uuid.uuid4().hex}",
    )
    with tempfile.TemporaryDirectory() as td:
        _configure_lease_state(controller, Path(td))
        controller._write_state({"lease": "not-a-uuid", "status": "pending"})
        calls: list[str] = []
        controller._call_actuator = lambda authorization: calls.append(authorization)

        assert controller._resume_pending_trigger_on_startup() is False
        assert calls == []


def test_lease_busy_response_is_boundedly_retried() -> None:
    controller = _load(
        LEASE_CONTROLLER,
        f"test_intake_wake_lease_controller_{uuid.uuid4().hex}",
    )
    with tempfile.TemporaryDirectory() as td:
        _configure_lease_state(controller, Path(td))
        lease = str(uuid.uuid4())
        controller._write_state({"lease": lease, "status": "pending"})
        controller.ACTUATOR_BUSY_RETRY_SECONDS = (0, 0)
        statuses = [409, 409, 200]
        calls: list[str] = []

        def call_actuator(authorization: str):
            calls.append(authorization)
            return statuses.pop(0), b"{}"

        controller._call_actuator = call_actuator
        controller._trigger_intake_in_background(lease, "Bearer test-token")

        assert len(calls) == 3
        assert controller._load_state() == {
            "lease": lease,
            "status": "active",
            "upstream_status": 200,
        }


def test_completion_comment_edge_failure_defers_without_recursive_enqueue() -> None:
    entrypoint = _entrypoint()
    with tempfile.TemporaryDirectory() as td:
        core = _fresh_core(entrypoint, edge_failure=True)
        _configure_router_state(core, Path(td))
        core._write_state_unlocked(
            {
                "scope_queue": [],
                "managed_repositories": ["rhgo1749/ctrl-hangul"],
            }
        )
        wakes = _wake_recorder(core)
        payload = {
            "action": "created",
            "repository": {"full_name": "rhgo1749/ctrl-hangul"},
            "issue": {
                "number": 115,
                "pull_request": {
                    "url": "https://api.github.com/repos/rhgo1749/ctrl-hangul/pulls/115"
                },
            },
            "comment": {
                "body": "AGENT_REWORK_COMPLETE\ntask=t_b03e4f35\nvalidation=passed"
            },
        }

        repositories = core._event_repositories("issue_comment", payload)
        result = core._enqueue_scope(
            full=False,
            repositories=repositories,
            delivery_id="completion-defer",
        )

        assert result["completion_edge_sync"] is True
        assert result["completion_edge_sync_status"] == 202
        assert wakes == ["wake"]
        state = core._load_state_unlocked()
        assert len(state["scope_queue"]) == 1
        assert state["scope_queue"][0]["delivery"] == "completion-defer"
