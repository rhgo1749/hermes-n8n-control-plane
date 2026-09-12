# REQ-150 — detached canonical anchor self-heal

- Status: Delivery-contract rework; implementation remains bounded to the reviewed PR #151.
- Project: `hermes-n8n-control-plane`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `hotfix/150-detached-canonical-anchor-selfheal`
- Source-of-truth base: `origin/main` / `c585d168cf5f6d3be8b714520b09d9cbb3e720b8`
- Existing delivery: PR #151, open, `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/151`
- Merge authority: Human/user only; no merge or auto-merge by this worker
- Automation stop state: `HOST_VALIDATION_REQUIRED`

Source incident: 2026-09-12 CtrlHangul Issue #112 `agent-ready` intake was received by the GitHub router but no Kanban task was created because the canonical CtrlHangul checkout was left on a clean detached HEAD after PR #111 work. The canonical anchor had HEAD `92d5eb1...`, local `main` `6bda1dd...`, and `origin/main` `db0579e...`. Manual safe recovery (`switch main` + `merge --ff-only origin/main`) immediately restored intake; scoped canonical intake then created task `t_02f63769` and dispatcher claimed it.
Source Issue: rhgo1749/hermes-n8n-control-plane#150.
Kanban provenance: parent intake `t_46bb97af`, investigation `t_914fd721`, implementation `t_144f8950`.
Implementation base: `origin/main` / `c585d168cf5f6d3be8b714520b09d9cbb3e720b8`.
Required implementation branch: `hotfix/150-detached-canonical-anchor-selfheal`.
Historical reviewed implementation head: `f2a3e19b5334405b570a1e847a25b2e48a4f7071`.
Current candidate identity is the fresh full head SHA read from PR #151 after this delivery; the operator binds it through `CANDIDATE_SHA` below.


## Objective

Extend the existing #117 stale/shallow checkout self-heal contract with one narrowly proven detached-anchor recovery case, without weakening dirty/diverged/wrong-origin safety.

## In scope

- When the canonical checkout is clean but `HEAD` is detached, allow recovery only if:
  - repository/origin/default-branch metadata is already trusted by the existing onboarding path;
  - the local ref for the trusted default branch exists;
  - detached `HEAD` is an ancestor of that local default branch (therefore switching cannot discard unique detached commits);
  - Git can switch to that existing default branch without force/reset and without stealing a branch from another worktree.
- Before cleanliness checks or materialization, reject repository-local
  executable configuration and `$GIT_DIR/info/attributes` rather than allowing
  filters, merge drivers, URL rewrites, or remote helpers to run.
- After reattaching, reuse the existing `_self_heal_stale_checkout` contract for shallow handling, target-SHA ancestry proof, bounded fetch, and `merge --ff-only`.
- Preserve fail-closed behavior for dirty checkout, detached HEAD with unique commits, missing local default branch, branch owned by another worktree, wrong origin, wrong repository, divergence, unsafe attributes/materialization, or any ambiguous Git result.
- Add focused regression coverage reproducing the incident shape and negative safety cases.
- Deploy through the existing intake/edge deployment path only after merge; do not mutate Kanban DBs or lifecycle labels as part of the hotfix.

## Non-goals

- `reset --hard`, `checkout -f`, forced branch moves, deleting worktrees, pruning user branches, broad cleanup, or history rewrite.
- Reusing task worktrees as canonical anchors.
- Changing `agent-ready` label semantics, dispatcher ownership, edge lifecycle semantics, or periodic fallback cadence.

## Validation

- RED-before/GREEN-after real-Git detached-anchor regression.
- Dirty / unique-detached-commit / missing-default-branch / branch-in-other-worktree remain mutation-free fail-closed.
- Local Git filter/transport configuration and `$GIT_DIR/info/attributes` remain
  fail-closed without executing their commands.
- Existing stale/shallow/divergence self-heal tests remain green.
- `py_compile`, focused pytest, `git diff --check`, and deploy dry-run.
- Repository/static validation is separate from post-merge live validation. Post-merge runtime deployment, source/live hash equality, scoped CtrlHangul intake/read-back, router/actuator health, no duplicate #112 task, and preservation of existing worktree/DB state remain host-owned gates.

## Merge/deploy authority

The user explicitly requested hotfix, merge, and deployment for this incident. Merge is allowed only after fresh PR head/mergeability/review/gate re-check. Live deployment must use the repository-owned deployment path and preserve timestamped backups. This rework does not change production source, tests, canonical docs, branch semantics, or deploy behavior.

## Operator acceptance — HOST_VALIDATION_REQUIRED

Repository/static validation is worker-executable evidence. Live post-merge validation is a separate operator gate and has not been run by this worker. Still-unrun items are:

- live deployment of the exact merged PR #151 candidate;
- source/live SHA equality for the deployed intake and edge entrypoint/core files;
- router, lease-controller, and intake-actuator health;
- one scoped signed CtrlHangul delivery for Issue #112 with an idempotent read-back of the existing `github:rhgo1749/ctrl-hangul:issue:112` task `t_02f63769`, with no duplicate creation;
- preservation of the existing CtrlHangul worktree and Kanban DB state outside the bounded clean-detached-anchor recovery.

The exact candidate/source identity is the full PR #151 head SHA returned by the fresh REST read-back after this rework is pushed. The operator must bind that value to `CANDIDATE_SHA`; no abbreviated or guessed SHA is accepted.

### Copy-paste minimum acceptance command

Run this on the operator-owned host after the human/user has merged the candidate. All host-specific values are supplied through guarded environment variables; the command uses only the repository-owned deployment, diagnosis, and signed intake paths.

```bash
set -Eeuo pipefail
REPO_ROOT="${REPO_ROOT:?set REPO_ROOT to the exact checkout of the merged candidate}"
HERMES_HOME="${HERMES_HOME:?set HERMES_HOME to the active Hermes runtime home}"
CANDIDATE_SHA="${CANDIDATE_SHA:?set CANDIDATE_SHA to the full PR #151 head SHA}"
WEBHOOK_URL="${WEBHOOK_URL:?set WEBHOOK_URL to the reviewed /github/hermes-intake endpoint}"
WEBHOOK_SECRET_FILE="${WEBHOOK_SECRET_FILE:?set WEBHOOK_SECRET_FILE to the protected GitHub webhook secret file}"
CTRLHANGUL_CHECKOUT="${CTRLHANGUL_CHECKOUT:?set CTRLHANGUL_CHECKOUT to the existing canonical CtrlHangul checkout}"

cd "$REPO_ROOT"
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
automation/hermes/scripts/deploy-intake-edge.sh --hermes-home "$HERMES_HOME"
automation/n8n/scripts/diagnose-github-onboarding.sh --hermes-home "$HERMES_HOME"

for pair in \
  "automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py:$HERMES_HOME/scripts/github-agent-ready-kanban-intake.py" \
  "automation/hermes/scripts/github-agent-ready-kanban-intake.py:$HERMES_HOME/scripts/github-agent-ready-kanban-intake-core.py" \
  "edge/kanban-github-sync-entrypoint.py:$HERMES_HOME/scripts/kanban-github-sync.py" \
  "edge/kanban-github-sync.py:$HERMES_HOME/scripts/kanban-github-sync-core.py"
do
  source_file="${pair%%:*}"
  live_file="${pair#*:}"
  test "$(sha256sum "$source_file" | cut -d' ' -f1)" = "$(sha256sum "$live_file" | cut -d' ' -f1)"
done

body='{"repository":{"full_name":"rhgo1749/ctrl-hangul"}}'
delivery_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
signature="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$(tr -d '\n' < "$WEBHOOK_SECRET_FILE")" -hex | awk '{print $2}')"
curl -fsS --max-time 30 -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' \
  -H "X-GitHub-Delivery: $delivery_id" \
  -H "X-Hub-Signature-256: sha256=$signature" \
  --data "$body"

git -C "$CTRLHANGUL_CHECKOUT" status --short --branch
HERMES_HOME="$HERMES_HOME" hermes kanban show t_02f63769
```

The signed delivery must be followed by a fresh task read-back. The read-back must show the existing Issue #112 idempotency key mapped to exactly `t_02f63769` and no second task. Record the pre/post canonical checkout branch, `HEAD`, and porcelain status, and the pre/post task/DB read-back; the only permitted checkout change is the reviewed clean-detached reattachment/fast-forward, never reset, force checkout, branch deletion, or unrelated DB mutation.

### PASS conditions

- [ ] The exact merged PR #151 candidate is deployed, and the source/live SHA checks above match for the deployed intake and edge entrypoint/core files.
- [ ] Router, lease-controller, and intake-actuator health are healthy through `diagnose-github-onboarding.sh`; the signed delivery is accepted on the reviewed intake route.
- [ ] The scoped signed path yields the existing `github:rhgo1749/ctrl-hangul:issue:112` record as `t_02f63769` exactly once, without duplicate task creation.
- [ ] Existing CtrlHangul worktree and Kanban DB state are preserved except for the bounded clean detached-anchor reattachment/fast-forward and the already-idempotent read-back; no reset, force, broad cleanup, or unrelated task mutation occurs.

### If validation fails — diagnostics, recovery, and rollback

1. Retain the deployment command output. It contains candidate validation, installed source/live checks, timestamped backups, and the repository-owned `Rollback:` commands.
2. Re-run the bounded diagnosis without mutation:

   ```bash
   automation/n8n/scripts/diagnose-github-onboarding.sh --hermes-home "$HERMES_HOME"
   curl -fsS --max-time 3 http://127.0.0.1:5681/healthz
   curl -fsS --max-time 3 http://127.0.0.1:5682/healthz
   HERMES_HOME="$HERMES_HOME" hermes kanban show t_02f63769
   git -C "$CTRLHANGUL_CHECKOUT" status --short --branch
   ```

3. Stop the canary on any identity, health, task, worktree, or DB mismatch. Do not create, delete, or manually repair a task. Use the exact timestamped `Rollback:` `mv` commands printed by `deploy-intake-edge.sh`, including its config backup restore, then rerun the diagnosis and state probes.
4. Retry only after the operator has resolved the reported boundary and has fresh source/live identity evidence. Private host paths, credentials, service restarts, and any rollback execution remain operator-owned.

### After PASS

- Merge-ready: YES only after the required local validation and independent reviewer gates are satisfied and the operator accepts the live evidence; this worker does not merge or auto-merge.
- Additional human judgment required: YES — the human/user owns merge authority, live acceptance, private-host/network decisions, and confirmation that pre-existing worktree/DB state is preserved.

## Local command attempted by the developer

`automation/hermes/scripts/deploy-intake-edge.sh --hermes-home "$HOME/.hermes" --dry-run` was attempted from the final candidate checkout and is reported separately from live validation. It validates and removes the candidate without mutating the active runtime. The live mutation/canary was not run because this worker is not authorized to merge, access the private host services, or mutate the active runtime/worktree/Kanban DB; therefore the automation stop state remains `HOST_VALIDATION_REQUIRED`.
