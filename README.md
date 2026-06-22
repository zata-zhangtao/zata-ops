# zata-ops

Zata 下游项目共用的运维 CLI 工具,提供数据库备份与恢复、S3 连通性检查、VPS 环境初始化、日志查看与终端状态面板。

---

## 安装

需要 Python >= 3.11 与 `uv`。

```bash
# 基础安装
uv tool install --force /path/to/zata-ops
zata-ops --version
```

需要 SSH 远程操作 VPS(`env provision`、`env fix`)时,加上 `ssh` 扩展:

```bash
uv tool install --force '/path/to/zata-ops[ssh]'
```

`[ssh]` extra 同时也是 `tunnel` 子命令的依赖(paramiko),一并安装后
可以本地用 `tunnel open` 建立 SSH 端口转发。

升级到新版本:

```bash
uv tool install --force --reinstall /path/to/zata-ops
```

---

## 快速上手

所有命令会读取**当前工作目录**下的 `.env` 与 `.env.local`;命令行参数(`--xxx`)的优先级最高。带 `--dry-run` 的命令建议**先预览再执行**,避免误操作打到线上。

### 1. 准备 `.env`

复制本仓库或下游项目中的 `.env.example` 为 `.env`,至少填好与备份相关的字段:

```env
PROJECT=my-app
DATABASE_URL=postgresql://user:pass@host:5432/dbname
S3_ENDPOINT=https://s3.example.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=app-backups
S3_PREFIX=app-backups
S3_ADDRESSING_STYLE=path
RETENTION_DAYS=30
FULL_BACKUP_DAY=6
```

### 2. 验证 S3 连通

```bash
zata-ops db check
```

### 3. 备份数据库

```bash
# 先看会做什么
zata-ops db backup --dry-run

# 真正执行(周日全量,其余日子增量)
zata-ops db backup

# 强制本次做全量
zata-ops db backup --force-full
```

### 4. 查看与恢复

```bash
# 列出可用的备份时间点
zata-ops db list

# 恢复到指定时间点(恢复 DB,跳过确认)
zata-ops db restore --from 2026-06-07_180000 --restore-db --yes
```

---

## 命令参考

执行 `zata-ops <command> --help` 查看每个子命令的完整参数。

### 数据库 `db`

| 命令 | 用途 |
|---|---|
| `zata-ops db backup [--force-full] [--dry-run]` | 备份 DB + 日志 + 资源到 S3,按 `RETENTION_DAYS` 自动清理旧备份 |
| `zata-ops db list` | 列出 S3 上可用的备份时间点与类型 |
| `zata-ops db restore --from <时间戳> [--restore-db] [--restore-logs] [--restore-resources] [--yes]` | 从 S3 恢复,支持链式恢复 |
| `zata-ops db check` | 验证 S3 端点连通性与凭据 |
| `zata-ops db migrate --source <url> --target <url> [--dry-run]` | 在两个 PostgreSQL 之间迁移数据(非 Alembic) |

### VPS 环境 `env`

```bash
# 初始化一台新 VPS:装 Docker、部署 Traefik
zata-ops env provision --host example.com --user deploy --dry-run
zata-ops env provision --host example.com --user deploy

# 同时部署监控栈(Vector + Loki + Prometheus + Grafana)
zata-ops env provision \
  --host example.com \
  --user deploy \
  --acme-email ops@example.com \
  --with-monitoring \
  --monitoring-domain example.com \
  --dry-run

# 修复 Traefik 的 ACME 邮箱并强制重新签发证书
zata-ops env fix --host example.com --email ops@example.com --dry-run
zata-ops env fix --host example.com --email ops@example.com
```

详细监控栈说明见 [监控栈文档](docs/architecture/monitoring-stack.md)。

### 日志 `logs`

```bash
# 跟踪容器或 systemd 日志
zata-ops logs tail --project my-app --since 1h --dry-run

# 在最近日志里搜索关键字
zata-ops logs search "ERROR" --project my-app --dry-run
```

### 终端状态面板 `dashboard`

```bash
zata-ops dashboard --project my-app
# 不接真实数据时用 mock 模式预览界面
zata-ops dashboard --mock --project my-app
```

### SSH 端口转发 `tunnel`

需要先安装 `[ssh]` extra(见上)。`--direction local` 对应 `ssh -L`,
`--direction remote` 对应 `ssh -R`。**不传任何 flag 时会进入交互表单
(arrow keys 选择方向,逐项填入参数)**;传了 flag 就用 flag,不走表单。

```bash
# 交互模式(推荐,适合快速验证)
zata-ops tunnel open

# 先看 plan,不真正连接
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --dry-run

# 前台:把远端 5432 映射到本地 19000,Ctrl+C 关闭
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432

# 后台:守护到后台,用 list/close 管理
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --background \
    --name db-access
zata-ops tunnel list
zata-ops tunnel status db-access
zata-ops tunnel close db-access

# 断线自动重连(指数退避 1s→30s,后台 + 长期任务推荐开)
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --reconnect
# 限最多 5 次:
zata-ops tunnel open ... --reconnect --max-reconnect 5

# 密码鉴权(前台专用,后台模式拒绝;推荐改用 ssh-add)
zata-ops tunnel open \
    --direction local \
    --ssh-host 47.101.71.219 --ssh-user root \
    --bind-port 19000 --target-port 5432 \
    --ssh-password
```

详细参数与常见问题见 [SSH 隧道指南](guides/tunnel.md)。

---

## 定时备份

`zata-ops` 只提供按需触发的 `db backup` 命令,定时调度由你已有的基础设施负责,选其一即可:

- 部署机上的 **systemd timer**
- crontab:`@daily zata-ops db backup`
- **GitHub Actions** 的 `schedule:` workflow
- **Dokploy 定时任务**调用 `zata-ops`
- 维护窗口前手动执行

---

## 配置项

`zata-ops` 从当前工作目录的 `.env` / `.env.local` 读取配置(优先级:CLI 参数 > `.env.local` > `.env`)。完整模板见本仓库或下游项目的 `.env.example`。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PROJECT` | `app` | 所有命令 |
| `DATABASE_URL` | — | `db backup` / `db restore` / `db migrate` |
| `S3_ENDPOINT` | — | `db *` |
| `S3_ACCESS_KEY` | — | `db *` |
| `S3_SECRET_KEY` | — | `db *` |
| `S3_BUCKET` | `app-backups` | `db *` |
| `S3_PREFIX` | `app-backups` | `db *` |
| `S3_ADDRESSING_STYLE` | `path` | `db *`(MinIO 用 `path`,阿里 OSS / 腾讯 COS 用 `virtual`,AWS S3 用 `auto`) |
| `RETENTION_DAYS` | `30` | `db backup` |
| `FULL_BACKUP_DAY` | `6`(周日,0=周一) | `db backup` |
| `BACKUP_TIME` | `18:00` | `db backup` |
| `LOGS_DIR` | `/app/backend/logs` | `db backup` / `db restore` |
| `RESOURCES_DIR` | `/app/backend/data` | `db backup` / `db restore` |
| `WORK_DIR` | `/tmp/backups` | `db backup` / `db restore` |

---

## 在下游项目里调用

`zata-ops` 设计为被下游项目的 `justfile` recipe 直接调用,例如:

```just
backup:
    zata-ops db backup

restore ts:
    zata-ops db restore --from {{ts}} --restore-db --yes
```

---

## 开发

在 zata-ops 仓库根目录下:

```bash
uv sync
uv run pytest            # 23 个快速单测
uv run zata-ops --help   # 不安装直接跑 CLI
```

如果正在二次开发 zata-ops,希望源码改动立即生效,可以可编辑方式安装:

```bash
uv tool install -e .                      # 可编辑安装,源码改动无需重装
uv tool install -e . --force              # 已有同名工具时覆盖
uv tool install -e . --reinstall          # pyproject.toml / 依赖变更后强制重装
```

---

## 背景

过去每个下游项目都从 `zata_code_template` 复制 `scripts/backup_service/` 与 `deploy/vps-traefik/`,意味着 N 个项目维护着同一份备份逻辑,修复需要在每个下游手工同步。

`zata-ops` 把这些能力收拢到一处:**装一次,所有下游项目在 `justfile` recipe 里调用即可**。
