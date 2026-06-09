# zata-ops

Shared operations toolkit CLI for Zata downstream projects. Provides database
backup/restore, S3 connectivity checks, VPS environment provisioning, log
inspection, and a terminal status dashboard — packaged as a single
`zata-ops` executable installable via `uv tool install`.

## Why

Previously every project copied `scripts/backup_service/` and
`deploy/vps-traefik/` from `zata_code_template`, which meant:

- N projects each maintained an identical copy of the backup logic.
- Fixes had to be hand-rolled back into every downstream project.
- The application template carried infrastructure responsibilities.

`zata-ops` lives in one place. Downstream projects install it once and call
it from their own `justfile` recipes.

## Install

```bash
cd /path/to/zata-ops
uv tool install --force .
zata-ops --version
```

You can also point `uv tool install` at the directory directly:

```bash
uv tool install --force /path/to/zata-ops
```

## Quick start

All commands respect project-local `.env` and `.env.local` files; CLI flags
override individual values. Use `--dry-run` wherever offered to preview the
plan without touching the network.

```bash
# Database backup (loads DATABASE_URL / S3_* from .env)
zata-ops db backup --dry-run
zata-ops db backup --force-full

# Restore
zata-ops db list
zata-ops db restore --from 2026-06-07_180000 --restore-db --yes

# S3 connectivity check
zata-ops db check

# VPS provisioning
zata-ops env provision --host example.com --user deploy --dry-run
zata-ops env fix --host example.com --email ops@example.com --dry-run

# Log inspection
zata-ops logs tail --project my-app --since 1h --dry-run
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

Run `zata-ops <command> --help` for full flag listings.

## Configuration

`zata-ops` loads `OpsSettings` from `.env` / `.env.local` in the current
working directory. Supported keys:

| Key | Default | Used by |
|---|---|---|
| `PROJECT` | `app` | All commands (label / container prefix) |
| `DATABASE_URL` | _empty_ | `db backup` / `db restore` / `db migrate` |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_PREFIX`, `S3_ADDRESSING_STYLE` | various | `db *` |
| `RETENTION_DAYS` | `30` | `db backup` |
| `FULL_BACKUP_DAY` | `6` (Sunday) | `db backup` |
| `LOGS_DIR`, `RESOURCES_DIR`, `WORK_DIR` | `/app/...` | `db backup` / `db restore` |

The legacy `BACKUP_TIME` key is still read for backwards compatibility but no
longer scheduled by the tool — see "Scheduling backups" below.

## Scheduling backups

The previous compose-embedded scheduler has been removed. `zata-ops` only
offers an on-demand `zata-ops db backup` command; the scheduling itself
should be wired to one of:

- **systemd timer** on a deploy box.
- **cron** (`@daily zata-ops db backup`).
- **GitHub Actions** `schedule:` workflow that runs `zata-ops db backup`.
- **Dokploy scheduled job** that shells out to `zata-ops`.
- **Manual** `zata-ops db backup` invocation before maintenance windows.

Pick whichever scheduler your infra already exposes.

## Architecture

`zata-ops` is its own repository. It does NOT import any code from
`zata_code_template`. Downstream projects reference it only through
`just ops-*` recipes and documentation pointers.

```
src/zata_ops/
├── cli.py              Typer root entry point
├── config.py           pydantic-settings OpsSettings loader
├── db/
│   ├── _backup_impl.py Pure backup workflow
│   ├── _restore_impl.py Pure restore workflow
│   ├── _database.py    pg_dump / psql / sqlite3 wrappers
│   ├── _archive.py     GNU tar incremental archive helpers
│   ├── _s3.py          boto3 S3 client + manifest helpers
│   └── cli.py          Typer commands
├── env/
│   ├── templates/      Shell scripts (install-docker-traefik.sh, ...)
│   ├── _runner.py      Template loader + SSH command planner
│   └── cli.py          Typer commands
├── logs/cli.py         docker logs / journalctl wrappers
└── observability/cli.py Rich-rendered dashboard
```

## Development

```bash
uv sync
uv run pytest          # 23 fast tests
uv run zata-ops --help # exercise CLI without installing
```
