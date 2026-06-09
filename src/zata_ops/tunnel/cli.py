"""Typer 命令:``zata-ops tunnel open | list | status | close``,以及后台
守护使用的隐藏子命令 ``tunnel run``。

设计要点:

- ``open`` 默认前台,``--background`` 时父进程把 spec 写到
  ``~/.local/share/zata-ops/tunnels/<name>.json``,通过 ``subprocess.Popen``
  启动 :func:`zata_ops.tunnel._runner.serve_daemon` 并等状态翻成 ``ready``。
- 缺 ``paramiko`` 时(``ImportError``)给中文友好提示,退出码 1。
- dry-run 模式只打印 plan,不建立连接也不写状态文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from zata_ops.tunnel import _interactive, _runner, _state

console = Console()
app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "SSH 端口转发:local 对应 ssh -L(本机端口 → 远端目标),"
        "remote 对应 ssh -R(远端端口 → 本机目标)。"
        "前台常驻用 Ctrl+C 关闭;--background 守护到后台用 tunnel list/close 管理。"
    ),
)


def _resolve_ssh_user(explicit_user: str) -> str:
    """Return ``explicit_user`` if non-empty, else ``$USER`` or ``"root"``."""
    if explicit_user:
        return explicit_user
    return os.environ.get("USER") or os.environ.get("USERNAME") or "root"


def resolved_user_from_cli(explicit_user: str) -> str:
    """Public alias for :func:`_resolve_ssh_user` (used in prefill construction)."""
    return _resolve_ssh_user(explicit_user)


def _validate_direction(direction: str) -> None:
    if direction not in {"local", "remote"}:
        console.print(
            f"[red]--direction 必须是 local 或 remote,收到 {direction!r}[/red]"
        )
        raise typer.Exit(code=2)


def _print_open_dry_run(options: _runner.TunnelOptions) -> None:
    """Render the dry-run plan in the same style as ``env provision --dry-run``."""
    console.print("[bold green]zata-ops tunnel open --dry-run[/bold green]")
    auth_method = (
        "password"
        if options.ssh_password
        else "key"
        if options.ssh_key
        else "agent+discovered"
    )
    plan_payload = {
        "direction": options.direction,
        "ssh": {
            "host": options.ssh_host,
            "user": options.ssh_user,
            "port": options.ssh_port,
            "auth_method": auth_method,
            "key": options.ssh_key,
            "password_set": bool(options.ssh_password),
            "strict_host_key": options.strict_host_key,
        },
        "listen": {"host": options.bind_host, "port": options.bind_port},
        "target": {"host": options.target_host, "port": options.target_port},
        "background": options.background,
        "name": options.name or None,
        "reconnect": options.reconnect,
        "max_reconnect": options.max_reconnect,
        "equivalent_ssh_command": _runner.plan_to_argv_preview(options),
        "server_requirements": _server_requirements_for(options),
    }
    console.print_json(json.dumps(plan_payload, indent=2, ensure_ascii=False))


def _server_requirements_for(options: _runner.TunnelOptions) -> list[str]:
    """返回远端 sshd_config 需要满足的条件,用于 dry-run 提示。"""
    base_requirements = [
        "/etc/ssh/sshd_config 需包含: AllowTcpForwarding yes "
        "(或 local/remote,只要覆盖本命令方向即可)",
    ]
    if options.direction == "remote" and options.bind_host not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        base_requirements.append(
            f"--bind-host {options.bind_host} 不是 loopback 地址,"
            "服务端还需要 GatewayPorts yes 或 GatewayPorts clientspecified "
            "才会绑定到非 loopback 接口。"
        )
    return base_requirements


def _spawn_daemon(spec: _state.TunnelSpec) -> subprocess.Popen:
    """启动 :func:`_runner.serve_daemon` 子进程,stdout/stderr 写到 ``spec.log_path``。"""
    log_path = Path(spec.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab", buffering=0)
    spawn_argv = [sys.executable, "-m", "zata_ops", "tunnel", "run", spec.name]
    spawn_env = os.environ.copy()
    return subprocess.Popen(  # noqa: S603 - argv 是项目自有入口,非 shell
        spawn_argv,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=spawn_env,
        close_fds=True,
    )


def _default_name(direction: str) -> str:
    """Generate a stable default name like ``local-20260609-163012``."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{direction}-{timestamp}"


def _ensure_paramiko_available() -> None:
    """如果 paramiko 缺失,打针对当前环境的修复提示并退出码 1。"""
    if _runner.paramiko is None:
        # ``[ssh]`` 字面量会被 Rich 当 markup 吞掉,所以用 markup=False
        # 保持原文(里面包含 ``[ssh]`` / ``[tool]`` 等 PEP 508 extra 写法)。
        console.print(
            "[red]paramiko 未安装,无法使用 tunnel 子命令。[/red]\n"
            + _runner._paramiko_missing_hint(),
            markup=False,
        )
        raise typer.Exit(code=1)


@app.command("open")
def open_command(
    direction: str = typer.Option(
        "",
        "--direction",
        help="local (-L) 或 remote (-R);不传则进入交互表单",
    ),
    ssh_host: str = typer.Option("", help="SSH 跳板机地址"),
    bind_port: int = typer.Option(0, help="监听端口"),
    target_port: int = typer.Option(0, help="目标端口"),
    ssh_user: str = typer.Option("", help="SSH 用户,默认 $USER"),
    ssh_port: int = typer.Option(22, help="SSH 服务端口"),
    ssh_key: Optional[str] = typer.Option(
        None, help="私钥路径(与 --ssh-password 互斥)"
    ),
    ssh_password: Optional[str] = typer.Option(
        None,
        "--ssh-password",
        help=(
            "SSH 密码(与 --ssh-key 互斥)。前台模式仅内存;后台模式禁用。"
            " 会进入 shell 历史,生产环境建议改用 ssh-add 注入 ssh-agent。"
        ),
        hide_input=True,
    ),
    bind_host: str = typer.Option("127.0.0.1", help="监听地址"),
    target_host: str = typer.Option("127.0.0.1", help="目标主机"),
    strict_host_key: bool = typer.Option(
        False, "--strict-host-key", help="严格校验 host key(默认 warn+accept)"
    ),
    name: str = typer.Option("", "--name", help="后台实例名(后台模式必填或自动生成)"),
    background: bool = typer.Option(False, "--background", help="后台守护模式"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印 plan,不建立连接"),
    reconnect: bool = typer.Option(
        False,
        "--reconnect",
        help="SSH 断了自动重连(指数退避 1s→30s)",
    ),
    max_reconnect: int = typer.Option(
        0,
        "--max-reconnect",
        help="最大重试次数(0=无限,仅 --reconnect 时生效)",
        min=0,
    ),
) -> None:
    """建立 SSH 端口转发,前台常驻或后台守护。

    不传任何必填参数(主要是 ``--direction``)时,会进入交互表单逐项填写。
    """
    prefill_options = _runner.TunnelOptions(
        direction=direction,
        ssh_host=ssh_host,
        ssh_user=resolved_user_from_cli(ssh_user),
        ssh_port=ssh_port,
        ssh_key=ssh_key,
        bind_host=bind_host,
        bind_port=bind_port,
        target_host=target_host,
        target_port=target_port,
        strict_host_key=strict_host_key,
        name=name,
        background=background,
        dry_run=dry_run,
        ssh_password=ssh_password,
        reconnect=reconnect,
        max_reconnect=max_reconnect,
    )
    if not prefill_options.direction:
        try:
            options = _interactive.collect_options(prefill_options)
        except _interactive.FormCancelledError:
            console.print("[yellow]已取消[/yellow]")
            raise typer.Exit(code=130) from None
        except _runner.TunnelError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    else:
        _validate_direction(prefill_options.direction)
        options = prefill_options
    _ensure_paramiko_available()
    resolved_name = options.name or (
        _default_name(options.direction) if options.background else ""
    )
    if options.background and not resolved_name:
        console.print("[red]--background 模式必须指定 --name[/red]")
        raise typer.Exit(code=2)
    options = _runner.TunnelOptions(
        direction=options.direction,
        ssh_host=options.ssh_host,
        ssh_user=options.ssh_user,
        ssh_port=options.ssh_port,
        ssh_key=options.ssh_key,
        bind_host=options.bind_host,
        bind_port=options.bind_port,
        target_host=options.target_host,
        target_port=options.target_port,
        strict_host_key=options.strict_host_key,
        name=resolved_name,
        background=options.background,
        dry_run=options.dry_run,
        ssh_password=options.ssh_password,
        reconnect=options.reconnect,
        max_reconnect=options.max_reconnect,
    )
    # 后台模式 + 密码:拒绝,避免明文落盘。推荐用 ssh-add 注入 ssh-agent。
    if options.background and options.ssh_password:
        console.print(
            "[red]--background 模式与 --ssh-password 互斥:[/red]\n"
            "后台模式会把 spec 写到 ~/.local/share/zata-ops/tunnels/<name>.json,"
            "其中若含明文密码就有泄露风险。请二选一:\n"
            "  1) 用 ssh-add 注入 ssh-agent,再去掉 --ssh-password\n"
            "  2) 改用前台模式(去掉 --background),Ctrl+C 退出"
        )
        raise typer.Exit(code=1)
    if options.dry_run:
        _print_open_dry_run(options)
        return
    if not options.background:
        try:
            _runner.serve_foreground(options)
        except _runner.TunnelError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except KeyboardInterrupt:
            console.print("[yellow]Tunnel closed by Ctrl+C[/yellow]")
            return
        return
    # 后台模式
    spec = _state.TunnelSpec(
        name=resolved_name,
        direction=options.direction,
        bind_host=options.bind_host,
        bind_port=options.bind_port,
        target_host=options.target_host,
        target_port=options.target_port,
        ssh_host=options.ssh_host,
        ssh_user=options.ssh_user,
        ssh_port=options.ssh_port,
        ssh_key=options.ssh_key,
        strict_host_key=options.strict_host_key,
        pid=0,
        log_path="",
        started_at=_state.now_iso(),
        state="pending",
        reconnect=options.reconnect,
        max_reconnect=options.max_reconnect,
    )
    spec.log_path = str(_state.state_dir() / f"{spec.name}.log")
    _state.write_spec(spec)
    child = _spawn_daemon(spec)
    spec.pid = child.pid
    _state.write_spec(spec)
    console.print(
        f"[green]Spawned tunnel '{spec.name}' (pid {child.pid}); "
        f"log: {spec.log_path}[/green]"
    )
    if not _state.wait_for_ready(spec.name):
        console.print(
            f"[red]Tunnel '{spec.name}' 启动超时({int(_state.READY_WAIT_SECONDS)}s),"
            f"请查看 {spec.log_path} 排查。[/red]"
        )
        try:
            child.terminate()
        except OSError:
            pass
        raise typer.Exit(code=1)
    final_spec = _state.load_spec(spec.name)
    console.print(
        f"[green]Tunnel '{final_spec.name}' is ready "
        f"({final_spec.direction}: "
        f"{final_spec.bind_host}:{final_spec.bind_port} -> "
        f"{final_spec.target_host}:{final_spec.target_port})[/green]"
    )


@app.command("list")
def list_command() -> None:
    """列出所有后台隧道实例。"""
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
    table.add_column("DIR")
    table.add_column("BIND", style="green")
    table.add_column("TARGET", style="green")
    table.add_column("SSH", style="magenta")
    table.add_column("PID", justify="right")
    table.add_column("STATE")
    table.add_column("STARTED")
    for spec in live_specs:
        table.add_row(
            spec.name,
            spec.direction,
            f"{spec.bind_host}:{spec.bind_port}",
            f"{spec.target_host}:{spec.target_port}",
            f"{spec.ssh_user}@{spec.ssh_host}:{spec.ssh_port}",
            str(spec.pid),
            spec.state,
            spec.started_at,
        )
    console.print(table)


@app.command("status")
def status_command(
    name: str = typer.Argument(..., help="后台实例名"),
) -> None:
    """显示单个后台实例的明细 + 最近日志。"""
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
    spec_dict = spec.to_dict()
    console.print(f"[bold]Tunnel '{name}'[/bold]")
    console.print_json(json.dumps(spec_dict, indent=2, ensure_ascii=False))
    if spec.log_path and Path(spec.log_path).is_file():
        log_text = Path(spec.log_path).read_text(encoding="utf-8", errors="replace")
        tail_lines = log_text.splitlines()[-20:]
        console.print("[bold]--- last 20 log lines ---[/bold]")
        for line in tail_lines:
            console.print(line)


@app.command("close")
def close_command(
    name: str = typer.Argument(..., help="后台实例名"),
) -> None:
    """停止一个后台隧道实例。"""
    try:
        stopped = _state.close_spec(name)
    except _state.StateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if stopped:
        console.print(f"[green]Tunnel '{name}' 已停止。[/green]")
    else:
        console.print(f"[yellow]Tunnel '{name}' 未在运行,清理状态。[/yellow]")


@app.command("run", hidden=True)
def run_command(
    spec_name: str = typer.Argument(..., help="spec 文件名(spec.name)"),
) -> None:
    """隐藏入口:由 ``tunnel open --background`` 派生的子进程执行。"""
    try:
        _runner.serve_daemon(spec_name)
    except _runner.TunnelError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
