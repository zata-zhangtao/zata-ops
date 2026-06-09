"""Pure backup workflow: dump DB, archive logs/resources, upload, prune.

Splits the orchestration out of the typer wrapper so the same code can be
exercised by tests and by a future external scheduler. Side effects are
gated on ``BackupRunOptions`` only — no environment access.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zata_ops.db._archive import archive_directory
from zata_ops.db._database import backup_database
from zata_ops.db._s3 import S3Client


@dataclass
class BackupRunOptions:
    """Inputs required to run one backup cycle.

    Attributes:
        project: Project slug, written into the manifest.
        database_url: Database connection URL.
        s3_endpoint: S3 endpoint URL.
        s3_access_key: S3 access key.
        s3_secret_key: S3 secret key.
        s3_bucket: S3 bucket name.
        s3_prefix: S3 key prefix.
        s3_addressing_style: ``path``, ``virtual``, or ``auto``.
        logs_dir: Local logs directory to archive (skipped if missing/empty).
        resources_dir: Local resources directory to archive (skipped if missing/empty).
        work_dir: Scratch directory for staging artifacts before upload.
        retention_days: Retention window for pruning old S3 backups.
        full_backup_day: ISO weekday for full backups (Monday=0).
        force_full: If True, treat this run as a full backup regardless of day.
    """

    project: str
    database_url: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_prefix: str
    s3_addressing_style: str
    logs_dir: str
    resources_dir: str
    work_dir: str
    retention_days: int
    full_backup_day: int
    force_full: bool = False


@dataclass
class BackupRunResult:
    """Outcome of one backup cycle.

    Attributes:
        timestamp: Backup timestamp in ``YYYY-MM-DD_HHMMSS`` UTC.
        backup_type: ``full`` or ``incremental``.
        manifest: Manifest dictionary that was (or would be) uploaded.
        uploaded_keys: S3 keys actually uploaded.
        deleted_old_objects: Number of old S3 objects pruned by retention.
        errors: Mapping from logical filename to error message, if any.
    """

    timestamp: str
    backup_type: str
    manifest: dict[str, Any]
    uploaded_keys: list[str] = field(default_factory=list)
    deleted_old_objects: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _compute_sha256(file_path: Path) -> str:
    """Return the SHA-256 hex digest of a file.

    Args:
        file_path: Path to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    sha256_hasher = hashlib.sha256()
    with open(file_path, "rb") as binary_reader:
        for chunk_bytes in iter(lambda: binary_reader.read(8192), b""):
            sha256_hasher.update(chunk_bytes)
    return sha256_hasher.hexdigest()


def _is_full_backup_today(now: datetime, full_backup_day: int) -> bool:
    """Return whether today's weekday matches the full-backup day."""
    return now.weekday() == full_backup_day


def determine_backup_type(now: datetime, full_backup_day: int, force_full: bool) -> str:
    """Pick ``full`` or ``incremental`` based on the calendar and override.

    Args:
        now: Reference datetime (UTC).
        full_backup_day: ISO weekday for full backups (Monday=0).
        force_full: When True, return ``full`` regardless of weekday.

    Returns:
        ``"full"`` or ``"incremental"``.
    """
    if force_full or _is_full_backup_today(now, full_backup_day):
        return "full"
    return "incremental"


def run_backup(
    options: BackupRunOptions,
    s3_client_factory=S3Client,
    now: datetime | None = None,
) -> BackupRunResult:
    """Execute one backup cycle end-to-end.

    Args:
        options: Backup configuration.
        s3_client_factory: Callable returning an :class:`S3Client`-compatible
            object. Tests override this to inject a fake.
        now: Reference datetime (UTC). Defaults to ``datetime.now(timezone.utc)``.

    Returns:
        :class:`BackupRunResult` describing what was uploaded.

    Raises:
        ValueError: If required configuration values are missing.
    """
    if not options.database_url:
        raise ValueError("database_url is required")
    if not options.s3_endpoint:
        raise ValueError("s3_endpoint is required")
    if not options.s3_access_key:
        raise ValueError("s3_access_key is required")
    if not options.s3_secret_key:
        raise ValueError("s3_secret_key is required")

    effective_now_dt = now or datetime.now(timezone.utc)
    backup_timestamp: str = effective_now_dt.strftime("%Y-%m-%d_%H%M%S")
    backup_type_str = determine_backup_type(
        effective_now_dt, options.full_backup_day, options.force_full
    )

    backup_work_dir = Path(options.work_dir) / backup_timestamp
    backup_work_dir.mkdir(parents=True, exist_ok=True)

    backup_manifest: dict[str, Any] = {
        "timestamp": backup_timestamp,
        "type": backup_type_str,
        "project": options.project,
        "files": [],
    }

    s3_client = s3_client_factory(
        endpoint=options.s3_endpoint,
        access_key=options.s3_access_key,
        secret_key=options.s3_secret_key,
        bucket=options.s3_bucket,
        prefix=options.s3_prefix,
        addressing_style=options.s3_addressing_style,
    )

    backup_run_result = BackupRunResult(
        timestamp=backup_timestamp,
        backup_type=backup_type_str,
        manifest=backup_manifest,
    )

    db_dump_output_path = backup_work_dir / "database.sql.gz"
    try:
        backup_database(options.database_url, db_dump_output_path)
        db_s3_key = (
            f"{options.s3_prefix}/{backup_timestamp}/{backup_type_str}/database.sql.gz"
        )
        s3_client.upload_file(db_dump_output_path, db_s3_key)
        backup_manifest["files"].append(
            {
                "name": "database.sql.gz",
                "size": db_dump_output_path.stat().st_size,
                "s3_key": db_s3_key,
                "sha256": _compute_sha256(db_dump_output_path),
            }
        )
        backup_run_result.uploaded_keys.append(db_s3_key)
    except Exception as db_backup_exc:
        backup_manifest["database_error"] = str(db_backup_exc)
        backup_run_result.errors["database.sql.gz"] = str(db_backup_exc)

    _archive_and_upload(
        source_dir=Path(options.logs_dir),
        output_filename="logs.tar.gz",
        snapshot_filename=".snapshot.logs",
        work_dir=backup_work_dir,
        scratch_root=Path(options.work_dir),
        options=options,
        backup_timestamp=backup_timestamp,
        backup_type_str=backup_type_str,
        s3_client=s3_client,
        result=backup_run_result,
        manifest=backup_manifest,
    )
    _archive_and_upload(
        source_dir=Path(options.resources_dir),
        output_filename="resources.tar.gz",
        snapshot_filename=".snapshot.resources",
        work_dir=backup_work_dir,
        scratch_root=Path(options.work_dir),
        options=options,
        backup_timestamp=backup_timestamp,
        backup_type_str=backup_type_str,
        s3_client=s3_client,
        result=backup_run_result,
        manifest=backup_manifest,
    )

    manifest_s3_key = (
        f"{options.s3_prefix}/{backup_timestamp}/{backup_type_str}/manifest.json"
    )
    s3_client.upload_json(backup_manifest, manifest_s3_key)
    backup_run_result.uploaded_keys.append(manifest_s3_key)

    shutil.rmtree(backup_work_dir, ignore_errors=True)

    try:
        backup_run_result.deleted_old_objects = s3_client.cleanup_old_backups(
            options.s3_prefix, options.retention_days
        )
    except Exception as retention_exc:
        backup_run_result.errors["retention"] = str(retention_exc)

    return backup_run_result


def _archive_and_upload(
    *,
    source_dir: Path,
    output_filename: str,
    snapshot_filename: str,
    work_dir: Path,
    scratch_root: Path,
    options: BackupRunOptions,
    backup_timestamp: str,
    backup_type_str: str,
    s3_client: S3Client,
    result: BackupRunResult,
    manifest: dict[str, Any],
) -> None:
    """Archive ``source_dir`` and upload it; updates ``result`` and ``manifest``."""
    if not source_dir.exists() or not any(source_dir.iterdir()):
        return

    output_archive_path = work_dir / output_filename
    snapshot_path = scratch_root / snapshot_filename
    try:
        archive_directory(
            source_dir,
            output_archive_path,
            snapshot_path,
            backup_type_str == "full",
        )
        archive_s3_key = f"{options.s3_prefix}/{backup_timestamp}/{backup_type_str}/{output_filename}"
        s3_client.upload_file(output_archive_path, archive_s3_key)
        manifest["files"].append(
            {
                "name": output_filename,
                "size": output_archive_path.stat().st_size,
                "s3_key": archive_s3_key,
                "sha256": _compute_sha256(output_archive_path),
            }
        )
        result.uploaded_keys.append(archive_s3_key)
    except Exception as archive_upload_exc:
        manifest[f"{output_filename}_error"] = str(archive_upload_exc)
        result.errors[output_filename] = str(archive_upload_exc)
