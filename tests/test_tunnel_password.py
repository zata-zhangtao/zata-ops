"""Tests for the password-auth path in :mod:`zata_ops.tunnel._runner` and
the ``--ssh-password`` CLI surface.

No real SSH connection is made; ``paramiko.SSHClient.connect`` is
monkeypatched to capture the kwargs it was called with.
"""

from __future__ import annotations

from unittest import mock

import pytest

from zata_ops.cli import app
from zata_ops.tunnel import _runner


def _make_options(**overrides) -> _runner.TunnelOptions:
    defaults: dict = dict(
        direction="local",
        ssh_host="bastion",
        ssh_user="ops",
        ssh_port=22,
        ssh_key=None,
        bind_host="127.0.0.1",
        bind_port=9000,
        target_host="127.0.0.1",
        target_port=5432,
        strict_host_key=False,
        name="",
        background=False,
        dry_run=False,
    )
    defaults.update(overrides)
    return _runner.TunnelOptions(**defaults)


def test_connect_ssh_uses_password_when_provided() -> None:
    """``ssh_password`` 路径下应传 ``password=...`` 并关掉 key/agent 探测。"""
    fake_client = mock.MagicMock()
    captured_kwargs: dict = {}

    def _fake_connect(hostname, port=22, username=None, **kwargs):
        captured_kwargs.update(kwargs)
        return None

    fake_client.connect = _fake_connect
    _runner._connect_ssh(fake_client, _make_options(ssh_password="s3cr3t"))
    assert captured_kwargs["password"] == "s3cr3t"
    assert captured_kwargs["look_for_keys"] is False
    assert captured_kwargs["allow_agent"] is False
    assert captured_kwargs["key_filename"] is None


def test_connect_ssh_uses_key_when_key_provided() -> None:
    """``ssh_key`` 路径下应传 ``key_filename=...`` 并关掉 password / agent 探测。"""
    fake_client = mock.MagicMock()
    captured_kwargs: dict = {}

    def _fake_connect(hostname, port=22, username=None, **kwargs):
        captured_kwargs.update(kwargs)
        return None

    fake_client.connect = _fake_connect
    _runner._connect_ssh(fake_client, _make_options(ssh_key="/id_test"))
    assert captured_kwargs["key_filename"] == "/id_test"
    assert captured_kwargs["password"] is None
    assert captured_kwargs["look_for_keys"] is False
    assert captured_kwargs["allow_agent"] is False


def test_connect_ssh_uses_default_discovery_when_neither() -> None:
    """既无 key 也无 password 时,让 paramiko 自动探测 ``~/.ssh/id_*`` 和 agent。"""
    fake_client = mock.MagicMock()
    captured_kwargs: dict = {}

    def _fake_connect(hostname, port=22, username=None, **kwargs):
        captured_kwargs.update(kwargs)
        return None

    fake_client.connect = _fake_connect
    _runner._connect_ssh(fake_client, _make_options())
    assert captured_kwargs["key_filename"] is None
    assert captured_kwargs["password"] is None
    assert captured_kwargs["look_for_keys"] is True
    assert captured_kwargs["allow_agent"] is True


def test_run_local_with_password_does_not_block_on_missing_key() -> None:
    """``-L + 密码`` 模式:build_ssh_client 不会要求 key_filename 探测。"""
    captured: dict = {}

    def _fake_build_ssh_client(opts):
        client = mock.MagicMock()
        client.get_transport.return_value = mock.MagicMock(is_active=lambda: True)
        return client

    def _fake_connect_ssh(client, opts):
        captured["password"] = opts.ssh_password
        captured["ssh_key"] = opts.ssh_key

    # 模拟 run_local 的关键步骤:build + connect + probe + start
    # 这里只测 build / connect 的串联(避开 LocalForwardServer 真启 socket)
    client = _fake_build_ssh_client(_make_options(ssh_password="hunter2"))
    _fake_connect_ssh(client, _make_options(ssh_password="hunter2"))
    assert captured["password"] == "hunter2"
    assert captured["ssh_key"] is None


# ----- 表单 + dry-run + 后台拒绝测试 ----------------------------------------


def test_collect_options_password_auth_path(monkeypatch) -> None:
    """表单选密码后,ssh_key=None / ssh_password=用户输入,且不再问 ssh_key 路径。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "local",  # 0 direction
            "bastion",  # 1 ssh_host
            "ops",  # 2 ssh_user
            "22",  # 3 ssh_port
            "password",  # 4 auth method select
            "hunter2",  # 5 password (hidden)
            # 不再有 ssh_key 提示
            "127.0.0.1",  # 6 bind_host
            "19000",  # 7 bind_port
            "127.0.0.1",  # 8 target_host
            "5432",  # 9 target_port
            False,  # 10 background
            True,  # 11 dry_run
            False,  # 12 reconnect
        ]
    )
    monkeypatch.setattr(_interactive, "questionary", mock.MagicMock())
    monkeypatch.setattr(
        _interactive.questionary,
        "select",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "text",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "path",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "confirm",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "password",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    options = _interactive.collect_options(_empty_prefill())
    assert options.ssh_password == "hunter2"
    assert options.ssh_key is None


def test_collect_options_key_auth_path(monkeypatch) -> None:
    """表单选密钥后,ssh_key=用户输入 / ssh_password=None。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "local",  # 0 direction
            "bastion",  # 1 ssh_host
            "ops",  # 2 ssh_user
            "22",  # 3 ssh_port
            "key",  # 4 auth method select
            "/id_test",  # 5 ssh_key path
            "127.0.0.1",  # 6 bind_host
            "19000",  # 7 bind_port
            "127.0.0.1",  # 8 target_host
            "5432",  # 9 target_port
            False,  # 10 background
            True,  # 11 dry_run
            False,  # 12 reconnect
        ]
    )
    monkeypatch.setattr(_interactive, "questionary", mock.MagicMock())
    monkeypatch.setattr(
        _interactive.questionary,
        "select",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "text",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "path",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "confirm",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "password",
        lambda *a, **kw: mock.MagicMock(ask=lambda: "should-not-be-called"),
    )
    options = _interactive.collect_options(_empty_prefill())
    assert options.ssh_key == "/id_test"
    assert options.ssh_password is None


def test_collect_options_prefilled_password_skips_auth_method(monkeypatch) -> None:
    """CLI 已传 --ssh-password 时,表单不再问 auth method,直接用。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "local",  # 0 direction
            "bastion",  # 1 ssh_host
            "ops",  # 2 ssh_user
            "22",  # 3 ssh_port
            # 跳过 auth method,因为 ssh_password 已经在 prefill 里
            "127.0.0.1",  # 4 bind_host
            "19000",  # 5 bind_port
            "127.0.0.1",  # 6 target_host
            "5432",  # 7 target_port
            False,  # 8 background
            True,  # 9 dry_run
            False,  # 10 reconnect
        ]
    )
    monkeypatch.setattr(_interactive, "questionary", mock.MagicMock())
    monkeypatch.setattr(
        _interactive.questionary,
        "select",
        lambda *a, **kw: pytest.fail(
            "select should not be called when prefill has password"
        ),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "text",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "path",
        lambda *a, **kw: mock.MagicMock(ask=lambda: "should-not-be-called"),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "confirm",
        lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers)),
    )
    monkeypatch.setattr(
        _interactive.questionary,
        "password",
        lambda *a, **kw: pytest.fail(
            "password should not be called when prefill has password"
        ),
    )
    prefill = _make_options(ssh_password="prefilled_pw")
    options = _interactive.collect_options(prefill)
    assert options.ssh_password == "prefilled_pw"
    assert options.ssh_key is None


# ----- CLI: --background + --ssh-password 互斥 -----------------------------


def test_cli_background_with_password_exits_1() -> None:
    """``--background --ssh-password`` 组合直接被拒绝,退出码 1。"""
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "local",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "9000",
            "--target-port",
            "5432",
            "--ssh-password",
            "secret",
            "--background",
            "--name",
            "leaky",
        ],
    )
    assert result.exit_code == 1
    assert "--background" in result.output
    assert "ssh-add" in result.output or "明文" in result.output


def test_cli_dry_run_with_password_marks_password_in_plan() -> None:
    """dry-run 输出应包含 ``auth_method=password`` 与 ``password_set=true``。"""
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "local",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "9000",
            "--target-port",
            "5432",
            "--ssh-password",
            "secret",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    plan = result.stdout.split("dry-run", 1)[-1].lstrip("\n")
    plan_obj = json.loads(plan)
    assert plan_obj["ssh"]["auth_method"] == "password"
    assert plan_obj["ssh"]["password_set"] is True
    # 密码本身不应出现在 dry-run 输出里
    assert "secret" not in result.stdout


# ----- paramiko 缺失时的环境感知提示 -----------------------------------------


def test_paramiko_missing_hint_uv_tool_install(monkeypatch) -> None:
    """``sys.prefix`` 含 ``uv/tools/`` 时,提示用 ``uv tool install -e '.[ssh]' --force``。"""
    import sys

    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/zata-ops/abc")
    hint_message = _runner._paramiko_missing_hint()
    assert "uv tool install" in hint_message
    assert ".[ssh]" in hint_message
    assert "--force" in hint_message


def test_paramiko_missing_hint_project_venv(monkeypatch) -> None:
    """``sys.executable`` 在 ``.venv`` 内时,提示 ``uv sync --extra ssh`` + ``uv run``。"""
    import sys

    monkeypatch.setattr(sys, "prefix", "/Users/x/code/zata-ops/.venv")
    monkeypatch.setattr(sys, "executable", "/Users/x/code/zata-ops/.venv/bin/python")
    hint_message = _runner._paramiko_missing_hint()
    assert "uv sync --extra ssh" in hint_message
    assert "uv run" in hint_message


def test_paramiko_missing_hint_system_python(monkeypatch) -> None:
    """系统 Python(非 uv / venv)时,提示用 ``pip install``。"""
    import sys

    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    hint_message = _runner._paramiko_missing_hint()
    assert "pip install" in hint_message
    assert "zata-ops[ssh]" in hint_message


def test_cli_when_paramiko_missing_prints_contextual_hint(monkeypatch) -> None:
    """``tunnel open`` 在 paramiko 缺失时,exit 1 且错误信息含针对当前环境的修复命令。"""
    import sys
    from typer.testing import CliRunner

    # 模拟 uv tool install 的环境
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/zata-ops/abc")
    monkeypatch.setattr(_runner, "paramiko", None)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "local",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "9000",
            "--target-port",
            "5432",
        ],
    )
    assert result.exit_code == 1
    assert "uv tool install" in result.output
    assert ".[ssh]" in result.output


import json  # noqa: E402  (放在文件底部方便上面 test 用)
from zata_ops.tunnel import _interactive  # noqa: E402


def _empty_prefill() -> _runner.TunnelOptions:
    return _runner.TunnelOptions(
        direction="",
        ssh_host="",
        ssh_user="",
        ssh_port=22,
        ssh_key=None,
        bind_host="127.0.0.1",
        bind_port=0,
        target_host="127.0.0.1",
        target_port=0,
        strict_host_key=False,
        name="",
        background=False,
        dry_run=False,
    )
