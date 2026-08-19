# REQ-042: github-router X-GitHub-Delivery replay deduplication

- Status: in progress
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Source-of-truth base: fetched `origin/main` @ `ff1c4daf3bce3aa3d05458a59fb64476994bd8c6`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-042-webhook-delivery-replay-dedupe.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#42`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/42
- Kanban task ID: `t_830599ce`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:42`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-main` (bounded lead implementation; delegation unused — single-file router change)
- Automation stop state: `NONE`
- Work branch: Kanban worktree branch (Kanban task contract; PR head = this branch)

## 1. Objective

`POST /github/hermes-intake`의 서명 검증 성공 요청에 대해 `X-GitHub-Delivery` 기반
replay deduplication을 추가한다. 동일 delivery ID가 TTL 내 재전송되면 downstream
`_wake()`(lease-controller → Hermes job trigger)를 다시 호출하지 않고
성공 계열(202) no-op으로 처리한다. store는 router restart 후에도 유효할 정도로
persist되고, 크기와 TTL이 bounded이다.

## 2. Confirmed background

- Current behavior: `automation/n8n/github-router/router.py`의 `_github_event()`는
  `X-Hub-Signature-256` HMAC + constant-time 비교를 수행한다. `X-GitHub-Delivery`는
  어디에서도 읽지 않는다(테스트 `_signed_headers`만 헤더를 보낸다).
- Defect/limitation: 동일 delivery가 재전송되면 scope enqueue + `_wake()`가
  재발생한다(lease-trigger 이중 발생).
- Repository evidence: `docs/GITHUB_EVENT_CONCURRENCY.md`(event serialization
  canonical doc), `docs/OPERATIONS.md`(runbook), `tests/test_github_router.py`
  (실제 ThreadingHTTPServer + monkeypatched `_wake` harness),
  `tests/test_github_event_concurrency_contract.py`(router source marker assertions).
- Open/overlapping PR: 없음(2026-08-19 조회, open PR 0건, delivery/dedupe 키워드 0건).
- Assumptions: 실제 GitHub webhook delivery는 항상 `X-GitHub-Delivery` 헤더를
  포함한다(production impact 없음). operator가 직접 inject하는 테스트 이벤트는
  이제 매 테스트마다 신규 UUID delivery ID를 명시해야 한다.

### Control-plane ownership gate

- Ownership impact: NONE(ingress dedup만; GitHub/Kanban/edge/n8n/Overview/Telegram
  ownership 불변)
- New state/store introduced: NO(기존 router state 파일의 atomic JSON primitive를
  재사용 — 별도 store/파일/SQLite 생성 금지)
- Existing authority bypassed: NO

### Security / auth / secret / policy impact gate

- Security impact: AFFECTED(긍정 — replay 공격면 축소; signature 체계는 불변)
- Auth/permission/secret boundary: NONE(기존 HMAC/constant-time 비교 유지)
- Host/network exposure: NONE(127.0.0.1:5681 loopback 유지)
- External API/platform policy impact: NONE
- Required canonical docs/evidence: `docs/GITHUB_EVENT_CONCURRENCY.md`,
  `docs/OPERATIONS.md`, `README.md` topology, `docs/REPOSITORY_REGISTRY.md` topology
- Residual risk / owner: dedupe TTL 내 dispatch 실패(502)는 레코드를 해제(release)
  하여 GitHub의 5xx 재전송 복구 경로를 유지한다. owner: router 운영자.

## 3. In scope

1. `X-GitHub-Delivery`를 ingress metadata로 읽고, **missing/invalid delivery ID는
   fail-closed `400 invalid_delivery_id`로 거부**(dedupe 우회 불가; GitHub은 항상
   헤더를 보내므로 production 영향 없음). 형식: trim 후 `[A-Za-z0-9._:-]{1,128}`.
2. dedupe 판정은 signature 검증 성공 직후, event-type 분기 **앞**에서 수행한다.
3. store: 기존 `STATE_PATH` JSON 파일의 `delivery_dedupe` 맵
   (`{id: {created_at, expires_at}}`). 별도 파일/DB를 만들지 않는다.
   기존 `_STATE_LOCK` + atomic `os.replace` write primitive 재사용 →
   concurrent duplicate에서 단일 claim이 보장된다.
4. 이미 저장된( TTL 내) delivery ID 재도착 → `_wake()`/enqueue 없이
   `202 {"ok": true, "duplicate": true, "reason": "duplicate_delivery"}` +
   `duplicate/no-op` 로그. TTL은 refresh하지 않는다(첫 기록 기준).
5. store는 **accepted deliveries만** 기록한다: queued(202), ping(200),
   unsupported(202), repository_not_managed(202). fail-closed 계열 결과
   (`400 invalid_json`, `400 repository_missing`, `502` dispatch failure)는
   기록하지 않거나 기록을 해제하여 GitHub 5xx 재전송 복구 경로와
   재시도 가능함을 유지한다.
6. invalid/missing signature 요청은 store에 기록되지 않는다(401 기존 응답 유지).
7. TTL/cap env knobs: `GITHUB_ROUTER_DELIVERY_TTL_SECONDS`(기본 3600),
   `GITHUB_ROUTER_DELIVERY_MAX_ENTRIES`(기본 4096). 매 기록/조회 시 만료 항목
   자동 prune, cap 초과 시 가장 오래된 항목부터 evict → 무한 성장 금지.
   restart 후에도 state 파일에서 복원되어 replay protection 유지.
8. canonical 문서 갱신: `docs/GITHUB_EVENT_CONCURRENCY.md`(dedupe 섹션 + topology),
   `docs/OPERATIONS.md`(operator 테스트 이벤트가 신규 `X-GitHub-Delivery` UUID를
   포함해야 함 + dedupe knobs), `README.md` topology 1줄,
   `docs/REPOSITORY_REGISTRY.md` topology 1줄,
   `automation/n8n/compose.yaml`에 두 env knobs 명시.
9. `tests/test_github_router.py`에 회귀 테스트 추가:
   - duplicate no-op + 단일 `_wake()`
   - restart(상태 파일 유지) 후 duplicate 억제
   - TTL 만료 후 재처리(만료 항목 prune)
   - 서로 다른 delivery ID는 독립 이벤트(wake 2회)
   - invalid signature 미기록(이후 동일 ID 정상 처리)
   - missing delivery ID → `400 invalid_delivery_id`(미기록, wake 없음)
   - invalid delivery ID(공백/초과 길이/무효 문자) → `400`
   - concurrent duplicate(2스레드 동시 POST) → wake 정확히 1회
   - dispatch(502) 실패 시 기록 해제 → 재전송 재처리
   - cap 초과 시 oldest evict (bounded store)

## 4. Explicit non-goals

- `X-Hub-Signature-256` 검증 방식(constant-time, secret load) 변경 없음
- repository registry / topic discovery / repo-scoped routing / stale-pause lease
  동작 변경 없음(요구 10)
- n8n/Kanban state machine, Hermes core, edge rework/blocked/completion 변경 없음
- downstream idempotency 제거 없음(dedupe는 defense-in-depth)
- 새 상태 저장소/파일/SQLite, 별도 프로세스, GitHub-hosted Actions 없음
- merge/auto-merge

## 5. Implementation requirements

- authoritative base `origin/main` ff1c4da fetch 확인 완료.
- Kanban worktree `t_830599ce` 전용 branch에서 작업(공유 checkout 미접촉).
- secret/machine-local path 미포함. diff는 router 1파일 + 테스트 + 문서 + compose
  env 2줄 + REQ 요청서로 한정.
- `_wake`/`_enqueue_scope` 호출 계약(서버 스레드, monkeypatchable module global) 유지.

## 6. Validation contract

`STATIC_UNIT`만 선택. 실제 실행한 명령만 PASS로 기록한다:

```bash
python3 -m py_compile automation/n8n/github-router/router.py
python3 tests/test_github_router.py
python3 tests/test_github_event_concurrency_contract.py
python3 automation/n8n/scripts/validate.py
python3 tests/test_intake_lease_controller.py
python3 tests/test_repo_scoped_intake.py
python3 tests/test_repository_registry.py
python3 tests/test_repository_registry_board_workdir.py
python3 tests/test_cutover_snapshot_boundary.py
python3 tests/test_h4v3_overview.py
python3 tests/test_h4v3_notification_policy.py
git diff --check
```

- HOST_VALIDATION(실제 webhook 재전송 canary)은 operator 권한 영역 →
  OPERATIONS.md canary절차에 delivery 헤더 요건만 반영하고 실행은 HOST에 위임
  (worker stop state는 NONE — 이 PR의 correctness는 local static/unit 계약으로
  검증되며, host canary는 rollout 시 확인).
- n8n/edge/plugin/Hermes 검증 프로필: 변경 표면 무관 → NOT RUN(SKIPPED, 무관).

## 7. Host/operator acceptance handoff

PR 병합 후 서비스 재기동 시 기존 state 파일에 dedupe 맵이 자동 편입된다
(legacy 상태 파일에 `delivery_dedupe` key가 없어도 빈 맵으로 안전 시작).
canary 시 operator는 매 테스트 이벤트마다 **신규** `X-GitHub-Delivery` UUID를
보내야 하며, 동일 UUID 재전송은 202 `duplicate` no-op이 정상이다.
rollback: PR revert + 서비스 재기동만으로 원상 복구(dedupe key는 미사용 잔존).

## 8. Lead / delegation contract

단일 파일 router 변경 + 문서 갱신 — lead가 직접 구현하며 worker delegation
미사용(작업이 1 PR-sized boundary 안에 적합).

## 9. Understanding handoff

- Confirmed cause/need: ingress에 delivery 단위 replay 감지가 없음.
- Before flow: `webhook → HMAC → event-type → managed-repo → enqueue+wake`.
- After flow: `webhook → HMAC → X-GitHub-Delivery fail-closed 확인 →
  dedupe claim(store) → event-type → managed-repo → enqueue+wake → (accepted
  유지 / rejected 해제)`.
- Canonical state/source owner: router state file(`STATE_PATH`)의 `delivery_dedupe`.
- Callers/consumers: `/github/hermes-intake` ingress(GitHub + operator canary);
  `/fallback`, `/scope/claim`, `/reconcile`, `/healthz`는 dedupe 대상 아님.
- Ownership preserved/changed: 유지.
- Key design decision: (a) dedupe는 accepted deliveries만 기록 — fail-closed
  결과(400/502)는 미기록/해제하여 GitHub 5xx 재전송 복구 유지;
  (b) store는 새 파일/DB가 아니라 기존 state 파일 + 기존 lock/write primitive;
  (c) missing/invalid delivery ID는 fail-closed 400(안전한 no-route 선택지 대신
  우회 봉쇄); (d) TTL refresh 금지(첫 기록 기준).
- Rejected alternative: 별도 SQLite/파일 store — router state 파일이 이미
  atomic write + lock primitive를 갖고 있고 compose가 `./state` 볼륨을 마운트
  하므로 추가 스토어는 운영 부담만 증가(Non-goal).
- First debugging entry point: `router.py:_record_delivery` / `_github_event`
  dedupe 블록, `tests/test_github_router.py::test_duplicate_*`.

## 10. Completion criteria

Issue #42 완료 조건 9개 항목 전부가 §3 테스트 목록에 매핑되어 실행되어야 한다.
- [ ] provenance 기록(위 header)
- [ ] origin/main base + Kanban worktree branch 사용
- [ ] objective 완료, non-goals 준수
- [ ] STATIC_UNIT gate 전부 실행 + PASS 증거
- [ ] work branch push + 한국어 PR 1건 생성(merge 금지)
- [ ] 남은 HUMAN/HOST/BLOCKED 상태 명시(NONE + host canary 위임)

## 11. Final report

### Summary
- Implemented:
  - `automation/n8n/github-router/router.py`: `X-GitHub-Delivery` ingress
    validation (fail-closed `400 invalid_delivery_id`, format
    `[A-Za-z0-9._:-]{1,128}` after trim), atomic `_claim_delivery` /
    `_release_delivery` on the existing state-file `delivery_dedupe` map,
    duplicate → `202 {duplicate: true, reason: "duplicate_delivery"}` no-op
    with `duplicate/no-op` log and zero downstream dispatch, dispatch-failure
    (`502`) record release for GitHub 5xx retry / operator resend recovery,
    TTL + max-entries bounded store (TTL default 3600s, cap default 4096,
    oldest-eviction, prune on every production read path), Unicode header
    fail-closed `400 invalid_header` at the signed-ingress surface.
  - `tests/test_github_router.py`: 10 new regression tests (duplicate no-op
    single-wake, restart persistence, TTL expiry reprocess, distinct IDs
    independent, invalid signature unrecorded, missing ID fail-closed,
    invalid ID fail-closed, concurrent single dispatch, dispatch-failure
    release, bounded store) + harness `_signed_headers` unique-delivery
    default.
  - `tests/test_github_event_concurrency_contract.py`: router dedupe markers.
  - Docs: `docs/GITHUB_EVENT_CONCURRENCY.md` (new "Delivery replay
    deduplication" section + topology), `docs/OPERATIONS.md` (dedupe knobs +
    canary fresh-UUID requirement), `README.md` topology,
    `docs/REPOSITORY_REGISTRY.md` boundary note.
  - `automation/n8n/compose.yaml`: `GITHUB_ROUTER_DELIVERY_TTL_SECONDS` /
    `GITHUB_ROUTER_DELIVERY_MAX_ENTRIES` environment entries.
- Intentionally not implemented:
  - New store/file/SQLite (reused existing router state file + lock/write
    primitive — see rejected alternative in §9).
  - Signature scheme, scope queue, registry, lease, reconciliation changes
    (non-goals; requirement 10).
  - Any merge/auto-merge.

### Provenance
- Source issue: `rhgo1749/hermes-n8n-control-plane#42`
- Kanban task: `t_830599ce`
- Idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:42`
- Request path: `.agent/pr-requests/REQ-042-webhook-delivery-replay-dedupe.md`
- Lead/delegated workers: `kanban-main` lead (no delegation)
- Automation stop state: `NONE` (host canary handed to operator at rollout;
  see OPERATIONS.md §9)

### Repository findings
- Selected route: `docs/GITHUB_EVENT_CONCURRENCY.md` (event serialization
  canonical doc) + `docs/OPERATIONS.md` (runbook) +
  `automation/n8n/github-router/router.py`.
- Canonical owners: router ingress (dedupe), lease-controller (wake),
  existing Hermes job `default:bf431b2a6ba6` (execution), edge (reconciliation
  surface, unchanged).
- Request assumptions differing from repository/runtime evidence:
  - Test harness `_signed_headers` sent a fixed `"delivery-test"` ID —
    changed to unique-per-call default so existing tests remain valid under
    the new contract (behavioral note, not a contract violation).

### Cross-cutting impact
- Security: AFFECTED (positive — replay surface closed at ingress; signature
  scheme untouched).
- Auth/permission/secrets: NONE.
- Host/network exposure: NONE (loopback 5681 unchanged).
- External API/platform policy: NONE (GitHub always sends the header;
  contract documented for operator canary events).
- Follow-up / residual risk: dedupe window is a TTL — replays older than the
  TTL are re-dispatched by design (bounded memory trade-off); dispatch
  failures release the record (intentional, GitHub 5xx retry recovery).

### Files changed
- `automation/n8n/github-router/router.py`: delivery dedupe store + ingress
  validation + dispatch-failure release + Unicode header fail-closed.
- `tests/test_github_router.py`: 10 new regression tests + harness update.
- `tests/test_github_event_concurrency_contract.py`: dedupe source markers.
- `docs/GITHUB_EVENT_CONCURRENCY.md`: dedupe contract section + topology.
- `docs/OPERATIONS.md`: dedupe knobs + canary requirement.
- `README.md`: topology line.
- `docs/REPOSITORY_REGISTRY.md`: registry/ingress boundary note.
- `automation/n8n/compose.yaml`: 2 environment knobs.
- `.agent/pr-requests/REQ-042-webhook-delivery-replay-dedupe.md`: this
  request.

### Validation
| Validation | Result | Notes |
|---|---|---|
| Static/unit | PASS | see rows below |
| `python3 -m py_compile automation/n8n/github-router/router.py` | PASS | |
| `python3 tests/test_github_router.py` | PASS | 16/16 (10 new + 6 pre-existing) |
| `python3 tests/test_github_event_concurrency_contract.py` | PASS | dedupe markers |
| `python3 automation/n8n/scripts/validate.py` | PASS | n8n contract unchanged |
| `python3 tests/test_intake_lease_controller.py` | PASS | 6/6 |
| `python3 tests/test_repo_scoped_intake.py` | PASS | 23/23 |
| `python3 tests/test_repository_registry.py` | PASS | 20/20 |
| `python3 tests/test_repository_registry_board_workdir.py` | PASS | 5/5 |
| `python3 tests/test_cutover_snapshot_boundary.py` | PASS | |
| `python3 tests/test_h4v3_overview.py` | PASS | 12/12 |
| `python3 tests/test_h4v3_notification_policy.py` | PASS | 10/10 |
| `venv python3 tests/test_n8n_cron_auth_plugin.py` | PASS | 2 routes |
| `venv python3 tests/test_hermes_cron_trigger_pause.py` | PASS | trigger→tick→pause |
| `git diff --check` | PASS | |
| n8n validation | PASS | = `validate.py` row above |
| Edge rework | NOT RUN | edge surface unchanged (no edge file touched) |
| Hermes plugin | NOT RUN | plugin surface unchanged |
| Host dashboard/network | NOT RUN — HOST_VALIDATION at rollout | operator canary per OPERATIONS.md §9 with fresh delivery UUID |
| Telegram E2E | NOT RUN | no notification surface change |

### Operator acceptance
Automation stop state is `NONE`, so a dedicated operator-acceptance block is
not required. Rollout note: after service restart the existing state file
picks up the `delivery_dedupe` map automatically (legacy files without the
key start empty and safe). Canary events must use fresh delivery UUIDs.

### Remaining risks / owner
- TTL-window replay (older than TTL) re-dispatches by design — owner: router
  operator (knob: `GITHUB_ROUTER_DELIVERY_TTL_SECONDS`).
- Dispatch-failure release means a crash between claim and release cannot
  suppress a retry (fail-open for availability, fail-closed for signature).

### Git / PR
- Base SHA: `ff1c4daf3bce3aa3d05458a59fb64476994bd8c6` (origin/main at work start)
- Branch: Kanban worktree branch for task `t_830599ce`
- Commits: see git log below
- PR number/title/URL: filled at PR creation
- Working tree: clean at commit time (verified `git status`)
- Merge performed: NO
