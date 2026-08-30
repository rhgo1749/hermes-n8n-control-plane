# REQ-087: 신규 hermes-agent 저장소 checkout·실시간 webhook 자동 온보딩

- Status: Rework round 5 latest-main integration and validation complete; PR refresh and host validation required
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `issue87/onboarding-checkout-webhook`
- Source-of-truth base: `origin/main` at `4424d654127797ddaedf712b94c0cea72806af58`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request path: `.agent/pr-requests/REQ-087-onboarding-checkout-webhook.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#87`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/87
- Kanban task ID: `t_fc3dc14c` (round 5 latest-main integration/validation); prior round 4 ordinary App installation-identity rework: `t_f1244aea`; prior round 3 standalone/provenance/analyzer rework: `t_c55ea2fa`; prior safety/provenance rework: `t_577aa06e`; prior implementation/rework card: `t_14dfe9ee`; original implementation card: `t_1631db5d`
- Design provenance: `t_d26a81e2`, completed DESIGN handoff/comment `227`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:87`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

Personal-owner GitHub App delivery를 기존 `github-router`의 서명·delivery dedupe·durable scope queue에 연결하고, 기존 Hermes intake authority가 새 `hermes-agent` 저장소를 안전하게 검증·checkout·board bootstrap·첫 `agent-ready` Issue intake까지 처리하도록 한다. 조직 owner는 명시적 설정 없이는 선택하지 않으며 기존 intake/edge ownership을 유지한다.

## Confirmed scope and boundaries

- App webhook은 지원되는 repository-bearing delivery와 installation/access 후보를 bounded하게 추출한다. topic-only 또는 undocumented repository-created event를 추정하지 않는다. `GITHUB_ROUTER_INSTALLATION_ID`와 일치하는 양의 decimal `installation.id`를 요구하며, 중첩 `installation.account` owner/type은 있을 때만 추가 검증한다.
- 이벤트 HTTP 요청은 clone/API 장기 작업을 수행하지 않고 기존 queue와 `default:bf431b2a6ba6` lease wake를 사용한다.
- intake가 owner, repository id/name, archived/disabled, default branch, `hermes-agent` topic, default-branch contract visibility를 재검증한다.
- canonical checkout은 repository-name `casefold()` 경로를 사용한다. 기존 경로는 read-only 검증하고 dirty/origin/path conflict를 fail-closed한다. 새 clone은 agent-owned 임시 경로에서 검증 후 no-replace 원자 등록하며 cloned code를 실행하지 않는다.
- repository별 lock, bounded retry/timeout, cleanup ownership, secret-safe Git credential boundary를 적용한다.
- registry는 read-only이고 board mutation은 기존 intake만 담당한다. canonical board slug는 repository name에서 파생하며, authoritative intake job/trigger가 없으면 진단만 하고 replacement를 만들지 않는다.

## Explicit non-goals

Hermes core 수정, second dispatcher 또는 n8n Schedule Trigger, 별도 task/notification DB, GitHub App/hook의 자동 live 설치, destructive reset/delete/remote rewrite, Telegram topic mapping, auto-merge/merge, GitHub-hosted Actions 활성화, unrelated cleanup/redesign, base branch 직접 push는 하지 않는다.

## Validation evidence

검증 결과는 최신 `origin/main` `4424d654127797ddaedf712b94c0cea72806af58`을
merge한 combined worktree head `eca01449fab37ca00751e0fa28fed3e44436255b`에서
실행했다. GitHub Actions는 repository policy상 로컬 PASS를 대신하지 않으며,
host/App 검증은 별도 게이트다.

| Gate | Result | Evidence |
|---|---|---|
| Ordinary App installation-shape regressions | PASS | Signed ordinary-shape `issues`/`pull_request` payloads queue an unknown repository once without edge-sync; wrong, missing, malformed, and missing-configuration installation IDs fail closed. `tests/test_github_router.py` — 48 passed within the focused run. |
| Focused implementation/regression suites | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_repository_onboarding.py tests/test_onboarding_diagnostics.py tests/test_github_router.py tests/test_github_intake_actuator.py tests/test_repo_scoped_intake.py tests/test_repository_registry.py tests/test_repository_registry_bootstrap.py tests/test_intake_lease_controller.py tests/test_n8n_import_contract.py tests/test_intake_merged_pr_guard.py tests/test_github_redirect_safety.py` — 197 passed |
| Focused onboarding safety subset | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q tests/test_repository_onboarding.py tests/test_repo_scoped_intake.py tests/test_repository_registry.py tests/test_onboarding_diagnostics.py` — 96 passed |
| Standalone registry/concurrency contract runners | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_repository_registry.py` — 31 tests; `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_github_event_concurrency_contract.py` — exit 0 |
| Full local suite excluding known baseline modules | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q --ignore=tests/test_board_identity_migration.py --ignore=tests/test_completion_wake_contention_retry.py` — 257 passed |
| Full local suite candidate vs detached `origin/main` | BASELINE-EQUIVALENT FAIL | Candidate — 1 failed, 260 passed, 23 errors; detached `origin/main` — 1 failed, 185 passed, 23 errors. Parsed failure/error identities are identical: 24 candidate-only `0`, baseline-only `0`; failures/errors are in the known baseline modules and were not introduced by this change. |
| `N8N_VALIDATE` | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 automation/n8n/scripts/validate.py` — exit 0 (`schedule_workflows=0`, `edge_sync_workflows=1`, `github_workflows=1`, `github_event_router=1`) |
| Python compile / shell syntax / diff check | PASS | `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q automation tests`, `bash -n automation/n8n/scripts/diagnose-github-onboarding.sh`, `shellcheck automation/n8n/scripts/diagnose-github-onboarding.sh`, and `git diff --check` — exit 0 before this REQ refresh |
| LSP/type diagnostics | BASELINE-EQUIVALENT (errors; production clean) | `basedpyright 1.39.10` / Pyright `1.1.412` over all 18 touched Python files: candidate `107 errors, 3713 warnings, 0 informations`; matching detached `origin/main` baseline over 15 common files: `141 errors, 2286 warnings, 0 informations`; candidate-only error rules `0`, candidate production errors `0`. |
| Touched Python scope | PASS | Analyzer scope is 18 Python files from the complete `origin/main...HEAD` changed-file set; the prior six-file claim is removed. |
| Ruff changed-code gate | BASELINE-EQUIVALENT / PASS | Default Ruff over all 18 touched Python files: candidate `22` diagnostics vs baseline `28`, candidate-only file/rule diagnostics `0`; selected `python3 -m ruff check --select E4,E7,E9,F` over the same set — candidate `0` diagnostics / exit 0. |
| Pyflakes | PASS | Pyflakes over all 18 touched Python files — exit 0, no output |
| Shellcheck | PASS | `shellcheck automation/n8n/scripts/diagnose-github-onboarding.sh` — exit 0 |
| Host/App/network canary | NOT RUN — USER VALIDATION REQUIRED | GitHub App installation/permissions, protected secrets, public HTTPS route, checkout root, production Hermes job/lease, signed canary, duplicate delivery/pause behavior, and operator runtime remain user-owned |

The standalone registry runner passed at the combined head. Existing bite evidence is
retained from prior rounds: the pre-fix runner failed with the expected missing
`monkeypatch` argument, and the prior ordinary-shape router regressions failed before
the installation-identity fix and passed after restoration. This round added no
production-code fix; the latest-main merge was conflict-free and the combined tree
introduced no candidate-only failure/error identity.

## Operator handoff / recovery

Live acceptance must prove an active personal-owner GitHub App webhook whose `installation.id` matches the protected `GITHUB_ROUTER_INSTALLATION_ID`, protected HMAC/API/intake credentials, writable checkout root, healthy router/lease-controller, exactly one scoped wake of `default:bf431b2a6ba6`, one canonical board/task, duplicate-delivery no-op, and paused state after the lease. Topic-only changes without a later supported delivery require explicit `--repository owner/repo` recovery. Archived, foreign, unavailable, contract-invalid, lock-busy, clone-failed, partial, or board/intake-authority failures remain fail-closed; only the current attempt's temporary paths may be removed. Existing checkout rollback is an explicit human action.

## Automation stop state

`HOST_VALIDATION_REQUIRED`: repository-local implementation and deterministic checks may complete here, but live App registration/installation, secret provisioning, public Funnel path, host filesystem permissions, production intake job/trigger, and signed canary remain operator-owned. Merge/auto-merge is not automated.

## Git / PR

- Base SHA: `4424d654127797ddaedf712b94c0cea72806af58` (latest fetched `origin/main`)
- Branch: `issue87/onboarding-checkout-webhook`
- Commits through the pre-REQ-refresh combined validation head (complete `origin/main..HEAD` set): `0fe9a431161b26b1e0cc2ec25f613fb17d056a72`, `99334584fb297db105f975d0c527521c7d286a33`, `110bcb907d3a774b56004b7849bb8f80b4dd3845`, `f4cad8f739d9c16c229dd160cbaba4ac50b4872d`, `b044ea831e175a07a14df74c757988a71e31788e`, `1707128ce6501b2e521ad8edb5c3b95a5cd95e93`, `8be40ad80d2bb538ea0123c50abcb79ffb08c6e2`, `8b5c96b83ae8ba918f4b26d792174f15a5290d69`, `8d188042fe72209aa498fcacabf2beea32c07e17`, `ef34614232e44835295cf8c72b385da51e1919ce`, `2c7b56aa1162a8d2898fbd6b025a43d5c8a7679c`, `da5406a88f6f6e81a3a0dbf1337060599bf21023`, `11dea2df308fa570efbb3fe37837e11342d79021`, `12820fcf1854f213be4e8f389ef2d3d1ee0fa0f2`, `b7027b821d3bdfec8267dc1aacccef791d47fbcbc`, `8b79ddf216677fe9cc906683fa4e01ec06a5d864`, `441114d191d09fb4b2e9a5486bb70a2c39194f72`, `ffad4844be778a9f09f74d8c40b34de11bb8d214`, `41b98a1b39ef7b4e0cc9ef045236d0ef56141dd7`, `eca01449fab37ca00751e0fa28fed3e44436255b`
- Round 5 provenance: latest-main integration merge `eca01449fab37ca00751e0fa28fed3e44436255b` incorporates `origin/main` `4424d654127797ddaedf712b94c0cea72806af58` conflict-free; combined validation was run at that head. This REQ refresh commit is intentionally not self-referenced; the exact final PR head is independently recorded by the post-push REST/GraphQL and remote-branch read-back.
- Last non-self-referential implementation/validation head: `eca01449fab37ca00751e0fa28fed3e44436255b`
- Final PR head: exact full OID is the post-push REST/GraphQL and remote-branch read-back recorded in the live PR body and Kanban handoff; no stale round-4 head is used here.
- PR number/title/URL: PR #88 — `Issue #87: 신규 hermes-agent 저장소 webhook 온보딩` — https://github.com/rhgo1749/hermes-n8n-control-plane/pull/88
- Working tree: clean after final commit; PR #88 open and verified by REST/GraphQL read-back
- Merge performed: NO
