# GitHub event intake concurrency guard

## Final event topology

Repository-specific n8n GitHub Trigger workflows are replaced by a single loopback GitHub router.

```text
GitHub repository webhook
  -> HTTPS Funnel path
  -> github-router :5681
       -> HMAC verification
       -> durable FIFO wake-scope queue
       -> lease-controller :5680
            -> Hermes intake cron trigger

n8n five-minute Schedule
  -> github-router /fallback
       -> webhook reconciliation
       -> full-registry wake scope
       -> same lease-controller
```

Each event/fallback enqueues its scope before triggering Hermes. Each intake invocation claims exactly one queued scope. This keeps an overlapping fallback and repository event distinct instead of allowing one transient scope to contaminate the other. Expired unclaimed scopes are pruned. If the router cannot be reached, intake falls back to the existing full-registry behavior.

The persisted lease-controller remains the stale delayed-pause correctness guard. `N8N_CONCURRENCY_PRODUCTION_LIMIT=1` is only a load limiter.

Webhook reconciliation is topic-driven. Removing `hermes-agent` removes only the webhook whose URL exactly matches this router; boards and tasks are never deleted. Reconciliation failure is reported but never disables the five-minute full-registry fallback.
