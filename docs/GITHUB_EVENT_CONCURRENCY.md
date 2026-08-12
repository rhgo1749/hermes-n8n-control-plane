# GitHub event intake concurrency guard

## Problem

The five GitHub Trigger workflows and the five-minute polling fallback all
control the same Hermes intake job:

```text
default:bf431b2a6ba6
Each execution follows:
trigger -> wait 75 seconds -> pause
Overlapping executions can therefore produce:
A trigger -> B trigger -> A pause -> B pause
The delayed A pause is stale and must never disable intake after B has
already issued a newer wake.
n8n concurrency limit is not the correctness guard
The n8n instance keeps:
N8N_CONCURRENCY_PRODUCTION_LIMIT: "1"
as a burst/load limiter.
Live testing on n8n 2.32.7 demonstrated that executions containing a Wait node
can overlap: later production executions started while earlier executions were
still inside their 75-second Wait. The concurrency limit therefore cannot be
used as the stale-pause correctness boundary.
Lease controller
All schedule and GitHub event wake/pause paths instead call the loopback-only
lease controller:
n8n
  -> 127.0.0.1:5680
       -> Hermes dashboard
POST /trigger creates and durably persists a new lease before forwarding the
wake to Hermes. Creating a newer lease permanently supersedes all older leases.
POST /pause?lease=<token> compares the supplied lease with the latest
persisted lease:
stale lease: HTTP 200, paused=false, reason=superseded; Hermes is not called
latest active lease: forward the pause to Hermes, then persist paused
latest pending lease: reject the pause rather than risk an ambiguous shutdown
A transport failure after a trigger attempt never resurrects an older lease.
The next polling or event wake supersedes the pending lease and restores a
known active state.
The persisted state survives controller restarts.
Live canary
The deployed controller was tested with multiple production GitHub event
executions overlapping inside their 75-second Wait windows.
Four wake executions produced four distinct leases. Their delayed pause calls
arrived in order after newer leases had already been created. Older pauses were
handled through the lease guard and the final/latest lease completed the real
Hermes pause. The controller ended in:
{"ok":true,"lease_status":"paused"}
This is the required stale-pause race canary.
Operational boundary
GitHub remains source/code/PR truth.
Kanban remains task state and history.
n8n remains cross-system event/cadence glue.
Hermes remains the execution authority.
The five-minute polling workflow remains the reconciliation fallback.
The lease controller is loopback-only and does not store the Hermes bearer
token; n8n forwards the existing Authorization header to Hermes.
Hermes core, dispatcher, workers, callbacks, and Kanban state-machine logic
are unchanged.
