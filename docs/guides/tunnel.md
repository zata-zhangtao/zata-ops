# SSH 端口转发 `tunnel`

`zata-ops tunnel` 是 `ssh -L/-R` 的 **pid 标签管理器**：帮你给后台 ssh 转发进程起个
名字、列出当前在跑的几个、按名字关掉。**它不做任何 SSH 协议层面的事**——认证、
端口转发、host key、KeepAlive、重连都交给 `ssh` 进程自己。

## 快速开始

```bash
# 后台跑一个本地转发 (-L)
zata-ops tunnel open db-access -- \
    ssh -N -L 6669:localhost:5432 root@172.188.74.58

# 后台跑一个远端转发 (-R)
zata-ops tunnel open dev-server -- \
    ssh -N -R 5432:localhost:6669 root@host

# 任意 ssh flag 直接透传
zata-ops tunnel open vpn -- \
    ssh -N -D 1080 -C root@host -i ~/.ssh/cd.pem -p 2222

# 列出
zata-ops tunnel list

# 明细 + 最近 20 行日志
zata-ops tunnel status db-access

# 停止(发 SIGTERM,5s 后升级 SIGKILL)
zata-ops tunnel close db-access
```

`--` 之后第一个参数必须是 `ssh`（`scp`/`rsync` 等不算），其他 token 原样塞给 ssh
进程，不做解析。

## 行为细节

- `tunnel open` 立刻返回。ssh 进程是 detached 子进程（`start_new_session=True`），
  stdout/stderr 追加到 `~/.local/share/zata-ops/tunnels/<name>.log`。
- 状态文件 `~/.local/share/zata-ops/tunnels/<name>.json` 只存：name、pid、
  启动时间、原始 ssh argv。
- `tunnel list` 会自动清理"僵尸"条目（pid 已退出），无需手工干预。
- `tunnel close` 走 SIGTERM → 5 秒宽限期 → SIGKILL 协议。

## 与"裸 `ssh -fN`"对比

| | 裸 `ssh -fN -L ...` | `zata-ops tunnel open <name> -- ssh ...` |
|---|---|---|
| 后台跑 | ✓ | ✓ |
| 按名字管理 | ✗（`pgrep -af ssh`） | ✓（`list/status/close <name>`） |
| 日志分流 | ✗（混进 shell） | ✓（`~/.local/share/.../tunnels/<name>.log`） |
| 鉴权/host key | 走 `~/.ssh/config` + agent | 走 `~/.ssh/config` + agent（完全一样） |
| 断线自动重连 | ✗（用 `autossh`） | ✗（用 `autossh`，见下） |

## 长跑 + 自动重连

`tunnel` 故意不做 supervisor。要给"长期挂着 + 断了自动恢复"加这层，包一层
`autossh` 即可：

```bash
# autossh -f 派生 autossh -f ssh ...;ssh 死了 autossh 立刻拉起
autossh -f -M 0 -N \
    -o "ServerAliveInterval=10" -o "ServerAliveCountMax=3" \
    -L 6669:localhost:5432 root@172.188.74.58

# 用 zata-ops 跟踪这个 autossh 派生的 ssh,名字化管理
zata-ops tunnel open db-access -- \
    ssh -N -L 6669:localhost:5432 root@172.188.74.58
```

要 systemd 管，就写一个 `~/.config/systemd/user/db-access.service`：

```ini
[Unit]
Description=db-access tunnel

[Service]
ExecStart=/usr/bin/ssh -N -L 6669:localhost:5432 root@172.188.74.58
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

## 限制

- **不解析 `ssh` argv**。`--` 之后原样转给 ssh,打错就 ssh 自己报错。
- **不做 SSH 协议层的事**(不鉴权、不转发、不重连、不读 `~/.ssh/config`)。这
  些都归 ssh 进程管。
- **前台模式不存在**。想前台就 `ssh -L`,不必过 zata-ops。
- **不存历史**。复用走 shell history(↑ 一下)即可。
