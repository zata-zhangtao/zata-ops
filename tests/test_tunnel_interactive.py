"""Tests for :mod:`zata_ops.tunnel._interactive`.

The form function is mocked at the ``questionary`` level so the tests
do not need a real TTY. We exercise both the happy path (local + remote
+ background) and the failure paths (non-TTY, Ctrl+C, missing ssh_host).
"""

from __future__ import annotations

from unittest import mock

import pytest

from zata_ops.tunnel import _interactive, _runner


def _empty_prefill() -> _runner.TunnelOptions:
    """Build a :class:`TunnelOptions` that triggers the form to ask everything."""
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


def test_collect_options_local_foreground_happy_path(monkeypatch) -> None:
    """A full ``local`` form run produces the expected :class:`TunnelOptions`."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "local",  # 0 select direction
            "bastion.example.com",  # 1 text ssh_host
            "ops",  # 2 text ssh_user
            "22",  # 3 text ssh_port (int port prompt, returns immediately)
            "key",  # 4 select auth method
            "/home/ops/.ssh/id_ed25519",  # 5 path ssh_key
            "127.0.0.1",  # 6 text bind_host
            "19000",  # 7 text bind_port (int port prompt)
            "127.0.0.1",  # 8 text target_host
            "5432",  # 9 text target_port (int port prompt)
            False,  # 10 confirm background
            True,  # 11 confirm dry_run
            False,  # 12 confirm reconnect
        ]
    )
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
    options = _interactive.collect_options(_empty_prefill())
    assert options.direction == "local"
    assert options.ssh_host == "bastion.example.com"
    assert options.ssh_user == "ops"
    assert options.ssh_port == 22
    assert options.ssh_key == "/home/ops/.ssh/id_ed25519"
    assert options.ssh_password is None
    assert options.bind_port == 19000
    assert options.target_port == 5432
    assert options.background is False
    assert options.dry_run is True
    assert options.reconnect is False


def test_collect_options_remote_background_with_name(monkeypatch) -> None:
    """``remote`` + background + named instance flows through the form correctly."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "remote",  # 0 direction
            "bastion",  # 1 ssh_host
            "ops",  # 2 ssh_user
            "2222",  # 3 ssh_port
            "key",  # 4 select auth method
            "",  # 5 ssh_key (empty → None)
            "127.0.0.1",  # 6 bind_host
            "8080",  # 7 bind_port
            "127.0.0.1",  # 8 target_host
            "3000",  # 9 target_port
            True,  # 10 background
            "dev-server",  # 11 name
            False,  # 12 dry_run
            True,  # 13 confirm reconnect
        ]
    )
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
    options = _interactive.collect_options(_empty_prefill())
    assert options.direction == "remote"
    assert options.ssh_port == 2222
    assert options.bind_port == 8080
    assert options.target_port == 3000
    assert options.background is True
    assert options.name == "dev-server"
    assert options.dry_run is False
    assert options.reconnect is True
    assert options.ssh_key is None
    assert options.ssh_password is None


def test_collect_options_rejects_non_tty() -> None:
    """Running outside a TTY raises :class:`TunnelError` with install hint."""
    with mock.patch("sys.stdin.isatty", return_value=False), mock.patch(
        "sys.stdout.isatty", return_value=True
    ):
        with pytest.raises(_runner.TunnelError) as exc_info:
            _interactive.collect_options(_empty_prefill())
        message = str(exc_info.value)
        assert "TTY" in message
        assert "--direction" in message


def test_collect_options_propagates_ctrl_c(monkeypatch) -> None:
    """``Ctrl+C`` during the form surfaces as :class:`FormCancelledError`."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    # First prompt returns None to simulate Ctrl+C / EOF
    monkeypatch.setattr(
        _interactive.questionary,
        "select",
        lambda *a, **kw: mock.MagicMock(ask=lambda: None),
    )
    with pytest.raises(_interactive.FormCancelledError):
        _interactive.collect_options(_empty_prefill())


def test_collect_options_rejects_empty_ssh_host(monkeypatch) -> None:
    """Pressing enter on ``--ssh-host`` (or entering blank) raises :class:`TunnelError`."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    answers = iter(
        [
            "local",  # 0 direction
            "",  # 1 ssh_host empty
        ]
    )
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
    with pytest.raises(_runner.TunnelError) as exc_info:
        _interactive.collect_options(_empty_prefill())
    assert "ssh-host" in str(exc_info.value)


def test_collect_options_validates_port_range(monkeypatch) -> None:
    """Out-of-range port answers are caught by questionary's ``validate`` callback.

    We directly call :func:`_ask_int_port` with a mocked questionary that
    captures validators, then assert at least one rejects ``"70000"``.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    captured_validators = []

    def fake_text(prompt, *args, **kwargs):
        validator = kwargs.get("validate")
        if validator is not None:
            captured_validators.append(validator)
        return mock.MagicMock(ask=lambda: "22")

    monkeypatch.setattr(_interactive.questionary, "text", fake_text)
    # Trigger the int port prompt
    result = _interactive._ask_int_port("测试端口?", 22)
    assert result == 22
    # At least one captured validator must reject "70000"
    assert any(
        validator("70000") is not True and "端口" in str(validator("70000"))
        for validator in captured_validators
    )


def test_collect_options_uses_prefill_for_partial_input(monkeypatch) -> None:
    """If the caller already supplied some fields, the form pre-fills them.

    We mock the underlying ``_ask_*`` helpers to return the prefill values,
    simulating "user pressed Enter for every default". The resulting options
    must round-trip the prefill.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    prefill = _runner.TunnelOptions(
        direction="local",
        ssh_host="known.example.com",
        ssh_user="ops",
        ssh_port=2222,
        ssh_key="/id_special",
        bind_host="127.0.0.1",
        bind_port=19000,
        target_host="127.0.0.1",
        target_port=5432,
        strict_host_key=False,
        name="",
        background=False,
        dry_run=False,
    )
    # Replace each ask helper so the form short-circuits to prefill values.
    monkeypatch.setattr(_interactive, "_ask_direction", lambda: prefill.direction)
    monkeypatch.setattr(_interactive, "_ask_ssh_user", lambda: prefill.ssh_user)
    monkeypatch.setattr(_interactive, "_ask_optional_ssh_key", lambda: prefill.ssh_key)
    monkeypatch.setattr(_interactive, "_ask_text", lambda prompt, default: default)
    monkeypatch.setattr(_interactive, "_ask_int_port", lambda prompt, default: default)
    monkeypatch.setattr(_interactive, "_ask_confirm", lambda prompt, default: default)
    options = _interactive.collect_options(prefill)
    assert options.ssh_host == "known.example.com"
    assert options.ssh_user == "ops"
    assert options.ssh_port == 2222
    assert options.ssh_key == "/id_special"
    assert options.bind_port == 19000
    assert options.target_port == 5432
    assert options.background is False
    # The form's dry-run confirm defaults to True regardless of prefill; the
    # mocked _ask_confirm returns the default, so we expect True here.
    assert options.dry_run is True


def test_cli_open_no_args_falls_back_to_form_in_non_tty(monkeypatch) -> None:
    """``zata-ops tunnel open`` with no args exits 1 in non-TTY and prints hint."""
    from typer.testing import CliRunner
    from zata_ops.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open"])
    # The form raises TunnelError("not in TTY"), which cli.py turns into exit 1
    assert result.exit_code == 1
    assert "TTY" in result.output or "--direction" in result.output


def test_collect_options_uses_direction_aware_prompts(monkeypatch) -> None:
    """-L 与 -R 的 bind/target 提示文案应该显著不同,避免用户搞混。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    captured_prompts: list[str] = []
    current_answers = [None]  # mutable so the factory can swap iters

    def _capture_text_factory():
        def _factory(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return mock.MagicMock(ask=lambda: next(current_answers[0]))

        return _factory

    def _configure_mocks(direction: str, answers_iter):
        current_answers[0] = answers_iter
        monkeypatch.setattr(_interactive, "questionary", mock.MagicMock())
        monkeypatch.setattr(
            _interactive.questionary,
            "select",
            lambda *a, **kw: mock.MagicMock(ask=lambda: direction),
        )
        monkeypatch.setattr(_interactive.questionary, "text", _capture_text_factory())
        monkeypatch.setattr(
            _interactive.questionary,
            "path",
            lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers_iter)),
        )
        monkeypatch.setattr(
            _interactive.questionary,
            "confirm",
            lambda *a, **kw: mock.MagicMock(ask=lambda: next(answers_iter)),
        )

    _configure_mocks(
        "local",
        iter(
            [
                "bastion",
                "ops",
                "22",
                "/id_test",
                "127.0.0.1",
                "19000",
                "127.0.0.1",
                "5432",
                False,
                True,
                False,
            ]
        ),
    )
    _interactive.collect_options(_empty_prefill())
    local_prompts = " | ".join(
        p for p in captured_prompts if "地址" in p or "主机" in p
    )
    assert "你本机" in local_prompts
    assert "bastion 视角" in local_prompts or "跳板机本地服务" in local_prompts

    captured_prompts.clear()
    _configure_mocks(
        "remote",
        iter(
            [
                "bastion",
                "ops",
                "22",
                "/id_test",
                "127.0.0.1",
                "8080",
                "127.0.0.1",
                "3000",
                False,
                True,
                False,
            ]
        ),
    )
    _interactive.collect_options(_empty_prefill())
    remote_prompts = " | ".join(
        p for p in captured_prompts if "地址" in p or "主机" in p
    )
    assert "bastion 上" in remote_prompts or "远端用户" in remote_prompts
    assert "你本机上的服务" in remote_prompts
