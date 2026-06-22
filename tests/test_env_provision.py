"""Tests for ``zata-ops env`` commands and shell-template rendering."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.env import _runner


def test_load_shell_template_returns_install_traefik_contents() -> None:
    rendered_install_script = _runner.load_shell_template("install-docker-traefik.sh")
    assert "TRAEFIK_NETWORK" in rendered_install_script
    assert "Docker" in rendered_install_script


def test_env_provision_dry_run_emits_ssh_argv() -> None:
    runner = CliRunner()
    provision_result = runner.invoke(
        app,
        [
            "env",
            "provision",
            "--host",
            "example.com",
            "--user",
            "deploy",
            "--profile",
            "vps-traefik",
            "--acme-email",
            "ops@example.com",
            "--dry-run",
        ],
    )
    assert provision_result.exit_code == 0
    plan_payload = json.loads(
        provision_result.stdout.split("dry-run[/bold green]", 1)[-1].strip()
        if "dry-run[/bold green]" in provision_result.stdout
        else provision_result.stdout.split("\n", 1)[1]
    )
    assert plan_payload["host"] == "example.com"
    assert plan_payload["acme_email"] == "ops@example.com"
    assert any(
        "ssh" in command_entry.get("argv", [""])[0]
        for command_entry in plan_payload["remote_commands"]
        if "argv" in command_entry
    )


def test_env_fix_dry_run_includes_email_arg() -> None:
    runner = CliRunner()
    fix_result = runner.invoke(
        app,
        [
            "env",
            "fix",
            "--host",
            "example.com",
            "--email",
            "ops@example.com",
            "--dry-run",
        ],
    )
    assert fix_result.exit_code == 0
    assert "ops@example.com" in fix_result.stdout


def test_env_provision_with_monitoring_dry_run_includes_monitoring_deploy() -> None:
    runner = CliRunner()
    provision_result = runner.invoke(
        app,
        [
            "env",
            "provision",
            "--host",
            "example.com",
            "--user",
            "deploy",
            "--profile",
            "vps-traefik",
            "--acme-email",
            "ops@example.com",
            "--with-monitoring",
            "--dry-run",
        ],
    )
    assert provision_result.exit_code == 0
    plan_payload = json.loads(
        provision_result.stdout.split("dry-run[/bold green]", 1)[-1].strip()
        if "dry-run[/bold green]" in provision_result.stdout
        else provision_result.stdout.split("\n", 1)[1]
    )
    assert plan_payload["with_monitoring"] is True
    monitoring_commands = [
        command_entry
        for command_entry in plan_payload["remote_commands"]
        if "Vector" in command_entry.get("description", "")
    ]
    assert monitoring_commands
    assert "Grafana" in monitoring_commands[0]["description"]
    assert "/opt/apps/monitoring" in monitoring_commands[0]["argv"][-1]
