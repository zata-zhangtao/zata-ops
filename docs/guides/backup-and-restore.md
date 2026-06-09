# 备份与恢复

## Backup

`zata-ops db backup` 完整流程：

1. 读取项目 `.env` / `.env.local`（CLI flag 优先）。
2. 根据当天 weekday 与 `FULL_BACKUP_DAY` 决定 backup type，`--force-full`
   可强制 full。
3. `pg_dump` / `sqlite3 .dump` → 压缩为 `database.sql.gz`。
4. GNU tar `--listed-incremental` 打包 `LOGS_DIR` 与 `RESOURCES_DIR`。
5. 上传 S3：`<S3_PREFIX>/<YYYY-MM-DD_HHMMSS>/<full|incremental>/<file>`。
6. 写 `manifest.json` 描述本次备份的所有 file + SHA-256 + S3 key + type。
7. 按 `RETENTION_DAYS` 清理 S3 中过期备份目录。

`--dry-run` 在 `_run_backup` 触发任何 IO 之前打印计划，并 mask `DATABASE_URL`
中的密码。CI 推荐用 `--dry-run` 走真实入口验证。

## Restore

```bash
zata-ops db list
zata-ops db restore --from 2026-06-07_180000 \
    --restore-db --restore-logs --restore-resources --yes
```

可选行为：

- `--chain`：对 logs/resources 重放 full + 所有 incremental，确保增量删除
  / 修改全部生效。
- `--clean-target-schema` / `--drop-target-db`：危险操作，需显式确认；
  互斥，不能同时启用。
- `--sanitize-invalid-utf8`：对脏 dump 用 Unicode replacement 字符替换非法
  UTF-8 字节后再 pipe 到 `psql`。
- `--verify-table foo`：restore 后校验 `foo` 表存在且行数 > 0。

## Manifest 兼容性

`zata-ops` 保留了旧 `backup_service` 的 manifest shape（`timestamp`、
`type`、`files[]` 中的 `name`/`size`/`s3_key`/`sha256`），并在原有字段上
追加了 `project` 字段。旧 manifest 可被 `zata-ops db restore` 直接消费。
