# SSH 端口转发 `tunnel`

`zata-ops tunnel` 把"通过 SSH 打通本地/远端端口"这条能力做成一等 CLI
子命令,免去每次手敲 `ssh -L/-R -N -f ...` 然后 `lsof` / `pkill` 收尾。
底层用 [paramiko](https://www.paramiko.org/),无需本地 `ssh` 二进制,
不依赖 `~/.ssh/config` 之外的配置文件。

支持两种方向:

- `local` (对应 `ssh -L`):把**远端**的 `target_host:target_port` 映射到**本机**
  的 `bind_host:bind_port` —— 适合访问只在内网暴露的服务。
- `remote` (对应 `ssh -R`):把**本机**的 `target_host:target_port` 暴露到**远端**
  的 `bind_host:bind_port` —— 适合把本地 dev 服务给远端/同事访问。

执行模式分两种:

- **前台**:命令阻塞在前台,`Ctrl+C` 退出,端口立即释放。
- **后台** (`--background`):fork 守护进程并写状态文件,用
  `tunnel list / status / close` 后续管理。

## 安装 paramiko 依赖

`paramiko` 是 `[ssh]` 额外依赖,基础安装不带。首次使用前:

```bash
# 在仓库根目录,以可编辑方式安装并启用 ssh extra
uv tool install -e '.[ssh]'

# 或者只补参数
uv tool install --force '/path/to/zata-ops[ssh]'
```

如果忘记装,直接 `tunnel open` 会打印中文提示并以退出码 1 退出。

## Dry run

跟 `env provision` 一样,`tunnel open` 支持 `--dry-run` 预览,不建立
SSH 连接也不占用端口:

```bash
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --ssh-user deploy \
    --bind-port 19000 \
    --target-host 127.0.0.1 \
    --target-port 5432 \
    --dry-run
```

输出包含完整 plan(JSON 格式)和一行等价的 `ssh -L ...` 命令,方便
对照验证。

## 交互模式

不传任何必填参数(主要是 `--direction`)时,`tunnel open` 会进入
[questionary](https://github.com/tmbo/questionary) 驱动的交互表单,
逐项用 arrow keys / tab 自动补全填写:

```bash
$ zata-ops tunnel open
? 隧道方向(对应 ssh -L 还是 ssh -R)? local
? SSH 跳板机地址? bastion.example.com
? SSH 用户名? [zata]: ops
? SSH 服务端口? [22]: 22
? SSH 私钥路径(留空则自动探测 ~/.ssh/id_* 与 ssh-agent)? []
? 监听地址(local 模式下是本机,remote 模式下是远端)? [127.0.0.1]: 127.0.0.1
? 监听端口? 19000
? 目标主机(流量的实际终点)? [127.0.0.1]: db.internal
? 目标端口? 5432
? 后台守护?(No 走前台,Ctrl+C 退出) (y/N): n
? 先 dry-run 预览一下 plan 再执行? (Y/n): y
```

行为约定:

- **非 TTY 环境** (CI、管道) 不进入表单,直接以退出码 1 退出,提示
  用 `--direction` / `--ssh-host` 等显式 flag 或加 `--dry-run`。
- **Ctrl+C** 任意时刻退出表单,退出码 130,与 shell 习惯一致。
- **端口范围** 1-65535,超出会被 questionary 的 `validate` 拒绝,无法继续。
- 传了任何 flag(包括 `--direction`)就**不**进表单,与显式调用习惯一致。
- 表单最后会问"先 dry-run 预览吗?",默认 Yes(推荐新用户先看 plan)。

## 快速开始

### 1. 前台模式:临时把远端 PostgreSQL 暴露到本机

```bash
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --ssh-user deploy \
    --bind-port 19000 \
    --target-host 127.0.0.1 \
    --target-port 5432
```

执行后:

- 终端打印 `Tunnel ... is ready`,命令阻塞。
- 另一个终端 `psql -h 127.0.0.1 -p 19000` 可以连到 `bastion.example.com`
  上的 PostgreSQL。
- 按 `Ctrl+C` 退出,本地 19000 端口立即释放(可以用 `lsof -nP -iTCP:19000 -sTCP:LISTEN` 验证)。

### 2. 后台模式:长期挂在后台,需要时关闭

```bash
# 启动后台实例
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --background \
    --name db-access

# 列出所有后台实例
zata-ops tunnel list

# 查看指定实例的明细 + 最近日志
zata-ops tunnel status db-access

# 停止后台实例
zata-ops tunnel close db-access
```

后台模式下:

- 父进程等子进程把 `state` 翻成 `ready`(最多 8s)后再返回。
- 状态文件在 `~/.local/share/zata-ops/tunnels/<name>.json`,
  日志在 `~/.local/share/zata-ops/tunnels/<name>.log`。
- `tunnel close` 先发 `SIGTERM`,5 秒宽限期内 daemon 走优雅退出,
  超时则升级到 `SIGKILL`,最后清理状态文件。

### 3. 远端转发:把本地 dev 服务暴露给远端

```bash
zata-ops tunnel open \
    --direction remote \
    --ssh-host bastion.example.com \
    --bind-port 8080 \
    --target-host 127.0.0.1 \
    --target-port 3000
```

执行后,远端的 `127.0.0.1:8080` 会被打回到你本机的 `127.0.0.1:3000`。
适合临时给同事/远端测试机器访问你的本地 dev 服务。

## 鉴权

参数 `--ssh-key` 不传时,paramiko 会按以下顺序探测:

1. 环境变量 `$SSH_AUTH_SOCK` 指向的 ssh-agent
2. `~/.ssh/id_ed25519`、`~/.ssh/id_rsa`、`~/.ssh/id_ecdsa`、`~/.ssh/id_dsa`

如果你的私钥不在默认路径,显式传 `--ssh-key ~/.ssh/cd-my-app`。

## Host key 校验

默认行为与 OpenSSH 的 `StrictHostKeyChecking=accept-new` 一致:
加载 `~/.ssh/known_hosts`,未知 host 时打 warning 并把新 key
写入 `known_hosts` 继续连接。

如果你的工作流要求"未知 host 立刻拒绝"(--strict 模式),加
`--strict-host-key`,与 OpenSSH 的 `StrictHostKeyChecking=yes` 等价。

## 鉴权

`tunnel open` 同时支持私钥和密码两种 SSH 鉴权方式,优先级:**私钥 > 密码 > 自动探测**。

### 怎么选 `-L` 还是 `-R`

| 你想做的事 | 选 |
|---|---|
| "我本机连 `127.0.0.1:19000` 就能访问远端 47.101.71.219 上的服务" | **`-L`** (本机 → 远端) |
| "远端 47.101.71.219 上的 `127.0.0.1:9001` 反过来访问我本机的服务" | **`-R`** (远端 → 本机) |

三种"地址"的填法:

| 名字 | `-L` 时填 | `-R` 时填 |
|---|---|---|
| **SSH 跳板机** | 你 SSH 连过去的那台机器 | 同左 |
| **监听地址:端口** | 你**本机**的地址(别人从这里连进隧道) | 远端 SSH 服务器上的地址(从远端连进) |
| **目标主机:端口** | 远端服务的实际地址(从跳板机视角看) | 你**本机**服务的实际地址 |

`-R` 走通后的效果:**远端 SSH 服务器自己**(或局域网用户)连
`<远端>:9001` 会被转到**你的电脑** `127.0.0.1:8000`,前提是 `<远端>`
上有人主动发起连接;远端用户自己看不到这个端口在远端"已经开了"——
它只在你 SSH 通道活着的时候才工作。

### 私钥(推荐)

不传 `--ssh-key` 时,paramiko 按以下顺序探测:

1. 环境变量 `$SSH_AUTH_SOCK` 指向的 ssh-agent
2. `~/.ssh/id_ed25519`、`~/.ssh/id_rsa`、`~/.ssh/id_ecdsa`、`~/.ssh/id_dsa`

如果你的私钥不在默认路径,显式传 `--ssh-key ~/.ssh/cd-my-app`。

### 密码

```bash
# CLI(密码走隐藏输入,但仍会进 shell 历史,谨慎使用)
zata-ops tunnel open \
    --direction local \
    --ssh-host 47.101.71.219 \
    --ssh-user root \
    --bind-port 19000 \
    --target-port 5432 \
    --ssh-password

# 提示输入:
# ? SSH 密码(输入时不回显)? ********
```

交互表单里会先问 "SSH 认证方式?",选 "密码" 后会用 `questionary.password`
走隐藏输入,**不回显到终端**。密码仅保留在前台进程内存,绝不落盘到状态文件
或日志。

**安全限制**:`--background` 模式与 `--ssh-password` **互斥**。后台守护需要把
spec 写到 `~/.local/share/zata-ops/tunnels/<name>.json`,明文密码有泄露风险。
带密码时跑后台会直接被拒绝并提示:

```
--background 模式与 --ssh-password 互斥:
后台模式会把 spec 写到 ~/.local/share/zata-ops/tunnels/<name>.json,
其中若含明文密码就有泄露风险。请二选一:
  1) 用 ssh-add 注入 ssh-agent,再去掉 --ssh-password
  2) 改用前台模式(去掉 --background),Ctrl+C 退出
```

### 推荐:用 ssh-add 代替明文密码

如果你的私钥是带密码的(常见于 `~/.ssh/id_rsa` 有 passphrase),或者你习惯
用密码登录,**不要**反复输入明文密码,改成:

```bash
# 一次性:把密钥加进 ssh-agent(后台进程会持续生效)
ssh-add ~/.ssh/id_rsa
# 输入 passphrase,只输这一次

# 此后所有 tunnel open 都不用 --ssh-password,agent 自动帮你鉴权
zata-ops tunnel open --direction local ... --background --name db
```

后台 + 自动重连 + ssh-agent 是最舒服的组合:启动时输一次密码,之后
`Ctrl+C` 也不用,断线自动恢复,杀掉也能干净退出。

## 自动重连 (--reconnect)

SSH 连接可能被服务端主动踢、网络波动、VPN 切换、NAT 超时等因素断掉。
加 `--reconnect` 后:

- 指数退避 1s → 2s → 4s → 8s → 16s → 30s(封顶),带 ±30% 抖动
- transport `keepalive` 间隔默认 15s,让"半死"连接能更快被发现
- 重连日志写到后台 daemon 的 `<name>.log`(前台模式打到 stderr,带 `[reconnect]` 前缀)
- `--max-reconnect N` 限定最大重试次数,0 表示无限(默认)

```bash
# 前台 + 断线自动重连(推荐长期跑 dev/staging 调试)
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --reconnect

# 限制最多重试 5 次(到 5 次还连不上就退出非零)
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --reconnect --max-reconnect 5

# 后台 + 重连 + 命名
zata-ops tunnel open \
    --direction local \
    --ssh-host bastion.example.com \
    --bind-port 19000 \
    --target-port 5432 \
    --background --name db \
    --reconnect
```

断线期间两种方向的行为不同:

| 方向 | 重连窗口内的新连接 | 已有 in-flight 连接 |
|---|---|---|
| `-L`(本机端口 → 远端) | 客户端连本地端口会被立刻拒(transport 暂时 None) | 继续工作(每个连接独立 channel) |
| `-R`(远端端口 → 本机) | 远端用户连远端端口会拿到 connection refused | 已建立的 channel 不受影响 |

注意 **第一次** (重)连尝试失败时,Reconnector **不会** 退避重试,而是
直接退出非零(reconnect 配错的情况下立即报错更友好)。只有"第一次
成功、之后才断"才走退避重试。

## 服务端 sshd_config 要求

客户端无法通过 SSH 协议读取远端 `sshd_config`,只能"尝试请求→看响应"。
如果远端禁用了相关配置,`tunnel` 会把错误包装成清晰的中文提示,
但不会自动修复。`--dry-run` 输出的 `server_requirements` 字段会列出
本次命令具体需要的服务端配置。

- **本地/远端转发都需要** `AllowTcpForwarding yes`(默认就是 yes,显式设回即可)。
  取值可以是 `yes` / `no` / `local`(只允许 `-L`)/ `remote`(只允许 `-R`)/ `all`(同 yes)。
- **远端转发 (`-R`) 绑到非 loopback 地址时** 还需要 `GatewayPorts yes` 或
  `GatewayPorts clientspecified`(默认是 `no`,只允许绑到 127.0.0.1)。
  如果你只是想给"本机 ↔ 远端"打通,保留默认 `--bind-host 127.0.0.1` 即可,
  无需碰 `GatewayPorts`。

修改后记得 `sudo systemctl reload sshd` 让新配置生效。

## 常见问题

### 端口被占用

前台模式下,`--bind-port` 已被别的进程占用会立刻报
`无法绑定本地端口 ...` 并以退出码 1 退出。先用 `lsof -nP -iTCP:<port> -sTCP:LISTEN` 排查。

### 服务端拒绝了转发

`run_remote` 失败时会把 `AllowTcpForwarding` / `GatewayPorts` 的
`/etc/ssh/sshd_config` 检查项直接打出来,照着排查即可。
`run_local` 在启动时做了一次 no-op 探针,也会在第一时间报这个错,
而不是等用户实际连一次才失败。

### 鉴权失败

确认 `~/.ssh/known_hosts` 里 `bastion.example.com` 的条目与远端
实际 host key 一致;或者用 `--ssh-key` 显式指定私钥。

### ssh-agent 不可用

如果本机没有 ssh-agent 且 `~/.ssh/id_*` 都不存在,paramiko 会回退
到交互式密码输入,前台模式可用,后台模式会因为没有 TTY 失败。
解决方法:启 ssh-agent 并 `ssh-add` 私钥,或显式传 `--ssh-key`。

### 后台实例的 PID 找不到

`tunnel list` 会自动清理"僵尸"状态文件(PID 对应的进程已退出)。
如果你手动 `kill` 了后台 daemon 进程,再次跑 `tunnel list` 时
对应条目会被静默清除,无需手工干预。

## 限制

- **跳板机 (JumpHost / ProxyJump)** 不支持。需要链式 SSH 转发时,
  请在远端 SSH 服务端 `~/.ssh/config` 中配置 `ProxyCommand`,
  或者手工链两次 `tunnel open`。
- **SOCKS 动态代理 (`ssh -D`)** 暂不支持,留待 v2。
- **多端口批量转发** 单次命令只支持一对绑定/目标,需要多个
  端口时多次 `tunnel open` 即可。
