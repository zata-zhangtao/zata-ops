"""SSH 连接的自动重连管理。

:paramiko:`Transport` 在网络波动时不会自动重连;一旦底层 socket
中断,后续 ``open_channel`` / ``request_port_forward`` 都会失败。
本模块提供 :class:`Reconnector`,把 SSH 客户端的生命周期从
``run_local`` / ``run_remote`` 中抽出来,做到:

- 指数退避 1s → 2s → 4s → 8s → 16s → 30s(封顶),带 ±30% 抖动
- 保持 :class:`paramiko.Transport` 上的 ``keepalive`` 间隔 15s,
  让半死连接更快被发现
- 通过 :meth:`Reconnector.get_client` 提供线程安全的 client 拉取,
  供 :class:`LocalForwardServer` 在 -L 模式下热替换 transport
- 通过 :meth:`Reconnector.on_ssh_ready` 回调,把新 transport 交给
  ``request_port_forward`` 重新注册(-R 模式)
- 退出由 :attr:`Reconnector.stop_event` 触发,优雅停掉

注意 ``-L`` 在重连窗口内新连接会被直接拒绝(transport 暂时为 None),
已建立的连接保持到对端关 channel;``-R`` 在重连窗口内远端端口不服务,
远端会收到 connection refused。
"""

from __future__ import annotations

import random
import threading
from typing import Callable, Optional

from zata_ops.tunnel import _runner

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None  # type: ignore[assignment]


#: 默认 keepalive 间隔(秒);SSH 服务端通常 3 次未响应就主动断。
DEFAULT_KEEPALIVE_SECONDS: float = 15.0
#: 指数退避基础间隔(秒)。
DEFAULT_BASE_DELAY_SECONDS: float = 1.0
#: 指数退避封顶(秒)。
DEFAULT_MAX_DELAY_SECONDS: float = 30.0
#: 抖动相对延迟的比例(0.3 = ±30%)。
DEFAULT_JITTER_RATIO: float = 0.3
#: "成功保持连接"被视为稳定的最小时长(秒),超过后重置 attempt 计数。
STABLE_CONNECTION_SECONDS: float = 5.0


class Reconnector:
    """管理 SSH 客户端的生命周期,提供热替换能力。

    Args:
        options: 解析后的隧道选项。
        on_ssh_ready: 每次(重)连成功后回调,签名
            ``(ssh_client: paramiko.SSHClient) -> None``。
            典型用法:``-L`` 模式 ``set_ssh_client`` 到 LocalForwardServer;
            ``-R`` 模式 ``request_port_forward`` 重新注册。
        log: 日志输出回调,签名 ``(message: str) -> None``。
        max_attempts: 最大重试次数,0 表示无限。
        base_delay: 指数退避基础间隔。
        max_delay: 指数退避封顶。
        keepalive: SSH keepalive 间隔。
    """

    def __init__(
        self,
        options: "_runner.TunnelOptions",
        on_ssh_ready: Callable[["paramiko.SSHClient", int], None],
        log: Callable[[str], None],
        max_attempts: int = 0,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        keepalive: float = DEFAULT_KEEPALIVE_SECONDS,
    ) -> None:
        self._options = options
        self._on_ssh_ready = on_ssh_ready
        self._log = log
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._keepalive = keepalive
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._ssh_client: Optional["paramiko.SSHClient"] = None
        self._thread: Optional[threading.Thread] = None
        # ``on_ssh_ready`` 完成(或首次失败)的事件,外部可用 wait_for_first_attempt 等
        self._first_attempt_event = threading.Event()
        self._first_attempt_exception: Optional[Exception] = None
        self._first_attempt_succeeded = False

    @property
    def stop_event(self) -> threading.Event:
        """外部可 ``wait()`` 或 ``set()`` 的停止事件。"""
        return self._stop_event

    def get_client(self) -> Optional["paramiko.SSHClient"]:
        """线程安全地拉取当前 SSH 客户端,重连窗口内返回 None。"""
        with self._lock:
            return self._ssh_client

    def start(self) -> None:
        """启动后台重连线程(daemon=True)。"""
        self._thread = threading.Thread(
            target=self._run_loop, name="tunnel-reconnect", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """设置停止事件并关闭当前 client,线程会在下一个循环点退出。"""
        self._stop_event.set()
        with self._lock:
            current_client = self._ssh_client
            self._ssh_client = None
        if current_client is not None:
            try:
                current_client.close()
            except Exception:  # noqa: BLE001
                pass

    def join(self, timeout: Optional[float] = None) -> None:
        """等后台线程退出(可选超时)。"""
        if self._thread is not None:
            self._thread.join(timeout)

    def wait_for_first_attempt(self, timeout: Optional[float] = None) -> bool:
        """阻塞到第一次 (重)连尝试完成(成功或失败)。

        Args:
            timeout: 最大等待秒数;None 永久等待。

        Returns:
            True 表示第一次尝试已结束(成功或失败,查看 :attr:`first_attempt_exception`)。
        """
        return self._first_attempt_event.wait(timeout)

    @property
    def first_attempt_succeeded(self) -> bool:
        """``True`` 当第一次 (重)连尝试成功建立 SSH 连接。"""
        return self._first_attempt_succeeded

    @property
    def first_attempt_exception(self) -> Optional[Exception]:
        """第一次尝试的异常,首次成功后为 None。"""
        return self._first_attempt_exception

    def _run_loop(self) -> None:
        # 退避用 attempt:每次失败 +1,成功后清零;
        # 给 on_ssh_ready 的 attempt_number 单调递增,不重置。
        attempt = 0
        attempt_number = 0
        first_attempt_done = False
        while not self._stop_event.is_set():
            attempt += 1
            attempt_number += 1
            try:
                ssh_client = _runner.build_ssh_client(self._options)
                _runner._connect_ssh(ssh_client, self._options)
                transport = ssh_client.get_transport()
                if transport is not None and self._keepalive > 0:
                    try:
                        transport.set_keepalive(int(self._keepalive))
                    except Exception:  # noqa: BLE001
                        # keepalive 在某些服务端会抛 BadImplementation,忽略
                        pass
                with self._lock:
                    self._ssh_client = ssh_client
                if attempt_number > 1:
                    self._log(f"reconnected on attempt {attempt_number}")
                else:
                    self._log(
                        f"SSH connected to {self._options.ssh_host}:{self._options.ssh_port}"
                    )
                # on_ssh_ready 收到 (client, attempt_number);若抛错,本次视为失败。
                self._on_ssh_ready(ssh_client, attempt_number)
                if not first_attempt_done:
                    self._first_attempt_succeeded = True
                    self._first_attempt_event.set()
                    first_attempt_done = True
                attempt = 0  # 连接成功,重置退避计数器
                if self._wait_for_death(transport):
                    return
                with self._lock:
                    self._ssh_client = None
                try:
                    ssh_client.close()
                except Exception:  # noqa: BLE001
                    pass
                if self._stop_event.is_set():
                    return
                self._log("SSH connection lost, scheduling reconnect")
            except _runner.TunnelError as exc:
                # 业务异常:首次失败直接退出(reconnect 模式重试不会救得了配错的服务器);
                # 重连窗口内由调用方决定。
                if not first_attempt_done:
                    self._first_attempt_exception = exc
                    self._first_attempt_event.set()
                    first_attempt_done = True
                    return
                if self._stop_event.is_set():
                    return
                if self._max_attempts and attempt > self._max_attempts:
                    self._log(
                        f"max reconnect attempts ({self._max_attempts}) reached, giving up"
                    )
                    return
                sleep_seconds = self._backoff_seconds(attempt)
                self._log(
                    f"reconnect attempt {attempt} failed: {exc}; "
                    f"sleeping {sleep_seconds:.1f}s before next try"
                )
                if self._stop_event.wait(timeout=sleep_seconds):
                    return
            except Exception as exc:  # noqa: BLE001
                # 通用异常:首次失败也记下,让上层决定。
                if not first_attempt_done:
                    self._first_attempt_exception = exc
                    self._first_attempt_event.set()
                    first_attempt_done = True
                    return
                if self._stop_event.is_set():
                    return
                if self._max_attempts and attempt > self._max_attempts:
                    self._log(
                        f"max reconnect attempts ({self._max_attempts}) reached, giving up"
                    )
                    return
                sleep_seconds = self._backoff_seconds(attempt)
                self._log(
                    f"reconnect attempt {attempt} failed: {exc}; "
                    f"sleeping {sleep_seconds:.1f}s before next try"
                )
                if self._stop_event.wait(timeout=sleep_seconds):
                    return
        self._log("reconnect loop stopped")

    def _wait_for_death(self, transport) -> bool:
        """阻塞到 transport 死亡或 stop_event 被 set。返回 True 表示后者。"""
        if transport is None:
            return self._stop_event.wait(timeout=1.0)
        # 简单轮询:每 1s 检查 is_active。配合 keepalive(15s)可以在最多
        # ~30s 内发现半死连接。
        while not self._stop_event.is_set():
            try:
                if not transport.is_active():
                    return False
            except Exception:  # noqa: BLE001
                return False
            if self._stop_event.wait(timeout=1.0):
                return True
        return True

    def _backoff_seconds(self, attempt: int) -> float:
        """根据 attempt 索引计算下一次重试延迟(带 jitter)。"""
        capped_delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)))
        jitter = random.uniform(0, DEFAULT_JITTER_RATIO * capped_delay)
        return capped_delay + jitter
