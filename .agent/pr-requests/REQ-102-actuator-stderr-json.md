# REQ-102: actuator stderr / JSON protocol isolation hotfix

- Status: Ready
- Project: hermes-n8n-control-plane
- Product type: CONTROL_PLANE_AUTOMATION
- Source issue: rhgo1749/hermes-n8n-control-plane#102
- Required work branch: `hotfix/102-actuator-stderr-json`
- Source-of-truth base: `origin/main` @ `7e5b3f06ebbafa60c6b00bdf77924b5c4cfe46c7`
- Validation profiles: STATIC_UNIT / N8N_VALIDATE / HOST_NETWORKING
- Kanban task ID: none — direct user-authorized hotfix
- Automation stop state: NONE
- Merge authority: user explicitly authorized merge and host deployment for this hotfix

## Objective

GitHub PR edge-sync actuator가 child stderr diagnostic을 stdout JSON protocol과 합쳐 파싱하는 결함을 제거해, harmless runtime warning이 있어도 valid edge JSON 결과를 정상 처리한다.

## Confirmed background

- Production n8n `Run edge sync actuator` execution이 HTTP 502로 반복 실패했다.
- 동일 edge command는 rc=0, stdout valid JSON, stderr에는 SQLite WAL-reset warning만 출력한다.
- actuator는 현재 `stdout=PIPE, stderr=STDOUT` 뒤 합쳐진 bytes 전체를 `json.loads()`한다.
- actuator와 동일한 merged-stream 조건에서 `JSONDecodeError`가 재현됐다.
- #101 edge source/live hashes는 동일하며 해당 lifecycle invariant 자체는 정상 배포됐다.

## Canonical route

- `AGENTS.md`
- `.agent/REQ_REQUEST_TEMPLATE.md`
- `docs/OPERATIONS.md`
- `docs/GITHUB_EVENT_CONCURRENCY.md`
- `automation/hermes/actuator/github_intake_actuator.py`
- `tests/test_github_intake_actuator.py`
- `automation/n8n/scripts/install-intake-actuator.sh`

## Contract

- stdout만 machine-readable JSON protocol로 파싱한다.
- stderr는 diagnostic channel로 분리하되 stdout/stderr를 동시에 drain해 child pipe deadlock을 만들지 않는다.
- 기존 edge timeout, combined output budget, `shell=False`, fixed argv, credential boundary, rework dispatch env 계약을 유지한다.
- stderr warning + valid stdout JSON이 성공하는 회귀 테스트를 추가한다.
- lifecycle semantics, GitHub/Kanban ownership, n8n lifecycle ownership은 변경하지 않는다.

## Non-goals

- SQLite warning 자체를 숨기거나 Hermes/SQLite runtime을 이 hotfix에서 업그레이드
- edge lifecycle/state-machine 변경
- n8n workflow 구조 변경
- GitHub Actions 활성화
- unrelated cleanup

## Validation

- `python3 -m py_compile automation/hermes/actuator/github_intake_actuator.py tests/test_github_intake_actuator.py`
- focused actuator tests including stderr-warning regression
- `python3 automation/n8n/scripts/validate.py`
- `git diff --check`
- merge 후 canonical actuator install/deploy contract로 host 반영
- `:5681`, `:5682`, `:5678` health 재확인
- authenticated `:5682/v1/edge-sync` bounded invocation이 200 + valid JSON인지 확인
- n8n published workflow의 `:5682/v1/edge-sync` 연결 및 실제 event-path execution 재검증
