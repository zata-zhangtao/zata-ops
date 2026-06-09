# zata-ops

Shared operations toolkit CLI for Zata downstream projects.

`zata-ops` provides:

- `db backup` / `db restore` / `db list` — backups to any S3-compatible store.
- `db check` — S3 connectivity diagnostic.
- `db migrate` — PostgreSQL data migration (not Alembic).
- `env provision` / `env fix` — VPS + Traefik bootstrap and ACME repair.
- `logs tail` / `logs search` — Docker and systemd log inspection.
- `dashboard` — terminal status snapshot.

Install:

```bash
cd /path/to/zata-ops
uv tool install --force .
zata-ops --version
```

Read the [Backup & Restore guide](guides/backup-and-restore.md), the
[Scheduling examples](guides/scheduling.md), or the
[VPS provisioning guide](guides/vps-provisioning.md) to get started.
