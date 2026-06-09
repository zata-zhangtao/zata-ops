# 快速开始

## 1. 安装

```bash
cd /path/to/zata-ops
uv tool install --force .
zata-ops --version
```

## 2. 配置项目 `.env`

`zata-ops` 在 CWD 中加载 `.env` / `.env.local`：

```env
PROJECT=my-app
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/my_app
S3_ENDPOINT=https://s3.us-east-005.backblazeb2.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=my-app-backups
S3_PREFIX=my-app-backups
S3_ADDRESSING_STYLE=path
RETENTION_DAYS=30
FULL_BACKUP_DAY=6
LOGS_DIR=/var/log/my-app
RESOURCES_DIR=/var/data/my-app
WORK_DIR=/tmp/backups
```

## 3. Dry run

```bash
zata-ops db backup --dry-run
```

输出 JSON 描述了将上传的 S3 keys、retention 计划与目标 bucket，不会发起
任何网络调用。

## 4. 执行一次真正的备份

```bash
zata-ops db backup --force-full
zata-ops db list
```

## 5. 从模板的 justfile 委托

`zata_code_template` 派生项目已附带 `just ops-backup`、`just ops-restore`
等 recipe，会把当前项目 `.env` 透传给 `zata-ops`。详见模板仓库的
`docs/guides/backup.md`。
