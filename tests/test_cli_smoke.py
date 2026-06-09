"""Smoke tests for the zata-ops CLI entry points.

Verifies that ``zata-ops --version``, ``--help``, and each top-level subcommand
help text load without raising. Uses :class:`typer.testing.CliRunner` so we
exercise the real Typer app, not a mock.
"""

from __future__ import annotations

from typer.testing import CliRunner

from zata_ops.cli import app


def test_version_prints_semver() -> None:
    runner = CliRunner()
    version_result = runner.invoke(app, ["--version"])
    assert version_result.exit_code == 0
    assert "zata-ops" in version_result.stdout


def test_root_help_lists_all_subcommands() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    for subcommand_name in ("db", "env", "logs", "dashboard"):
        assert subcommand_name in help_result.stdout


def test_db_help_lists_baseline_commands() -> None:
    runner = CliRunner()
    db_help_result = runner.invoke(app, ["db", "--help"])
    assert db_help_result.exit_code == 0
    for db_command_name in ("backup", "restore", "list", "check", "migrate"):
        assert db_command_name in db_help_result.stdout


def test_env_help_lists_subcommands() -> None:
    runner = CliRunner()
    env_help_result = runner.invoke(app, ["env", "--help"])
    assert env_help_result.exit_code == 0
    assert "provision" in env_help_result.stdout
    assert "fix" in env_help_result.stdout


def test_logs_help_lists_subcommands() -> None:
    runner = CliRunner()
    logs_help_result = runner.invoke(app, ["logs", "--help"])
    assert logs_help_result.exit_code == 0
    assert "tail" in logs_help_result.stdout
    assert "search" in logs_help_result.stdout


def test_dashboard_mock_renders() -> None:
    runner = CliRunner()
    dashboard_result = runner.invoke(app, ["dashboard", "--mock", "--project", "demo"])
    assert dashboard_result.exit_code == 0
    assert "demo" in dashboard_result.stdout


def test_db_migrate_help_clarifies_non_alembic_semantics() -> None:
    runner = CliRunner()
    migrate_help_result = runner.invoke(app, ["db", "migrate", "--help"])
    assert migrate_help_result.exit_code == 0
    assert "Alembic" in migrate_help_result.stdout
