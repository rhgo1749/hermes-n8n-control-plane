# REQ-058 regression hotfix: dispatcher-side completion safety wake

- Source Issue: #58
- Regression of merged PR: #60
- Base: `main` @ `efff047735c7b3cab0dd1366aa94c4582af5805a`
- Branch: `hotfix/req-058-completion-safety-wake`
- Merge authority: human/user only
- Runtime deployment: separate host gate

## Live regression evidence

Two GitHub-backed root tasks completed successfully but remained provisional `done`
until a later edge reconciliation:

- `ctrlhangul/t_ac34f08d`: completed `1788502406`; `github_pr_sync`
  `1788509757` -> about 2h 02m stranded.
- `re-bound/t_32d91a93`: completed `1788504048`; `github_pr_sync`
  `1788509763` -> about 1h 35m stranded.
- Both late syncs occurred six seconds apart, which is inconsistent with each
  worker-side completion observer immediately parking its own open PR in
  `review`.
- `github-completion-edge-wake` is installed and enabled, including from the
  `kanban-main` profile.
- Both root runs ended `outcome=completed` and their task bodies carry the
  importer-owned `completion contract: github-pr` provenance.

## Hotfix objective

Keep PR #60's worker-side completion observer as the primary zero-latency path.
Add a dispatcher-side bounded safety observer which, on the existing dispatcher
tick only, detects a recent committed `completed` event whose GitHub-backed task
is still `done` and replays the primary observer once.

## Invariants

- Canonical `kanban-github-sync.py` remains the sole GitHub/Kanban state owner.
- No direct task status write by the safety plugin.
- No cron, Schedule Trigger, sleep loop, or independent GitHub polling.
- No second durable state database.
- Recent-completion lookback is bounded.
- Replay attempts are bounded to two per completion event per dispatcher process.
- Ordinary/non-GitHub tasks and already-projected `review` tasks are ignored.
- Human merge authority remains unchanged.

## Host acceptance

After repository-local validation:
1. install/enable companion plugin;
2. restart only the Hermes gateway/dispatcher owner required to register the
   new long-lived hook;
3. create a controlled GitHub-backed canary that completes to an open PR;
4. verify `completed -> github_pr_sync(review)` occurs on the same/next
   dispatcher tick, not minutes/hours later;
5. verify no duplicate worker spawn and no direct status mutation by the safety
   plugin.
