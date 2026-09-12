# Hermes → GitHub Event Control Plane

Event-driven GitHub ↔ Hermes Kanban integration with **n8n as a bounded event hop, not a lifecycle owner**.

This repository connects signed GitHub events to an existing Hermes/Kanban runtime without modifying Hermes core. It focuses on explicit state ownership, idempotent event handling, fail-closed reconciliation, and reproducible validation.

- **처음 보는 분 / 채용 검토자:** [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md)
- **정본 문서 인덱스:** [`docs/README.md`](docs/README.md)
- **Public 전환 점검:** [`docs/PUBLIC_RELEASE_CHECKLIST.md`](docs/PUBLIC_RELEASE_CHECKLIST.md)

## What this project does

GitHub-backed work has several failure modes that a simple webhook → action pipeline does not solve by itself:

- the same delivery can arrive more than once;
- events can arrive after local state has changed;
- a worker can finish internally while its PR is still open;
- a maintainer can request another rework round on an existing PR;
- an external lookup or mutation can fail or return stale evidence;
- two components can accidentally become competing state owners.

This control plane separates **event transport** from **authoritative state projection** so those cases converge through one canonical edge state machine.

## Architecture

```text
GitHub repository event
          |
          v
   github-router
   - HMAC verification
   - delivery dedupe
   - repository admission
          |
          +-- Issue/intake event
          |       -> lease-controller
          |       -> fixed intake actuator :5682
          |       -> deployed Hermes intake script
          |
          +-- PR merge / trusted rework
                  -> private n8n Webhook
                  -> fixed edge actuator
                  -> edge/kanban-github-sync.py

GitHub-backed worker completion
          -> kanban_task_completed observer
          -> fixed edge wake
          -> edge/kanban-github-sync.py
          -> fresh GitHub evidence based projection
```

The current topology is event-driven. The tracked n8n workflow is an on-demand PR edge-sync Webhook; it is not a polling Schedule Trigger and it does not register itself as a second dispatcher or completion owner.

## Ownership boundaries

### GitHub router

`github-router` owns external event admission:

- GitHub webhook signature verification;
- `X-GitHub-Delivery` replay deduplication;
- managed-repository admission;
- bounded routing into the existing intake path or PR edge-sync path.

It does **not** decide final GitHub/Kanban lifecycle state.

### n8n

n8n is deliberately narrow glue:

- receives a normalized, authenticated PR event;
- allows only the supported merged/rework event shapes;
- calls one fixed loopback edge actuator.

n8n does **not** own polling, Kanban state, worker spawning, merge authority, or an alternative lifecycle state machine.

### Canonical edge

[`edge/kanban-github-sync.py`](edge/kanban-github-sync.py) owns GitHub ↔ Kanban lifecycle reconciliation. It reads current evidence and projects the authoritative state instead of trusting a previous webhook snapshot as completion evidence.

This means:

- Issue-side readiness/hold and PR-side rework lifecycle are distinct surfaces;
- a trusted rework request opens a new PR rework round rather than following review-ready automatically;
- current-round claim/delivery evidence matters;
- stale delivery evidence must not create a fresh review-ready state;
- internal Kanban completion can remain provisional until GitHub evidence confirms the external result.

The exact lifecycle contract lives in [`docs/EDGE_REWORK_LIFECYCLE.md`](docs/EDGE_REWORK_LIFECYCLE.md) and [`docs/GITHUB_COMPLETION_LIFECYCLE.md`](docs/GITHUB_COMPLETION_LIFECYCLE.md).

## Main event flows

### Issue intake

```text
signed GitHub event
  -> github-router
  -> repository-scoped durable wake
  -> lease-controller
  -> fixed intake actuator :5682
  -> deployed Hermes intake script
  -> repository / board / idempotency revalidation
```

The lease-controller serializes intake wakes and invokes the fixed loopback actuator. The legacy Hermes cron intake job is not required in the current runtime topology.

### PR merge / rework

```text
signed pull_request event
  -> github-router
  -> private n8n Webhook
  -> normalized allowlist
  -> fixed edge actuator
  -> canonical edge reconciliation
```

Only the reviewed event shapes are forwarded. Unsupported events are no-ops rather than generic command execution.

### Worker completion

```text
core kanban_complete
  -> committed provisional DONE
  -> completion observer
  -> canonical edge wake
  -> fresh GitHub state check
  -> REVIEW / DONE projection
```

The observer is a trigger, not a completion owner. A failed wake or ambiguous runtime condition is diagnostic evidence, not merge evidence.

## Security and failure boundaries

The implementation is designed around narrow trust boundaries rather than a broadly exposed automation API.

- GitHub webhook ingress is signature-verified and delivery-deduplicated.
- Internal n8n/router/controller/actuator paths remain private/loopback-oriented services.
- Runtime credentials are generated or copied into protected external state rather than committed to workflow JSON.
- The tracked n8n workflow contains credential placeholders and binds protected credentials during deployment.
- The actuator invokes a fixed reconciliation path instead of accepting arbitrary shell commands.
- Detailed operator diagnostics require authentication; a public health surface is not treated as a state dump.
- External lookup/mutation ambiguity fails closed rather than being promoted to success.

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the actual host deployment and recovery contract.

## Repository layout

| Path | Purpose |
| --- | --- |
| `automation/n8n/github-router/` | Signed GitHub event ingress and bounded routing |
| `automation/n8n/lease-controller/` | Direct-actuator intake wake/lease guard |
| `automation/n8n/workflows/github-pr-edge-sync.json` | Private PR lifecycle Webhook → fixed actuator workflow |
| `automation/n8n/scripts/` | Registry, deployment, workflow import and validation helpers |
| `automation/hermes/scripts/` | Hermes-side intake/edge deployment and integration helpers |
| `edge/` | Canonical GitHub/Kanban edge reconciliation and guards |
| `hermes-plugin/` | Narrow Hermes observer / overview integrations |
| `docs/` | Durable lifecycle, ownership, operations and registry contracts |
| `tests/` | Repository-local regression and integration-contract tests |
| `.agent/pr-requests/` | Historical task/request evidence for implementation work |

## Reading guide

| If you want to understand... | Start here |
| --- | --- |
| Project intent and design choices | [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) |
| GitHub webhook concurrency / delivery dedupe | [`docs/GITHUB_EVENT_CONCURRENCY.md`](docs/GITHUB_EVENT_CONCURRENCY.md) |
| PR rework lifecycle | [`docs/EDGE_REWORK_LIFECYCLE.md`](docs/EDGE_REWORK_LIFECYCLE.md) |
| Worker completion vs GitHub review/done | [`docs/GITHUB_COMPLETION_LIFECYCLE.md`](docs/GITHUB_COMPLETION_LIFECYCLE.md) |
| Worker resource admission | [`docs/EDGE_WORKER_RESOURCE_ADMISSION.md`](docs/EDGE_WORKER_RESOURCE_ADMISSION.md) |
| Repository discovery / checkout / board authority | [`docs/REPOSITORY_REGISTRY.md`](docs/REPOSITORY_REGISTRY.md) |
| Kanban role authority | [`docs/KANBAN_ROLE_CONTRACTS.md`](docs/KANBAN_ROLE_CONTRACTS.md) |
| Host install, rollout and recovery | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| Public-release privacy/security gate | [`docs/PUBLIC_RELEASE_CHECKLIST.md`](docs/PUBLIC_RELEASE_CHECKLIST.md) |

The documentation index in [`docs/README.md`](docs/README.md) identifies the canonical owner for each contract. Source and tests remain authoritative for executable behavior.

## Host deployment

This is an operating control-plane repository, not a generic hosted n8n template. Host operations must follow [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and the current runtime prerequisites.

The high-level deployment path is:

```bash
# Install/start the private control-plane stack.
automation/n8n/scripts/host-install.sh --enable-docker-service

# Configure protected GitHub/router credentials outside the repository.
automation/n8n/scripts/configure-github-router-secrets.sh

# Reconcile managed repository webhooks when required.
automation/n8n/scripts/reconcile-github-router.sh

# Render/import/publish the managed private n8n edge-sync workflow.
automation/n8n/scripts/import-workflows.sh
```

Do not infer that repository-local validation proves the live signed-delivery path. Host/runtime canaries are separate evidence gates.

## Validation

The repository intentionally relies on repository-local deterministic validation rather than treating GitHub Actions as the required correctness surface.

A useful starting point is:

```bash
python3 automation/n8n/scripts/validate.py
```

Focused suites then cover router behavior, lease ordering, repository registry, completion wake, resource admission, rework lifecycle and edge reconciliation. The owning documentation names the relevant validation surface for each subsystem.

A test result, internal Kanban state, webhook acknowledgement, or successful command exit is not automatically authoritative GitHub completion evidence.

## Explicit non-goals

This repository does not aim to:

- modify or replace Hermes core;
- create a second Kanban/task database;
- make n8n a dispatcher or lifecycle owner;
- expose n8n as a public generic completion API;
- recreate worker/worktree/spawn behavior in n8n;
- infer merge from labels or internal completion alone;
- perform automatic merge;
- silently invent a new state when external evidence is ambiguous.

## Public repository note

Before changing repository visibility, review not only the current tree but the full Git history and GitHub Issues/PR discussions for credentials, personal paths, private hostnames and copied runtime logs. The repository-specific gate is documented in [`docs/PUBLIC_RELEASE_CHECKLIST.md`](docs/PUBLIC_RELEASE_CHECKLIST.md).
