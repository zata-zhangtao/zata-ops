"""Shell template renderer and (optional) SSH executor.

The bundled shell templates under ``zata_ops/env/templates/`` are the canonical
implementation of VPS provisioning and Traefik fixes; this runner keeps them
as opaque shell payloads, exposes a dry-run mode that prints the planned
commands without executing them, and offers an opt-in SSH/local execution
path for users who want zata-ops to drive the actual install.
"""

from __future__ import annotations

import base64
import io
import shlex
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
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
    with_monitoring: bool = False,
    monitoring_domain: str | None = None,
) -> dict[str, Any]:
    """Build a dry-run plan describing the upcoming provisioning run.

    Args:
        host: Target hostname.
        user: SSH user.
        ssh_key: Optional path to the SSH private key.
        profile: Provisioning profile (e.g. ``vps-traefik``).
        acme_email: Optional ACME email for Let's Encrypt.
        traefik_network: Traefik Docker network name.
        with_monitoring: Whether to deploy the bundled monitoring stack.
        monitoring_domain: Domain used for Grafana Traefik routing.

    Returns:
        Structured plan, including the rendered shell scripts.
    """
    install_traefik_script = load_shell_template("install-docker-traefik.sh")
    bootstrap_script = load_shell_template("bootstrap.sh")
    remote_commands: list[dict[str, Any]] = [
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
    ]

    if with_monitoring:
        monitoring_bundle_b64 = _pack_monitoring_bundle()
        deploy_monitoring_script = _build_deploy_monitoring_script(
            bundle_b64=monitoring_bundle_b64,
            traefik_network=traefik_network,
            domain=monitoring_domain or _derive_monitoring_domain(acme_email),
        )
        remote_commands.append(
            {
                "description": "Deploy Vector + Loki + Prometheus + Grafana stack",
                "argv": _build_remote_argv(
                    user=user,
                    host=host,
                    ssh_key=ssh_key,
                    inline_script=deploy_monitoring_script,
                    env={
                        "TRAEFIK_NETWORK": traefik_network,
                        "DOMAIN": monitoring_domain
                        or _derive_monitoring_domain(acme_email),
                    },
                ),
            }
        )

    remote_commands.append(
        {
            "description": (
                "bootstrap.sh is intended to run locally; copy it to a "
                "workstation with SSH access and execute manually if you "
                "want zata-ops to skip the deploy-user setup step."
            ),
            "script_length_bytes": len(bootstrap_script),
        },
    )

    return {
        "profile": profile,
        "host": host,
        "user": user,
        "ssh_key": ssh_key,
        "traefik_network": traefik_network,
        "acme_email": acme_email,
        "with_monitoring": with_monitoring,
        "monitoring_domain": monitoring_domain,
        "remote_commands": remote_commands,
    }


def _derive_monitoring_domain(acme_email: str | None) -> str:
    """Return a placeholder domain for Grafana when none is provided.

    The monitoring stack requires a ``DOMAIN`` value for Traefik routing. In a
    real deployment the caller should pass ``--monitoring-domain``; this helper
    only keeps dry-run output valid when no domain has been supplied.
    """
    if acme_email and "@" in acme_email:
        return f"example.{acme_email.split('@', 1)[1]}"
    return "example.com"


def _pack_monitoring_bundle() -> str:
    """Pack ``deploy/monitoring/`` into a base64-encoded gzip tarball.

    Returns:
        Base64 string of the tarball, suitable for embedding in a shell script.
    """
    runner_path = Path(__file__).resolve()
    monitoring_dir = runner_path.parents[3] / "deploy" / "monitoring"
    if not monitoring_dir.is_dir():
        raise FileNotFoundError(
            f"Monitoring bundle directory not found: {monitoring_dir}"
        )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(monitoring_dir, arcname="monitoring")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _build_deploy_monitoring_script(
    *,
    bundle_b64: str,
    traefik_network: str,
    domain: str,
) -> str:
    """Render the inline bash script that deploys the monitoring stack.

    The script unpacks the bundled ``deploy/monitoring/`` configuration onto
    ``/opt/apps/monitoring`` and starts the stack with Docker Compose.
    """
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

MONITORING_DIR="/opt/apps/monitoring"
BUNDLE_B64='{bundle_b64}'
DOMAIN="{domain}"
TRAEFIK_NETWORK="{traefik_network}"

log() {{
  printf '\\n[%s] %s\\n' "$(date +'%H:%M:%S')" "$*"
}}

log "创建监控栈目录：$MONITORING_DIR"
mkdir -p "$MONITORING_DIR"
cd "$MONITORING_DIR"

log "解压监控栈配置..."
echo "$BUNDLE_B64" | base64 -d | tar -xzf - --strip-components=1 -C "$MONITORING_DIR"

log "写入环境变量..."
cat > "$MONITORING_DIR/.env" <<EOF
DOMAIN=$DOMAIN
TRAEFIK_NETWORK=$TRAEFIK_NETWORK
GRAFANA_ROOT_URL=https://grafana.$DOMAIN
GF_SECURITY_ADMIN_PASSWORD=admin
EOF

log "启动监控栈..."
docker compose up -d

log "监控栈部署完成。Grafana: https://grafana.$DOMAIN"
"""


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
