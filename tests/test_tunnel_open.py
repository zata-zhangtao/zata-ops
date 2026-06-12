"""Tests for ``zata-ops tunnel open`` CLI surface.

We mock ``subprocess.Popen`` so the tests never spawn a real ``ssh``. The
core behaviors verified here:

- ``tunnel --help`` advertises the four user-facing subcommands.
- ``tunnel open <name> -- <ssh-args>`` validates that the first arg after
  ``--`` is ``ssh`` and rejects anything else with exit 2.
- The detached Popen invocation uses ``start_new_session=True`` and
  appends stdout/stderr to ``<state_dir>/<name>.log``.
- The state file is written with the verbatim ``ssh_argv`` and the pid
  returned by Popen.
- A missing ssh binary on ``$PATH`` is reported with a friendly hint.
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest
from typer.testing import CliRunner

import zata_ops.tunnel.cli as cli_module
from zata_ops.cli import app
from zata_ops.tunnel import _state


@pytest.fixture
def isolated_state_dir(monkeypatch, tmp_path):
    """Redirect :func:`_state.state_dir` to a per-test tmp directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "zata-ops" / "tunnels"


@pytest.fixture
def captured_popen(monkeypatch):
    """Replace ``subprocess.Popen`` on the cli module and capture every call.

    Returns ``(captured_calls, fake_process)``. Each entry in
    ``captured_calls`` is a ``(argv, kwargs)`` tuple, where ``argv`` is
    the list form (a single-element wrapping is collapsed so the
    ``Popen(ssh_argv, ...)`` calling style still looks like a flat list).
    """
    captured: list[tuple] = []
    fake_process = mock.Mock(pid=99999)

    def _capture(*args, **kwargs):
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            argv = list(args[0])
        else:
            argv = list(args)
        captured.append((argv, kwargs))
        return fake_process

    monkeypatch.setattr(cli_module.subprocess, "Popen", _capture)
    return captured, fake_process


def test_root_help_lists_tunnel_subcommand() -> None:
    """The top-level ``--help`` mentions ``tunnel`` alongside the other subcommands."""
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "tunnel" in result.output


def test_tunnel_help_lists_user_facing_subcommands() -> None:
    """``tunnel --help`` advertises only the four user-facing subcommands."""
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "--help"])
    assert result.exit_code == 0
    for subcommand_name in ("open", "list", "status", "close"):
        assert subcommand_name in result.output
    # No hidden ``run`` daemon subcommand anymore.
    assert "\b run " not in result.output


def test_open_rejects_argv_without_ssh(isolated_state_dir) -> None:
    """``tunnel open <name> -- scp ...`` exits 2 with a hint to use ``ssh``."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tunnel", "open", "demo", "--", "scp", "user@host:/etc/hosts", "."],
    )
    assert result.exit_code == 2
    assert "must be `ssh`" in result.output
    assert list(isolated_state_dir.glob("*.json")) == []


def test_open_rejects_empty_argv(isolated_state_dir) -> None:
    """``tunnel open <name> --`` with no ssh argv also exits 2 (Typer-level)."""
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "open", "demo", "--"])
    assert result.exit_code == 2
    # Typer's "Missing argument" message is fine; we just need to bail out
    # without forking anything or writing a spec file.
    assert list(isolated_state_dir.glob("*.json")) == []


def test_open_forks_ssh_via_popen_with_detached_session(
    isolated_state_dir, captured_popen
) -> None:
    """Popen is called with verbatim argv and ``start_new_session=True``."""
    runner = CliRunner()
    captured, _ = captured_popen
    with mock.patch.object(_state, "now_iso", return_value="2026-06-12T14:34:14+00:00"):
        result = runner.invoke(
            app,
            [
                "tunnel",
                "open",
                "demo",
                "--",
                "ssh",
                "-L",
                "6669:localhost:5432",
                "root@172.188.74.58",
            ],
        )
    assert result.exit_code == 0, result.output
    ssh_argv, popen_kwargs = captured[0]
    assert ssh_argv == ["ssh", "-L", "6669:localhost:5432", "root@172.188.74.58"]
    assert popen_kwargs["start_new_session"] is True
    assert popen_kwargs["stdin"] == subprocess.DEVNULL
    assert popen_kwargs["stderr"] == subprocess.STDOUT
    # Log file was opened for writing by us; close it.
    log_handle = popen_kwargs["stdout"]
    log_handle.close()
    assert (isolated_state_dir / "demo.log").exists()
    # State file mirrors what we passed.
    spec = _state.TunnelSpec.from_dict(
        json.loads((isolated_state_dir / "demo.json").read_text("utf-8"))
    )
    assert spec.name == "demo"
    assert spec.pid == 99999
    assert spec.ssh_argv == ssh_argv
    assert spec.started_at == "2026-06-12T14:34:14+00:00"


def test_open_passes_extra_ssh_flags_verbatim(
    isolated_state_dir, captured_popen
) -> None:
    """Flags after ``ssh`` (``-i``, ``-p``, ``-o``...) are passed through unchanged."""
    runner = CliRunner()
    captured, _ = captured_popen
    result = runner.invoke(
        app,
        [
            "tunnel",
            "open",
            "alpha",
            "--",
            "ssh",
            "-N",
            "-L",
            "19000:db.internal:5432",
            "ops@bastion",
            "-i",
            "/tmp/key",
            "-p",
            "2222",
            "-o",
            "ServerAliveInterval=15",
        ],
    )
    assert result.exit_code == 0, result.output
    ssh_argv, _ = captured[0]
    assert ssh_argv == [
        "ssh",
        "-N",
        "-L",
        "19000:db.internal:5432",
        "ops@bastion",
        "-i",
        "/tmp/key",
        "-p",
        "2222",
        "-o",
        "ServerAliveInterval=15",
    ]


def test_open_reports_failure_when_ssh_binary_missing(isolated_state_dir) -> None:
    """A FileNotFoundError from Popen is mapped to a friendly Chinese error."""
    runner = CliRunner()
    with mock.patch.object(
        cli_module.subprocess,
        "Popen",
        side_effect=FileNotFoundError(2, "No such file", "ssh"),
    ):
        result = runner.invoke(
            app,
            ["tunnel", "open", "demo", "--", "ssh", "-L", "1:2:3", "h"],
        )
    assert result.exit_code == 1
    assert "ssh" in result.output
    assert "PATH" in result.output
    assert list(isolated_state_dir.glob("*.json")) == []


def test_open_rejects_invalid_name_with_path_separator(isolated_state_dir) -> None:
    """Names containing ``/`` are rejected before any Popen call."""
    runner = CliRunner()
    with mock.patch.object(cli_module.subprocess, "Popen") as fake_popen:
        result = runner.invoke(
            app,
            ["tunnel", "open", "bad/name", "--", "ssh", "-L", "1:2:3", "h"],
        )
    assert result.exit_code == 1
    fake_popen.assert_not_called()
    assert list(isolated_state_dir.glob("*.json")) == []
