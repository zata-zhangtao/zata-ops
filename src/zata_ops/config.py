"""Configuration loading for zata-ops.

Settings come from three layers, in increasing priority:

1. CLI flags (highest)
2. Project-local ``.env`` / ``.env.local``
3. User-global ``~/.config/zata-ops/config.toml`` (lowest)

CLI commands typically build an :class:`OpsSettings` instance, then let the
caller override individual fields with CLI flags before executing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsSettings(BaseSettings):
    """Operational settings loaded from project ``.env`` files.

    Attributes:
        project: Project slug, used to label backups and dashboards.
        database_url: Database connection URL (PostgreSQL or SQLite).
        s3_endpoint: S3-compatible endpoint URL.
        s3_access_key: S3 access key.
        s3_secret_key: S3 secret key.
        s3_bucket: S3 bucket name (default ``app-backups``).
        s3_prefix: S3 key prefix (default ``app-backups``).
        s3_addressing_style: S3 addressing style (``path``, ``virtual``, ``auto``).
        backup_time: Daily backup scheduled time (HH:MM, kept for compatibility
            with externally scheduled cron/timer setups, not used by the CLI
            itself anymore).
        full_backup_day: ISO weekday for full backups (Monday=0, Sunday=6).
        logs_dir: Local directory containing application logs to archive.
        resources_dir: Local directory containing application resources to archive.
        work_dir: Local scratch directory for staging backup artifacts.
        retention_days: How many days of backups to retain in S3.
    """

    project: str = "app"
    database_url: str = ""
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "app-backups"
    s3_prefix: str = "app-backups"
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    backup_time: str = "18:00"
    full_backup_day: int = 6
    logs_dir: str = "/app/backend/logs"
    resources_dir: str = "/app/backend/data"
    work_dir: str = "/tmp/backups"
    retention_days: int = 30

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


def load_settings(working_dir: Path | None = None) -> OpsSettings:
    """Load :class:`OpsSettings` from the given working directory.

    Args:
        working_dir: Directory to look for ``.env`` / ``.env.local``. Defaults
            to the current working directory.

    Returns:
        Populated :class:`OpsSettings` instance. Empty string fields signal
        that no value was provided; callers should validate before use.
    """
    base_working_dir = Path(working_dir) if working_dir is not None else Path.cwd()
    env_file_paths = (
        str(base_working_dir / ".env"),
        str(base_working_dir / ".env.local"),
    )
    loaded_settings_obj = OpsSettings(_env_file=env_file_paths)  # type: ignore[call-arg]
    return loaded_settings_obj
