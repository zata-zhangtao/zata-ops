"""Tests for ``zata_ops.tunnel._state``: spec file lifecycle and liveness.

These tests never touch the real user's ``$HOME``/``$XDG_DATA_HOME``; they
redirect :func:`_state.state_dir` to ``tmp_path`` via the XDG env var.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import datetime, timezone

import pytest

from zata_ops.tunnel import _state


@pytest.fixture
def isolated_state_dir(monkeypatch, tmp_path):
    """Redirect :func:`_state.state_dir` to a per-test tmp directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "zata-ops" / "tunnels"


def _spec(name: str, pid: int, log_path: str = "/tmp/x.log") -> _state.TunnelSpec:
    """Build a :class:`TunnelSpec` with sensible defaults for state tests."""
    return _state.TunnelSpec(
        name=name,
        pid=pid,
        ssh_argv=["ssh", "-L", "6669:localhost:5432", "root@host"],
        log_path=log_path,
        started_at="2026-06-12T14:34:14+00:00",
    )


def test_state_dir_resolves_under_xdg_data_home(isolated_state_dir):
    """The state dir is created on first access and lives under XDG_DATA_HOME."""
    resolved_path = _state.state_dir()
    assert resolved_path == isolated_state_dir
    assert resolved_path.is_dir()


def test_write_and_load_spec_roundtrip(isolated_state_dir):
    """A spec written to disk can be loaded back with identical fields."""
    original_spec = _spec("alpha", pid=os.getpid(), log_path="/tmp/alpha.log")
    written_path = _state.write_spec(original_spec)
    assert written_path == isolated_state_dir / "alpha.json"
    loaded_spec = _state.load_spec("alpha")
    assert loaded_spec == original_spec


def test_write_spec_emits_valid_json(isolated_state_dir):
    """The on-disk file is parseable JSON with the expected top-level keys."""
    _state.write_spec(_spec("beta", pid=12345))
    raw_dict = json.loads((isolated_state_dir / "beta.json").read_text("utf-8"))
    assert set(raw_dict) >= {
        "name",
        "pid",
        "ssh_argv",
        "log_path",
        "started_at",
        "state",
    }
    assert raw_dict["name"] == "beta"
    assert raw_dict["ssh_argv"] == ["ssh", "-L", "6669:localhost:5432", "root@host"]


def test_spec_path_rejects_empty_name(isolated_state_dir):
    """``spec_path`` rejects empty names with :class:`StateError`."""
    with pytest.raises(_state.StateError):
        _state.spec_path("")


def test_spec_path_rejects_path_separators(isolated_state_dir):
    """``spec_path`` rejects names containing ``/`` or ``\\``."""
    with pytest.raises(_state.StateError):
        _state.spec_path("foo/bar")
    with pytest.raises(_state.StateError):
        _state.spec_path("foo\\bar")


def test_load_spec_missing_file_raises(isolated_state_dir):
    """``load_spec`` for an unknown name raises :class:`StateError`."""
    with pytest.raises(_state.StateError, match="tunnel not found"):
        _state.load_spec("nope")


def test_is_pid_alive_for_current_and_bogus(isolated_state_dir):
    """``is_pid_alive`` returns True for our own pid and False for 0 / negative."""
    assert _state.is_pid_alive(os.getpid()) is True
    assert _state.is_pid_alive(0) is False
    assert _state.is_pid_alive(-1) is False


def test_list_specs_prunes_dead_pids(isolated_state_dir):
    """``list_specs`` removes state files whose pid is no longer running."""
    bogus_pid = 2_000_000
    free_pid = os.getpid()
    _state.write_spec(_spec("alive", pid=free_pid, log_path="/tmp/alive.log"))
    _state.write_spec(_spec("dead", pid=bogus_pid, log_path="/tmp/dead.log"))
    live_specs = _state.list_specs()
    assert [spec.name for spec in live_specs] == ["alive"]
    assert not (isolated_state_dir / "dead.json").exists()


def test_list_specs_ignores_corrupt_files(isolated_state_dir):
    """A non-JSON file under state_dir is silently dropped."""
    isolated_state_dir.mkdir(parents=True, exist_ok=True)
    (isolated_state_dir / "broken.json").write_text("not-json{", encoding="utf-8")
    assert _state.list_specs() == []
    assert not (isolated_state_dir / "broken.json").exists()


def test_close_spec_returns_false_when_pid_already_dead(isolated_state_dir):
    """``close_spec`` cleans up the spec file and returns False when pid is gone."""
    _state.write_spec(_spec("ghost", pid=2_000_001, log_path="/tmp/ghost.log"))
    assert _state.close_spec("ghost") is False
    assert not (isolated_state_dir / "ghost.json").exists()


def test_close_spec_terminates_alive_pid(isolated_state_dir):
    """``close_spec`` SIGTERMs an alive pid and cleans the spec file."""
    sleeper = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _state.write_spec(_spec("doomed", pid=sleeper.pid, log_path="/tmp/doomed.log"))
        assert _state.close_spec("doomed") is True
        assert not (isolated_state_dir / "doomed.json").exists()
        assert _state.is_pid_alive(sleeper.pid) is False
    finally:
        try:
            os.kill(sleeper.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        sleeper.wait(timeout=5)


def test_now_iso_returns_utc_iso8601(isolated_state_dir):
    """``now_iso`` produces an ISO 8601 UTC timestamp with no microseconds."""
    timestamp = _state.now_iso()
    parsed = datetime.fromisoformat(timestamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert parsed.microsecond == 0
