# REQ-016: H4V3 Overview compact project status matrix/list

- Status: Final candidate handoff refreshed at `e447b92e552e9e267977e33e19f20baaea1d12c8`; this bounded task updates the REQ only. Local repository validation is PASS after this edit; host/browser/AT and GitHub Actions remain NOT RUN/external; existing PR #23 is retained and no merge is performed.
- Project: `hermes-n8n-control-plane`
- Product type: `HERMES_PLUGIN`
- Validation profiles: `HERMES_PLUGIN`, `STATIC_UNIT`, `HOST_DASHBOARD`
- Integration target branch: `main`
- Required work branch: `wt/t_765929ec`
- Source-of-truth base: latest fetched `origin/main` at verification: `87ae8be0078bb2e3fabd64da53dc5de4a03276ef`; candidate branch merge-base: `7dc746fd2918e955b7028c4f00f6bb728f69940d` (candidate was not rebased)
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#16`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/16`
- Kanban task ID: `t_4639b6b7` (bounded REQ correction; candidate workspace/task provenance: `t_765929ec`; root: `t_566c1cd6`)
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
8. `tests/test_h4v3_overview_ui.py`
9. `tests/test_h4v3_notification_policy.py`
10. `.agent/REQ_REQUEST_TEMPLATE.md` 및 `.agent/pr-requests/README.md`
11. Issue #16 / design handoff `t_d4d8ff6d` / existing PR #23 provenance is retained from the Kanban parent and repository history. This worker does not claim live PR metadata: `gh` is unavailable and an unauthenticated REST probe did not return the private repository object. Git refs and candidate file scope are verified locally/remotely below.

## 1. Objective

최종 후보는 기존 board card grid 대신 presentation-only desktop semantic matrix를 렌더링하고, `max-width: 900px`에서 같은 payload를 compact labeled vertical list로 표시한다. 여러 board의 현재 상태와 bottleneck을 한 화면에서 비교하되, read-only projection/API, refresh, 오류/empty/deep-link contract는 유지한다. 이 correction은 최종 후보의 CSS cascade/test rework와 validation evidence를 durable REQ에 반영한다.

## 2. Confirmed background

- Current behavior at final candidate: Overview static IIFE reads `/api/plugins/h4v3-overview/overview`, preserves 15-second polling/manual Refresh, and renders one semantic table row per board on desktop plus the labeled mobile list at `max-width: 900px`.
- Limitation addressed: the prior board-card presentation did not provide shared columns for comparing Need You/Blocked/status/rework across projects.
- Affected operators/systems: Hermes dashboard를 통해 여러 Kanban board를 확인하는 운영자. Kanban DB/API와 notification/edge ownership은 영향 없음.
- Repository evidence: `docs/H4V3_OVERVIEW.md`, `dist/index.js`, `dist/style.css`, `plugin_api.py`, `tests/test_h4v3_overview.py`.
- Source Issue product intent: Issue #16 및 `t_d4d8ff6d` handoff의 compact project status matrix/list 요구.
- Runtime/host evidence: 이 worker는 설치된 Hermes dashboard/browser를 대상으로 하지 않는다. 실제 host dashboard render, mobile overflow, keyboard/screen-reader, theme contrast는 아직 확인하지 않았다.
- Assumptions/unresolved facts: 이 worker는 설치된 Hermes dashboard/browser를 대상으로 하지 않으며 실제 host acceptance와 assistive-technology validation은 수행하지 않았다. `origin/main`은 `87ae8be0078bb2e3fabd64da53dc5de4a03276ef`로 fetch되었고, candidate branch는 `7dc746fd2918e955b7028c4f00f6bb728f69940d`에서 유지되며 최신 `origin/main`과 merge-tree conflict는 없고 rebase/merge는 수행하지 않았다. GitHub Actions와 live PR metadata are external/not run in this worker.

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

Candidate implementation allowlist, exactly five files against the candidate base:

1. `hermes-plugin/h4v3-overview/dashboard/dist/index.js`: one board per semantic table row with exact columns `Project`, `Need You`, `Blocked`, `Running`, `Review`, `Ready`, `Rework`, `Recent meaningful state`; attention-derived counts, human-readable recent-state labels, accessible zero/read-error treatment, mobile labeled list, and existing links/loading/error/empty/read-only/refresh/polling behavior.
2. `hermes-plugin/h4v3-overview/dashboard/dist/style.css`: compact matrix hierarchy, Need You/Blocked emphasis, muted zero values, focus-visible treatment, and `max-width: 900px` list fallback without horizontal-scroll-only behavior.
3. `tests/test_h4v3_overview_ui.py`: deterministic static contract coverage for structure, semantics, responsive rules, projection invariants, and unchanged refresh/deep-link/error behavior.
4. `docs/H4V3_OVERVIEW.md`: durable description updated from board cards to matrix/list and missing-file/read-error semantics.
5. `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`: this durable request/evidence record.

This correction task changes only item 5. It does not alter the candidate implementation/API/UI files or create a second PR.

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
- In final commit `e447b92e552e9e267977e33e19f20baaea1d12c8`, the desktop and mobile zero/unavailable selectors follow the status-color selectors so zero values remain muted; non-zero Need You/Blocked emphasis remains specific and visible.
- The same final commit adds `test_zero_colors_override_status_colors_on_desktop_and_mobile`; `tests/test_h4v3_overview_ui.py` therefore has seven UI contract tests, not six.
- Recent internal event kinds/reasons map to `Rework requested`, `Human action required`, `Recent activity`, or `No recent activity`; raw internal names are not the primary UI text.
- Board names remain links using `board.kanban_url`; global Need You task links use existing `task.kanban_url` through `linkFor`.
- Manual Refresh, `/overview`, `setInterval(load, 15000)`, read-only note, initial `role=status`, fatal `role=alert`, stale `role=status`, empty state, and retry behavior remain present.

## 7. Validation contract and evidence

Required local commands:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview_ui.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_notification_policy.py
PYTHONPYCACHEPREFIX=/tmp/req016-pycache /ws/hermes-agent/venv/bin/python3 -m py_compile hermes-plugin/h4v3-overview/dashboard/plugin_api.py tests/test_h4v3_overview_ui.py
node --check hermes-plugin/h4v3-overview/dashboard/dist/index.js
bash -n automation/hermes/scripts/install-h4v3-overview.sh
PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py
git diff --check
```

Validation evidence:

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview.py`: PASS, 12 tests.
- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_overview_ui.py`: PASS, 7 static UI contract tests, including the final desktop/mobile zero-color cascade regression.
- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_h4v3_notification_policy.py`: PASS, 8 tests.
- `PYTHONPYCACHEPREFIX=/tmp/req016-pycache /ws/hermes-agent/venv/bin/python3 -m py_compile hermes-plugin/h4v3-overview/dashboard/plugin_api.py tests/test_h4v3_overview_ui.py`: PASS with bytecode outside the repository.
- `node --check hermes-plugin/h4v3-overview/dashboard/dist/index.js`: PASS.
- `bash -n automation/hermes/scripts/install-h4v3-overview.sh`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py`: PASS.
- `git diff --check`: PASS.
- LSP/static diagnostics: `pyright`, `basedpyright`, `eslint`, `deno`, `biome`, and `ruff` are unavailable in the worker environment; no LSP PASS is claimed. Parse/compile/static checks above are the available local evidence.
- Exact candidate scope: five paths above; current correction diff is only this REQ path.
- Candidate graph/state: `git merge-tree` against `origin/main` is clean; final candidate implementation commit is e447; working-tree cleanliness is checked after validation.

## 8. Host/operator acceptance handoff

No production deployment is requested for this implementation gate. If host acceptance is performed later, use the repository-owned installer and existing dashboard supervisor:

```bash
automation/hermes/scripts/install-h4v3-overview.sh --hermes-home "$HOME/.hermes"
```

Exact candidate identity for this handoff:

- Historical/previous candidate: `9a2edbaf1169fdeb7c92c24c1bd38e9ff5e21cdf` was the earlier documentation state; it is not the final candidate.
- Final candidate implementation commit: `e447b92e552e9e267977e33e19f20baaea1d12c8` (`fix(overview): keep zero status cells muted`).
- Candidate base used: `7dc746fd2918e955b7028c4f00f6bb728f69940d`; latest fetched `origin/main`: `87ae8be0078bb2e3fabd64da53dc5de4a03276ef`; no rebase or merge was performed.
- Candidate branch: `wt/t_765929ec`; `origin/wt/t_765929ec` resolves to the same e447 full SHA at this verification.
- Candidate changed-file allowlist: exactly `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`, `docs/H4V3_OVERVIEW.md`, `hermes-plugin/h4v3-overview/dashboard/dist/index.js`, `hermes-plugin/h4v3-overview/dashboard/dist/style.css`, and `tests/test_h4v3_overview_ui.py`.
- CSS/test rework scope in e447: reorder the desktop/mobile zero and unavailable selectors after status-color selectors so muted zero precedence wins, while retaining specific non-zero Need You/Blocked emphasis; add the seventh UI regression test asserting both cascades.
- This task's correction scope: only this REQ document. The candidate implementation/API/UI behavior is described from e447 and is not represented as unchanged-by-rework.

PASS conditions:

- [ ] exact branch/PR candidate is installed through the atomic installer path;
- [ ] at least three boards are comparable in a desktop viewport;
- [ ] Need You/Blocked evidence is immediately identifiable and plain Review remains informational;
- [ ] 390px/mobile view exposes every field label/value/recent/task link without horizontal overflow;
- [ ] keyboard focus, contrast, read-only behavior, refresh, errors, empty state, and board/task navigation remain correct.

If validation fails, preserve the installer backup and use the printed rollback command; do not mutate Kanban state or deploy an unverified candidate.

## 9. Final report placeholders

### Summary
- Implemented: final candidate records the compact semantic board matrix plus responsive labeled mobile list, the e447 CSS cascade correction, and the seven-test UI contract.
- Intentionally not implemented: backend/API/schema/store, production deployment, host/browser acceptance.

### Files changed
- `hermes-plugin/h4v3-overview/dashboard/dist/index.js`: matrix/list renderer and presentation helpers.
- `hermes-plugin/h4v3-overview/dashboard/dist/style.css`: compact/responsive/accessibility styles.
- `tests/test_h4v3_overview_ui.py`: static UI contract checks.
- `docs/H4V3_OVERVIEW.md`: current UI description.
- `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md`: durable request/evidence record.
- Current bounded correction diff: `.agent/pr-requests/REQ-016-overview-compact-status-matrix.md` only.

### Validation
| Validation | Result | Notes |
|---|---|---|
| Static/unit | PASS | Overview 12 + UI contract 7 + notification policy 8; external-cache Python compile and `git diff --check` PASS. |
| Hermes plugin | PASS | Node IIFE syntax and installer shell syntax PASS; backend compile PASS. |
| n8n validation | PASS | `/ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py`. |
| Host dashboard/network | NOT RUN / EXTERNAL | Requires authorized installed Hermes dashboard/browser and host supervisor. |
| Browser/accessibility/AT | NOT RUN / EXTERNAL | No exact candidate host browser or assistive-technology session was available. |
| GitHub Actions | NOT RUN / DISABLED BY POLICY | Local validation is authoritative. |

### Git / PR
- Candidate base SHA used: `7dc746fd2918e955b7028c4f00f6bb728f69940d`
- Latest fetched `origin/main`: `87ae8be0078bb2e3fabd64da53dc5de4a03276ef`; candidate branch was not rebased or merged; merge-tree conflict probe is clean.
- Final candidate implementation HEAD: `e447b92e552e9e267977e33e19f20baaea1d12c8`; branch: `wt/t_765929ec`; remote branch ref matches this full SHA.
- Historical/previous request evidence: `9a2edbaf1169fdeb7c92c24c1bd38e9ff5e21cdf` is retained only as a previous candidate label, not current state.
- Candidate changed-file allowlist against its base: exactly the five paths listed in §4; backend/API/schema/notification files are absent.
- Existing PR #23 on `wt/t_765929ec` remains the delivery target; no new PR, merge, or auto-merge is performed. Live PR title/base/state/body metadata is `NOT RUN / EXTERNAL` in this worker and is not claimed PASS.
- Current correction diff: this REQ path only; the correction commit and clean working-tree status are recorded in the Kanban completion handoff because a commit cannot embed its own SHA.
- Merge performed: NO

### Remaining risks / owner
- Human/host owner must verify real dashboard layout and assistive technology behavior.
- Human/remote owner retains merge authority; no merge or auto-merge is performed by this task.
