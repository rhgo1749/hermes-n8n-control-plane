# Public release checklist

이 문서는 `hermes-n8n-control-plane`을 private에서 public으로 전환하기 전에 수행할 **security/privacy release gate**입니다.

현재 working tree가 깨끗해 보여도 과거 commit, Issue/PR 본문과 댓글, 첨부 로그에는 이미 삭제된 값이 남아 있을 수 있습니다. 따라서 아래 항목을 모두 확인하기 전에는 저장소 visibility 변경을 완료 조건으로 보지 않습니다.

## 1. 현재 tracked tree

다음을 추적하고 있지 않은지 확인합니다.

- 실제 `.env` 또는 local override
- API token, bearer token, webhook secret, encryption key
- SSH/private key, signing key, keystore
- n8n runtime database/state
- SQLite/Kanban database copy
- application/container log dump
- host-only credential or curl config
- 개인 사용자명이나 불필요한 absolute home path
- 실제 private hostname/IP가 들어간 operator scratch document

기본 확인 예시:

```bash
git status --short
git ls-files | sort
```

`.env.example`이나 workflow template은 **placeholder만** 포함해야 합니다. 파일명이 예제라고 해서 내용까지 안전하다고 가정하지 않습니다.

## 2. 전체 Git history secret scan

현재 checkout만 grep해서 끝내지 않습니다. 전체 Git history를 대상으로 secret scanner를 실행합니다.

Gitleaks가 설치되어 있다면 repository root에서:

```bash
gitleaks git --redact
```

scanner가 실제 credential 가능성을 보고하면:

1. 먼저 해당 credential을 revoke/rotate 합니다.
2. 현재 tree에서 제거합니다.
3. 필요하면 Git history rewrite를 별도 검토합니다.
4. rewrite 후 다시 전체 history scan을 실행합니다.

탐지 결과를 단순히 `allowlist` 처리하기 전에 test fixture인지 실제 credential인지 사람이 확인합니다.

## 3. GitHub metadata / discussion privacy audit

Git history 밖의 GitHub 데이터도 공개됩니다. 최소한 다음을 검색합니다.

- Issues
- PR title/body
- Issue/PR comments
- Review comments
- commit messages

검색 후보:

```text
/home/<username>
Users/<username>
@company-or-personal-domain
password
secret
token
Authorization
Bearer
api_key
private key
.ts.net
10.x.x.x / 192.168.x.x / 172.16-31.x.x
```

단어가 발견됐다는 이유만으로 곧바로 삭제하지는 않습니다. 예를 들어 `token`이라는 변수명이나 placeholder는 정상일 수 있습니다. 실제 값, 개인 식별자, private infrastructure detail인지 문맥을 확인합니다.

## 4. n8n-specific 확인

- tracked workflow JSON의 credential ID/value가 placeholder인지 확인
- exported credential object가 Git에 들어오지 않았는지 확인
- `N8N_ENCRYPTION_KEY`가 비어 있는 example/config template인지 확인
- production execution export 또는 n8n database가 추적되지 않았는지 확인
- webhook sample payload에 실제 사용자/organization/private repository payload가 들어 있지 않은지 확인
- public ingress 예시는 placeholder host만 사용하고 실제 private host를 박지 않음

## 5. Runtime / log hygiene

Runtime에서 생성되는 파일은 Git에 들어오지 않아야 합니다.

예:

- `*.log`
- `*.db`, `*.sqlite`, `*.sqlite3`
- n8n local data
- router/lease state
- generated secret files
- temporary credential/canary artifacts

`.gitignore`는 사고 예방 장치일 뿐 이미 tracked된 파일을 보호해주지 않습니다. 항상 `git ls-files`도 같이 확인합니다.

## 6. 공개 문서의 정보 밀도

credential이 아니더라도 다음 값은 외부 독자에게 필요하지 않으면 일반화합니다.

- 개인 OS username이 포함된 home path
- 실제 private hostnames/IPs
- unrelated local service names
- 일회성 PID/process snapshot
- temporary Kanban task IDs
- 운영상 필요하지 않은 내부 job identifiers

단, canonical architecture나 재현 가능한 contract에 필요한 identifier를 무조건 지우지는 않습니다. 개인정보/보안과 운영 정본의 정확성을 분리해서 판단합니다.

## 7. 공개 직전 최종 gate

- [ ] current tracked tree review 완료
- [ ] full Git history secret scan 완료
- [ ] Issue/PR/comment privacy search 완료
- [ ] `.env.example` 및 tracked workflow credential placeholder 확인
- [ ] runtime logs/DB/state 미추적 확인
- [ ] 공개 문서에서 개인 username/private host 제거
- [ ] 외부 독자용 [`PROJECT_OVERVIEW.md`](../PROJECT_OVERVIEW.md) 링크 및 canonical docs 링크 확인
- [ ] 발견된 실제 credential이 있었다면 revoke/rotate가 **먼저** 완료됨
- [ ] repository를 public으로 바꾼 뒤 anonymous/public view에서 README, files, Issues, PRs를 다시 확인

이 체크리스트 통과는 특정 시점의 release gate입니다. 이후 새 운영 로그나 incident evidence가 추가되면 다시 확인해야 합니다.
