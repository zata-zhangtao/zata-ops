"""Tests for ``zata-ops logs`` dry-run plans and dashboard smoke render."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from zata_ops.cli import app


def test_logs_tail_dry_run_emits_docker_argv() -> None:
    runner = CliRunner()
    tail_result = runner.invoke(
        app,
        [
            "logs",
            "tail",
            "--project",
            "demo",
            "--since",
            "1h",
            "--dry-run",
        ],
    )
    assert tail_result.exit_code == 0
    tail_payload = json.loads(
        tail_result.stdout.split("dry-run[/bold green]", 1)[-1].strip()
        if "dry-run[/bold green]" in tail_result.stdout
        else tail_result.stdout.split("\n", 1)[1]
    )
    assert tail_payload["source"] == "docker"
    assert tail_payload["argv"][0] == "docker"
    assert "demo-backend" in tail_payload["argv"]


def test_logs_search_dry_run_describes_pipeline() -> None:
    runner = CliRunner()
    search_result = runner.invoke(
        app,
        [
            "logs",
            "search",
            "ERROR",
            "--project",
            "demo",
            "--dry-run",
        ],
    )
    assert search_result.exit_code == 0
    search_payload = json.loads(
        search_result.stdout.split("dry-run[/bold green]", 1)[-1].strip()
        if "dry-run[/bold green]" in search_result.stdout
        else search_result.stdout.split("\n", 1)[1]
    )
    assert search_payload["pattern"] == "ERROR"
    assert search_payload["consumer"][0] == "grep"


def test_dashboard_mock_includes_service_table() -> None:
    runner = CliRunner()
    dashboard_result = runner.invoke(app, ["dashboard", "--mock", "--project", "alpha"])
    assert dashboard_result.exit_code == 0
    assert "alpha-backend" in dashboard_result.stdout
    assert "running" in dashboard_result.stdout
