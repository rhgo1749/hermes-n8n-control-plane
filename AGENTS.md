# Root agent policy

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
