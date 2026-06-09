# 调度示例

`zata-ops` 不再附带常驻 scheduler。请在你的基础设施上选用以下任一方案。

## systemd timer

`/etc/systemd/system/zata-ops-backup.service`:

```ini
[Unit]
Description=zata-ops daily backup
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/apps/my-app
EnvironmentFile=/opt/apps/my-app/.env
ExecStart=/usr/local/bin/zata-ops db backup
```

`/etc/systemd/system/zata-ops-backup.timer`:

```ini
[Unit]
Description=Run zata-ops backup daily at 18:00 UTC

[Timer]
OnCalendar=*-*-* 18:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

启用：

```bash
systemctl daemon-reload
systemctl enable --now zata-ops-backup.timer
```

## cron

```cron
0 18 * * * cd /opt/apps/my-app && /usr/local/bin/zata-ops db backup >> /var/log/zata-ops-backup.log 2>&1
```

## GitHub Actions

```yaml
on:
  schedule:
    - cron: "0 18 * * *"
  workflow_dispatch:

jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv tool install --force /path/to/zata-ops
      - run: zata-ops db backup
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          S3_ENDPOINT:  ${{ secrets.S3_ENDPOINT }}
          S3_ACCESS_KEY: ${{ secrets.S3_ACCESS_KEY }}
          S3_SECRET_KEY: ${{ secrets.S3_SECRET_KEY }}
          S3_BUCKET:     ${{ secrets.S3_BUCKET }}
```

## Dokploy scheduled job

在 Dokploy 中创建 Scheduled Job，命令：

```bash
docker run --rm \
  -e DATABASE_URL -e S3_ENDPOINT -e S3_ACCESS_KEY -e S3_SECRET_KEY \
  -e S3_BUCKET -e S3_PREFIX -e RETENTION_DAYS \
  zata-ops:latest db backup
```

## 手动

`zata-ops db backup` 也可以在维护窗口前手工运行。
