"""Shell template renderer and (optional) SSH executor.

The bundled shell templates under ``zata_ops/env/templates/`` are the canonical
implementation of VPS provisioning and Traefik fixes; this runner keeps them
as opaque shell payloads, exposes a dry-run mode that prints the planned
commands without executing them, and offers an opt-in SSH/local execution
path for users who want zata-ops to drive the actual install.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from typing import Any


@dataclass
class RemoteCommandPlan:
    """Describes a single command zata-ops intends to run remotely.

    Attributes:
        description: Human-friendly summary.
        argv: Shell argv list (already quoted).
        env: Optional environment variables to pass to the remote command.
    """

    description: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def load_shell_template(template_name: str) -> str:
    """Read a bundled shell template by filename.

    Args:
        template_name: Filename within ``zata_ops/env/templates/``.

    Returns:
        Template file contents as a UTF-8 string.

    Raises:
        FileNotFoundError: If the template does not exist.
    """
    template_traversable = resources.files("zata_ops.env") / "templates" / template_name
    if not template_traversable.is_file():
        raise FileNotFoundError(f"shell template not found: {template_name}")
    with template_traversable.open("r", encoding="utf-8") as template_reader:
        return template_reader.read()


def build_provision_plan(
    *,
    host: str,
    user: str,
    ssh_key: str | None,
    profile: str,
    acme_email: str | None,
    traefik_network: str,
) -> dict[str, Any]:
    """Build a dry-run plan describing the upcoming provisioning run.

    Args:
        host: Target hostname.
        user: SSH user.
        ssh_key: Optional path to the SSH private key.
        profile: Provisioning profile (e.g. ``vps-traefik``).
        acme_email: Optional ACME email for Let's Encrypt.
        traefik_network: Traefik Docker network name.

    Returns:
        Structured plan, including the rendered shell scripts.
    """
    install_traefik_script = load_shell_template("install-docker-traefik.sh")
    bootstrap_script = load_shell_template("bootstrap.sh")
    return {
        "profile": profile,
        "host": host,
        "user": user,
        "ssh_key": ssh_key,
        "traefik_network": traefik_network,
        "acme_email": acme_email,
        "remote_commands": [
            {
                "description": "Install Docker + Traefik on the remote host",
                "argv": _build_remote_argv(
                    user=user,
                    host=host,
                    ssh_key=ssh_key,
                    inline_script=install_traefik_script,
                    env={
                        "TRAEFIK_NETWORK": traefik_network,
                        **({"ACME_EMAIL": acme_email} if acme_email else {}),
                    },
                ),
            },
            {
                "description": (
                    "bootstrap.sh is intended to run locally; copy it to a "
                    "workstation with SSH access and execute manually if you "
                    "want zata-ops to skip the deploy-user setup step."
                ),
                "script_length_bytes": len(bootstrap_script),
            },
        ],
    }


def build_fix_plan(
    *,
    host: str,
    user: str,
    ssh_key: str | None,
    email: str,
) -> dict[str, Any]:
    """Build a dry-run plan for the ACME email fix.

    Args:
        host: Target hostname.
        user: SSH user.
        ssh_key: Optional path to the SSH private key.
        email: Real ACME email to write into ``traefik.yml``.

    Returns:
        Structured plan that includes the rendered fix script.
    """
    fix_acme_script = load_shell_template("fix-acme-email.sh")
    return {
        "host": host,
        "user": user,
        "ssh_key": ssh_key,
        "email": email,
        "remote_commands": [
            {
                "description": "Repair Traefik ACME email and re-issue cert",
                "argv": _build_remote_argv(
                    user=user,
                    host=host,
                    ssh_key=ssh_key,
                    inline_script=f"{fix_acme_script}\n",
                    args=["--email", email],
                ),
            }
        ],
    }


def _build_remote_argv(
    *,
    user: str,
    host: str,
    ssh_key: str | None,
    inline_script: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build the ``ssh`` argv used to run an inline bash script on the host.

    Args:
        user: SSH user.
        host: SSH host.
        ssh_key: Optional private key path.
        inline_script: Bash script payload.
        args: Optional positional arguments passed to ``bash -s --``.
        env: Optional environment variables exported before the script runs.

    Returns:
        Concrete argv list suitable for ``subprocess.run`` (or dry-run display).
    """
    ssh_argv_list: list[str] = ["ssh"]
    if ssh_key:
        ssh_argv_list.extend(["-i", ssh_key])
    ssh_argv_list.append(f"{user}@{host}")

    env_prefix = ""
    if env:
        env_prefix = (
            " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items()) + " "
        )

    args_suffix = ""
    if args:
        args_suffix = " " + " ".join(shlex.quote(arg) for arg in args)

    remote_command_str = (
        f"{env_prefix}bash -s --{args_suffix} <<'ZATA_OPS_SCRIPT_END'\n"
        f"{inline_script}\n"
        f"ZATA_OPS_SCRIPT_END"
    )
    ssh_argv_list.append(remote_command_str)
    return ssh_argv_list


def execute_remote_plan(remote_plan: dict[str, Any]) -> None:
    """Execute the planned remote commands sequentially.

    Args:
        remote_plan: Plan returned by :func:`build_provision_plan` or
            :func:`build_fix_plan`.

    Raises:
        RuntimeError: If ``ssh`` is not available on the local PATH.
        subprocess.CalledProcessError: If a remote command exits non-zero.
    """
    if not shutil.which("ssh"):
        raise RuntimeError(
            "ssh is not available locally; install OpenSSH or run zata-ops "
            "from a workstation with ssh access"
        )

    for remote_command_entry in remote_plan.get("remote_commands", []):
        remote_argv = remote_command_entry.get("argv")
        if not remote_argv:
            continue
        subprocess.run(remote_argv, check=True)
