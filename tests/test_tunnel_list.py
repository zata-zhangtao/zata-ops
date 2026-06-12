"""Tests for ``zata-ops tunnel list`` and ``tunnel status``."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.tunnel import _state


def test_list_empty(isolated_state_dir, cli_runner):
    """``tunnel list`` with no specs prints a dim placeholder and exits 0."""
    result = cli_runner.invoke(app, ["tunnel", "list"])
    assert result.exit_code == 0
    assert "(no background tunnels)" in result.output


def test_list_renders_live_specs(isolated_state_dir, cli_runner):
    """``tunnel list`` renders a Rich table with name / pid / argv / started."""
    _state.write_spec(
        _state.TunnelSpec(
            name="db-access",
            pid=os.getpid(),
            ssh_argv=["ssh", "-L", "6669:localhost:5432", "root@host"],
            log_path="/tmp/x.log",
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    _state.write_spec(
        _state.TunnelSpec(
            name="dev-server",
            pid=os.getpid(),
            ssh_argv=["ssh", "-R", "5432:localhost:6669", "root@host"],
            log_path="/tmp/y.log",
            started_at="2026-06-12T14:35:00+00:00",
        )
    )
    result = cli_runner.invoke(app, ["tunnel", "list"])
    assert result.exit_code == 0
    assert "db-access" in result.output
    assert "dev-server" in result.output
    assert "root@host" in result.output
    # ssh_argv is shell-quoted so ``-L`` and the bind:target:host spec are
    # visible without Rich eating them.
    assert "6669:localhost:5432" in result.output


def test_list_skips_dead_pids(isolated_state_dir, cli_runner):
    """``tunnel list`` silently prunes state files whose pid is gone."""
    _state.write_spec(
        _state.TunnelSpec(
            name="alive",
            pid=os.getpid(),
            ssh_argv=["ssh", "-L", "1:2:3", "h"],
            log_path="/tmp/alive.log",
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    _state.write_spec(
        _state.TunnelSpec(
            name="ghost",
            pid=2_000_002,
            ssh_argv=["ssh", "-L", "1:2:3", "h"],
            log_path="/tmp/ghost.log",
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    result = cli_runner.invoke(app, ["tunnel", "list"])
    assert result.exit_code == 0
    assert "alive" in result.output
    assert "ghost" not in result.output
    # Pruned spec file is gone from disk.
    assert not (isolated_state_dir / "ghost.json").exists()


def test_status_unknown_name(isolated_state_dir, cli_runner):
    """``tunnel status nope`` exits 1 with a clear error."""
    result = cli_runner.invoke(app, ["tunnel", "status", "nope"])
    assert result.exit_code == 1
    assert "tunnel not found" in result.output


def test_status_prints_spec_and_log_tail(isolated_state_dir, cli_runner):
    """``tunnel status`` shows the JSON spec and the last 20 log lines."""
    log_path = Path("/tmp/zata-ops-test-status.log")
    log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _state.write_spec(
        _state.TunnelSpec(
            name="probe",
            pid=os.getpid(),
            ssh_argv=["ssh", "-L", "1:2:3", "h"],
            log_path=str(log_path),
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    result = cli_runner.invoke(app, ["tunnel", "status", "probe"])
    assert result.exit_code == 0
    assert "probe" in result.output
    # The spec dict is dumped as JSON.
    spec_payload = json.loads("{" + result.output.split("{", 1)[1].split("}")[0] + "}")
    assert spec_payload["name"] == "probe"
    # Last 20 log lines are tailed.
    assert "line1" in result.output
    assert "line3" in result.output
    log_path.unlink()


def test_status_prunes_spec_for_dead_pid(isolated_state_dir, cli_runner):
    """``tunnel status`` of a dead-pid spec exits 1 and removes the spec file."""
    _state.write_spec(
        _state.TunnelSpec(
            name="zombie",
            pid=2_000_003,
            ssh_argv=["ssh", "-L", "1:2:3", "h"],
            log_path="/tmp/z.log",
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    result = cli_runner.invoke(app, ["tunnel", "status", "zombie"])
    assert result.exit_code == 1
    assert "已不在运行" in result.output
    assert not (isolated_state_dir / "zombie.json").exists()


# --- fixtures ---------------------------------------------------------


import pytest  # noqa: E402


@pytest.fixture
def isolated_state_dir(monkeypatch, tmp_path):
    """Redirect :func:`_state.state_dir` to a per-test tmp directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "zata-ops" / "tunnels"


@pytest.fixture
def cli_runner():
    """Fresh :class:`CliRunner` per test so state doesn't leak."""
    return CliRunner()
