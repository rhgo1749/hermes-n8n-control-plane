# Hermes → GitHub Event Control Plane

> 외부 독자와 채용 검토자를 위한 빠른 개요입니다. 이 문서는 lifecycle이나 운영 계약의 새로운 정본이 아닙니다. 정확한 현재 계약은 [`README.md`](README.md), [`docs/`](docs/README.md), 그리고 해당 source/tests를 따릅니다.

## 한눈에 보기

이 저장소는 **Hermes core를 수정하지 않고** GitHub 이벤트를 Hermes Kanban 작업 흐름과 연결하는 외부 control plane입니다.

핵심 설계 원칙은 단순합니다.

- GitHub 이벤트를 검증하고 중복 delivery를 제거합니다.
- n8n은 제한된 이벤트 전달·필터링 hop으로만 사용합니다.
- GitHub/Kanban lifecycle의 authoritative projection은 canonical edge가 소유합니다.
- 내부 작업 완료와 외부 GitHub 완료를 같은 것으로 취급하지 않습니다.
- 실패하거나 애매한 외부 상태를 성공으로 추측하지 않고 fail-closed 합니다.

```text
GitHub event
    |
    v
GitHub router
(HMAC verification / delivery dedupe / repository admission)
    |
    +-- Issue intake ------> lease controller ------> existing Hermes intake job
    |
    +-- PR merge/rework --> private n8n Webhook --> fixed edge actuator
                                                    |
                                                    v
                                           canonical edge reconciler

GitHub-backed worker completion
    -> completion observer
    -> canonical edge reconciler
    -> fresh GitHub evidence 기반 REVIEW / DONE projection
```

## 왜 이렇게 나눴나

### 1. 이벤트 전달과 상태 소유권을 분리

Webhook을 받았다는 사실만으로 작업이 완료되었다고 판단하지 않습니다. `github-router`와 n8n은 이벤트를 전달하고 깨우는 역할에 한정하고, 실제 GitHub/Kanban 상태 전이는 edge reconciler가 현재 상태를 다시 조회한 뒤 결정합니다.

이 경계 덕분에 webhook 재전송, 순서 뒤바뀜, timeout, stale state가 발생해도 별도의 lifecycle owner가 서로 다른 결론을 쓰는 상황을 피할 수 있습니다.

### 2. Issue intake와 PR rework를 같은 선형 상태로 보지 않음

GitHub Issue의 runnable/readiness 상태와 기존 PR의 rework round는 서로 다른 surface입니다. 특히 PR rework는 trusted request가 새 round를 열고, 현재 round의 claim/delivery evidence가 확인될 때만 working/review-ready projection이 진행됩니다.

세부 lifecycle은 [`docs/EDGE_REWORK_LIFECYCLE.md`](docs/EDGE_REWORK_LIFECYCLE.md)와 [`docs/GITHUB_COMPLETION_LIFECYCLE.md`](docs/GITHUB_COMPLETION_LIFECYCLE.md)가 정본입니다.

### 3. 내부 완료는 provisional일 수 있음

GitHub-backed task가 내부 Kanban에서 완료되었더라도 그것만으로 PR merge를 주장하지 않습니다. completion observer는 edge reconciliation을 깨우는 역할만 하며, authoritative completion은 fresh GitHub evidence를 확인한 edge가 확정합니다.

## 이 저장소에서 볼 수 있는 엔지니어링 포인트

- **Event-driven integration** — signed GitHub webhook intake와 repository-scoped wake
- **Idempotency / replay safety** — `X-GitHub-Delivery` dedupe와 bounded retry
- **Explicit ownership boundaries** — router, n8n, Hermes job, edge reconciler의 역할 분리
- **Defensive state reconciliation** — 현재 외부 상태 재조회 후 projection
- **Least-privilege automation** — 고정된 actuator/credential 경계와 loopback-only internal services
- **Failure isolation** — ambiguous failure를 merge/completion evidence로 승격하지 않음
- **Regression-oriented development** — incident/rework 조건을 재현하는 repository-local tests
- **Operations discipline** — repository validation과 live host evidence를 구분

## 보안 경계

현재 설계는 다음을 기본 경계로 둡니다.

- GitHub ingress에서 HMAC signature와 delivery identity를 검증합니다.
- n8n, lease controller, actuator 같은 내부 경로는 public lifecycle API로 사용하지 않습니다.
- tracked n8n workflow에는 실제 credential value를 넣지 않고 import 시 protected credential을 결합합니다.
- runtime secret은 repository 밖의 protected state에 보관하도록 구성되어 있습니다.
- edge actuator는 임의 shell command surface가 아니라 정해진 reconciliation 경로만 호출합니다.

공개 저장소 전환 전에는 현재 tree뿐 아니라 **전체 Git history와 Issue/PR discussion까지 별도 secret/privacy audit**해야 합니다. 절차는 [`docs/PUBLIC_RELEASE_CHECKLIST.md`](docs/PUBLIC_RELEASE_CHECKLIST.md)를 참고합니다.

## 어디부터 읽으면 되나

| 관심사 | 문서 / 코드 |
| --- | --- |
| 전체 구조 | [`README.md`](README.md) |
| GitHub event / concurrency | [`docs/GITHUB_EVENT_CONCURRENCY.md`](docs/GITHUB_EVENT_CONCURRENCY.md) |
| PR rework lifecycle | [`docs/EDGE_REWORK_LIFECYCLE.md`](docs/EDGE_REWORK_LIFECYCLE.md) |
| completion / review projection | [`docs/GITHUB_COMPLETION_LIFECYCLE.md`](docs/GITHUB_COMPLETION_LIFECYCLE.md) |
| repository discovery / authority | [`docs/REPOSITORY_REGISTRY.md`](docs/REPOSITORY_REGISTRY.md) |
| host deployment / recovery | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| 핵심 edge state machine | [`edge/kanban-github-sync.py`](edge/kanban-github-sync.py) |
| n8n workflow | [`automation/n8n/workflows/github-pr-edge-sync.json`](automation/n8n/workflows/github-pr-edge-sync.json) |
| GitHub router | [`automation/n8n/github-router/router.py`](automation/n8n/github-router/router.py) |
| 테스트 | [`tests/`](tests/) 및 [`edge/`](edge/)의 focused regression tests |

## 범위

이 프로젝트는 범용 n8n template이나 Hermes 대체 구현을 목표로 하지 않습니다. H4V3/Hermes 환경의 GitHub-backed 작업을 안전하게 연결하기 위한 외부 control plane이며, Hermes core 수정·두 번째 task store·두 번째 lifecycle owner·자동 merge는 의도적인 비범위입니다.
