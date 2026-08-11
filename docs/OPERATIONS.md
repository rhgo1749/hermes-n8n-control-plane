# Operations runbook

## Confirmed migration scope and live Hermes cron inventory

The GitHub agent-ready intake is the sole n8n migration target. The following
states were observed during the 2026-08-11 scope reduction. They are historical
migration context, not a runtime state-restoration instruction. Before any host
change, use the change owner's explicit canonical state for that maintenance
window; never infer or mutate a non-intake job from this table. Do not create
replacement jobs.

| Profile | Hermes job ID | Existing script | Existing schedule | Observed state | Decision |
|---|---|---|---|---|---|
| `default` | `168bd63461e7` | `daily_session_cleanup.sh` | `0 9 * * *` | active | keep Hermes-owned |
| `default` | `e432a90c1361` | `cleanup-stale-feature-repos.py` | `0 9 * * *` | active | keep Hermes-owned |
| `default` | `df360bfa297d` | `repo-fetch-check.sh` | `0 9 * * *` | paused | keep Hermes-owned |
| `default` | `bf431b2a6ba6` | `github-agent-ready-kanban-intake.py` | `*/5 * * * *` | active | migrate: `schedule-github-agent-ready-intake.json` |
| `dj-broadcast` | `27f6725028ff` | `h4v3-broadcast-monitor.sh` | every 5 minutes | active | keep Hermes-owned; no n8n-native redesign |
| `dj-broadcast` | `8b564532d38b` | `h4v3-music-generator.py` | hourly | paused | keep paused |
| `dj-broadcast` | `6c7e6c7a9fd1` | `hermes-dj-stream-supervisor.py` | every minute | paused | keep paused; never modify for this migration |
| `eval`, `kanban-main` | — | — | — | — | no jobs |

## Why the adapter is a user plugin

The existing Hermes dashboard already exposes:

- `POST /api/cron/jobs/{job_id}/trigger?profile=...`
- `POST /api/cron/jobs/{job_id}/pause?profile=...`

and routes calls into the existing profile-aware cron store. `trigger_job()` schedules the job for the next existing ticker cycle. The route normally requires an interactive dashboard session; it has no reusable long-lived machine token route. The `hermes-n8n-cron-auth` **user plugin** therefore registers exactly two existing paths (trigger + pause for `bf431b2a6ba6`) with Hermes' pre-existing token-auth seam.

It does not add an API endpoint or touch Hermes core. Its bearer token cannot list, create, edit, delete, or trigger any other cron job. It reads a root/plugin-owned mode-`0600` token file, rejects broad permissions or weak tokens, and compares with `hmac.compare_digest`. Because Hermes' generic token seam otherwise tries every service-token provider on an opted-in route, this plugin also refuses to register when another non-interactive dashboard token provider is already present. Do not enable a second service-token plugin alongside this migration without a separate security review.

The upstream token seam matches a path and does not pass the `profile` query to the provider. Before installing the plugin, `configure-hermes-service-auth.sh` therefore fails closed unless every allowlisted job ID occurs exactly once in its intended profile. Re-run that installer/check after any manual profile or cron-inventory change. Formal per-request query scoping would require an upstream/core or new adapter interface and is intentionally out of scope.

The retained n8n intake workflows follow this intentionally small sequence:

1. n8n Schedule Trigger (or optional GitHub Trigger) calls the existing Hermes `trigger` route with a short timeout.
2. The existing Hermes ticker claims and executes the existing no-agent script through the normal `run_one_job()` path.
3. n8n waits 75 seconds, more than the built-in 60-second ticker interval.
4. n8n calls the existing Hermes `pause` route, keeping the legacy schedule disabled until the next n8n fire.

If the trigger request returns an error or times out, n8n treats its effect as unknown and still proceeds to the compensating pause. A pause error remains a failed n8n execution. The 75-second wait is a bounded cleanup delay, **not** a Hermes completion acknowledgement; only the per-job host canary below establishes that the current ticker actually claimed and completed the job before the pause.

A separate isolated-runtime canary verified both `trigger → tick → pause` and the asynchronous ticker variant: the original no-agent script ran once and ended with `enabled=false`, `state=paused`, and `last_status=ok`.

## 1. Host prerequisites

This repository cannot install n8n from the current agent container: it has no host Docker socket, sudo, systemd, or host SSH authority. On the Ubuntu host, install Docker Engine and Docker Compose v2 from [Docker's official Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/) if they are absent.

Do **not** use a Docker socket mount for n8n. The provided Compose file is deliberately private:

- binds only `127.0.0.1:<host port>`;
- uses a named `n8n_data` volume;
- has `restart: unless-stopped`;
- asks Docker/systemd to restore Docker on boot when `--enable-docker-service` is explicitly supplied;
- disables telemetry/templates/public n8n API and workflow access to process environment;
- mounts only its own ignored `state/` directory, never the Hermes home or source trees.

## 2. Install n8n CE

From the repository root on the host:

```bash
automation/n8n/scripts/host-install.sh --enable-docker-service
```

The first boot creates a local owner account at `http://127.0.0.1:5678`. Pass `--port <private-port>` only when intentionally changing the host port; the script updates `N8N_HOST_PORT` and probes that effective value. The generated `automation/n8n/.env` and Docker volume are runtime state and are ignored by Git.

`N8N_ENCRYPTION_KEY` is generated once and must remain stable. Before changing n8n version or replacing data, export workflows and back up the n8n volume. This control plane intentionally uses the `2.32.7` tag rather than `latest`; record the pulled image digest in the host change record before an upgrade.

### Required backup before cutover or upgrade

The named volume and `.env` are a pair: the volume contains encrypted n8n credentials and `.env` contains the encryption key. On the host, create a protected backup before importing credentials, cutover, or version changes:

```bash
BACKUP_DIR="$PWD/automation/n8n/state/backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP_DIR"
cp --preserve=mode,timestamps automation/n8n/.env "$BACKUP_DIR/n8n.env"
chmod 600 "$BACKUP_DIR/n8n.env"
docker run --rm \
  -v hermes-n8n-control-plane-data:/data:ro \
  -v "$BACKUP_DIR:/backup" \
  alpine tar -C /data -czf /backup/n8n-data.tgz .
docker image inspect docker.n8n.io/n8nio/n8n:2.32.7 --format '{{index .RepoDigests 0}}'
```

`state/backups/` is ignored by Git; treat it as secret-bearing runtime data.

### Destructive recovery restore

This replaces the current n8n database and credentials. Stop and review before running it; preserve a newer backup first if one exists.

```bash
BACKUP_DIR=/absolute/path/to/selected-backup
docker compose --env-file automation/n8n/.env -f automation/n8n/compose.yaml stop n8n
docker run --rm \
  -v hermes-n8n-control-plane-data:/data \
  -v "$BACKUP_DIR:/backup:ro" \
  alpine sh -ec 'rm -rf /data/* /data/.[!.]* /data/..?*; tar -C /data -xzf /backup/n8n-data.tgz'
install -m 600 "$BACKUP_DIR/n8n.env" automation/n8n/.env
docker compose --env-file automation/n8n/.env -f automation/n8n/compose.yaml up -d
```

## 3. Enable least-privilege Hermes service auth

```bash
automation/n8n/scripts/configure-hermes-service-auth.sh \
  --hermes-home "$HOME/.hermes"
```

The script deploys the plugin to `$HERMES_HOME/plugins/hermes-n8n-cron-auth/`, enables it without tool override permission, and creates `.n8n-cron-token` with mode `0600`. It deliberately does not print the token or assume a dashboard supervisor.

Restart the existing Hermes dashboard through its existing supervisor. Then create one n8n **HTTP Header Auth** credential:

| Field | Value |
|---|---|
| Credential name | `Hermes n8n cron` |
| Header name | `Authorization` |
| Header value | `Bearer <contents of protected .n8n-cron-token>` |

Attach this credential to both HTTP Request nodes in every imported workflow. Do not put the value in JSON, `.env.example`, Git, or chat.

## 4. Render and import intake workflows

The tracked JSON templates are inactive and use a URL placeholder. Render/import them for the dashboard's current bind address:

```bash
automation/n8n/scripts/import-workflows.sh \
  --dashboard-url http://100.107.12.90:9119
```

`100.107.12.90:9119` is the observed current Tailnet dashboard bind. If it changes, pass the actual current dashboard address instead. The renderer permits plain `http` only for loopback, private, or Tailnet CGNAT IPs; use `https` for a hostname or public target. The n8n container communicates through that private Tailnet endpoint; it does not receive the Hermes home, Docker socket, or a shell capability.

Only `Hermes schedule · GitHub agent-ready Issue intake` is a retained Schedule
Trigger workflow. If n8n was previously imported from the broader template set,
deactivate or delete these obsolete Schedule workflows in the n8n UI before any
activation:

- `Hermes schedule · 매일 저가치 세션 정리`;
- `Hermes schedule · cleanup-stale-feature-repos`;
- `Hermes schedule · Repo fetch check (CtrlHangul + Re-Bound)`;
- `Hermes schedule · H4V3 Broadcast Health Monitor`.

`import-workflows.sh` removes only stale ignored generated Schedule JSON before
rendering the retained templates. It deliberately does not delete persisted n8n
workflow records, so UI cleanup remains explicit and reviewable.

## 5. Canary and cutover gate

For the sole Schedule Trigger workflow, run it manually at a time that is not
its regular schedule. Wait for the 75-second pause node to finish. Any trigger
or pause HTTP error means the outcome is not approved: leave the workflow
inactive, restore the legacy intake job, resolve the error, and re-run the
canary. Verify `bf431b2a6ba6` has all of:

- `last_status=ok`;
- a fresh `last_run_at`;
- `enabled=false` and `state=paused` after the workflow completes.

Then restore the intake immediately after the canary:

```bash
HERMES_HOME="$HOME/.hermes" hermes -p default cron resume bf431b2a6ba6
```

After the intake canary passes and the source job is restored active, **leave
the Schedule Trigger workflow inactive** and first pause only the legacy intake
schedule:

```bash
automation/n8n/scripts/cutover.sh --confirm-n8n-verified \
  --hermes-home "$HOME/.hermes"
```

The cutover script writes a mode-`0600` snapshot under ignored `state/cutover/`, pauses only `bf431b2a6ba6`, and restores the complete pre-cutover snapshot if a pause or post-pause verification fails. It does not delete jobs, source scripts, n8n data, or Kanban records. Only after it succeeds, activate the verified Schedule Trigger workflow in n8n. This order prevents the legacy intake scheduler and its corresponding n8n Schedule Trigger from firing the same job concurrently. If activation fails, leave n8n inactive and run the rollback procedure below.

## 6. Rollback

Rollback is intentionally gated to avoid dual scheduling. While n8n is still
running, deactivate every retained intake workflow (the one Schedule workflow
and any GitHub Trigger workflows later enabled) in the n8n UI and verify that
their inactive state persists. Then run:

```bash
automation/n8n/scripts/cutover.sh rollback \
  --confirm-n8n-workflows-deactivated \
  --hermes-home "$HOME/.hermes"
```

It stops n8n without removing its volume or data, then restores the mode-`0600` pre-cutover snapshot and verifies the captured Hermes enabled states. It does not touch the two pre-existing paused DJ jobs. Do not run `docker compose up` after rollback unless the migration workflows remain persistently inactive.

Snapshots from the former broader migration are deliberately rejected: rollback
requires both `latest.json` and its backup rows to name exactly
`default:bf431b2a6ba6` and the current Hermes home. Do not edit a legacy
snapshot to bypass this boundary; complete a fresh one-job cutover after the
intake canary instead.

## 7. GitHub event trigger (optional, requires ingress)

The five `github-*-intake.json` workflows use n8n's native GitHub Trigger node for `issues`, `issue_comment`, and `pull_request` events. They remain **inactive** initially because GitHub cannot reach the current Tailnet-only dashboard/n8n setup.

Before activation, provide a reviewed public HTTPS endpoint for n8n. Update the runtime `.env` with the actual public URL:

```dotenv
N8N_EDITOR_BASE_URL=https://n8n.example.com
N8N_WEBHOOK_URL=https://n8n.example.com
N8N_PROXY_HOPS=1
N8N_SECURE_COOKIE=true
```

Configure the reverse proxy to forward `X-Forwarded-For`, `X-Forwarded-Host`, and `X-Forwarded-Proto`; do not publish port 5678 directly. This follows n8n's [reverse-proxy webhook guidance](https://docs.n8n.io/hosting/configuration/configuration-examples/webhook-url/).

Attach a GitHub credential with repository-hook administration permission to each GitHub Trigger node, then activate its workflow. n8n creates and signature-verifies the GitHub webhook. The downstream action remains the existing five-minute Hermes intake script, so its current filter/idempotency/reconciliation behavior remains the safety net.

## 8. Export and verification

```bash
python3 automation/n8n/scripts/validate.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
automation/n8n/scripts/export-workflows.sh
```

`export-workflows.sh` writes only to ignored `state/exports/`. n8n workflow exports can retain credential names/IDs even though they do not contain secret values; review/redact before copying a changed export into Git.

For a live host, also retain:

- `docker compose ps` with n8n healthy;
- `curl -fsS http://127.0.0.1:<N8N_HOST_PORT-from-protected-.env>/healthz`;
- n8n execution records for each canary;
- Hermes job `last_run_at`/`last_status` evidence;
- the cutover backup path and the exact rollback command.
