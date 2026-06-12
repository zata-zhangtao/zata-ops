"""Tests for ``zata-ops tunnel close``."""

from __future__ import annotations

import os
import signal
import subprocess
from unittest import mock

import pytest
from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.tunnel import _state


@pytest.fixture
def isolated_state_dir(monkeypatch, tmp_path):
    """Redirect :func:`_state.state_dir` to a per-test tmp directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "zata-ops" / "tunnels"


def test_close_unknown_name(isolated_state_dir):
    """``tunnel close nope`` exits 1 with a clear error."""
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "close", "nope"])
    assert result.exit_code == 1
    assert "tunnel not found" in result.output


def test_close_returns_clean_message_when_pid_already_dead(isolated_state_dir):
    """When the spec's pid is gone, close prints the cleanup message and exits 0."""
    _state.write_spec(
        _state.TunnelSpec(
            name="ghost",
            pid=2_000_010,
            ssh_argv=["ssh", "-L", "1:2:3", "h"],
            log_path="/tmp/ghost.log",
            started_at="2026-06-12T14:34:14+00:00",
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, ["tunnel", "close", "ghost"])
    assert result.exit_code == 0
    assert "未在运行" in result.output
    assert not (isolated_state_dir / "ghost.json").exists()


def test_close_terminates_alive_pid(isolated_state_dir):
    """An alive pid is SIGTERMed, then the spec file is removed."""
    sleeper = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _state.write_spec(
            _state.TunnelSpec(
                name="doomed",
                pid=sleeper.pid,
                ssh_argv=["ssh", "-L", "1:2:3", "h"],
                log_path="/tmp/doomed.log",
                started_at="2026-06-12T14:34:14+00:00",
            )
        )
        runner = CliRunner()
        result = runner.invoke(app, ["tunnel", "close", "doomed"])
        assert result.exit_code == 0
        assert "已停止" in result.output
        assert not (isolated_state_dir / "doomed.json").exists()
        assert _state.is_pid_alive(sleeper.pid) is False
    finally:
        try:
            os.kill(sleeper.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        sleeper.wait(timeout=5)


def test_close_escalates_to_sigkill_when_sigterm_ignored(isolated_state_dir):
    """If SIGTERM doesn't take the pid down in time, SIGKILL is sent."""
    # Trap SIGTERM but stay alive forever; force the close path into SIGKILL.
    trapper = subprocess.Popen(
        [
            "python3",
            "-c",
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n    time.sleep(0.1)\n",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Shorten the grace period so the test runs in <1s.
        with mock.patch.object(
            _state, "SHUTDOWN_GRACE_SECONDS", 0.2
        ), mock.patch.object(_state, "SHUTDOWN_POLL_INTERVAL_SECONDS", 0.05):
            _state.write_spec(
                _state.TunnelSpec(
                    name="stubborn",
                    pid=trapper.pid,
                    ssh_argv=["ssh", "-L", "1:2:3", "h"],
                    log_path="/tmp/stubborn.log",
                    started_at="2026-06-12T14:34:14+00:00",
                )
            )
            runner = CliRunner()
            result = runner.invoke(app, ["tunnel", "close", "stubborn"])
        assert result.exit_code == 0
        assert not (isolated_state_dir / "stubborn.json").exists()
        assert _state.is_pid_alive(trapper.pid) is False
    finally:
        try:
            os.kill(trapper.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        trapper.wait(timeout=5)
