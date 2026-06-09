"""``tunnel open`` 的交互式表单。

当 ``open_command`` 检测到用户没有传任何必填 flag 时,会调用
:func:`collect_options` 一次问完所有字段。底层用
:mod:`questionary`,在 TTY 下支持 arrow keys、tab 自动补全、密码
隐藏输入等。

非 TTY 环境(例如 CI、pipe)直接拒绝并提示用 --flag 形式。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import questionary
from questionary import Choice

from zata_ops.tunnel import _runner

# 端口范围校验(避免用户输入超出 TCP 端口范围)
_PORT_RANGE = range(1, 65536)


class FormCancelledError(Exception):
    """用户按 Ctrl+C 退出表单。"""


def _ensure_tty() -> None:
    """非 TTY 环境下不允许进入交互表单,直接报错退出。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise _runner.TunnelError(
            "当前不在 TTY 环境下,无法使用交互表单。"
            "请显式传 --direction / --ssh-host / --bind-port / --target-port 等参数,"
            "或附加 --dry-run 预览 plan。"
        )


def _default_ssh_user() -> str:
    """Return ``$USER`` (POSIX) / ``$USERNAME`` (Windows) or ``"root"``."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or "root"


def _ask_direction() -> str:
    """弹一个 arrow-key select,选 ``local`` 还是 ``remote``。"""
    answer = questionary.select(
        "隧道方向(对应 ssh -L 还是 ssh -R)?",
        choices=[
            Choice("local  (本机端口 → 远端目标,对应 ssh -L)", value="local"),
            Choice("remote (远端端口 → 本机目标,对应 ssh -R)", value="remote"),
        ],
        qmark="?",
    ).ask()
    if answer is None:
        raise FormCancelledError()
    return answer


def _ask_ssh_user() -> str:
    """弹一个文本输入,缺省用 ``$USER``。"""
    answer = questionary.text(
        "SSH 用户名?",
        default=_default_ssh_user(),
    ).ask()
    if answer is None:
        raise FormCancelledError()
    answer = answer.strip()
    return answer or _default_ssh_user()


def _ask_optional_ssh_key() -> Optional[str]:
    """可选的私钥路径;回车跳过。"""
    # 注意:questionary.path() 的合法参数是 ``only_directories``(默认 False,即文件可走),
    # 没有 ``only_files``。之前误传 ``only_files=True`` 会让新版 prompt-toolkit
    # 的 PromptSession 抛 TypeError。
    answer = questionary.path(
        "SSH 私钥路径(留空则自动探测 ~/.ssh/id_* 与 ssh-agent)?",
        default="",
    ).ask()
    if answer is None:
        raise FormCancelledError()
    answer = (answer or "").strip()
    return answer or None


def _ask_int_port(prompt: str, default: int) -> int:
    """弹一个 int 端口输入,带范围校验。"""
    while True:
        raw = questionary.text(
            prompt,
            default=str(default),
            validate=lambda value, _range=_PORT_RANGE: (
                value.isdigit() and int(value) in _range
            )
            or "端口必须是 1-65535 的整数",
        ).ask()
        if raw is None:
            raise FormCancelledError()
        if raw.strip().isdigit():
            parsed = int(raw.strip())
            if parsed in _PORT_RANGE:
                return parsed


def _ask_text(prompt: str, default: str) -> str:
    """弹一个文本输入,缺省值可回车跳过。"""
    answer = questionary.text(prompt, default=default).ask()
    if answer is None:
        raise FormCancelledError()
    return (answer or "").strip() or default


def _ask_confirm(prompt: str, default: bool) -> bool:
    """弹一个 y/N 确认。"""
    answer = questionary.confirm(prompt, default=default).ask()
    if answer is None:
        raise FormCancelledError()
    return bool(answer)


def _ask_name(default_value: str) -> str:
    """后台模式下,弹一个文本输入要 name。"""
    answer = questionary.text(
        "后台实例名(留空使用自动生成的时间戳名)?",
        default=default_value,
    ).ask()
    if answer is None:
        raise FormCancelledError()
    return (answer or "").strip()


def _ask_auth_method(prefill: _runner.TunnelOptions) -> str:
    """弹一个 select,选 SSH 认证方式。返回 ``"key"`` 或 ``"password"``。"""
    default_method = "password" if prefill.ssh_password else "key"
    answer = questionary.select(
        "SSH 认证方式?",
        choices=[
            Choice("私钥 (--ssh-key 或自动探测 ~/.ssh/id_* 与 ssh-agent)", value="key"),
            Choice("密码 (前台专用;后台模式禁用,避免明文落盘)", value="password"),
        ],
        default=default_method,
    ).ask()
    if answer is None:
        raise FormCancelledError()
    return answer


def _ask_ssh_password() -> str:
    """弹一个隐藏输入的密码,不会回显到终端。"""
    answer = questionary.password("SSH 密码(输入时不回显,前台内存使用,绝不落盘)?").ask()
    if answer is None:
        raise FormCancelledError()
    return answer


def collect_options(prefill: _runner.TunnelOptions) -> _runner.TunnelOptions:
    """通过交互表单收集 :class:`TunnelOptions`。

    Args:
        prefill: 已有的 CLI 解析结果(可能部分字段已填),表单中相应
            字段会使用 prefill 的值作为默认。

    Returns:
        完整填充的 :class:`TunnelOptions`;``background=True`` 时
        ``name`` 必填(空时由调用方生成时间戳名)。

    Raises:
        _runner.TunnelError: 非 TTY 环境。
        FormCancelledError: 用户按 Ctrl+C 中断。
    """
    _ensure_tty()
    resolved_direction = prefill.direction or _ask_direction()
    ssh_host = _ask_text(
        "SSH 跳板机地址?",
        prefill.ssh_host or "",
    )
    if not ssh_host:
        raise _runner.TunnelError("--ssh-host 不能为空")
    ssh_user = _ask_ssh_user() if not prefill.ssh_user else prefill.ssh_user
    ssh_port = _ask_int_port(
        "SSH 服务端口?",
        prefill.ssh_port or 22,
    )
    # 认证方式:CLI 已显式给了 key 或 password 就尊重,否则弹 select 让用户选
    if prefill.ssh_key:
        ssh_key = prefill.ssh_key
        ssh_password = None
    elif prefill.ssh_password:
        ssh_key = None
        ssh_password = prefill.ssh_password
    else:
        auth_method = _ask_auth_method(prefill)
        if auth_method == "key":
            ssh_key = _ask_optional_ssh_key()
            ssh_password = None
        else:
            ssh_key = None
            ssh_password = _ask_ssh_password()
    # bind / target 的语义随方向完全相反,prompt 文案要分别写清楚。
    # -L: bind 在你电脑(你连它进入隧道),target 在远端(SSH 跳板机看到的)
    # -R: bind 在远端 SSH 服务器(别人连它进入隧道),target 在你电脑
    if resolved_direction == "local":
        bind_host_prompt = (
            "监听地址(你本机的地址,你从这个地址进入隧道,127.0.0.1=只本机能连)?"
        )
        target_host_prompt = (
            f"目标主机(从 {ssh_host} 视角看到的服务主机;"
            "127.0.0.1=跳板机本地服务,内网 IP/域名=跳板机可访问到的其他机器)?"
        )
    else:
        bind_host_prompt = (
            f"监听地址({ssh_host} 上的地址,远端用户从这里连进来转回你;"
            "127.0.0.1=只跳板机能连,0.0.0.0=全部可连需 GatewayPorts)?"
        )
        target_host_prompt = "目标主机(你本机上的服务地址,127.0.0.1=只本机 loopback)?"
    bind_host = _ask_text(bind_host_prompt, prefill.bind_host or "127.0.0.1")
    bind_port = _ask_int_port(
        "监听端口?",
        prefill.bind_port or 0,
    )
    target_host = _ask_text(target_host_prompt, prefill.target_host or "127.0.0.1")
    target_port = _ask_int_port(
        "目标端口?",
        prefill.target_port or 0,
    )
    background = _ask_confirm(
        "后台守护?(No 走前台,Ctrl+C 退出)",
        default=prefill.background,
    )
    name_value = prefill.name
    if background and not name_value:
        name_value = _ask_name("")
    # dry_run 与 ssh_user / ssh_key 一致:CLI 已显式设了就尊重,否则表单默认 True(推荐先 dry-run)
    if prefill.dry_run:
        dry_run = True
    else:
        dry_run = _ask_confirm(
            "先 dry-run 预览一下 plan 再执行?",
            default=True,
        )
    # 自动重连:CLI 已显式设了就尊重,否则表单默认 False(给个开关让用户显式打开)
    if prefill.reconnect:
        reconnect = True
    else:
        reconnect = _ask_confirm(
            "SSH 断了是否自动重连?(指数退避 1s→30s,推荐长期后台任务开)",
            default=prefill.reconnect,
        )
    return _runner.TunnelOptions(
        direction=resolved_direction,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_key=ssh_key,
        bind_host=bind_host,
        bind_port=bind_port,
        target_host=target_host,
        target_port=target_port,
        strict_host_key=prefill.strict_host_key,
        name=name_value,
        background=background,
        dry_run=dry_run,
        ssh_password=ssh_password,
        reconnect=reconnect,
        max_reconnect=prefill.max_reconnect,
    )
