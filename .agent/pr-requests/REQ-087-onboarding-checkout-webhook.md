# REQ-087: 신규 hermes-agent 저장소 checkout·실시간 webhook 자동 온보딩

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `issue87/onboarding-checkout-webhook`
- Source-of-truth base: `origin/main` at `dd7f6ad6c6fe39108d87a821c635046ab1fb88e1`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request path: `.agent/pr-requests/REQ-087-onboarding-checkout-webhook.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#87`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/87
- Kanban task ID: `t_1631db5d`
- Design provenance: `t_d26a81e2`, completed DESIGN handoff/comment `227`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:87`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

Personal-owner GitHub App delivery를 기존 `github-router`의 서명·delivery dedupe·durable scope queue에 연결하고, 기존 Hermes intake authority가 새 `hermes-agent` 저장소를 안전하게 검증·checkout·board bootstrap·첫 `agent-ready` Issue intake까지 처리하도록 한다. 조직 owner는 명시적 설정 없이는 선택하지 않으며 기존 intake/edge ownership을 유지한다.

## Confirmed scope and boundaries

- App webhook은 지원되는 repository-bearing delivery와 installation/access 후보를 bounded하게 추출한다. topic-only 또는 undocumented repository-created event를 추정하지 않는다.
- 이벤트 HTTP 요청은 clone/API 장기 작업을 수행하지 않고 기존 queue와 `default:bf431b2a6ba6` lease wake를 사용한다.
- intake가 owner, repository id/name, archived/disabled, default branch, `hermes-agent` topic, default-branch contract visibility를 재검증한다.
- canonical checkout은 repository-name `casefold()` 경로를 사용한다. 기존 경로는 read-only 검증하고 dirty/origin/path conflict를 fail-closed한다. 새 clone은 agent-owned 임시 경로에서 검증 후 no-replace 원자 등록하며 cloned code를 실행하지 않는다.
- repository별 lock, bounded retry/timeout, cleanup ownership, secret-safe Git credential boundary를 적용한다.
- registry는 read-only이고 board mutation은 기존 intake만 담당한다. canonical board slug는 repository name에서 파생하며, authoritative intake job/trigger가 없으면 진단만 하고 replacement를 만들지 않는다.

## Explicit non-goals

Hermes core 수정, second dispatcher 또는 n8n Schedule Trigger, 별도 task/notification DB, GitHub App/hook의 자동 live 설치, destructive reset/delete/remote rewrite, Telegram topic mapping, auto-merge/merge, GitHub-hosted Actions 활성화, unrelated cleanup/redesign, base branch 직접 push는 하지 않는다.

## Validation evidence

검증 결과는 2026-08-29 현재 implementation worktree에서 실행했다. GitHub Actions는
repository policy상 로컬 PASS를 대신하지 않으며, host/App 검증은 별도 게이트다.

| Gate | Result | Evidence |
|---|---|---|
| Focused implementation/regression suites | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_repository_onboarding.py tests/test_onboarding_diagnostics.py tests/test_github_router.py tests/test_github_intake_actuator.py tests/test_repo_scoped_intake.py tests/test_repository_registry.py tests/test_repository_registry_bootstrap.py tests/test_intake_lease_controller.py tests/test_n8n_import_contract.py` — 132 passed |
| Full local suite excluding known baseline modules | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --ignore=tests/test_board_identity_migration.py --ignore=tests/test_completion_wake_contention_retry.py` — 197 passed |
| Full local suite | FAIL (baseline) | 1 failed, 200 passed, 23 errors; the exact failure/error identities also occur on clean `origin/main` (1 failed, 185 passed, 23 errors). |
| `N8N_VALIDATE` | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 automation/n8n/scripts/validate.py` — exit 0 |
| Python compile / shell syntax / diff check | PASS | `python3 -m py_compile` for changed Python sources, `bash -n automation/n8n/scripts/diagnose-github-onboarding.sh`, and `git diff --check` — exit 0 |
| LSP/type diagnostics | PASS | repository Pyright runner — `0 errors, 0 warnings, 0 informations` |
| Ruff changed-code check | PASS | `ruff check --ignore EXE001,FURB188,BLE001,F401` on changed Python sources — all checks passed; unignored findings are pre-existing baseline rules in touched files |
| Host/App/network canary | NOT RUN — USER VALIDATION REQUIRED | GitHub App installation, protected secrets, public HTTPS route, host checkout root, production Hermes job, and signed delivery remain operator-owned |

The new-repository router regression was also run with `_has_app_installation_context()` temporarily
forced to its pre-onboarding false behavior: it failed as expected; restoring the implementation made
it pass. The read-only onboarding diagnostic passes against a synthetic authoritative-job fixture and
fails closed on this host because `default:bf431b2a6ba6` is not installed in the active Hermes profile.

## Operator handoff / recovery

Live acceptance must prove an active personal-owner GitHub App webhook, protected HMAC/API/intake credentials, writable checkout root, healthy router/lease-controller, exactly one scoped wake of `default:bf431b2a6ba6`, one canonical board/task, duplicate-delivery no-op, and paused state after the lease. Topic-only changes without a later supported delivery require explicit `--repository owner/repo` recovery. Archived, foreign, unavailable, contract-invalid, lock-busy, clone-failed, partial, or board/intake-authority failures remain fail-closed; only the current attempt's temporary paths may be removed. Existing checkout rollback is an explicit human action.

## Automation stop state

`HOST_VALIDATION_REQUIRED`: repository-local implementation and deterministic checks may complete here, but live App registration/installation, secret provisioning, public Funnel path, host filesystem permissions, production intake job/trigger, and signed canary remain operator-owned. Merge/auto-merge is not automated.

## Git / PR

- Base SHA: `dd7f6ad6c6fe39108d87a821c635046ab1fb88e1`
- Branch: `issue87/onboarding-checkout-webhook`
- Commits: implementation commit (final SHA recorded in delivery handoff)
- PR number/title/URL: to be recorded after remote delivery
- Working tree: clean implementation commit; remote delivery pending
- Merge performed: NO
