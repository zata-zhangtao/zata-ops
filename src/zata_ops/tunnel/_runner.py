"""SSH 端口转发的核心实现。

通过 paramiko 建立 SSH 客户端并按 ``TunnelOptions.direction`` 调度本地
(``ssh -L``) 或远端 (``ssh -R``) 转发。前台模式下,主循环在前台
阻塞;后台模式下,父进程 fork 出的子进程跑同一份代码,通过
``signal.SIGTERM`` 优雅退出。

实现要点:

- 本地转发 (``-L``):自管本地 ``socket`` + ``accept`` 循环;每条连接调用
  :func:`paramiko.Transport.open_channel` 拿 channel,双向 pump。
- 远端转发 (``-R``):调用 :func:`paramiko.Transport.request_port_forward` 时
  传 ``handler`` 回调,paramiko 在新连接到来时直接调用 handler,把现成的
  Channel 转到本地目标 socket 上 pump。这是 paramiko 公开 API。
"""

from __future__ import annotations

import shlex
import signal
import socket
import sys
import threading
from dataclasses import dataclass

from zata_ops.tunnel import _reconnect, _state

try:  # paramiko is declared as an optional [ssh] extra
    import paramiko
except ImportError:  # pragma: no cover - exercised only when extra is missing
    paramiko = None  # type: ignore[assignment]


class TunnelError(Exception):
    """隧道建立/运行过程中抛出的业务异常。"""


@dataclass
class TunnelOptions:
    """``tunnel open`` 解析后的运行时选项。

    Attributes:
        direction: ``"local"`` 或 ``"remote"``。
        ssh_host: SSH 跳板机地址,必填。
        bind_port: 监听端口,必填。
        target_port: 流量终点端口,必填。
        ssh_user: SSH 登录用户,默认 ``$USER``。
        ssh_port: SSH 服务端口,默认 22。
        ssh_key: 可选私钥路径(与 ``ssh_password`` 互斥)。
        ssh_password: 可选明文密码(与 ``ssh_key`` 互斥,前台模式仅内存,
            不写盘;后台模式禁用,避免明文密码落盘)。
        bind_host: 监听地址,默认 ``127.0.0.1``。
        target_host: 流量终点主机,默认 ``127.0.0.1``。
        strict_host_key: True 时未知 host key 拒绝;False(默认)时打 warning 并接受。
        name: 后台实例的友好名(前台模式可空)。
        background: 是否后台守护。
        dry_run: 只打印 plan 不建立连接。
        reconnect: True 时 SSH 断了自动重连(指数退避 1s→30s)。
        max_reconnect: 最多重试次数,0 表示无限(仅 ``reconnect=True`` 时生效)。
    """

    direction: str
    ssh_host: str
    bind_port: int
    target_port: int
    ssh_user: str
    ssh_port: int
    ssh_key: str | None
    bind_host: str
    target_host: str
    strict_host_key: bool
    name: str
    background: bool
    dry_run: bool
    ssh_password: str | None = None
    reconnect: bool = False
    max_reconnect: int = 0


def build_spec_dict(options: TunnelOptions) -> dict:
    """把 :class:`TunnelOptions` 转成可直接写到状态文件的 dict。

    Args:
        options: CLI 解析后的选项。

    Returns:
        用于 :func:`_state.TunnelSpec.from_dict` 的字典,缺省字段已被填平。
    """
    return {
        "name": options.name,
        "direction": options.direction,
        "bind_host": options.bind_host,
        "bind_port": options.bind_port,
        "target_host": options.target_host,
        "target_port": options.target_port,
        "ssh_host": options.ssh_host,
        "ssh_user": options.ssh_user,
        "ssh_port": options.ssh_port,
        "ssh_key": options.ssh_key,
        "strict_host_key": options.strict_host_key,
        "pid": 0,
        "log_path": "",
        "started_at": "",
        "state": "pending",
        "reconnect": options.reconnect,
        "max_reconnect": options.max_reconnect,
    }  # 注意:不写 ssh_password —— 后台模式禁用密码,这条路径不会被走到


def plan_to_argv_preview(options: TunnelOptions) -> str:
    """生成等价 ``ssh`` 命令的可读字符串,用于 dry-run 展示。

    Args:
        options: CLI 解析后的选项。

    Returns:
        一行可粘贴的 shell 命令,展示等价 ``ssh -L/-R`` 形态。
    """
    ssh_argv: list[str] = ["ssh"]
    if options.ssh_port != 22:
        ssh_argv.extend(["-p", str(options.ssh_port)])
    if options.ssh_key:
        ssh_argv.extend(["-i", options.ssh_key])
    if options.strict_host_key:
        ssh_argv.extend(["-o", "StrictHostKeyChecking=yes"])
    else:
        ssh_argv.extend(["-o", "StrictHostKeyChecking=accept-new"])
    ssh_argv.extend(["-N", "-o", "ExitOnForwardFailure=yes"])
    if options.direction == "local":
        forward_flag = "-L"
    else:
        forward_flag = "-R"
    spec_str = f"{options.bind_host}:{options.bind_port}:{options.target_host}:{options.target_port}"
    ssh_argv.extend([forward_flag, spec_str, f"{options.ssh_user}@{options.ssh_host}"])
    return " ".join(shlex.quote(part) for part in ssh_argv)


def _require_paramiko() -> "paramiko":
    """Return the imported paramiko module or raise :class:`TunnelError`.

    错误信息会区分两种常见的安装场景,给出对应的修复命令:
    - 通过 ``uv tool install`` 装的全局副本 → 重装时加 ``[ssh]`` extra
    - 通过 ``uv run`` 跑项目 venv → 在项目目录里 ``uv sync --extra ssh``
    - 通过 ``pip install`` 装的 → ``pip install "zata-ops[ssh]"`
    """
    if paramiko is None:
        raise TunnelError(_paramiko_missing_hint())
    return paramiko


def _paramiko_missing_hint() -> str:
    """生成针对当前 Python 环境的 paramiko 缺失提示。"""
    import sys

    sys_prefix_path = sys.prefix
    # uv tool install 的 venv 路径形如 ~/.local/share/uv/tools/zata-ops/...
    if "uv/tools/" in sys_prefix_path or "/.local/share/uv/" in sys_prefix_path:
        return (
            "paramiko 未装到当前 zata-ops 所在的 Python 环境里。\n"
            "你装的是 `uv tool install` 出来的全局副本,该副本没带 [ssh] extra。\n"
            "修复(在 zata-ops 仓库根目录下执行):\n"
            "  uv tool install -e '.[ssh]' --force\n"
            "  # 或:  uv tool install --reinstall '/path/to/zata-ops[ssh]'\n"
            "重装后再跑 zata-ops 就能用 tunnel 子命令了。"
        )
    if "uv" in sys.executable or ".venv" in sys.executable or "venv" in sys.executable:
        return (
            "paramiko 未装到当前 Python 环境的 venv 里。\n"
            "如果你在 zata-ops 仓库根目录,运行:\n"
            "  uv sync --extra ssh\n"
            "然后用 `uv run zata-ops ...` 启动(而不是裸 `zata-ops`)。"
        )
    return (
        "paramiko 未安装。\n"
        '请运行: pip install "zata-ops[ssh]"  '
        '或 pip install "zata-ops[ssh]" 后重试。'
    )


def build_ssh_client(options: TunnelOptions) -> "paramiko.SSHClient":
    """构造一个尚未连接的 :class:`paramiko.SSHClient`。

    负责 host key policy 与私钥/ssh-agent 探测;不实际发起连接。

    Args:
        options: 解析后的 CLI 选项。

    Returns:
        配置完成的客户端实例,调用方负责 ``connect()``。

    Raises:
        TunnelError: 当 paramiko 不可用时。
    """
    pk = _require_paramiko()
    client = pk.SSHClient()
    try:
        client.load_system_host_keys()
    except OSError:
        # known_hosts 不存在时不要阻塞
        pass
    if options.strict_host_key:
        client.set_missing_host_key_policy(pk.RejectPolicy())
    else:
        # 默认:未知 host 打 warning 并接受,与 OpenSSH 的
        # StrictHostKeyChecking=accept-new 行为一致。
        client.set_missing_host_key_policy(pk.AutoAddPolicy())
    return client


def _pump_socket_to_channel(local_sock: socket.socket, channel) -> None:
    """把本地 socket 的读端数据写到 SSH channel。"""
    try:
        while True:
            chunk = local_sock.recv(65536)
            if not chunk:
                break
            sent_total = 0
            while sent_total < len(chunk):
                sent_total += channel.send(chunk[sent_total:])
    except (OSError, EOFError):
        pass
    finally:
        try:
            channel.shutdown_write()
        except (OSError, EOFError):
            pass


def _pump_channel_to_socket(channel, local_sock: socket.socket) -> None:
    """把 SSH channel 的读端数据写到本地 socket。"""
    try:
        while True:
            chunk = channel.recv(65536)
            if not chunk:
                break
            local_sock.sendall(chunk)
    except (OSError, EOFError):
        pass


class LocalForwardServer:
    """``ssh -L`` 形态的本地端口转发服务。"""

    def __init__(
        self,
        ssh_client: "paramiko.SSHClient",
        bind_host: str,
        bind_port: int,
        target_host: str,
        target_port: int,
    ) -> None:
        self._ssh_client = ssh_client
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._target_host = target_host
        self._target_port = target_port
        self._server_socket: socket.socket | None = None
        self._stop_event = threading.Event()
        self._accept_thread: threading.Thread | None = None
        # 用于支持 Reconnector 热替换 transport:lock 保护对 _ssh_client 的读写
        self._ssh_client_lock = threading.Lock()

    def set_ssh_client(self, new_ssh_client: "paramiko.SSHClient") -> None:
        """由 :class:`Reconnector` 在每次(重)连后调用,热替换内部 client。

        Args:
            new_ssh_client: 新的、已 connected 的 SSH 客户端。
        """
        with self._ssh_client_lock:
            self._ssh_client = new_ssh_client

    def _get_ssh_client(self) -> "paramiko.SSHClient | None":
        """线程安全地拉取当前 SSH 客户端。"""
        with self._ssh_client_lock:
            return self._ssh_client

    def start(self) -> None:
        """绑定本地端口并启动 accept 循环(独立线程)。"""
        bind_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            bind_socket.bind((self._bind_host, self._bind_port))
        except OSError as exc:
            bind_socket.close()
            raise TunnelError(
                f"无法绑定本地端口 {self._bind_host}:{self._bind_port}: {exc}"
            ) from exc
        bind_socket.listen(128)
        self._server_socket = bind_socket
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="tunnel-local-accept", daemon=True
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        assert self._server_socket is not None
        self._server_socket.settimeout(0.5)
        while not self._stop_event.is_set():
            try:
                client_sock, peer_addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                # socket 被 close() 唤醒
                break
            handler_thread = threading.Thread(
                target=self._handle_connection,
                args=(client_sock, peer_addr),
                name="tunnel-local-conn",
                daemon=True,
            )
            handler_thread.start()

    def _handle_connection(self, client_sock: socket.socket, peer_addr) -> None:
        # 热替换支持:Reconnector 重连时会把 _ssh_client 换成新的。
        ssh_client = self._get_ssh_client()
        if ssh_client is None:
            # 重连窗口内,S transport 暂时不可用,直接拒掉新连接。
            try:
                client_sock.close()
            except OSError:
                pass
            return
        try:
            transport = ssh_client.get_transport()
            if transport is None or not transport.is_active():
                try:
                    client_sock.close()
                except OSError:
                    pass
                return
            channel = transport.open_channel(
                "direct-tcpip",
                (self._target_host, self._target_port),
                peer_addr,
            )
        except Exception:  # noqa: BLE001 - 任何 paramiko 错误都关闭 client 即可
            client_sock.close()
            return
        pump_to_channel = threading.Thread(
            target=_pump_socket_to_channel,
            args=(client_sock, channel),
            daemon=True,
        )
        pump_to_socket = threading.Thread(
            target=_pump_channel_to_socket,
            args=(channel, client_sock),
            daemon=True,
        )
        pump_to_channel.start()
        pump_to_socket.start()
        pump_to_socket.join()
        pump_to_channel.join()
        try:
            client_sock.close()
        except OSError:
            pass
        try:
            channel.close()
        except OSError:
            pass

    def stop(self) -> None:
        """停止 accept 循环并关闭 server socket。"""
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass


def _connect_ssh(client: "paramiko.SSHClient", options: TunnelOptions) -> None:
    """建立 SSH 连接并校验鉴权。"""
    pk = _require_paramiko()
    key_filename = options.ssh_key if options.ssh_key else None
    password = options.ssh_password if options.ssh_password else None
    # 优先级:key > password > 探测 ~/.ssh 与 agent。三者互斥时显式关掉
    # 探测,避免 paramiko "按顺序都试一遍" 把密码也忽略掉。
    explicit_key = key_filename is not None
    explicit_password = password is not None
    try:
        client.connect(
            hostname=options.ssh_host,
            port=options.ssh_port,
            username=options.ssh_user,
            key_filename=key_filename,
            password=password,
            look_for_keys=not explicit_key and not explicit_password,
            allow_agent=not explicit_key and not explicit_password,
            timeout=10.0,
            banner_timeout=10.0,
            auth_timeout=10.0,
        )
    except pk.AuthenticationException as exc:
        raise TunnelError(_auth_failure_hint(options, exc)) from exc
    except pk.SSHException as exc:
        raise TunnelError(f"SSH 连接错误: {exc}") from exc
    except OSError as exc:
        raise TunnelError(
            f"无法连接 {options.ssh_host}:{options.ssh_port}: {exc}"
        ) from exc


#: SSH 鉴权失败时的常见原因 + 排查建议
_AUTH_FAILURE_HINT = (
    "\n最常见原因:"
    "\n  1. 密码错 → 重新输一遍"
    "\n  2. 服务端没开密码登录(/etc/ssh/sshd_config 里 PasswordAuthentication no)"
    "\n     → 改用 ssh-copy-id + ssh-add 走公钥,或服务端改 sshd_config"
    "\n  3. 服务端不允许 root 登(PermitRootLogin no / prohibit-password)"
    "\n     → 改用一个普通用户名,登录后 sudo 提权"
    "\n先用这条命令在终端单独验证服务端是否接受密码(绕开 zata-ops 与 paramiko):"
    "\n  ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no "
    "{user}@{host}"
    "\n如果这条都报 'Permission denied',那是服务端配置问题,不是 zata-ops 的锅。"
)


def _auth_failure_hint(options: TunnelOptions, exc: Exception) -> str:
    """组装带排查方向的 SSH 鉴权失败错误信息。"""
    return f"SSH 鉴权失败: {exc}。" + _AUTH_FAILURE_HINT.format(
        user=options.ssh_user, host=options.ssh_host
    )


# 错误信息模板:当服务端拒绝 TCP 转发时,提示用户去查 sshd_config。
_FORWARDING_DENIED_HINT = (
    "可能原因:服务端 sshd_config 未启用 TCP 转发。"
    "请在远端 /etc/ssh/sshd_config 检查:\n"
    "  AllowTcpForwarding yes   # 默认值,显式设回 yes 即可\n"
    "  # 或更细粒度:AllowTcpForwarding local (仅允许 -L)\n"
    "  #           AllowTcpForwarding remote (仅允许 -R)\n"
    "修改后需要 sudo systemctl reload sshd 才会生效。"
)


def _wrap_forwarding_denial(direction_label: str, exc: Exception) -> TunnelError:
    """把 paramiko 的 SSHException 包装成带 ``AllowTcpForwarding`` 提示的 :class:`TunnelError`。"""
    return TunnelError(
        f"{direction_label} 转发被服务端拒绝: {exc}\n{_FORWARDING_DENIED_HINT}"
    )


def _make_reverse_handler(target_host: str, target_port: int):
    """构造 paramiko ``request_port_forward`` 的 handler 回调。

    paramiko 在新连接到来时会调用
    ``handler(channel, (src_addr, src_port), (server_addr, server_port))``,
    channel 已是 paramiko 的 :class:`Channel` 实例。
    """

    def _handle_incoming(channel, src_addr, dest_addr_port):  # noqa: ARG001
        try:
            local_sock = socket.create_connection(
                (target_host, target_port), timeout=5.0
            )
        except OSError:
            try:
                channel.close()
            except OSError:
                pass
            return
        pump_to_channel = threading.Thread(
            target=_pump_socket_to_channel,
            args=(local_sock, channel),
            daemon=True,
        )
        pump_to_socket = threading.Thread(
            target=_pump_channel_to_socket,
            args=(channel, local_sock),
            daemon=True,
        )
        pump_to_channel.start()
        pump_to_socket.start()

    return _handle_incoming


def run_local(options: TunnelOptions, ready_callback=None) -> LocalForwardServer:
    """建立并启动 ``ssh -L`` 形态的本地转发,返回 server 句柄。

    Args:
        options: 解析后的选项。
        ready_callback: 隧道就绪后回调(后台模式下用来翻 spec.state)。

    Returns:
        已启动的 :class:`LocalForwardServer`,调用方负责 ``serve_forever()`` 或 ``stop()``。
    """
    ssh_client = build_ssh_client(options)
    _connect_ssh(ssh_client, options)
    # 探针:尝试一次 no-op 的 direct-tcpip channel open,用来在第一时间发现
    # ``AllowTcpForwarding no`` 的服务端配置,而不是等用户实际连一次才发现。
    transport = ssh_client.get_transport()
    if transport is None:
        raise TunnelError("SSH transport unexpectedly missing after connect")
    _probe_local_forwarding(transport)
    server = LocalForwardServer(
        ssh_client=ssh_client,
        bind_host=options.bind_host,
        bind_port=options.bind_port,
        target_host=options.target_host,
        target_port=options.target_port,
    )
    server.start()
    if ready_callback is not None:
        ready_callback()
    return server


def run_remote(
    options: TunnelOptions, ready_callback=None
) -> tuple["paramiko.SSHClient", "paramiko.Transport"]:
    """建立 ``ssh -R`` 形态的远端转发。

    Args:
        options: 解析后的选项。
        ready_callback: 隧道就绪后回调。

    Returns:
        ``(ssh_client, transport)`` 元组;调用方负责 ``ssh_client.close()``。
    """
    ssh_client = build_ssh_client(options)
    _connect_ssh(ssh_client, options)
    transport = ssh_client.get_transport()
    if transport is None:
        raise TunnelError("SSH transport unexpectedly missing after connect")
    _request_remote_forward(transport, options)
    if ready_callback is not None:
        ready_callback()
    return ssh_client, transport


def install_signal_handlers(stop_event: threading.Event) -> None:
    """注册 SIGTERM/SIGINT 处理,触发 :class:`threading.Event`。"""

    def _handle_signal(signum, frame):  # noqa: ARG001 - 标准 signal handler 签名
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            # 在子线程或非主线程中注册信号会失败,前台模式下可以忽略
            pass


def serve_foreground(options: TunnelOptions) -> None:
    """前台模式入口:建立隧道并阻塞到收到终止信号。"""
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    if options.reconnect:
        if options.direction == "local":
            _serve_local_with_reconnect(options, stop_event, log_fn=_foreground_log)
        else:
            _serve_remote_with_reconnect(options, stop_event, log_fn=_foreground_log)
        return
    if options.direction == "local":
        server = run_local(options)
        try:
            stop_event.wait()
        finally:
            server.stop()
        return
    ssh_client, transport = run_remote(options)
    try:
        stop_event.wait()
    finally:
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            ssh_client.close()
        except Exception:  # noqa: BLE001
            pass


def _foreground_log(message: str) -> None:
    """前台模式的日志回调:打到 stderr。"""
    import sys

    print(f"[reconnect] {message}", file=sys.stderr)


def _probe_local_forwarding(transport) -> None:
    """对 ``-L`` 做 no-op ``direct-tcpip`` 探针,失败抛带 ``AllowTcpForwarding`` 提示的 :class:`TunnelError`。"""
    pk = _require_paramiko()
    try:
        probe_channel = transport.open_channel(
            "direct-tcpip", ("127.0.0.1", 0), ("127.0.0.1", 0)
        )
    except (pk.ChannelException, pk.SSHException) as exc:
        raise _wrap_forwarding_denial("-L 本地", exc) from exc
    try:
        probe_channel.close()
    except OSError:
        pass


def _request_remote_forward(transport, options: "TunnelOptions") -> None:
    """对 ``-R`` 调 ``request_port_forward``,失败抛带 ``AllowTcpForwarding`` 提示的 :class:`TunnelError`。"""
    pk = _require_paramiko()
    reverse_handler = _make_reverse_handler(options.target_host, options.target_port)
    try:
        transport.request_port_forward(
            options.bind_host, options.bind_port, handler=reverse_handler
        )
    except pk.SSHException as exc:
        extra_hint = ""
        if options.bind_host not in ("127.0.0.1", "localhost", "::1"):
            extra_hint = (
                f"\n另外:你指定的 --bind-host {options.bind_host} 不是 loopback 地址,"
                "如果服务端 sshd_config 是 GatewayPorts no (默认) 或 "
                "GatewayPorts clientspecified,会拒绝绑定到非 loopback 地址。"
                "可以加 --bind-host 127.0.0.1 试试,或请服务端管理员放开 GatewayPorts。"
            )
        raise TunnelError(
            f"-R 远端转发被服务端拒绝: {exc}\n{_FORWARDING_DENIED_HINT}{extra_hint}"
        ) from exc


def _serve_local_with_reconnect(
    options: TunnelOptions,
    stop_event: threading.Event,
    log_fn,
    ready_callback=None,
) -> None:
    """``-L`` 模式的 reconnect 版:本地 listener 一直挂着,SSH 由 Reconnector 维护。"""
    server = LocalForwardServer(
        ssh_client=None,  # 占位;首次连上后由 on_ssh_ready 注入
        bind_host=options.bind_host,
        bind_port=options.bind_port,
        target_host=options.target_host,
        target_port=options.target_port,
    )
    server.start()
    first_ready = [False]

    def _on_ssh_ready(ssh_client, attempt):
        _require_paramiko()
        transport = ssh_client.get_transport()
        if transport is None:
            raise TunnelError("SSH transport unexpectedly missing after connect")
        # 探针:每次 (重)连后都验证,首次失败直接抛出(让 Reconnector 退出
        # 线程,上游拿到 first_attempt_exception)
        _probe_local_forwarding(transport)
        server.set_ssh_client(ssh_client)
        if not first_ready[0]:
            first_ready[0] = True
            if ready_callback is not None:
                ready_callback()

    reconn = _reconnect.Reconnector(
        options,
        on_ssh_ready=_on_ssh_ready,
        log=log_fn,
        max_attempts=options.max_reconnect,
    )
    # 把 Reconnector 的 stop_event 与外部 stop_event 串起来:任一被 set 都退出。
    bridge_stop_thread = threading.Thread(
        target=lambda: (
            stop_event.wait(),
            reconn.stop_event.set(),
        ),
        name="tunnel-stop-bridge",
        daemon=True,
    )
    bridge_stop_thread.start()
    reconn.start()
    try:
        # 等首次尝试结束
        reconn.wait_for_first_attempt(timeout=30.0)
        if reconn.first_attempt_exception is not None:
            raise reconn.first_attempt_exception
        # 阻塞到 stop
        reconn.stop_event.wait()
    finally:
        reconn.stop()
        reconn.join(timeout=2.0)
        server.stop()


def _serve_remote_with_reconnect(
    options: TunnelOptions,
    stop_event: threading.Event,
    log_fn,
    ready_callback=None,
) -> None:
    """``-R`` 模式的 reconnect 版:每次 (重)连后重新注册 ``request_port_forward``。"""
    first_ready = [False]

    def _on_ssh_ready(ssh_client, attempt):
        _require_paramiko()
        transport = ssh_client.get_transport()
        if transport is None:
            raise TunnelError("SSH transport unexpectedly missing after connect")
        _request_remote_forward(transport, options)
        if not first_ready[0]:
            first_ready[0] = True
            if ready_callback is not None:
                ready_callback()

    reconn = _reconnect.Reconnector(
        options,
        on_ssh_ready=_on_ssh_ready,
        log=log_fn,
        max_attempts=options.max_reconnect,
    )
    bridge_stop_thread = threading.Thread(
        target=lambda: (
            stop_event.wait(),
            reconn.stop_event.set(),
        ),
        name="tunnel-stop-bridge",
        daemon=True,
    )
    bridge_stop_thread.start()
    reconn.start()
    try:
        reconn.wait_for_first_attempt(timeout=30.0)
        if reconn.first_attempt_exception is not None:
            raise reconn.first_attempt_exception
        reconn.stop_event.wait()
    finally:
        reconn.stop()
        reconn.join(timeout=2.0)


def serve_daemon(spec_name: str) -> None:
    """后台守护入口:由父进程 exec 调用,自身不返回直到收到信号。

    Args:
        spec_name: 状态文件名(spec.name),用于在 ready 时翻 state。
    """
    spec = _state.load_spec(spec_name)
    options = TunnelOptions(
        direction=spec.direction,
        ssh_host=spec.ssh_host,
        ssh_user=spec.ssh_user,
        ssh_port=spec.ssh_port,
        ssh_key=spec.ssh_key,
        bind_host=spec.bind_host,
        bind_port=spec.bind_port,
        target_host=spec.target_host,
        target_port=spec.target_port,
        strict_host_key=spec.strict_host_key,
        name=spec.name,
        background=True,
        dry_run=False,
        reconnect=spec.reconnect,
        max_reconnect=spec.max_reconnect,
    )
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    if options.reconnect:

        def _daemon_log(message: str) -> None:
            print(f"[reconnect] {message}", file=sys.stderr, flush=True)

        def _on_daemon_ready() -> None:
            _state.mark_state(spec_name, "ready")

        ready_cb = _on_daemon_ready
        if options.direction == "local":
            _serve_local_with_reconnect(options, stop_event, _daemon_log, ready_cb)
        else:
            _serve_remote_with_reconnect(options, stop_event, _daemon_log, ready_cb)
        _state.mark_state(spec_name, "stopped")
        return
    if options.direction == "local":
        server = run_local(
            options, ready_callback=lambda: _state.mark_state(spec_name, "ready")
        )
        stop_event.wait()
        server.stop()
    else:
        ssh_client, transport = run_remote(
            options, ready_callback=lambda: _state.mark_state(spec_name, "ready")
        )
        stop_event.wait()
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            ssh_client.close()
        except Exception:  # noqa: BLE001
            pass
    _state.mark_state(spec_name, "stopped")


def cli_entrypoint(argv: list[str]) -> int:
    """供 :mod:`zata_ops.cli` 在 ``_tunnel-run`` 隐藏子命令中调用的入口。"""
    if len(argv) < 2:
        print("usage: zata-ops _tunnel-run <spec-name>", file=sys.stderr)
        return 2
    spec_name = argv[1]
    try:
        serve_daemon(spec_name)
    except TunnelError as exc:
        print(f"tunnel failed: {exc}", file=sys.stderr)
        return 1
    return 0
