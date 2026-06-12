"""Typer 命令:``zata-ops tunnel open | list | status | close``。

薄壳:把 ``ssh <argv>`` 派生为 detached 子进程,把 pid 和 argv 写到
``<state_dir>/<name>.json``;``list/close/status`` 读这个目录管理。
所有 SSH 鉴权 / 转发 / 重连 / 配置文件探测都交给外部 ``ssh`` 进程。
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from zata_ops.tunnel import _state

console = Console()
app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "SSH 端口转发的 pid 标签管理器:open 派生 ssh 到后台,"
        "list/status/close 按名字管理。想做自动重连请用 autossh / systemd --user,"
        "想要前台直接 `ssh -L`,不必过 zata-ops。"
    ),
)


def _launch_ssh(ssh_argv: list[str], log_path: Path) -> subprocess.Popen:
    """Fork the given ``ssh_argv`` as a detached background process.

    The child gets a new session group (``start_new_session=True``) so it
    survives the parent's exit. stdout/stderr is appended to ``log_path``;
    stdin is closed to avoid the child inheriting a TTY.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab", buffering=0)
    return subprocess.Popen(  # noqa: S603 - argv is user-supplied; not a shell
        ssh_argv,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


@app.command("open")
def open_command(
    name: str = typer.Argument(..., help="tunnel friendly name (state file basename)"),
    ssh_argv: list[str] = typer.Argument(
        ...,
        help=(
            "ssh argv passed verbatim. The first element must be ``ssh``. "
            "Example: -- ssh -L 6669:localhost:5432 root@host"
        ),
    ),
) -> None:
    """Run ``ssh <argv>`` in the background and remember it as ``<name>``."""
    # Validate the name up front so an invalid name doesn't fork ssh first.
    try:
        _state.spec_path(name)
    except _state.StateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not ssh_argv or ssh_argv[0] != "ssh":
        first_arg_repr = repr(ssh_argv[0]) if ssh_argv else "(nothing)"
        console.print(
            f"[red]first arg after `--` must be `ssh`, got {first_arg_repr}.[/red]\n"
            "Example: zata-ops tunnel open db -- ssh -L 6669:localhost:5432 root@host"
        )
        raise typer.Exit(code=2)
    log_path = _state.state_dir() / f"{name}.log"
    try:
        child_process = _launch_ssh(ssh_argv, log_path)
    except FileNotFoundError as exc:
        console.print(
            f"[red]failed to exec {ssh_argv[0]!r}: {exc}.[/red]\n"
            "Is `ssh` on your $PATH? On Windows, install OpenSSH via Settings → "
            "Apps → Optional features, or use WSL."
        )
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        console.print(f"[red]failed to spawn ssh: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    spec = _state.TunnelSpec(
        name=name,
        pid=child_process.pid,
        ssh_argv=ssh_argv,
        log_path=str(log_path),
        started_at=_state.now_iso(),
    )
    _state.write_spec(spec)
    console.print(
        f"[green]Spawned tunnel '{name}' (pid {child_process.pid}); "
        f"log: {log_path}[/green]"
    )


@app.command("list")
def list_command() -> None:
    """列出所有后台 ssh 隧道实例。"""
    try:
        live_specs = _state.list_specs()
    except _state.StateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not live_specs:
        console.print("[dim](no background tunnels)[/dim]")
        return
    table = Table(title="Background tunnels", show_lines=False)
    table.add_column("NAME", style="cyan")
    table.add_column("PID", justify="right")
    table.add_column("ARGV", style="green")
    table.add_column("STARTED")
    for spec in live_specs:
        table.add_row(
            spec.name,
            str(spec.pid),
            " ".join(shlex.quote(part) for part in spec.ssh_argv),
            spec.started_at,
        )
    console.print(table)


@app.command("status")
def status_command(
    name: str = typer.Argument(..., help="tunnel name"),
) -> None:
    """显示单个后台实例的明细 + 最近 20 行日志。"""
    try:
        spec = _state.load_spec(name)
    except _state.StateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not _state.is_pid_alive(spec.pid):
        console.print(f"[yellow]Tunnel '{name}' 已不在运行,清理状态文件。[/yellow]")
        try:
            _state.spec_path(name).unlink()
        except FileNotFoundError:
            pass
        raise typer.Exit(code=1)
    console.print(f"[bold]Tunnel '{name}'[/bold]")
    console.print_json(json.dumps(spec.to_dict(), indent=2, ensure_ascii=False))
    if spec.log_path and Path(spec.log_path).is_file():
        log_text = Path(spec.log_path).read_text(encoding="utf-8", errors="replace")
        tail_lines = log_text.splitlines()[-20:]
        console.print("[bold]--- last 20 log lines ---[/bold]")
        for line in tail_lines:
            console.print(line)


@app.command("close")
def close_command(
    name: str = typer.Argument(..., help="tunnel name"),
) -> None:
    """停止一个后台 ssh 隧道实例。"""
    try:
        stopped = _state.close_spec(name)
    except _state.StateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if stopped:
        console.print(f"[green]Tunnel '{name}' 已停止。[/green]")
    else:
        console.print(f"[yellow]Tunnel '{name}' 未在运行,清理状态。[/yellow]")
