# zata-ops

Shared operations toolkit CLI for Zata downstream projects. Provides database
backup/restore, S3 connectivity checks, VPS environment provisioning, log
inspection, and a terminal status dashboard.

## What it does

Previously every project copied `scripts/backup_service/` and
`deploy/vps-traefik/` from `zata_code_template`, which meant N projects each
maintained an identical copy of the backup logic and fixes had to be hand-rolled
into every downstream.

`zata-ops` lives in one place. Install it once and call it from your project's
`justfile` recipes.

## Install

Requires Python >= 3.11 and `uv`.

```bash
uv tool install --force /path/to/zata-ops
zata-ops --version
```

For SSH-based VPS commands (`env provision`, `env fix`), install with the
optional `ssh` extra:

```bash
uv tool install --force '/path/to/zata-ops[ssh]'
```

## Usage

All commands read project-local `.env` and `.env.local` files; CLI flags always
override. Use `--dry-run` wherever offered to preview the plan without touching
the network.

### Database

```bash
# Backup — loads DATABASE_URL / S3_* from .env
zata-ops db backup --dry-run
zata-ops db backup --force-full

# List and restore
zata-ops db list
zata-ops db restore --from 2026-06-07_180000 --restore-db --yes

# Migrate data between two PostgreSQL databases
zata-ops db migrate --source postgresql://... --target postgresql://... --dry-run

# Verify S3 connectivity
zata-ops db check
```

### VPS environment

```bash
# Provision a fresh VPS with Docker + Traefik
zata-ops env provision --host example.com --user deploy --dry-run

# Fix Traefik ACME email and force certificate re-issuance
zata-ops env fix --host example.com --email ops@example.com --dry-run
```

### Logs and dashboard

```bash
# Stream container or systemd logs
zata-ops logs tail --project my-app --since 1h --dry-run

# Search logs for a pattern
zata-ops logs search "ERROR" --project my-app --dry-run

# Terminal status dashboard
zata-ops dashboard --mock --project my-app
```

## Commands

| Command | Purpose |
|---|---|
| `db backup` | Dump DB + logs/resources to S3 with retention pruning. |
| `db restore` | Restore DB/logs/resources from S3, with chain support. |
| `db list` | List available backup dates and types. |
| `db check` | Verify S3 endpoint connectivity. |
| `db migrate` | Copy data between two PostgreSQL DBs (not Alembic). |
| `env provision` | Render and (optionally) run the VPS bootstrap script over SSH. |
| `env fix` | Repair Traefik ACME email and force certificate re-issuance. |
| `logs tail` | Stream container or systemd logs. |
| `logs search` | grep recent logs for a pattern. |
| `dashboard` | Render a terminal status snapshot. |

Run `zata-ops <command> --help` for full flags.

## Configuration

`zata-ops` reads settings from `.env` / `.env.local` in the working directory.

| Variable | Default | Used by |
|---|---|---|
| `PROJECT` | `app` | All commands |
| `DATABASE_URL` | — | `db backup`, `db restore`, `db migrate` |
| `S3_ENDPOINT` | — | `db *` |
| `S3_ACCESS_KEY` | — | `db *` |
| `S3_SECRET_KEY` | — | `db *` |
| `S3_BUCKET` | `app-backups` | `db *` |
| `S3_PREFIX` | `app-backups` | `db *` |
| `S3_ADDRESSING_STYLE` | `path` | `db *` |
| `RETENTION_DAYS` | `30` | `db backup` |
| `FULL_BACKUP_DAY` | `6` (Sunday) | `db backup` |
| `LOGS_DIR` | `/app/backend/logs` | `db backup`, `db restore` |
| `RESOURCES_DIR` | `/app/backend/data` | `db backup`, `db restore` |
| `WORK_DIR` | `/tmp/backups` | `db backup`, `db restore` |

See `.env.example` for a full template.

## Scheduling backups

`zata-ops` only offers an on-demand `db backup` command. Wire it to whichever
scheduler your infra already uses:

- **systemd timer** on a deploy box.
- **cron** (`@daily zata-ops db backup`).
- **GitHub Actions** `schedule:` workflow.
- **Dokploy scheduled job** that shells out to `zata-ops`.
- **Manual** invocation before maintenance windows.

## Development

```bash
uv sync
uv run pytest          # 23 fast tests
uv run zata-ops --help # exercise CLI without installing
```
