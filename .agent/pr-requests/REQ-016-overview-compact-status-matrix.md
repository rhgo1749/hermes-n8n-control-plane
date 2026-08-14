# REQ-016: H4V3 Overview compact project status matrix/list

- Status: Implementation complete; local validation PASS, PR delivery blocked by unavailable GitHub authentication
- Project: `hermes-n8n-control-plane`
- Product type: `HERMES_PLUGIN`
- Validation profiles: `HERMES_PLUGIN`, `STATIC_UNIT`, `HOST_DASHBOARD`
- Integration target branch: `main`
- Required work branch: `wt/t_765929ec`
- Source-of-truth base: `origin/main` at `7dc746fd2918e955b7028c4f00f6bb728f69940d`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#16`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/16`
- Kanban task ID: `t_765929ec` (root: `t_566c1cd6`)
- Design handoff: `t_d4d8ff6d`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:16`
- Planning/lead owner: `kanban-main` / `kanban-designer`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## 0. Mandatory repository route and evidence

확인한 route:

1. `AGENTS.md`
2. `README.md`
3. `docs/H4V3_OVERVIEW.md`
4. `hermes-plugin/h4v3-overview/dashboard/dist/index.js`
5. `hermes-plugin/h4v3-overview/dashboard/dist/style.css`
6. `hermes-plugin/h4v3-overview/dashboard/plugin_api.py`
7. `tests/test_h4v3_overview.py`
8. `.agent/REQ_REQUEST_TEMPLATE.md` 및 `.agent/pr-requests/README.md`
9. Issue/PR live evidence는 현재 worker 환경에서 확인을 시도했으나 GitHub URL은 `404`를 반환했고 `gh` CLI도 설치되어 있지 않았다. Issue의 canonical body와 design handoff는 Kanban parent provenance로 보존되어 있다.

## 1. Objective

기존 큰 board card grid를 presentation-only desktop semantic matrix로 교체하고, `max-width: 900px`에서 같은 payload를 compact labeled vertical list로 표시한다. 여러 board의 현재 상태와 bottleneck을 한 화면에서 비교하되, read-only projection/API, refresh, 오류/empty/deep-link contract는 유지한다.

## 2. Confirmed background

- Current behavior: Overview static IIFE가 `/api/plugins/h4v3-overview/overview`를 읽고 15초 polling/manual Refresh로 board card grid를 렌더링했다.
- Limitation: board별 card가 같은 열 기준 비교를 제공하지 않아 여러 project의 Need You/Blocked/status/rework를 한눈에 비교하기 어려웠다.
- Affected operators/systems: Hermes dashboard를 통해 여러 Kanban board를 확인하는 운영자. Kanban DB/API와 notification/edge ownership은 영향 없음.
- Repository evidence: `docs/H4V3_OVERVIEW.md`, `dist/index.js`, `dist/style.css`, `plugin_api.py`, `tests/test_h4v3_overview.py`.
- Source Issue product intent: Issue #16 및 `t_d4d8ff6d` handoff의 compact project status matrix/list 요구.
- Runtime/host evidence: 이 worker는 설치된 Hermes dashboard/browser를 대상으로 하지 않는다. 실제 host dashboard render, mobile overflow, keyboard/screen-reader, theme contrast는 아직 확인하지 않았다.
- Assumptions/unresolved facts: GitHub Issue live thread는 익명 URL/API 404와 `gh` 미설치로 직접 재확인하지 못했으며, parent handoff의 canonical Issue provenance를 사용했다. `origin/main` remote fetch는 credential 없이 실패했지만 로컬에 보존된 `origin/main` SHA를 확인하고 그 SHA에서 dedicated worktree branch를 reset했다.

## 3. Ownership and security gates

- H4V3 Overview remains a read-only projection of Kanban/task_events; ownership impact: `NONE`.
- New state/store introduced: `NO`.
- Existing authority bypassed: `NO`.
- `plugin_api.py`, API/schema, Kanban DB/schema, notification policy, and state ownership: unchanged.
- Security impact: `NONE`.
- Auth/permission/secret boundary: `NONE`.
- Host/network exposure: `NONE` (no install/deploy or network change).
- External API/platform policy impact: `NONE`.
- GitHub-hosted Actions: not enabled or changed.

## 4. In scope

1. `dist/index.js`: one board per semantic table row with exact columns `Project`, `Need You`, `Blocked`, `Running`, `Review`, `Ready`, `Rework`, `Recent meaningful state`.
2. `dist/index.js`: per-board Need You count derived only from existing `task.attention === true`; human-readable recent-state labels; zero/read-error accessibility treatment; mobile labeled list; existing board/task links and loading/error/empty/read-only/refresh/polling behavior.
3. `dist/style.css`: compact matrix hierarchy, Need You/Blocked emphasis, muted zero values, focus-visible treatment, and `max-width: 900px` list fallback without horizontal-scroll-only behavior.
4. `tests/test_h4v3_overview_ui.py`: deterministic static contract coverage for structure, semantics, responsive rules, projection invariants, and unchanged refresh/deep-link/error behavior.
5. `docs/H4V3_OVERVIEW.md`: durable description updated from board cards to matrix/list.
6. This task-specific REQ request.

## 5. Explicit non-goals

- `plugin_api.py`, API/schema, Kanban state/schema, notification policy, or any database/store change.
- New status/filter/sort/control or severity-based board reordering.
- Task mutation, worker control, Issue/PR creation, or public exposure.
- New repository metadata source; repository provenance remains API-only secondary data.
- Production install/deploy, dashboard restart, or host/browser acceptance in this worker gate.
- GitHub-hosted Actions enable/re-enable, unrelated cleanup, merge, or auto-merge.

## 6. Implementation contract

- Stable payload board order is preserved.
- Desktop uses native `<table>`/`<caption>`/`<thead>`/`scope="col"` and row headers with `scope="row"`.
- `Need You` is not a Kanban status; explicit task attention remains the only per-board derivation and global task links remain the action surface.
- Ordinary `Review` is informational; only Need You/Blocked non-zero evidence receives attention styling.
- Zero matrix/list counts render as muted `—` while accessible labels retain the numeric value. A `read_error` board renders unavailable values and an inline `Board read unavailable` status instead of claiming zero.
- Recent internal event kinds/reasons map to `Rework requested`, `Human action required`, `Recent activity`, or `No recent activity`; raw internal names are not the primary UI text.
- Board names remain links using `board.kanban_url`; global Need You task links use existing `task.kanban_url` through `linkFor`.
- Manual Refresh, `/overview`, `setInterval(load, 15000)`, read-only note, initial `role=status`, fatal `role=alert`, stale `role=status`, empty state, and retry behavior remain present.

## 7. Validation contract and evidence

Required local commands:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview_ui.py
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile hermes-plugin/h4v3-overview/dashboard/plugin_api.py
node --check hermes-plugin/h4v3-overview/dashboard/dist/index.js
git diff --check
```

Validation evidence:

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview.py`: PASS, 12 tests.
- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview_ui.py`: PASS, 6 static UI contract tests.
- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_notification_policy.py`: PASS, 8 tests.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile hermes-plugin/h4v3-overview/dashboard/plugin_api.py tests/test_h4v3_overview_ui.py`: PASS.
- `node --check hermes-plugin/h4v3-overview/dashboard/dist/index.js`: PASS.
- `bash -n automation/hermes/scripts/install-h4v3-overview.sh` plus manifest/static asset probe: PASS.
- `git diff --check`: PASS.
- LSP/static diagnostics: `pyright`, `basedpyright`, `eslint`, `deno`, `biome`, and `ruff` are unavailable in the worker environment; available parse/compile/static checks above were run instead. No LSP PASS is claimed.
- A temporary Node mocked-SDK fixture rendered the actual registered IIFE component and verified matrix headers, accessible unavailable/Need You counts, mobile list, and recent-label mapping: PASS (`node /tmp/h4v3_overview_ui_smoke.js`).

## 8. Host/operator acceptance handoff

No production deployment is requested for this implementation gate. If host acceptance is performed later, use the repository-owned installer and existing dashboard supervisor:

```bash
automation/hermes/scripts/install-h4v3-overview.sh --hermes-home "$HOME/.hermes"
```

PASS conditions:

- [ ] exact branch/PR candidate is installed through the atomic installer path;
- [ ] at least three boards are comparable in a desktop viewport;
- [ ] Need You/Blocked evidence is immediately identifiable and plain Review remains informational;
- [ ] 390px/mobile view exposes every field label/value/recent/task link without horizontal overflow;
- [ ] keyboard focus, contrast, read-only behavior, refresh, errors, empty state, and board/task navigation remain correct.

If validation fails, preserve the installer backup and use the printed rollback command; do not mutate Kanban state or deploy an unverified candidate.

## 9. Final report placeholders

### Summary
- Implemented: compact semantic board matrix plus responsive labeled mobile list.
- Intentionally not implemented: backend/API/schema/store, production deployment, host/browser acceptance.

### Files changed
- `hermes-plugin/h4v3-overview/dashboard/dist/index.js`: matrix/list renderer and presentation helpers.
- `hermes-plugin/h4v3-overview/dashboard/dist/style.css`: compact/responsive/accessibility styles.
- `tests/test_h4v3_overview_ui.py`: static UI contract checks.
- `docs/H4V3_OVERVIEW.md`: current UI description.
- `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`: durable request/evidence record.

### Validation
| Validation | Result | Notes |
|---|---|---|
| Static/unit | PASS | Overview 12 + UI contract 6 + notification policy 8; Python compile and diff check PASS. |
| Hermes plugin | PASS | Node IIFE syntax, installer shell syntax, manifest/static asset probe PASS; backend unchanged and compiled. |
| Host dashboard/network | NOT RUN | Requires authorized installed Hermes dashboard/browser. |
| GitHub Actions | NOT RUN / DISABLED BY POLICY | Local validation is authoritative. |

### Git / PR
- Base SHA: `7dc746fd2918e955b7028c4f00f6bb728f69940d`
- Branch: `wt/t_765929ec`
- Commits: `3b87b01` (`feat(overview): compact project status matrix`)
- PR number/title/URL: NOT CREATED — `git push -u origin wt/t_765929ec` failed with exit 128 because HTTPS credentials were unavailable; create exactly one Korean PR against `main` after authenticated delivery is available.
- Working tree: clean after local commits; merge/auto-merge not performed
- Merge performed: NO

### Remaining risks / owner
- Human/host owner must verify real dashboard layout and assistive technology behavior.
- Remote GitHub delivery requires authenticated GitHub tooling; worker must record any inability to fetch/push/create the PR as an explicit delivery gap rather than fabricating success.
