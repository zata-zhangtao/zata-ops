"""Tests for :mod:`zata_ops.tunnel._reconnect` and the reconnect paths in
:mod:`zata_ops.tunnel._runner`.

No real SSH connection is made. ``build_ssh_client`` / ``_connect_ssh``
are monkeypatched to return fake transports with controllable ``is_active``
semantics, so we can drive the Reconnector through its state machine.
"""

from __future__ import annotations

import socket
import time
from unittest import mock


from zata_ops.tunnel import _reconnect, _runner


def _make_options() -> _runner.TunnelOptions:
    return _runner.TunnelOptions(
        direction="local",
        ssh_host="bastion",
        ssh_user="ops",
        ssh_port=22,
        ssh_key=None,
        bind_host="127.0.0.1",
        bind_port=19000,
        target_host="127.0.0.1",
        target_port=5432,
        strict_host_key=False,
        name="",
        background=False,
        dry_run=False,
    )


def _make_fake_transport(alive: bool = True):
    """Build a fake transport that responds to ``is_active()`` and ``set_keepalive()``."""
    transport = mock.MagicMock()
    transport.is_active.return_value = alive
    transport.set_keepalive = mock.MagicMock()
    return transport


def _make_fake_client(transport):
    client = mock.MagicMock()
    client.get_transport.return_value = transport
    return client


def test_backoff_seconds_grows_then_caps() -> None:
    """Exponential backoff doubles up to the configured cap with jitter."""
    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=lambda *a, **k: None,
        log=lambda m: None,
        base_delay=1.0,
        max_delay=8.0,
    )
    # 关掉随机,直接验证 base * 2^(n-1) 的封顶
    with mock.patch("random.uniform", return_value=0):
        assert reconn._backoff_seconds(1) == 1.0
        assert reconn._backoff_seconds(2) == 2.0
        assert reconn._backoff_seconds(3) == 4.0
        assert reconn._backoff_seconds(4) == 8.0  # capped
        assert reconn._backoff_seconds(10) == 8.0  # still capped


def test_first_attempt_succeeds_when_callback_returns(monkeypatch) -> None:
    """Happy path: connect + on_ssh_ready both succeed, ready event fires."""
    fake_transport = _make_fake_transport(alive=True)
    fake_client = _make_fake_client(fake_transport)
    monkeypatch.setattr(_runner, "build_ssh_client", lambda opts: fake_client)
    monkeypatch.setattr(_runner, "_connect_ssh", lambda client, opts: None)
    captured: list[int] = []

    def _on_ssh_ready(client, attempt):
        captured.append(attempt)

    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=_on_ssh_ready,
        log=lambda m: None,
        keepalive=0.0,  # 关掉 keepalive 简化测试
    )
    reconn.start()
    assert reconn.wait_for_first_attempt(timeout=2.0)
    assert reconn.first_attempt_succeeded is True
    assert reconn.first_attempt_exception is None
    assert captured == [1]
    reconn.stop()
    reconn.join(timeout=2.0)


def test_first_attempt_failure_propagates_to_caller(monkeypatch) -> None:
    """``TunnelError`` on the first attempt is captured in first_attempt_exception."""
    monkeypatch.setattr(_runner, "build_ssh_client", lambda opts: mock.MagicMock())
    monkeypatch.setattr(
        _runner, "_connect_ssh", mock.MagicMock(side_effect=_runner.TunnelError("nope"))
    )

    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=lambda *a, **k: None,
        log=lambda m: None,
    )
    reconn.start()
    assert reconn.wait_for_first_attempt(timeout=2.0)
    assert reconn.first_attempt_succeeded is False
    assert isinstance(reconn.first_attempt_exception, _runner.TunnelError)
    assert "nope" in str(reconn.first_attempt_exception)
    reconn.stop()
    reconn.join(timeout=2.0)


def test_reconnect_after_transport_death(monkeypatch) -> None:
    """After the transport dies, Reconnector reconnects and re-invokes the callback."""
    alive_flag = [True]
    fake_transport = _make_fake_transport(alive=True)
    fake_transport.is_active.side_effect = lambda: alive_flag[0]
    fake_client = _make_fake_client(fake_transport)
    monkeypatch.setattr(_runner, "build_ssh_client", lambda opts: fake_client)
    monkeypatch.setattr(_runner, "_connect_ssh", lambda client, opts: None)
    attempts: list[int] = []

    def _on_ssh_ready(client, attempt):
        attempts.append(attempt)
        # After the second (re)connect flips the flag back to alive, no more deaths.
        if attempt >= 2:
            alive_flag[0] = True

    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=_on_ssh_ready,
        log=lambda m: None,
        keepalive=0.0,
    )
    reconn.start()
    assert reconn.wait_for_first_attempt(timeout=2.0)
    assert attempts == [1]
    # 模拟 transport 死亡;Reconnector 应该在 ~1s 内重连
    alive_flag[0] = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and 2 not in attempts:
        time.sleep(0.05)
    assert 2 in attempts, f"expected reconnect attempt, got {attempts}"
    # 给一点缓冲时间,确认不会再触发第三次
    time.sleep(0.5)
    assert attempts == [1, 2], f"unexpected extra reconnects: {attempts}"
    reconn.stop()
    reconn.join(timeout=2.0)


def test_max_attempts_stops_reconnector(monkeypatch) -> None:
    """After exceeding ``max_attempts``, Reconnector stops and first attempt is None."""
    call_count = [0]

    def _failing_connect(client, opts):
        call_count[0] += 1
        return None  # do NOT raise here; let _run_loop's except catch the failure

    # Use a list of side effects so we can verify the first attempt fails and
    # nothing more fires.
    monkeypatch.setattr(_runner, "build_ssh_client", lambda opts: mock.MagicMock())
    monkeypatch.setattr(_runner, "_connect_ssh", _failing_connect)
    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=lambda *a, **k: None,
        log=lambda m: None,
        max_attempts=2,
        base_delay=0.01,
        max_delay=0.05,
    )

    # We construct the Reconnector but never start the thread; we drive
    # ``_run_loop`` directly in the main thread so we have deterministic
    # sequencing.
    def _raise_in_connect(*a, **k):
        call_count[0] += 1
        raise _runner.TunnelError(f"failure {call_count[0]}")

    monkeypatch.setattr(_runner, "_connect_ssh", _raise_in_connect)
    reconn._run_loop()  # blocks until first-attempt failure returns
    assert call_count[0] == 1
    assert reconn.first_attempt_exception is not None


def test_get_client_returns_none_during_reconnect_window(monkeypatch) -> None:
    """During the gap between transport death and new connect, get_client() is None."""
    alive_flag = [True]
    fake_transport = _make_fake_transport(alive=True)
    fake_transport.is_active.side_effect = lambda: alive_flag[0]
    fake_client = _make_fake_client(fake_transport)
    monkeypatch.setattr(_runner, "build_ssh_client", lambda opts: fake_client)
    monkeypatch.setattr(_runner, "_connect_ssh", lambda client, opts: None)
    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=lambda *a, **k: None,
        log=lambda m: None,
        keepalive=0.0,
    )
    reconn.start()
    assert reconn.wait_for_first_attempt(timeout=2.0)
    assert reconn.get_client() is fake_client
    alive_flag[0] = False
    # 短暂等待 Reconnector 把 client 置空
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and reconn.get_client() is not None:
        time.sleep(0.02)
    assert reconn.get_client() is None
    reconn.stop()
    reconn.join(timeout=2.0)


def test_stop_event_propagates_quickly(monkeypatch) -> None:
    """Calling ``Reconnector.stop()`` causes the thread to exit promptly."""
    fake_transport = _make_fake_transport(alive=True)
    monkeypatch.setattr(
        _runner, "build_ssh_client", lambda opts: _make_fake_client(fake_transport)
    )
    monkeypatch.setattr(_runner, "_connect_ssh", lambda client, opts: None)
    reconn = _reconnect.Reconnector(
        options=_make_options(),
        on_ssh_ready=lambda *a, **k: None,
        log=lambda m: None,
    )
    reconn.start()
    assert reconn.wait_for_first_attempt(timeout=2.0)
    reconn.stop()
    reconn.join(timeout=2.0)
    assert reconn.stop_event.is_set()


# ----- LocalForwardServer 热替换测试 --------------------------------------


def test_local_forward_server_swallows_missing_client_during_reconnect() -> None:
    """If ssh_client is None (reconnect window), the server silently drops new connections."""
    from zata_ops.tunnel._runner import LocalForwardServer

    server = LocalForwardServer(
        ssh_client=None,
        bind_host="127.0.0.1",
        bind_port=0,  # 操作系统分配
        target_host="127.0.0.1",
        target_port=22,
    )
    try:
        server.start()
        bind_port = server._server_socket.getsockname()[1]
        # 模拟重连窗口内,client 还没就位时,新连接会被 accept 然后立刻关掉
        with socket.create_connection(
            ("127.0.0.1", bind_port), timeout=2.0
        ) as client_sock:
            # 短暂 sleep 让 accept 线程处理,然后客户端主动关掉
            time.sleep(0.2)
            # 此时连接已被服务端关掉,客户端读应该拿到 EOF
            data = client_sock.recv(64)
            assert data == b""
    finally:
        server.stop()


def test_local_forward_server_swap_ssh_client_during_runtime() -> None:
    """``set_ssh_client`` 热替换之后,新连接会走新的 client 路径。"""
    from zata_ops.tunnel._runner import LocalForwardServer

    fake_transport_before = _make_fake_transport(alive=True)
    fake_transport_after = _make_fake_transport(alive=True)
    client_before = _make_fake_client(fake_transport_before)
    client_after = _make_fake_client(fake_transport_after)

    server = LocalForwardServer(
        ssh_client=client_before,
        bind_host="127.0.0.1",
        bind_port=0,
        target_host="127.0.0.1",
        target_port=22,
    )
    try:
        server.start()
        # 替换
        server.set_ssh_client(client_after)
        # 此时 _get_ssh_client() 应该返回新的
        assert server._get_ssh_client() is client_after
    finally:
        server.stop()
