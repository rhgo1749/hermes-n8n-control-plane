# Repository-owned REQ requests

이 디렉터리는 Issue-driven 구현에서 실제로 사용하는 축약된 REQ 요청서를 보관한다.

- 템플릿: `.agent/REQ_REQUEST_TEMPLATE.md`
- 실제 요청서 이름: `REQ-NNN-<slug>.md`
- `NNN`은 항상 Source GitHub Issue 번호이며 GitHub Pull Request 번호가 아니다.
- 실제 GitHub Pull Request는 `PR #<github-pr-number>`로 표기한다.
- 요청서에는 Source Issue, Kanban task, idempotency provenance, scope/non-goals, validation contract, stop state를 기록한다.
- 임시 대화 내용을 그대로 복사하지 말고, 최신 저장소/Issue/runtime evidence로 복구 가능한 사실만 남긴다.
- 요청서가 durable product/architecture 문서를 대체하지 않는다. 장기 계약은 해당 `docs/` canonical 문서가 계속 소유한다.
- 새 `PR-NNN-*` 요청 식별자/파일은 생성하지 않는다.
- GitHub PR merge 권한은 human/user에게 있다.

역사적 `PR-NNN-*` 파일을 `REQ-NNN-*`로 rename할 때 본문 안의 과거 artifact path, 이전 Git/PR evidence는 자동 재작성하지 않는다.
