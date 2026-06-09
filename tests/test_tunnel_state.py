"""Tests for ``zata_ops.tunnel._state``: state-file lifecycle and liveness.

These tests never touch the real user's ``$HOME``/``$XDG_DATA_HOME``; they
redirect :func:`_state.state_dir` to ``tmp_path`` via the XDG env var.
"""

from __future__ import annotations

import json
import os

import pytest

from zata_ops.tunnel import _state


@pytest.fixture
def isolated_state_dir(monkeypatch, tmp_path):
    """Redirect :func:`_state.state_dir` to a per-test tmp directory.

    Setting ``XDG_DATA_HOME`` short-circuits the macOS fallback in
    :func:`_state.state_dir`, so the resolved path is fully deterministic.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "zata-ops" / "tunnels"


def _make_spec(name: str, pid: int) -> _state.TunnelSpec:
    """Build a :class:`TunnelSpec` with sensible defaults for state tests."""
    return _state.TunnelSpec(
        name=name,
        direction="local",
        bind_host="127.0.0.1",
        bind_port=9000,
        target_host="db.internal",
        target_port=5432,
        ssh_host="bastion",
        ssh_user="ops",
        ssh_port=22,
        ssh_key=None,
        strict_host_key=False,
        pid=pid,
        log_path="",
        started_at="2026-06-09T16:00:00+00:00",
        state="ready",
    )


def test_state_dir_resolves_under_xdg_data_home(isolated_state_dir):
    """The state dir is created on first access and lives under XDG_DATA_HOME."""
    resolved_path = _state.state_dir()
    assert resolved_path == isolated_state_dir
    assert resolved_path.is_dir()


def test_write_and_load_spec_roundtrip(isolated_state_dir):
    """A spec written to disk can be loaded back with identical fields."""
    original_spec = _make_spec("alpha", pid=os.getpid())
    written_path = _state.write_spec(original_spec)
    assert written_path == isolated_state_dir / "alpha.json"
    raw_text = written_path.read_text(encoding="utf-8")
    raw_dict = json.loads(raw_text)
    assert raw_dict["name"] == "alpha"
    assert raw_dict["bind_port"] == 9000
    assert raw_dict["target_port"] == 5432
    loaded_spec = _state.load_spec("alpha")
    assert loaded_spec.name == original_spec.name
    assert loaded_spec.bind_port == original_spec.bind_port
    assert loaded_spec.strict_host_key == original_spec.strict_host_key
    assert loaded_spec.ssh_user == original_spec.ssh_user


def test_load_spec_missing_raises_state_error(isolated_state_dir):
    """Reading a non-existent spec raises :class:`StateError`."""
    with pytest.raises(_state.StateError):
        _state.load_spec("does-not-exist")


def test_list_specs_filters_zombie_entries(isolated_state_dir):
    """Entries whose pid is no longer alive are pruned and their files removed."""
    zombie_spec = _state.TunnelSpec(
        name="zombie",
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
        pid=2_000_000_000,  # well above any realistic pid
        log_path="",
        started_at="",
        state="ready",
    )
    _state.write_spec(zombie_spec)
    assert (_state.spec_path("zombie")).is_file()
    live_specs = _state.list_specs()
    assert live_specs == []
    assert not (_state.spec_path("zombie")).exists()


def test_list_specs_keeps_self_pid(isolated_state_dir):
    """The current process's pid is treated as alive and not pruned."""
    self_spec = _make_spec("self", pid=os.getpid())
    _state.write_spec(self_spec)
    live_specs = _state.list_specs()
    assert len(live_specs) == 1
    assert live_specs[0].name == "self"


def test_is_pid_alive_recognises_self_and_garbage():
    assert _state.is_pid_alive(os.getpid()) is True
    assert _state.is_pid_alive(0) is False
    assert _state.is_pid_alive(-1) is False
    # 999_999_999 is well above any realistic pid limit and triggers
    # ProcessLookupError on Linux/macOS, not OverflowError.
    assert _state.is_pid_alive(999_999_999) is False


def test_mark_state_updates_existing_spec(isolated_state_dir):
    """``mark_state`` mutates the ``state`` field in place."""
    spec = _make_spec("updater", pid=os.getpid())
    _state.write_spec(spec)
    _state.mark_state("updater", "ready")
    loaded = _state.load_spec("updater")
    assert loaded.state == "ready"
    _state.mark_state("updater", "stopped")
    loaded = _state.load_spec("updater")
    assert loaded.state == "stopped"


def test_close_spec_removes_state_file_for_dead_process(isolated_state_dir):
    """``close_spec`` returns False and cleans up the spec when pid is gone."""
    spec = _state.TunnelSpec(
        name="dead",
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
        pid=2_000_000_000,
        log_path="",
        started_at="",
        state="ready",
    )
    _state.write_spec(spec)
    closed = _state.close_spec("dead")
    assert closed is False
    assert not (_state.spec_path("dead")).exists()
