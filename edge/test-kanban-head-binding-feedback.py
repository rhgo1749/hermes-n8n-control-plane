#!/usr/bin/env python3
"""Regression coverage for the head-binding feedback overlay.

The test reuses the canonical rework harness, installs the production overlay
onto the same loaded core module, exercises the two rejection reasons and the
PR #36 verification-only success path, then runs the complete canonical
rework suite under the overlay to prove transition compatibility.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "test-kanban-github-sync-rework.py"

spec = importlib.util.spec_from_file_location("rework_harness", HARNESS)
assert spec is not None and spec.loader is not None
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)

from kanban_head_binding_feedback import install_head_binding_feedback  # noqa: E402

install_head_binding_feedback(h.mod)


def _ordinary_same_head_feedback() -> None:
    with h.isolated_test_environment():
        fake = h.fresh_env()
        head = "0123456789abcdef0123456789abcdef00000116"
        fake.prs[h.PR_N] = h.make_pr(
            h.PR_N, state="open", head_sha=head,
            title="same-head feedback", body="body",
        )
        fake.pr_labels[h.PR_N] = ["agent-rework"]
        fake.pr_timeline[h.PR_N] = h.labeled_timeline(h.LABEL_ADDED_OLD)
        fake.reviews[h.PR_N] = [h.review(
            "rhgo1749", "CHANGES_REQUESTED", "fix it",
            "2026-08-10T00:00:10Z",
        )]
        tid = h.new_task("review")
        h.run_sync(fake)
        payload = [
            event for event in h.task_events(tid)
            if event["kind"] == "github_pr_rework"
        ][-1]["payload"]
        with h.connect_closing() as conn:
            claimed = h.kanban_db.claim_task(conn, tid)
            assert claimed is not None
            conn.commit()
        h._close_rework_run(tid, head=head, outcome="completed", summary="no change")
        with h.connect_closing() as conn:
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input', "
                "completed_at=NULL WHERE id=?", (tid,),
            )
            conn.commit()
        h._post_completion_marker(
            fake, tid, head,
            request_comment=payload.get("request_comment_id"),
        )
        h.run_sync(fake)
        posted = h._pr_attention_comments(fake, tid, "rework_head_unchanged")
        assert len(posted) == 1, posted
        body = posted[0]
        assert head in body, body
        assert "worker-owned commit" in body and "head advances" in body, body
        h.run_sync(fake)
        assert len(h._pr_attention_comments(
            fake, tid, "rework_head_unchanged"
        )) == 1


def _run_head_mismatch_feedback() -> None:
    with h.isolated_test_environment():
        fake = h.fresh_env()
        tid = h._rework_ready_task(fake)
        requested_head = fake.prs[h.PR_N]["head"]["sha"]
        live_head = "0123456789abcdef0123456789abcdef00000117"
        run_head = "0123456789abcdef0123456789abcdef00000118"
        fake.prs[h.PR_N]["head"]["sha"] = live_head
        h._close_rework_run(
            tid, head=run_head, outcome="completed",
            summary="worker attested another head",
        )
        h._post_completion_marker(fake, tid, live_head)
        results = h.run_sync(fake)
        entries = [result for result in results if result.get("task_id") == tid]
        assert any(
            result.get("reason") == "rework_retry_scheduled"
            and result.get("retry_reason") == "run_head_mismatch"
            for result in entries
        ), entries
        posted = h._pr_attention_comments(fake, tid, "run_head_mismatch")
        assert len(posted) == 1, posted
        body = posted[0]
        assert requested_head in body, body
        assert "finished worker run" in body, body
        assert "A new source commit is not inherently required" in body, body
        assert "verification-only" in body and "AGENT_REWORK_RETRY" in body, body
        h.run_sync(fake)
        assert len(h._pr_attention_comments(fake, tid, "run_head_mismatch")) == 1


def _verification_only_success_has_no_warning() -> None:
    with h.isolated_test_environment():
        fake, tid = h._attention_blocked_retry_hold()
        head = fake.prs[h.PR_N]["head"]["sha"]
        retry_comment_id = h._post_retry_comment(fake, tid)
        h.run_sync(fake)
        with h.connect_closing() as conn:
            claimed = h.kanban_db.claim_task(conn, tid)
            assert claimed is not None
            conn.commit()
        h._close_rework_run(
            tid, head=head, outcome="completed",
            summary="verification: requested fixes already at head",
        )
        with h.connect_closing() as conn:
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input', "
                "completed_at=NULL WHERE id=?", (tid,),
            )
            conn.commit()
        h._post_completion_marker(
            fake, tid, head, request_comment=retry_comment_id,
        )
        results = h.run_sync(fake)
        entries = [result for result in results if result.get("task_id") == tid]
        accepted = [
            result for result in entries
            if result.get("reason") == "agent_review_ready"
        ]
        assert len(accepted) == 1, entries
        evidence = accepted[0].get("evidence") or {}
        assert evidence.get("verification_only") is True, evidence
        assert not h._pr_attention_comments(fake, tid, "rework_head_unchanged")
        assert not h._pr_attention_comments(fake, tid, "run_head_mismatch")


def main() -> int:
    _ordinary_same_head_feedback()
    _run_head_mismatch_feedback()
    _verification_only_success_has_no_warning()
    print("focused head-binding overlay regressions: PASS")
    # Run every pre-existing canonical rework regression under the overlay.
    return int(h.main())


if __name__ == "__main__":
    raise SystemExit(main())
