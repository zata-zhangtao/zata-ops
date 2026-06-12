"""Background tunnel state: one ``<name>.json`` per managed ``ssh`` process.

Each ``tunnel open <name> -- ssh ...`` writes a spec recording the ssh pid,
the verbatim ``ssh_argv`` it was launched with, the log path, and the start
time. ``list_specs`` filters out entries whose pid is gone; ``close_spec``
sends ``SIGTERM`` to the pid, escalates to ``SIGKILL`` after
:data:`SHUTDOWN_GRACE_SECONDS`, and removes the spec file.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


#: Grace period for SIGTERM before escalating to SIGKILL.
SHUTDOWN_GRACE_SECONDS: float = 5.0
#: Polling interval when waiting for the pid to exit.
SHUTDOWN_POLL_INTERVAL_SECONDS: float = 0.1


class StateError(Exception):
    """Spec file I/O or signal failure."""


@dataclass
class TunnelSpec:
    """State for one running ``ssh`` tunnel.

    Attributes:
        name: Friendly name, used as both list/close handle and state filename.
        pid: The ``ssh`` process pid we forked.
        ssh_argv: Verbatim ``ssh`` argv we exec'd (always starts with ``"ssh"``).
        log_path: Where the child's stdout/stderr is appended.
        started_at: ISO 8601 UTC timestamp set by the parent at fork time.
        state: Lifecycle marker (``"running"`` on fork; we do not flip it after).
    """

    name: str
    pid: int
    ssh_argv: list[str]
    log_path: str
    started_at: str
    state: str = "running"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of this spec."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw_dict: dict[str, Any]) -> "TunnelSpec":
        """Build a :class:`TunnelSpec` from a decoded JSON object."""
        return cls(
            name=str(raw_dict["name"]),
            pid=int(raw_dict["pid"]),
            ssh_argv=list(raw_dict["ssh_argv"]),
            log_path=str(raw_dict["log_path"]),
            started_at=str(raw_dict["started_at"]),
            state=str(raw_dict.get("state", "running")),
        )


def state_dir() -> Path:
    """Return the directory that holds tunnel state files.

    Resolution order:

    1. ``$XDG_DATA_HOME/zata-ops/tunnels`` (Linux/POSIX standard)
    2. ``$HOME/Library/Application Support/zata-ops/tunnels`` (macOS)
    3. ``$HOME/.local/share/zata-ops/tunnels`` (final fallback)

    The directory is created if missing.
    """
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        candidate_path = Path(xdg_data_home) / "zata-ops" / "tunnels"
    else:
        home_path = Path.home()
        if home_path.joinpath("Library").exists():
            candidate_path = (
                home_path / "Library" / "Application Support" / "zata-ops" / "tunnels"
            )
        else:
            candidate_path = home_path / ".local" / "share" / "zata-ops" / "tunnels"
    candidate_path.mkdir(parents=True, exist_ok=True)
    return candidate_path


def spec_path(name: str) -> Path:
    """Return the on-disk path for a tunnel state file.

    Args:
        name: Tunnel instance name.

    Returns:
        Absolute path under :func:`state_dir` named ``<name>.json``.

    Raises:
        StateError: If ``name`` is empty or contains path separators.
    """
    if not name or "/" in name or "\\" in name:
        raise StateError(f"invalid tunnel name: {name!r}")
    return state_dir() / f"{name}.json"


def write_spec(spec: TunnelSpec) -> Path:
    """Persist a spec to disk, overwriting any existing entry with the same name."""
    target_path = spec_path(spec.name)
    json_payload = json.dumps(spec.to_dict(), indent=2, ensure_ascii=False)
    target_path.write_text(json_payload + "\n", encoding="utf-8")
    return target_path


def load_spec(name: str) -> TunnelSpec:
    """Read a single spec from disk.

    Raises:
        StateError: If the file does not exist or cannot be parsed.
    """
    target_path = spec_path(name)
    if not target_path.is_file():
        raise StateError(f"tunnel not found: {name!r}")
    try:
        raw_text = target_path.read_text(encoding="utf-8")
        raw_dict = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"failed to read tunnel state {name!r}: {exc}") from exc
    return TunnelSpec.from_dict(raw_dict)


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running.

    Reaps a zombie child if ``pid`` is one of ours, since ``kill -0`` on a
    zombie still reports success until the parent calls ``waitpid``. If
    ``pid`` is not our child (``ECHILD``), fall back to the ``kill -0``
    check, which is the standard POSIX liveness probe.
    """
    if pid <= 0:
        return False
    reaped_pid = 0
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # ``pid`` is not our child (or already reaped). Fall through to
        # ``kill -0`` so we don't claim a foreign-but-alive pid is dead.
        pass
    except OSError:
        reaped_pid = 0
    if reaped_pid == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def list_specs() -> list[TunnelSpec]:
    """Read every state file and return live specs, with zombies pruned."""
    state_directory = state_dir()
    live_specs: list[TunnelSpec] = []
    for state_file in sorted(state_directory.glob("*.json")):
        try:
            raw_text = state_file.read_text(encoding="utf-8")
            raw_dict = json.loads(raw_text)
            parsed_spec = TunnelSpec.from_dict(raw_dict)
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            try:
                state_file.unlink()
            except OSError:
                pass
            continue
        if not is_pid_alive(parsed_spec.pid):
            try:
                state_file.unlink()
            except OSError:
                pass
            continue
        live_specs.append(parsed_spec)
    return live_specs


def close_spec(name: str) -> bool:
    """Stop a background tunnel, escalating from SIGTERM to SIGKILL.

    Args:
        name: Tunnel instance name.

    Returns:
        True if a live process was found and signalled; False if the spec
        did not exist or the process was already gone.

    Raises:
        StateError: If the spec file is unreadable or corrupted.
    """
    parsed_spec = load_spec(name)
    spec_file_path = spec_path(name)
    if not is_pid_alive(parsed_spec.pid):
        try:
            spec_file_path.unlink()
        except FileNotFoundError:
            pass
        return False
    target_pid = parsed_spec.pid
    os.kill(target_pid, signal.SIGTERM)
    deadline_monotonic = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    while time.monotonic() < deadline_monotonic:
        if not is_pid_alive(target_pid):
            break
        time.sleep(SHUTDOWN_POLL_INTERVAL_SECONDS)
    else:
        try:
            os.kill(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        spec_file_path.unlink()
    except FileNotFoundError:
        pass
    return True


def now_iso() -> str:
    """Return the current UTC time formatted as ISO 8601 with seconds."""
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
