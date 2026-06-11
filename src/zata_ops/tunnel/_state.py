"""后台隧道实例的持久化状态管理。

每个后台运行的隧道在本地状态目录下拥有一个 ``<name>.json`` 文件,
记录 SSH 参数、PID、日志路径与启动时间。``list_specs`` 在读取时会
过滤掉进程已退出的僵尸条目;``close_spec`` 通过 ``SIGTERM`` 触发
daemon 内的优雅退出,5 秒后回退到 ``SIGKILL``。
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

#: 状态文件稳定化的最长等待时间(秒),父进程会在这个时间内轮询子进程是否把 state 翻成 ready。
READY_WAIT_SECONDS: float = 8.0
#: 优雅退出宽限期,超过这个时间 SIGTERM 未生效则升级到 SIGKILL。
SHUTDOWN_GRACE_SECONDS: float = 5.0
#: 轮询子进程状态的最小间隔(秒)。
READY_POLL_INTERVAL_SECONDS: float = 0.1


class StateError(Exception):
    """状态文件读写或信号发送失败。"""


@dataclass
class TunnelSpec:
    """单条隧道实例的完整配置。

    Attributes:
        name: 后台实例的友好名,用于 list/close 引用,也是状态文件名。
        direction: ``"local"`` 对应 ``ssh -L``,``"remote"`` 对应 ``ssh -R``。
        bind_host: 监听地址;local 时是本机地址,remote 时是远端 SSH 服务端可见的地址。
        bind_port: 监听端口。
        target_host: 流量终点的主机。
        target_port: 流量终点的端口。
        ssh_host: SSH 跳板机地址。
        ssh_user: SSH 登录用户。
        ssh_port: SSH 服务端口。
        ssh_key: 可选的私钥路径,None 时由 paramiko 自动探测 ``~/.ssh/id_*`` 与 ssh-agent。
        strict_host_key: 缺省 False(用 ``~/.ssh/known_hosts`` 校验,缺失时 paramiko 警告并接受);
            True 时缺失直接抛错。
        pid: 后台进程 PID,由父进程在 fork 之后回填。
        log_path: 后台进程 stdout/stderr 重定向到的日志文件路径。
        started_at: ISO 8601 字符串,由父进程在启动时写入。
        state: 运行时状态,父进程写 ``"pending"``,daemon 建立隧道后翻成 ``"ready"``,
            退出后由 daemon 写 ``"stopped"``。
        reconnect: True 时 SSH 断了自动重连(指数退避 1s→30s)。
        max_reconnect: 最多重试次数,0 表示无限(仅 ``reconnect=True`` 时生效)。
    """

    name: str
    direction: str
    bind_host: str
    bind_port: int
    target_host: str
    target_port: int
    ssh_host: str
    ssh_user: str
    ssh_port: int
    ssh_key: str | None
    strict_host_key: bool
    pid: int = 0
    log_path: str = ""
    started_at: str = ""
    state: str = "pending"
    reconnect: bool = False
    max_reconnect: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of this spec."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw_dict: dict[str, Any]) -> "TunnelSpec":
        """Build a :class:`TunnelSpec` from a decoded JSON object.

        Args:
            raw_dict: Decoded JSON payload (typically loaded from disk).

        Returns:
            Populated :class:`TunnelSpec` instance.
        """
        return cls(
            name=str(raw_dict["name"]),
            direction=str(raw_dict["direction"]),
            bind_host=str(raw_dict["bind_host"]),
            bind_port=int(raw_dict["bind_port"]),
            target_host=str(raw_dict["target_host"]),
            target_port=int(raw_dict["target_port"]),
            ssh_host=str(raw_dict["ssh_host"]),
            ssh_user=str(raw_dict["ssh_user"]),
            ssh_port=int(raw_dict["ssh_port"]),
            ssh_key=(str(raw_dict["ssh_key"]) if raw_dict.get("ssh_key") else None),
            strict_host_key=bool(raw_dict.get("strict_host_key", False)),
            pid=int(raw_dict.get("pid", 0)),
            log_path=str(raw_dict.get("log_path", "")),
            started_at=str(raw_dict.get("started_at", "")),
            state=str(raw_dict.get("state", "pending")),
            reconnect=bool(raw_dict.get("reconnect", False)),
            max_reconnect=int(raw_dict.get("max_reconnect", 0)),
        )


def state_dir() -> Path:
    """Return the directory that holds tunnel state files.

    Resolution order:

    1. ``$XDG_DATA_HOME/zata-ops/tunnels`` (Linux/POSIX standard)
    2. ``$HOME/Library/Application Support/zata-ops/tunnels`` (macOS)
    3. ``$HOME/.local/share/zata-ops/tunnels`` (final fallback)

    The directory is created if missing. Tests can monkeypatch ``HOME`` and
    ``XDG_DATA_HOME`` to redirect.
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
    """Persist a spec to disk, overwriting any existing entry with the same name.

    Args:
        spec: Fully populated :class:`TunnelSpec` (typically with
            ``pid`` and ``started_at`` set by the caller).

    Returns:
        Path to the written JSON file.
    """
    target_path = spec_path(spec.name)
    json_payload = json.dumps(spec.to_dict(), indent=2, ensure_ascii=False)
    target_path.write_text(json_payload + "\n", encoding="utf-8")
    return target_path


def load_spec(name: str) -> TunnelSpec:
    """Read a single spec from disk.

    Args:
        name: Tunnel instance name.

    Returns:
        Populated :class:`TunnelSpec`.

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

    Args:
        pid: Process ID to probe.

    Returns:
        True when ``os.kill(pid, 0)`` does not raise; False when the
        process is gone (``ProcessLookupError``), the value is out of
        range for the OS (``OverflowError``), or the call fails for any
        other reason (``OSError``). ``PermissionError`` is treated as
        alive: the process exists, we just cannot signal it.
    """
    if pid <= 0:
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
    """Read every state file and return live specs, with zombies pruned.

    Returns:
        Specs in alphabetical order by name. Entries whose ``pid`` no longer
        exists are filtered out and their state files removed on the fly.
    """
    state_directory = state_dir()
    live_specs: list[TunnelSpec] = []
    for state_file in sorted(state_directory.glob("*.json")):
        try:
            raw_text = state_file.read_text(encoding="utf-8")
            raw_dict = json.loads(raw_text)
            parsed_spec = TunnelSpec.from_dict(raw_dict)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            # Corrupt or partially written spec: remove it so it does not
            # accumulate as noise. A new `tunnel open` will recreate it.
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
    if not is_pid_alive(parsed_spec.pid):
        spec_file_path = spec_path(name)
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
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    else:
        # Grace expired; force-kill the daemon.
        try:
            os.kill(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    spec_file_path = spec_path(name)
    try:
        spec_file_path.unlink()
    except FileNotFoundError:
        pass
    return True


def wait_for_ready(name: str, timeout_seconds: float = READY_WAIT_SECONDS) -> bool:
    """Poll a spec file until its ``state`` field becomes ``"ready"``.

    Args:
        name: Tunnel instance name.
        timeout_seconds: Maximum wall-clock seconds to wait.

    Returns:
        True if the daemon flipped the state to ``"ready"`` within the timeout,
        False otherwise. The spec file is left in place regardless of outcome.
    """
    target_path = spec_path(name)
    deadline_monotonic = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline_monotonic:
        if target_path.is_file():
            try:
                raw_text = target_path.read_text(encoding="utf-8")
                raw_dict = json.loads(raw_text)
                if raw_dict.get("state") == "ready":
                    return True
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    return False


def mark_state(name: str, new_state: str) -> None:
    """Update the ``state`` field of an existing spec file in place.

    Args:
        name: Tunnel instance name.
        new_state: New value for the ``state`` field (``"ready"``, ``"stopped"``, ...).
    """
    target_path = spec_path(name)
    if not target_path.is_file():
        return
    raw_text = target_path.read_text(encoding="utf-8")
    raw_dict = json.loads(raw_text)
    raw_dict["state"] = new_state
    updated_text = json.dumps(raw_dict, indent=2, ensure_ascii=False)
    target_path.write_text(updated_text + "\n", encoding="utf-8")


def now_iso() -> str:
    """Return the current UTC time formatted as ISO 8601 with seconds."""
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


#: 历史记录文件名，用于保存最近一次成功的 ``tunnel open`` 参数。
HISTORY_FILE_NAME: str = "last_open.json"


def history_path() -> Path:
    """Return the path to the last-open history file."""
    return state_dir() / HISTORY_FILE_NAME


def write_history(payload: dict[str, Any]) -> Path:
    """Persist the last successful ``tunnel open`` options (sans password).

    Args:
        payload: JSON-serializable dict of tunnel options.

    Returns:
        Path to the written history file.
    """
    target_path = history_path()
    json_payload = json.dumps(payload, indent=2, ensure_ascii=False)
    target_path.write_text(json_payload + "\n", encoding="utf-8")
    return target_path


def read_history() -> dict[str, Any] | None:
    """Load the last successful ``tunnel open`` options if present.

    Returns:
        Decoded dict, or ``None`` if the file is missing or unreadable.
    """
    target_path = history_path()
    if not target_path.is_file():
        return None
    try:
        raw_text = target_path.read_text(encoding="utf-8")
        return json.loads(raw_text)
    except (OSError, json.JSONDecodeError):
        return None
