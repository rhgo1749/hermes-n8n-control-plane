# REQ-048: Hermes intake cron 제거 및 direct actuator 전환

- Status: In Progress
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `refactor/direct-intake-actuator`
- Source-of-truth base: `5e71def`
- Source issue: `rhgo1749/hermes-n8n-control-plane#48`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:48`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

GitHub event intake가 Hermes cron job `bf431b2a6ba6`을 trigger/pause하지 않고,
Hermes runtime 내부의 배포된
`/home/hermes/.hermes/scripts/github-agent-ready-kanban-intake.py`
를 loopback 전용 direct actuator를 통해 실행하도록 전환한다.

## Confirmed runtime evidence

- Hermes container uses host networking.
- `127.0.0.1:5682` is free.
- Existing H4V3 owner gateway proves the accepted host systemd ->
  `docker exec --user 1000:1000` supervision pattern.
- `bf431b2a6ba6` is currently paused/disabled and remains untouched until the
  replacement path passes a live canary.
- Hermes core modification is prohibited.

## In scope

1. Dedicated authenticated intake actuator on loopback `:5682`.
2. Lease controller trigger -> actuator conversion.
3. Remote cron pause -> local lease-close conversion.
4. Cron-auth token dependency -> generic intake-control credential.
5. Repository-scoped wake claim remains functional after cron-auth retirement.
6. Live canary before deleting the legacy Hermes cron job.

## Non-goals

- Hermes core modification.
- Docker socket mount into control-plane containers.
- Generic shell/command execution API.
- New dispatcher/task database.
- GitHub Actions enablement.
- Legacy cron deletion before runtime canary PASS.

## Security boundary

The actuator accepts only:

- `GET /healthz`
- authenticated `POST /v1/intake`

It has no request-controlled command, argv, script path, profile, or shell.

## Expected final flow

`GitHub webhook -> github-router :5681 -> lease-controller :5680 -> intake actuator :5682 -> deployed intake script -> Hermes Kanban`

## Host acceptance

Before deleting `bf431b2a6ba6`:

- actuator health PASS
- router/controller health PASS
- signed/recovery intake wake PASS
- repository-scoped intake preserved
- legacy cron `last_run_at` does not advance
- direct actuator execution completes successfully

Only after those checks may the legacy cron job and cron-only auth surfaces be removed.
