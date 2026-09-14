#!/usr/bin/env python3
from __future__ import annotations

from types import SimpleNamespace

from kanban_head_binding_feedback import install_head_binding_feedback


def _original_operator_attention_reason(entry):
    reason = str(entry.get("reason") or "")
    if reason in {"rework_dispatch_failed", "needs_input"}:
        return reason
    for value in (entry.get("rework"), entry):
        if not isinstance(value, dict):
            continue
        try:
            if int(value.get("rework_round") or 0) >= 3:
                return "rework_threshold_exceeded"
        except (TypeError, ValueError):
            continue
    return None


def _dummy_delivery(*args, **kwargs):
    return False, "noop", {}


def _dummy_reconcile(*args, **kwargs):
    return None


def make_core():
    return SimpleNamespace(
        _head_binding_feedback_installed=False,
        _operator_attention_reason=_original_operator_attention_reason,
        _rework_delivery_evidence=_dummy_delivery,
        _reconcile_rework_lifecycle=_dummy_reconcile,
        REWORK_ATTENTION_MARKER="HERMES_KANBAN_REWORK_ATTENTION",
        REWORK_COMPLETE_MARKER="AGENT_REWORK_COMPLETE",
        REWORK_RETRY_MARKER="AGENT_REWORK_RETRY",
        GithubCompletionError=RuntimeError,
    )


def test_round_count_never_becomes_operator_attention():
    core = install_head_binding_feedback(make_core())
    for rework_round in (3, 4, 10, 999):
        assert core._operator_attention_reason({
            "rework": {"rework_round": rework_round},
        }) is None
        assert core._operator_attention_reason({
            "reason": "rework_threshold_exceeded",
            "rework_round": rework_round,
        }) is None


def test_real_operator_attention_reasons_still_pass_through():
    core = install_head_binding_feedback(make_core())
    assert core._operator_attention_reason({
        "reason": "rework_dispatch_failed",
        "rework_round": 10,
    }) == "rework_dispatch_failed"
    assert core._operator_attention_reason({
        "reason": "needs_input",
        "rework_round": 10,
    }) == "needs_input"


def main() -> int:
    test_round_count_never_becomes_operator_attention()
    test_real_operator_attention_reasons_still_pass_through()
    print("PASS: manual rework round count is not an operator-attention cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
