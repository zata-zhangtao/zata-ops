"""Typer commands for ``zata-ops env``: provision and fix VPS environments."""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console

from zata_ops.env import _runner

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command("provision")
def provision_command(
    host: str = typer.Option(..., help="Target server hostname or IP."),
    user: str = typer.Option("root", help="SSH user."),
    ssh_key: Optional[str] = typer.Option(None, help="Path to the SSH private key."),
    profile: str = typer.Option(
        "vps-traefik", help="Provisioning profile (currently only vps-traefik)."
    ),
    traefik_network: str = typer.Option(
        "traefik", help="Traefik external Docker network."
    ),
    acme_email: Optional[str] = typer.Option(
        None, help="Real email for Let's Encrypt (skip to use HTTP only)."
    ),
    with_monitoring: bool = typer.Option(
        False,
        "--with-monitoring",
        help="Also deploy the Vector + Loki + Prometheus + Grafana monitoring stack.",
    ),
    monitoring_domain: Optional[str] = typer.Option(
        None,
        "--monitoring-domain",
        help="Domain used for Grafana Traefik routing. Defaults to the parent of the first app domain if not set.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the planned remote commands and exit."
    ),
) -> None:
    """Provision a VPS (Docker + Traefik) using the bundled shell templates.

    By default this command only prints the SSH commands it would execute.
    The actual SSH execution path is opt-in: only runs when ``--dry-run`` is
    omitted and ``ssh`` is available on the local PATH.
    """
    if profile != "vps-traefik":
        console.print(f"[red]Unknown profile: {profile}[/red]")
        raise typer.Exit(code=2)

    provision_plan = _runner.build_provision_plan(
        host=host,
        user=user,
        ssh_key=ssh_key,
        profile=profile,
        acme_email=acme_email,
        traefik_network=traefik_network,
        with_monitoring=with_monitoring,
        monitoring_domain=monitoring_domain,
    )

    if dry_run:
        console.print("[bold green]zata-ops env provision --dry-run[/bold green]")
        console.print_json(json.dumps(provision_plan, indent=2))
        return

    _runner.execute_remote_plan(provision_plan)
    console.print(f"[green]Provisioned {host} via profile {profile}.[/green]")


@app.command("fix")
def fix_command(
    host: str = typer.Option(..., help="Target server hostname or IP."),
    user: str = typer.Option("root", help="SSH user."),
    ssh_key: Optional[str] = typer.Option(None, help="Path to the SSH private key."),
    email: str = typer.Option(..., help="Real ACME email for Let's Encrypt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the planned fix commands and exit."
    ),
) -> None:
    """Repair the Traefik ACME email and force certificate re-issuance.

    Wraps the ``fix-acme-email.sh`` template. The script auto-detects the
    Traefik install, rewrites the email line, removes stale acme.json, and
    waits for a fresh certificate.
    """
    fix_plan = _runner.build_fix_plan(
        host=host, user=user, ssh_key=ssh_key, email=email
    )
    if dry_run:
        console.print("[bold green]zata-ops env fix --dry-run[/bold green]")
        console.print_json(json.dumps(fix_plan, indent=2))
        return

    _runner.execute_remote_plan(fix_plan)
    console.print(f"[green]ACME email fix applied on {host}.[/green]")
