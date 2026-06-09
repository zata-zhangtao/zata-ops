"""Typer commands for ``zata-ops db``.

Each subcommand pulls defaults from :class:`zata_ops.config.OpsSettings`
(loaded from project ``.env``) and lets explicit CLI flags override them.
``--dry-run`` short-circuits before any network call so the same command can
be safely used in CI / smoke tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from zata_ops.config import OpsSettings, load_settings
from zata_ops.db import _backup_impl, _restore_impl
from zata_ops.db._s3 import S3Client, build_backup_plan

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _resolve(value: Optional[str], default: str) -> str:
    """Return ``value`` if provided, otherwise ``default``."""
    return value if value is not None and value != "" else default


def _settings_or_die() -> OpsSettings:
    """Load settings from the current working directory."""
    return load_settings()


@app.command("backup")
def backup_command(
    project: Optional[str] = typer.Option(
        None, help="Project slug used in S3 keys/manifest."
    ),
    db_url: Optional[str] = typer.Option(
        None, help="Database URL (overrides DATABASE_URL)."
    ),
    s3_endpoint: Optional[str] = typer.Option(None, help="S3 endpoint URL."),
    s3_bucket: Optional[str] = typer.Option(None, help="S3 bucket name."),
    s3_access_key: Optional[str] = typer.Option(None, help="S3 access key."),
    s3_secret_key: Optional[str] = typer.Option(None, help="S3 secret key."),
    s3_prefix: Optional[str] = typer.Option(None, help="S3 key prefix."),
    s3_addressing_style: Optional[str] = typer.Option(
        None, help="S3 addressing style: path, virtual, or auto."
    ),
    retention_days: Optional[int] = typer.Option(
        None, help="Retention window in days."
    ),
    logs_dir: Optional[str] = typer.Option(
        None, help="Local logs directory to archive."
    ),
    resources_dir: Optional[str] = typer.Option(
        None, help="Local resources directory to archive."
    ),
    work_dir: Optional[str] = typer.Option(None, help="Local scratch directory."),
    full_backup_day: Optional[int] = typer.Option(
        None, help="ISO weekday for full backups (Monday=0)."
    ),
    force_full: bool = typer.Option(
        False,
        "--force-full",
        help="Treat this run as a full backup regardless of weekday.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the planned S3 keys and exit; no network calls."
    ),
) -> None:
    """Back up the database, logs, and resources to S3.

    Loads defaults from the project's ``.env`` (or ``.env.local``) and lets
    each flag override an individual field. Use ``--dry-run`` to preview the
    plan without making any network calls — this is the recommended path for
    CI smoke validation.
    """
    project_settings = _settings_or_die()
    effective_project_slug = _resolve(project, project_settings.project)
    effective_db_url = _resolve(db_url, project_settings.database_url)
    effective_s3_endpoint = _resolve(s3_endpoint, project_settings.s3_endpoint)
    effective_s3_bucket = _resolve(s3_bucket, project_settings.s3_bucket)
    effective_s3_access_key = _resolve(s3_access_key, project_settings.s3_access_key)
    effective_s3_secret_key = _resolve(s3_secret_key, project_settings.s3_secret_key)
    effective_s3_prefix = _resolve(s3_prefix, project_settings.s3_prefix)
    effective_s3_addressing_style = _resolve(
        s3_addressing_style, project_settings.s3_addressing_style
    )
    effective_retention_days = (
        retention_days
        if retention_days is not None
        else project_settings.retention_days
    )
    effective_logs_dir = _resolve(logs_dir, project_settings.logs_dir)
    effective_resources_dir = _resolve(resources_dir, project_settings.resources_dir)
    effective_work_dir = _resolve(work_dir, project_settings.work_dir)
    effective_full_backup_day = (
        full_backup_day
        if full_backup_day is not None
        else project_settings.full_backup_day
    )

    if dry_run:
        plan_now_dt = datetime.now(timezone.utc)
        plan_timestamp = plan_now_dt.strftime("%Y-%m-%d_%H%M%S")
        backup_type_str = _backup_impl.determine_backup_type(
            plan_now_dt, effective_full_backup_day, force_full
        )
        dry_run_plan = build_backup_plan(
            project=effective_project_slug,
            s3_prefix=effective_s3_prefix,
            timestamp=plan_timestamp,
            backup_type=backup_type_str,
            include_logs=Path(effective_logs_dir).exists()
            and any(Path(effective_logs_dir).iterdir()),
            include_resources=Path(effective_resources_dir).exists()
            and any(Path(effective_resources_dir).iterdir()),
        )
        dry_run_plan["bucket"] = effective_s3_bucket
        dry_run_plan["retention_days"] = effective_retention_days
        dry_run_plan["addressing_style"] = effective_s3_addressing_style
        dry_run_plan["db_url"] = _redact_db_url(effective_db_url)
        console.print("[bold green]zata-ops db backup --dry-run[/bold green]")
        console.print_json(json.dumps(dry_run_plan))
        return

    backup_options = _backup_impl.BackupRunOptions(
        project=effective_project_slug,
        database_url=effective_db_url,
        s3_endpoint=effective_s3_endpoint,
        s3_access_key=effective_s3_access_key,
        s3_secret_key=effective_s3_secret_key,
        s3_bucket=effective_s3_bucket,
        s3_prefix=effective_s3_prefix,
        s3_addressing_style=effective_s3_addressing_style,
        logs_dir=effective_logs_dir,
        resources_dir=effective_resources_dir,
        work_dir=effective_work_dir,
        retention_days=effective_retention_days,
        full_backup_day=effective_full_backup_day,
        force_full=force_full,
    )
    backup_run_result = _backup_impl.run_backup(backup_options)

    console.print(
        f"[green]Backup {backup_run_result.timestamp} "
        f"({backup_run_result.backup_type}) complete[/green]"
    )
    for uploaded_key in backup_run_result.uploaded_keys:
        console.print(f"  uploaded: {uploaded_key}")
    if backup_run_result.deleted_old_objects:
        console.print(
            f"  pruned: {backup_run_result.deleted_old_objects} old object(s)"
        )
    if backup_run_result.errors:
        console.print(f"[yellow]errors: {backup_run_result.errors}[/yellow]")
        raise typer.Exit(code=1)


@app.command("restore")
def restore_command(
    target_date: str = typer.Option(
        ..., "--from", help="Backup date (YYYY-MM-DD_HHMMSS)."
    ),
    target_db_url: Optional[str] = typer.Option(None, help="Target database URL."),
    logs_dir: Optional[str] = typer.Option(None, help="Logs restore target directory."),
    resources_dir: Optional[str] = typer.Option(
        None, help="Resources restore target directory."
    ),
    s3_endpoint: Optional[str] = typer.Option(None),
    s3_bucket: Optional[str] = typer.Option(None),
    s3_access_key: Optional[str] = typer.Option(None),
    s3_secret_key: Optional[str] = typer.Option(None),
    s3_prefix: Optional[str] = typer.Option(None),
    s3_addressing_style: Optional[str] = typer.Option(None),
    restore_db: bool = typer.Option(
        False, "--restore-db", help="Restore the database."
    ),
    restore_logs: bool = typer.Option(False, "--restore-logs", help="Restore logs."),
    restore_resources: bool = typer.Option(
        False, "--restore-resources", help="Restore resources."
    ),
    chain: bool = typer.Option(
        False, "--chain", help="Apply full + incrementals chain."
    ),
    clean_target_schema: bool = typer.Option(
        False, "--clean-target-schema", help="Drop+recreate public schema first."
    ),
    drop_target_db: bool = typer.Option(
        False, "--drop-target-db", help="Drop+recreate target DB first (destructive)."
    ),
    sanitize_invalid_utf8: bool = typer.Option(
        False, "--sanitize-invalid-utf8", help="Replace invalid UTF-8 in the dump."
    ),
    verify_table: list[str] = typer.Option(
        [], "--verify-table", help="Require this table to exist with >0 rows."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Restore a backup from S3.

    Supports database / logs / resources restore, optional chain restore for
    incremental backups, and explicit destructive-operation flags. Pass
    ``--yes`` to skip the confirmation prompt when running non-interactively.
    """
    project_settings = _settings_or_die()
    restore_options = _restore_impl.RestoreRunOptions(
        target_date=target_date,
        database_url=_resolve(target_db_url, project_settings.database_url),
        logs_dir=_resolve(logs_dir, project_settings.logs_dir),
        resources_dir=_resolve(resources_dir, project_settings.resources_dir),
        s3_endpoint=_resolve(s3_endpoint, project_settings.s3_endpoint),
        s3_access_key=_resolve(s3_access_key, project_settings.s3_access_key),
        s3_secret_key=_resolve(s3_secret_key, project_settings.s3_secret_key),
        s3_bucket=_resolve(s3_bucket, project_settings.s3_bucket),
        s3_prefix=_resolve(s3_prefix, project_settings.s3_prefix),
        s3_addressing_style=_resolve(
            s3_addressing_style, project_settings.s3_addressing_style
        ),
        restore_db=restore_db,
        restore_logs=restore_logs,
        restore_resources=restore_resources,
        use_chain=chain,
        clean_target_schema=clean_target_schema,
        drop_target_db=drop_target_db,
        sanitize_invalid_utf8=sanitize_invalid_utf8,
        verify_tables=list(verify_table),
    )

    if not yes:
        confirm_message = (
            f"Restore from {target_date}? "
            f"db={restore_db} logs={restore_logs} resources={restore_resources}"
        )
        if drop_target_db:
            confirm_message += " [DROP TARGET DB]"
        if clean_target_schema:
            confirm_message += " [CLEAN PUBLIC SCHEMA]"
        if not typer.confirm(confirm_message):
            console.print("[yellow]Aborted by user.[/yellow]")
            raise typer.Exit(code=0)

    try:
        restore_run_result = _restore_impl.run_restore(restore_options)
    except ValueError as restore_value_exc:
        console.print(f"[red]{restore_value_exc}[/red]")
        raise typer.Exit(code=1)

    if not restore_run_result.success:
        for warning_msg in restore_run_result.warnings:
            console.print(f"[yellow]warn: {warning_msg}[/yellow]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Restore {target_date} ({restore_run_result.backup_type}) complete[/green]"
    )
    for restored_target_label in restore_run_result.restored_targets:
        console.print(f"  restored: {restored_target_label}")
    for warning_msg in restore_run_result.warnings:
        console.print(f"  warn: {warning_msg}")


@app.command("list")
def list_command(
    s3_endpoint: Optional[str] = typer.Option(None),
    s3_bucket: Optional[str] = typer.Option(None),
    s3_access_key: Optional[str] = typer.Option(None),
    s3_secret_key: Optional[str] = typer.Option(None),
    s3_prefix: Optional[str] = typer.Option(None),
    s3_addressing_style: Optional[str] = typer.Option(None),
) -> None:
    """List available backup dates and types from S3."""
    project_settings = _settings_or_die()
    s3_client = S3Client(
        endpoint=_resolve(s3_endpoint, project_settings.s3_endpoint),
        access_key=_resolve(s3_access_key, project_settings.s3_access_key),
        secret_key=_resolve(s3_secret_key, project_settings.s3_secret_key),
        bucket=_resolve(s3_bucket, project_settings.s3_bucket),
        prefix=_resolve(s3_prefix, project_settings.s3_prefix),
        addressing_style=_resolve(
            s3_addressing_style, project_settings.s3_addressing_style
        ),
    )

    available_backup_dates = s3_client.list_backup_dates()
    if not available_backup_dates:
        console.print("[yellow]No backups found.[/yellow]")
        return

    backups_table = Table(title="Available backups")
    backups_table.add_column("Date")
    backups_table.add_column("Type")
    for date_str in available_backup_dates:
        manifest_for_date = s3_client.get_backup_manifest(date_str)
        backup_type_str = (
            manifest_for_date.get("type", "unknown") if manifest_for_date else "unknown"
        )
        backups_table.add_row(date_str, backup_type_str)
    console.print(backups_table)


@app.command("check")
def check_command(
    s3_endpoint: Optional[str] = typer.Option(None),
    s3_bucket: Optional[str] = typer.Option(None),
    s3_access_key: Optional[str] = typer.Option(None),
    s3_secret_key: Optional[str] = typer.Option(None),
    s3_prefix: Optional[str] = typer.Option(None),
    s3_addressing_style: Optional[str] = typer.Option(None),
) -> None:
    """Verify S3 connectivity by uploading and deleting a small test object."""
    import uuid

    project_settings = _settings_or_die()
    effective_endpoint = _resolve(s3_endpoint, project_settings.s3_endpoint)
    effective_bucket = _resolve(s3_bucket, project_settings.s3_bucket)
    effective_prefix = _resolve(s3_prefix, project_settings.s3_prefix)

    s3_client = S3Client(
        endpoint=effective_endpoint,
        access_key=_resolve(s3_access_key, project_settings.s3_access_key),
        secret_key=_resolve(s3_secret_key, project_settings.s3_secret_key),
        bucket=effective_bucket,
        prefix=effective_prefix,
        addressing_style=_resolve(
            s3_addressing_style, project_settings.s3_addressing_style
        ),
    )
    diagnostic_object_key = f"{effective_prefix}/_diagnostics/{uuid.uuid4()}.txt"
    diagnostic_payload_bytes = b"zata-ops s3 diagnostic payload"

    console.print(
        f"[cyan]Checking S3:[/cyan] endpoint={effective_endpoint} bucket={effective_bucket}"
    )
    try:
        s3_client.client.head_bucket(Bucket=effective_bucket)
        s3_client.client.put_object(
            Bucket=effective_bucket,
            Key=diagnostic_object_key,
            Body=diagnostic_payload_bytes,
            ContentType="text/plain",
        )
        verification_response = s3_client.client.get_object(
            Bucket=effective_bucket, Key=diagnostic_object_key
        )
        downloaded_payload_bytes = verification_response["Body"].read()
        if downloaded_payload_bytes != diagnostic_payload_bytes:
            console.print("[red]Byte mismatch on round trip.[/red]")
            raise typer.Exit(code=1)
    finally:
        try:
            s3_client.client.delete_object(
                Bucket=effective_bucket, Key=diagnostic_object_key
            )
        except Exception:
            pass

    console.print("[green]S3 connectivity OK.[/green]")


@app.command("migrate")
def migrate_command(
    source_db_url: str = typer.Option(..., help="Source PostgreSQL URL (pg_dump)."),
    target_db_url: str = typer.Option(..., help="Target PostgreSQL URL (psql)."),
    work_dir: Optional[str] = typer.Option(None, help="Local scratch directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and exit."),
) -> None:
    """Migrate PostgreSQL data from one database to another.

    This is NOT an Alembic schema migration. It runs ``pg_dump`` on the
    source URL, then pipes the dump into ``psql`` on the target URL. Use it
    to copy data between environments (e.g. staging -> a fresh test DB).
    """
    project_settings = _settings_or_die()
    effective_work_dir_path = Path(_resolve(work_dir, project_settings.work_dir))
    effective_work_dir_path.mkdir(parents=True, exist_ok=True)
    migration_dump_path = effective_work_dir_path / "migrate.sql.gz"

    if dry_run:
        console.print("[bold green]zata-ops db migrate --dry-run[/bold green]")
        console.print_json(
            json.dumps(
                {
                    "source_db_url": _redact_db_url(source_db_url),
                    "target_db_url": _redact_db_url(target_db_url),
                    "intermediate_dump": str(migration_dump_path),
                    "note": "Data migration only — not Alembic schema migration.",
                }
            )
        )
        return

    from zata_ops.db._database import backup_database, restore_database

    console.print(f"[cyan]Dumping[/cyan] {_redact_db_url(source_db_url)}")
    backup_database(source_db_url, migration_dump_path)
    console.print(f"[cyan]Restoring[/cyan] into {_redact_db_url(target_db_url)}")
    restore_database(target_db_url, migration_dump_path)
    console.print("[green]Migration complete.[/green]")


def _redact_db_url(db_url: str) -> str:
    """Redact the password from a database URL for safe display.

    Args:
        db_url: Database URL.

    Returns:
        DB URL with the password component replaced by ``***``.
    """
    from urllib.parse import urlparse, urlunparse

    if not db_url:
        return ""
    try:
        parsed_db_url = urlparse(db_url)
    except Exception:
        return "<unparseable>"

    if parsed_db_url.password:
        safe_netloc = (
            f"{parsed_db_url.username}:***@{parsed_db_url.hostname}"
            f"{':' + str(parsed_db_url.port) if parsed_db_url.port else ''}"
        )
        return urlunparse(parsed_db_url._replace(netloc=safe_netloc))
    return db_url
