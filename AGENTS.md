# Root agent policy

## Issue-driven REQ request routing

Issue-driven implementation must use the repository-owned request contract at `.agent/REQ_REQUEST_TEMPLATE.md`.

- Start from the current GitHub Issue and latest fetched `origin/main`.
- Read `README.md`, then `docs/README.md` and select the smallest canonical document for the actual change surface before reading target source/tests.
- Do not preload every document under `docs/`; expand beyond the selected route only when repository evidence shows an adjacent contract is affected.
- Create a **task-specific, shortened request** at `.agent/pr-requests/REQ-NNN-<slug>.md`; do not copy the full template verbatim.
- `REQ-NNN` uses the Source GitHub Issue number. It is not the GitHub Pull Request number.
- Refer to actual GitHub Pull Requests as `PR #<github-pr-number>` and refer to work primarily by `Issue #<issue-number>`.
- Record Source Issue, Kanban task/idempotency provenance when available, scope/non-goals, validation profiles, and the final automation stop state.
- Treat GitHub, Hermes Kanban, edge reconciliation, n8n, H4V3 Overview, and Telegram according to the ownership boundaries defined by the request template and repository docs.
- PRs may be created or updated by agents, but merge/auto-merge remains human/user authority unless the user explicitly authorizes a merge.

## Runtime namespace resolution before deployment

Before declaring a host/runtime deployment gate unavailable, establish where the worker is already running and whether the canonical target runtime is directly accessible from that namespace.

- Read the owning operations/deploy contract first; do not infer the target namespace merely from the word "Docker".
- If the current process already runs inside the target runtime namespace and the canonical runtime home is directly accessible, invoke the repository-owned install/deploy script **directly** against that runtime home. Do not try to enter the same container again with `docker exec`.
- Absence of a `docker` CLI or host Docker socket proves only that nested/host Docker control is unavailable. It does **not** prove that the worker lacks access to the runtime it is already executing inside.
- Use host-level Docker commands only when the current namespace does not already provide the required target access and the canonical operations contract explicitly requires the host as the bridge.
- A capability blocker is valid only after direct target-runtime access and any authorized canonical bridge are both unavailable. Record the concrete preflight evidence rather than guessing from one missing binary.
- When a repository-owned deployer emits an `H4V3_RUNTIME_CONTEXT ...` marker, treat that marker plus the owning operations contract as the first deployment-context evidence before choosing host/container commands.

## Existing test contract governance

- 기존 테스트가 현재 구현과 충돌한다는 이유만으로 테스트를 수정·삭제·skip·assertion 완화하지 않는다.
- 기존 테스트를 변경하려면 먼저 해당 테스트가 표현하던 계약이 현재 GitHub Issue 또는 현재 canonical contract에 의해 변경·폐기되었음을 확인한다. 계약이 그대로라면 테스트를 보존하고 구현을 수정한다.
- 기존 테스트를 수정·삭제·약화하는 PR은 변경 이유, superseding contract evidence, replacement validation을 PR/final report에 명시한다.
- 계약 변경 작업은 관련 기존 테스트의 stale 여부를 함께 조사한다. 테스트의 나이·이름·실패 자체는 stale 근거가 아니며, 현재 canonical contract와의 의미적 충돌이 있어야 stale로 판정한다.

## GitHub Actions / CI policy

GitHub-hosted Actions are intentionally disabled because this project currently operates without a GitHub Actions budget.

- Do not enable or re-enable GitHub Actions unless the user explicitly requests it.
- Do not add new GitHub-hosted CI workflows unless explicitly requested.
- Existing `.github/workflows/*` files may remain for future reuse.
- Missing or disabled GitHub Actions checks are not themselves a validation failure.
- Required validation must be executed locally in the worker environment.
- A task or PR must not be treated as implementation-complete when a required validation gate is `NOT RUN` merely because GitHub Actions are unavailable.
- If a required local validation cannot be executed, report it explicitly as a blocker or rework condition.
- Never claim PASS for a validation command that was not actually executed.
- Prefer repository-native local validation commands defined by this repository.

### Worker completion rule

- Required validation PASS = eligible for handoff/review.
- Required validation NOT RUN ≠ PASS.
- GitHub Actions disabled ≠ permission to skip local validation.
- If a worker cannot execute a required validation (missing SDK/toolchain/runtime/device, environment failure), state exactly which gate was not run and why, and do not mark implementation-critical work complete.
