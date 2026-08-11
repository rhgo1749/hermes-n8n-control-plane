# GitHub event intake concurrency gate

## Problem

All five GitHub Trigger workflows and the temporary five-minute polling fallback
control the same Hermes cron job, `default:bf431b2a6ba6`, using the same sequence:

```text
trigger Hermes intake -> wait 75 seconds -> pause Hermes intake
```

Without serialization, two overlapping executions can interleave like this:

```text
A trigger -> B trigger -> A pause -> B pause
```

`A pause` is stale: it can disable the intake after `B` has just woken it.

## Fix

This n8n instance is intentionally dedicated to the GitHub intake bridge while
this migration is active. `automation/n8n/compose.yaml` therefore sets:

```yaml
N8N_CONCURRENCY_PRODUCTION_LIMIT: "1"
```

n8n production executions are queued when the production concurrency limit is
full. With a single production slot, each GitHub/Schedule execution completes
its full trigger -> wait -> pause sequence before the next production trigger
execution begins. The five repository workflows can therefore share the same
Hermes intake without a stale-pause race.

This is deliberately an edge-only fix. It does not change Hermes core, the
intake script, Kanban, dispatcher, worker spawning, or callback behavior.

## Rollout order

1. Keep the current five-minute Schedule workflow active as the fallback.
2. Pull this branch/commit on the Ubuntu host.
3. Recreate only the n8n container so the production concurrency limit is
   loaded. Do not restart Hermes for this change.
4. Verify n8n is healthy and its effective container environment contains
   `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`.
5. Import the five GitHub event workflow templates, attach their GitHub and
   Hermes credentials, but keep them unpublished/inactive.
6. Run the repository/static regression tests.
7. Perform a live concurrency canary on the exact deployed n8n version: cause
   two production GitHub-trigger executions close together and verify the
   second execution stays queued until the first completes its 75-second wait
   and pause. Do not use two manual executions for this proof because the
   production concurrency contract applies to production-triggered executions.
8. Verify Hermes intake executed successfully for the event(s), ends paused,
   and no stale pause interrupts the newer execution.
9. Publish the five GitHub event workflows.
10. Leave the five-minute Schedule fallback enabled for an observation window.
11. Only after event delivery and serialization are proven stable, unpublish
    the five-minute Schedule fallback. The Hermes intake job itself remains
    paused between n8n-driven wakes.

## Failure rule

If the second production execution starts before the first production execution
has completed, or if Hermes ends in an unexpected enabled/paused state, do not
publish the five GitHub event workflows and do not remove the five-minute
fallback. Restore the known-good polling-only state and investigate the exact
n8n runtime behavior first.

## Trade-off

The single production slot favors correctness over burst throughput. A burst of
GitHub events is processed FIFO rather than concurrently. That is acceptable
for this bridge because the existing Hermes intake scan remains the policy and
reconciliation authority across all five repositories. If event volume later
makes the queue delay material, replace the single-slot guard with a reviewed
lease/coalescing coordinator before increasing production concurrency.
