# REQ-104 — operator-attention semantic incident identity

Source Issue: rhgo1749/hermes-n8n-control-plane#104
Kanban task: t_6b2a4238
Kanban root: t_5ffdb93b (intake)
Intake idempotency key: github:rhgo1749/hermes-n8n-control-plane:issue:104
작업 브랜치: issue104-operator-attention-incident-identity
기준 브랜치: origin/main @ 76da64e9c11f365d8bb9de3010d65d0862bd70b9 (구현 전 fetch 완료)

## 범위

`github_operator_attention`의 raw event-id cursor 기반 중복 제거를 현재
canonical evidence에서 계산하는 안정적인 semantic incident identity로 교체한다.

- Edge 이벤트 키는 `attention_key = reason:incident_ref`로 유지한다.
- blocked/human-input 사건은 최신 `blocked` 이벤트의 payload kind/reason/created_at와
  task `block_kind`를 사용한다.
- rework 사건은 repository, Issue, PR, rework round, request comment identity와
  attention reason을 사용한다.
- 그 밖의 진단은 entry/rework/evidence에 있는 canonical PR identity와 reason을
  사용한다.
- provenance를 기존 `task_events` payload에 기록하고, identity가 없으면
  `incident_unresolved`로 표시하여 fail-open한다.
- Overview는 semantic identity를 해석하고, 일반 bookkeeping event churn에서는
  Need You를 잃지 않으며, semantic provenance가 없는 기존 `reason:<cursor>` row는
  legacy cursor 경로로 계속 읽는다.
- Telegram line에도 동일한 semantic `attention_key`를 포함해 event와 전달 dedupe의
  generation 규칙을 일치시킨다.

## 명시적 비목표

Hermes core, Kanban lifecycle state-machine, GitHub label/state ownership, n8n
transporter contract, Telegram credential/configuration, merge/auto-merge 동작은
변경하지 않는다. 새 notification 저장소를 만들지 않으며, raw task-event cursor를
incident identity로 사용하지 않는다.

## 검증

Focused edge/rework/blocker/notification/Overview 회귀 테스트, Python
compile/import 및 diff hygiene, static diagnostics를 실행한다. GitHub Actions가
비활성화된 저장소 정책에 따라 로컬 검증 결과와 원격 PR 상태를 분리해 보고한다.

## 중단 조건

canonical blocked/rework identity의 source-of-truth가 충돌하거나, Hermes core
수정·외부 state migration·lifecycle 소유권 변경이 필요하면 구현을 중단하고
근거를 남긴다.

## 최종 자동화 상태

로컬 검증, commit, push, PR read-back까지 완료한 뒤 구현 증거를 인계한다.
사람의 review, merge, runtime/device/manual acceptance는 이 요청의 자동화 범위
밖이다.
