"""Tests for ``zata-ops tunnel`` CLI surface.

Network-bound code paths (``serve_foreground`` / ``serve_daemon``) are not
exercised here; they need a real SSH server and are covered by manual
verification. These tests cover the CLI argument handling, dry-run plan
format, the paramiko-missing guard, and the AllowTcpForwarding /
GatewayPorts denial path.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import paramiko
import pytest
from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.tunnel import _runner


def test_root_help_lists_tunnel_subcommand() -> None:
    """The top-level ``--help`` mentions ``tunnel`` alongside the other subcommands."""
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "tunnel" in result.output


def test_tunnel_help_lists_user_facing_subcommands() -> None:
    """The hidden daemon subcommand ``run`` is not exposed in ``tunnel --help``."""
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "--help"])
    assert result.exit_code == 0
    for subcommand_name in ("open", "list", "status", "close"):
        assert subcommand_name in result.output
    # The hidden daemon subcommand should not be advertised
    assert "\b run " not in result.output


def _extract_dry_run_plan(stdout_text: str) -> dict:
    """Strip the Rich-marked header and parse the JSON plan that follows.

    CliRunner's stdout is plain text without ANSI sequences; the printed
    header is just ``zata-ops tunnel open --dry-run``, so we split on the
    first newline. Falls back to looking for the rich markup style used
    in the rendered version in case it ever leaks into stdout.
    """
    marker = "dry-run[/bold green]"
    if marker in stdout_text:
        return json.loads(stdout_text.split(marker, 1)[-1].strip())
    return json.loads(stdout_text.split("\n", 1)[1])


def test_open_dry_run_prints_equivalent_ssh_command() -> None:
    """``--dry-run`` produces a plan with the equivalent ``ssh -L`` argv line."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "local",
            "--ssh-host",
            "bastion.example.com",
            "--bind-port",
            "19000",
            "--target-port",
            "5432",
            "--target-host",
            "db.internal",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "zata-ops tunnel open --dry-run" in result.stdout
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["direction"] == "local"
    assert plan_payload["ssh"]["host"] == "bastion.example.com"
    assert plan_payload["listen"]["port"] == 19000
    assert plan_payload["target"]["port"] == 5432
    assert plan_payload["target"]["host"] == "db.internal"
    equivalent_cmd = plan_payload["equivalent_ssh_command"]
    assert "ssh" in equivalent_cmd
    assert "-L 127.0.0.1:19000:db.internal:5432" in equivalent_cmd
    assert "bastion.example.com" in equivalent_cmd


def test_open_dry_run_with_remote_direction_emits_minus_R() -> None:
    """``--direction remote`` flips the equivalent ssh argv to ``-R``."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "remote",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "8080",
            "--target-port",
            "3000",
            "--target-host",
            "127.0.0.1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["direction"] == "remote"
    assert "-R 127.0.0.1:8080:127.0.0.1:3000" in plan_payload["equivalent_ssh_command"]


def test_open_dry_run_does_not_require_ssh_user() -> None:
    """Dry-run resolves ``--ssh-user`` to ``$USER`` automatically."""
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
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["ssh"]["user"] != ""


def test_open_without_direction_drops_into_form_in_non_tty() -> None:
    """Omitting ``--direction`` triggers the form; non-TTY exits with code 1."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "9000",
            "--target-port",
            "5432",
        ],
    )
    # CliRunner is non-TTY; the form refuses and exits 1 with a friendly hint.
    assert result.exit_code == 1
    assert "TTY" in result.output or "--direction" in result.output


def test_open_rejects_unknown_direction() -> None:
    """``--direction sideways`` exits with code 2 and a friendly message."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "sideways",
            "--ssh-host",
            "bastion",
            "--bind-port",
            "9000",
            "--target-port",
            "5432",
        ],
    )
    assert result.exit_code == 2
    assert "sideways" in result.output


def test_list_with_no_state_prints_empty_marker(monkeypatch, tmp_path) -> None:
    """``tunnel list`` prints a friendly empty marker when no spec files exist."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "list"])
    assert result.exit_code == 0
    assert "no background tunnels" in result.output.lower()


def test_status_missing_spec_exits_1(monkeypatch, tmp_path) -> None:
    """``tunnel status`` for a non-existent name exits with code 1."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "status", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_close_missing_spec_exits_1(monkeypatch, tmp_path) -> None:
    """``tunnel close`` for a non-existent name exits with code 1."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "close", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_require_paramiko_raises_friendly_error_when_missing(monkeypatch) -> None:
    """``_require_paramiko`` raises :class:`TunnelError` with install hint."""
    import sys

    # 让 ``_paramiko_missing_hint`` 走 system Python 分支
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    monkeypatch.setattr(_runner, "paramiko", None)
    with pytest.raises(_runner.TunnelError) as exc_info:
        _runner._require_paramiko()
    error_message = str(exc_info.value)
    assert "paramiko" in error_message
    # 三种环境任一修复命令至少出现一种
    assert any(
        keyword in error_message
        for keyword in ("pip install", "uv tool install", "uv sync", "uv add")
    )


def _stub_ssh_client(monkeypatch, transport):
    """Patch ``build_ssh_client`` and ``_connect_ssh`` so ``run_local``/``run_remote`` skip real SSH."""
    monkeypatch.setattr(
        _runner,
        "build_ssh_client",
        lambda options: mock.MagicMock(get_transport=lambda: transport),
    )
    monkeypatch.setattr(_runner, "_connect_ssh", lambda client, options: None)


def _make_options(
    direction: str = "local", bind_host: str = "127.0.0.1"
) -> _runner.TunnelOptions:
    return _runner.TunnelOptions(
        direction=direction,
        ssh_host="bastion",
        ssh_user="ops",
        ssh_port=22,
        ssh_key=None,
        bind_host=bind_host,
        bind_port=9000,
        target_host="127.0.0.1",
        target_port=5432,
        strict_host_key=False,
        name="",
        background=False,
        dry_run=False,
    )


def test_connect_ssh_raises_auth_failure_with_troubleshooting_hints() -> None:
    """``AuthenticationException`` 抛带 3 个常见原因 + 调试命令的 :class:`TunnelError`。"""
    fake_client = mock.MagicMock()

    def _raise_auth(hostname, port=22, username=None, **kwargs):
        raise paramiko.AuthenticationException("bad password")

    fake_client.connect = _raise_auth
    options = _make_options()
    options_dict = options.__dict__.copy()
    options_dict["ssh_user"] = "root"
    options = _runner.TunnelOptions(**options_dict)
    with pytest.raises(_runner.TunnelError) as exc_info:
        _runner._connect_ssh(fake_client, options)
    message = str(exc_info.value)
    # 三条常见原因都要出现
    assert "PasswordAuthentication no" in message
    assert "PermitRootLogin" in message
    assert "ssh-copy-id" in message
    # 调试命令要包含用户和主机
    assert "ssh -o PreferredAuthentications=password" in message
    assert "root@bastion" in message


def test_run_local_raises_allow_tcp_forwarding_hint_on_probe_denial(
    monkeypatch,
) -> None:
    """``run_local`` probes a dummy ``direct-tcpip`` channel; denial surfaces the sshd_config hint."""
    fake_transport = mock.MagicMock()
    fake_transport.open_channel.side_effect = paramiko.ChannelException(
        1, b" administratively prohibited"
    )
    _stub_ssh_client(monkeypatch, fake_transport)
    options = _make_options(direction="local")
    with pytest.raises(_runner.TunnelError) as exc_info:
        _runner.run_local(options)
    message = str(exc_info.value)
    assert "AllowTcpForwarding" in message
    assert "sshd_config" in message


def test_run_remote_raises_allow_tcp_forwarding_hint_on_request_denial(
    monkeypatch,
) -> None:
    """``run_remote`` wraps ``request_port_forward`` denial with the sshd_config hint."""
    fake_transport = mock.MagicMock()
    fake_transport.request_port_forward.side_effect = paramiko.SSHException(
        "TCP forwarding request denied"
    )
    _stub_ssh_client(monkeypatch, fake_transport)
    options = _make_options(direction="remote", bind_host="127.0.0.1")
    with pytest.raises(_runner.TunnelError) as exc_info:
        _runner.run_remote(options)
    message = str(exc_info.value)
    assert "AllowTcpForwarding" in message
    assert "sshd_config" in message


def test_run_remote_also_mentions_gateway_ports_for_non_loopback_bind(
    monkeypatch,
) -> None:
    """A non-loopback ``--bind-host`` on -R also prompts the GatewayPorts concern."""
    fake_transport = mock.MagicMock()
    fake_transport.request_port_forward.side_effect = paramiko.SSHException(
        "TCP forwarding request denied"
    )
    _stub_ssh_client(monkeypatch, fake_transport)
    options = _make_options(direction="remote", bind_host="0.0.0.0")
    with pytest.raises(_runner.TunnelError) as exc_info:
        _runner.run_remote(options)
    message = str(exc_info.value)
    assert "GatewayPorts" in message
    assert "0.0.0.0" in message


def test_dry_run_plan_mentions_allow_tcp_forwarding() -> None:
    """The dry-run plan includes the sshd ``AllowTcpForwarding`` requirement."""
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
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    plan_payload = _extract_dry_run_plan(result.stdout)
    requirements = plan_payload["server_requirements"]
    assert any("AllowTcpForwarding" in req for req in requirements)


def test_dry_run_plan_mentions_gateway_ports_for_remote_non_loopback() -> None:
    """``-R`` with a non-loopback ``--bind-host`` adds a GatewayPorts note."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--direction",
            "remote",
            "--ssh-host",
            "bastion",
            "--bind-host",
            "0.0.0.0",
            "--bind-port",
            "8080",
            "--target-port",
            "3000",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    plan_payload = _extract_dry_run_plan(result.stdout)
    requirements = plan_payload["server_requirements"]
    assert any("GatewayPorts" in req for req in requirements)


def test_open_last_without_history_exits_1(monkeypatch, tmp_path) -> None:
    """``--last`` when no history exists exits with code 1 and prints a hint."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "--last"])
    assert result.exit_code == 1
    assert "历史" in result.output or "history" in result.output.lower()


def test_open_last_with_history_reuses_params(monkeypatch, tmp_path) -> None:
    """``--last`` reads history and produces the same dry-run plan."""
    from zata_ops.tunnel import _state

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _state.write_history(
        {
            "direction": "local",
            "ssh_host": "historical.bastion",
            "ssh_user": "history_user",
            "ssh_port": 2222,
            "ssh_key": "/home/hist/.ssh/id_rsa",
            "bind_host": "127.0.0.1",
            "bind_port": 19999,
            "target_host": "hist.db",
            "target_port": 3306,
            "strict_host_key": True,
            "background": False,
            "reconnect": True,
            "max_reconnect": 3,
        }
    )
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "--last", "--dry-run"])
    assert result.exit_code == 0, result.output
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["direction"] == "local"
    assert plan_payload["ssh"]["host"] == "historical.bastion"
    assert plan_payload["ssh"]["user"] == "history_user"
    assert plan_payload["ssh"]["port"] == 2222
    assert plan_payload["listen"]["port"] == 19999
    assert plan_payload["target"]["host"] == "hist.db"
    assert plan_payload["target"]["port"] == 3306


def test_open_last_with_override_merges(monkeypatch, tmp_path) -> None:
    """``--last`` combined with explicit flags merges history + overrides."""
    from zata_ops.tunnel import _state

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _state.write_history(
        {
            "direction": "local",
            "ssh_host": "historical.bastion",
            "ssh_user": "history_user",
            "ssh_port": 22,
            "ssh_key": None,
            "bind_host": "127.0.0.1",
            "bind_port": 19000,
            "target_host": "127.0.0.1",
            "target_port": 5432,
            "strict_host_key": False,
            "background": False,
            "reconnect": False,
            "max_reconnect": 0,
        }
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--last",
            "--bind-port",
            "20000",
            "--target-port",
            "3306",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["ssh"]["host"] == "historical.bastion"
    assert plan_payload["listen"]["port"] == 20000  # overridden
    assert plan_payload["target"]["port"] == 3306  # overridden


def test_open_from_missing_spec_exits_1(monkeypatch, tmp_path) -> None:
    """``--from`` with a non-existent name exits with code 1."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "--from", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_open_from_existing_spec_copies_params(monkeypatch, tmp_path) -> None:
    """``--from`` copies an existing background spec into the new command."""
    from zata_ops.tunnel import _state

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    existing = _state.TunnelSpec(
        name="existing",
        direction="remote",
        bind_host="0.0.0.0",
        bind_port=8080,
        target_host="127.0.0.1",
        target_port=3000,
        ssh_host="remote.bastion",
        ssh_user="remote_user",
        ssh_port=2222,
        ssh_key="/key",
        strict_host_key=True,
        pid=os.getpid(),
        log_path="",
        started_at="2026-06-09T16:00:00+00:00",
        state="ready",
        reconnect=True,
        max_reconnect=10,
    )
    _state.write_spec(existing)
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "--from", "existing", "--dry-run"])
    assert result.exit_code == 0, result.output
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["direction"] == "remote"
    assert plan_payload["ssh"]["host"] == "remote.bastion"
    assert plan_payload["ssh"]["user"] == "remote_user"
    assert plan_payload["ssh"]["port"] == 2222
    assert plan_payload["listen"]["port"] == 8080
    assert plan_payload["target"]["port"] == 3000


def test_open_from_with_override_merges(monkeypatch, tmp_path) -> None:
    """``--from`` combined with explicit flags merges spec + overrides."""
    from zata_ops.tunnel import _state

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    existing = _state.TunnelSpec(
        name="existing",
        direction="local",
        bind_host="127.0.0.1",
        bind_port=9000,
        target_host="db",
        target_port=5432,
        ssh_host="bastion",
        ssh_user="ops",
        ssh_port=22,
        ssh_key=None,
        strict_host_key=False,
        pid=os.getpid(),
        log_path="",
        started_at="",
        state="ready",
        reconnect=False,
        max_reconnect=0,
    )
    _state.write_spec(existing)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "--from",
            "existing",
            "--bind-port",
            "9999",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    plan_payload = _extract_dry_run_plan(result.stdout)
    assert plan_payload["ssh"]["host"] == "bastion"
    assert plan_payload["listen"]["port"] == 9999  # overridden


def test_open_last_and_from_mutual_exclusion() -> None:
    """``--last`` and ``--from`` together exit with code 2."""
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "--last", "--from", "x"])
    assert result.exit_code == 2
    assert "last" in result.output and "from" in result.output
