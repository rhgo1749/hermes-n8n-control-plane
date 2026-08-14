# Root agent policy

## Issue-driven REQ request routing

Issue-driven implementation must use the repository-owned request contract at `.agent/REQ_REQUEST_TEMPLATE.md`.

- Start from the current GitHub Issue and latest fetched `origin/main`.
- Read `README.md`, then the canonical document for the actual change surface, then target source/tests.
- Create a **task-specific, shortened request** at `.agent/pr-requests/REQ-NNN-<slug>.md`; do not copy the full template verbatim.
- `REQ-NNN` uses the Source GitHub Issue number. It is not the GitHub Pull Request number.
- Refer to actual GitHub Pull Requests as `PR #<github-pr-number>` and refer to work primarily by `Issue #<issue-number>`.
- Record Source Issue, Kanban task/idempotency provenance when available, scope/non-goals, validation profiles, and the final automation stop state.
- Treat GitHub, Hermes Kanban, edge reconciliation, n8n, H4V3 Overview, and Telegram according to the ownership boundaries defined by the request template and repository docs.
- PRs may be created or updated by agents, but merge/auto-merge remains human/user authority unless the user explicitly authorizes a merge.

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
