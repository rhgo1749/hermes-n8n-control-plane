# GitHub event intake concurrency guard

## Final event topology

Repository-specific n8n GitHub Trigger workflows and the five-minute n8n
Schedule fallback are retired. Production intake is event-driven through one
loopback GitHub router and the existing Hermes cron primitive.

```text
GitHub repository webhook
  -> HTTPS ingress
  -> github-router :5681
       -> HMAC verification
       -> durable FIFO wake-scope queue
       -> lease-controller :5680
            -> existing Hermes job default:bf431b2a6ba6 trigger
            -> Hermes ticker executes the existing intake script
            -> lease-controller pauses the same job after the bounded delay
```

The Hermes job itself is preserved. Its stored job ID, name, script, schedule,
and ownership are not migrated into n8n. Between event-driven invocations the
job normally remains paused; an accepted lease temporarily triggers that same
job and the latest lease alone may pause it again.

Each GitHub event enqueues its repository scope before triggering Hermes. Each
intake invocation claims exactly one queued scope. Expired unclaimed scopes are
pruned. The persisted lease-controller is the stale delayed-pause correctness
guard; `N8N_CONCURRENCY_PRODUCTION_LIMIT=1` remains only a load limiter for the
retained n8n service and is not the intake correctness mechanism.

## Registry reconciliation and fallback

Webhook reconciliation is topic-driven. `github-router /reconcile` discovers
repositories carrying `hermes-agent`, creates or updates the router webhook,
and removes only matching router-owned webhooks for repositories that leave the
topic. It never deletes boards, tasks, or Hermes cron jobs.

Reconciliation is now an explicit maintenance action rather than a five-minute
polling side effect:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

The authenticated `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake, but no tracked n8n Schedule Trigger
calls it automatically.
