# REQ-133: historical reviewer parent가 current delivery를 막지 않게 하는 edge provenance

- Status: Implementation complete; human review/merge required
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `EDGE_RECONCILIATION`
- Source Issue: GitHub Issue #133
- Source Issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/133
- Kanban intake: `t_ad452016`
- Kanban implementation task: `t_f89b9966`
- Intake idempotency: `github:rhgo1749/hermes-n8n-control-plane:issue:133:implementation`
- Source-of-truth base: `origin/main` @ `4152ac1572c7c479674410084cfa4ff6cd99014b`
- Integration target: `main`
- Work branch: `hotfix/issue-133-rework-handoff-selection`
- Validation profiles: `EDGE_REWORK`, `STATIC_UNIT`
- Remote delivery: Required through the existing Issue #133 PR
- Merge authority: Human/user only
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

Append-only `task_links`에 historical/superseded reviewer가 lead의 direct parent로 남아
있어도, current-round developer -> fresh reviewer PASS chain이 exact live PR full
head를 증명하면 edge-owned delivery를 허용한다. Current-round graph, developer
validation/head attestation, reviewer terminal PASS, marker/handoff, live-head
검증 중 하나라도 없거나 모호하거나 오래된 경우에는 계속 fail-closed한다.

## Root cause and bounded fix

기존 specialist selector는 모든 direct parent에 current rework timestamp를
요구해 historical reviewer가 current chain을 veto했다. 또한 developer-linked
specialist graph가 governing rework event 없이 다른 round를 요청하면 동일한
timestamp-matching run을 재사용할 수 있었다.

이번 변경은 canonical `edge/kanban-github-sync.py`에서 current-round reviewer를
결정하고, developer ancestor의 terminal/validation/head evidence와 reviewer
PASS/head를 같은 full SHA로 묶으며, developer-linked graph에는 해당 round의
rework event binding을 요구한다. Historical direct parent는 terminal dependency
검사에는 남지만 current-round reviewer 후보로 사용되지 않는다.

## In scope

- `edge/kanban-github-sync.py`: specialist graph selection과 missing/mismatched
  round fail-closed gate
- `edge/test-kanban-github-sync-rework.py`: exact-current-head delivery positive
  regression과 historical-only, reviewer/head mismatch, developer attestation
  누락, old-round 재사용 negative regressions
- `docs/EDGE_REWORK_LIFECYCLE.md`: specialist-graph delivery contract
- 이 task의 저장소 소유 request 및 기존 Issue #133 PR 설명/검증 증거

PR #134의 기존 implementation에는 deployed entrypoint가 canonical selector와
같은 strict provenance를 사용하도록 하는 guard synchronization도 포함되어
있으며, 이 task의 추가 변경은 위 canonical source/test/request 표면에 한정한다.

## Explicit non-goals and ownership gates

- Hermes core, ctrl-hangul PR #111의 현재 retry round, PR #129/Issue #104,
  runtime DB/labels/deployment 상태를 변경하지 않는다.
- GitHub가 Issue/PR/review의 durable record이고, Hermes Kanban이 execution
  lifecycle owner이며, edge가 reconciliation owner인 경계를 유지한다.
- n8n은 transport/glue일 뿐 second dispatcher/completion owner가 아니다.
- H4V3 Overview와 Telegram은 read-only/attention projection을 유지한다.
- merge/auto-merge, deploy, hosted GitHub Actions enablement는 수행하지 않는다.
- 새로운 state store, arbitrary command surface, secret/token/credential을
  추가하거나 request/PR에 기록하지 않는다.

## Validation and bite evidence

필수 실행 명령:

- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py`
- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-rework-delivery-provenance-guard.py`
- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-rework-attention-delivery-recovery.py`
- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-rework-edge-projection-label-history.py`
- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-terminal-convergence.py`
- changed Python `py_compile`/import, focused Ruff checks, `PYTHONPATH=/ws/hermes-agent basedpyright --level error` (0 errors), and `git diff --check`

Final local results: rework matrix `925` PASS assertions / `0` failures;
provenance guard `9`/`0`; attention recovery `5`/`0`; label-history `4`/`0`;
terminal convergence `95`/`0`. The full basedpyright run emitted repository
baseline warnings but `0 errors`; the errors-only gate passed.

Pre-fix bite/sabotage evidence: before the round-binding gate, the deterministic
old-round probe returned `old_round_candidate True` and selected the current
specialist run when `expected_round=2` had no matching round event. After restoring
the gate, the same probe returns `old_round_candidate False` and
`old_round_selected None`. The Issue #133 historical-parent fixture also fails on
base `origin/main` and passes after the selector fix.

## Delivery / residual risk

The existing PR remains open and is the only remote delivery for Issue #133. The
PR must retain visible plain-text `Closes #133.` and a verified GraphQL
`closingIssuesReferences` relationship. Local edge validation is repository evidence;
GitHub Actions are intentionally disabled by repository policy. Human review,
operator/runtime acceptance, merge, and any deployment remain separate gates.
