# Operations runbook

## Confirmed migration inventory

The following comes from the live Hermes cron stores audited on 2026-08-11. Only jobs that were active are part of the cutover; existing paused jobs remain untouched.

| Profile | Hermes job ID | Existing script | Existing schedule | n8n workflow | Decision |
|---|---|---|---|---|---|
| `default` | `168bd63461e7` | `daily_session_cleanup.sh` | `0 9 * * *` | `schedule-daily-session-cleanup.json` | migrate |
| `default` | `e432a90c1361` | `cleanup-stale-feature-repos.py` | `0 9 * * *` | `schedule-cleanup-stale-feature-repos.json` | migrate |
| `default` | `df360bfa297d` | `repo-fetch-check.sh` | `0 9 * * *` | `schedule-repo-fetch-check.json` | migrate |
| `default` | `bf431b2a6ba6` | `github-agent-ready-kanban-intake.py` | `*/5 * * * *` | `schedule-github-agent-ready-intake.json` | migrate |
| `dj-broadcast` | `27f6725028ff` | `h4v3-broadcast-monitor.sh` | every 5 minutes | `schedule-h4v3-broadcast-health.json` | migrate |
| `dj-broadcast` | `8b564532d38b` | `h4v3-music-generator.py` | hourly | — | keep paused |
| `dj-broadcast` | `6c7e6c7a9fd1` | `hermes-dj-stream-supervisor.py` | every minute | — | keep paused |
| `eval`, `kanban-main` | — | — | — | — | no jobs |

The default gateway has `gateway.multiplex_profiles=true`. Direct evidence showed fresh ticker heartbeats and successful scheduled runs for the `dj-broadcast` profile, so the same existing ticker path owns the active DJ monitor.

## Why the adapter is a user plugin

The existing Hermes dashboard already exposes:

- `POST /api/cron/jobs/{job_id}/trigger?profile=...`
- `POST /api/cron/jobs/{job_id}/pause?profile=...`

and routes calls into the existing profile-aware cron store. `trigger_job()` schedules the job for the next existing ticker cycle. The route normally requires an interactive dashboard session; it has no reusable long-lived machine token route. The `hermes-n8n-cron-auth` **user plugin** therefore registers exactly ten existing paths (trigger + pause for the five migrated jobs) with Hermes' pre-existing token-auth seam.

It does not add an API endpoint or touch Hermes core. Its bearer token cannot list, create, edit, delete, or trigger any other cron job. It reads a root/plugin-owned mode-`0600` token file, rejects broad permissions or weak tokens, and compares with `hmac.compare_digest`. Because Hermes' generic token seam otherwise tries every service-token provider on an opted-in route, this plugin also refuses to register when another non-interactive dashboard token provider is already present. Do not enable a second service-token plugin alongside this migration without a separate security review.

The upstream token seam matches a path and does not pass the `profile` query to the provider. Before installing the plugin, `configure-hermes-service-auth.sh` therefore fails closed unless every allowlisted job ID occurs exactly once in its intended profile. Re-run that installer/check after any manual profile or cron-inventory change. Formal per-request query scoping would require an upstream/core or new adapter interface and is intentionally out of scope.

Each n8n workflow follows this intentionally small sequence:

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

## 4. Render and import workflows

The tracked JSON templates are inactive and use a URL placeholder. Render/import them for the dashboard's current bind address:

```bash
automation/n8n/scripts/import-workflows.sh \
  --dashboard-url http://100.107.12.90:9119
```

`100.107.12.90:9119` is the observed current Tailnet dashboard bind. If it changes, pass the actual current dashboard address instead. The renderer permits plain `http` only for loopback, private, or Tailnet CGNAT IPs; use `https` for a hostname or public target. The n8n container communicates through that private Tailnet endpoint; it does not receive the Hermes home, Docker socket, or a shell capability.

## 5. Canary and cutover gate

For **each Schedule Trigger workflow**, run it manually at a time that is not its regular schedule. Wait for the 75-second pause node to finish. Any trigger or pause HTTP error means the outcome is not approved: leave the workflow inactive, restore the legacy job, resolve the error, and re-run the canary. Verify the corresponding Hermes job has all of:

- `last_status=ok`;
- a fresh `last_run_at`;
- `enabled=false` and `state=paused` after the workflow completes.

Then restore it immediately before testing the next workflow:

```bash
HERMES_HOME="$HOME/.hermes" hermes -p default cron resume <job-id>
# use -p dj-broadcast for 27f6725028ff
```

After all five canaries pass and source jobs are restored active, **leave the five Schedule Trigger workflows inactive** and first pause the legacy schedules:

```bash
automation/n8n/scripts/cutover.sh --confirm-n8n-verified \
  --hermes-home "$HOME/.hermes"
```

The cutover script writes a mode-`0600` snapshot under ignored `state/cutover/`, pauses only the five listed legacy jobs, and restores the complete pre-cutover snapshot if a pause or post-pause verification fails. It does not delete jobs, source scripts, n8n data, or Kanban records. Only after it succeeds, activate the verified Schedule Trigger workflows in n8n. This order prevents a legacy scheduler and its corresponding n8n Schedule Trigger from firing the same job concurrently. If activation fails, leave n8n inactive and run the rollback procedure below.

## 6. Rollback

Rollback is intentionally gated to avoid dual scheduling. While n8n is still running, deactivate **every migration workflow** (the five Schedule workflows and any GitHub workflows later enabled) in the n8n UI and verify that their inactive state persists. Then run:

```bash
automation/n8n/scripts/cutover.sh rollback \
  --confirm-n8n-workflows-deactivated \
  --hermes-home "$HOME/.hermes"
```

It stops n8n without removing its volume or data, then restores the mode-`0600` pre-cutover snapshot and verifies the captured Hermes enabled states. It does not touch the two pre-existing paused DJ jobs. Do not run `docker compose up` after rollback unless the migration workflows remain persistently inactive.

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
