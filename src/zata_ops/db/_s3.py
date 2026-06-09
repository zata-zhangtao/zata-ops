"""S3-compatible storage client for backup upload, listing, and retention."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config


class S3Client:
    """Thin wrapper around boto3 S3 for backup operations.

    Provides upload, JSON upload, listing, manifest lookup, download, and
    retention cleanup helpers tailored to the backup directory layout
    ``<prefix>/<YYYY-MM-DD_HHMMSS>/<full|incremental>/<file>``.
    """

    _BACKUP_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "",
        addressing_style: str = "path",
    ) -> None:
        """Initialise the underlying boto3 S3 client.

        Args:
            endpoint: S3-compatible endpoint URL.
            access_key: AWS-style access key.
            secret_key: AWS-style secret key.
            bucket: Default bucket name.
            prefix: Default key prefix used by list/manifest helpers.
            addressing_style: ``path`` (MinIO, AWS), ``virtual`` (OSS/COS), or
                ``auto`` (AWS S3 SDK default).
        """
        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(
                connect_timeout=30,
                read_timeout=300,
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def upload_file(self, file_path: Path, key: str) -> None:
        """Upload a local file to S3.

        Args:
            file_path: Local file to upload.
            key: Destination S3 key (relative to bucket root).
        """
        self.client.upload_file(str(file_path), self.bucket, key)

    def upload_json(self, data: dict, key: str) -> None:
        """Upload a JSON-serialisable dictionary to S3.

        Args:
            data: Dictionary to serialise.
            key: Destination S3 key.
        """
        encoded_json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode(
            "utf-8"
        )
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=encoded_json_bytes,
            ContentType="application/json",
        )

    def download_file(self, s3_key: str, output_path: Path) -> None:
        """Download a single file from S3.

        Args:
            s3_key: Source S3 key.
            output_path: Local path to write the downloaded file to.
        """
        self.client.download_file(self.bucket, s3_key, str(output_path))

    def list_backup_dates(self) -> list[str]:
        """Return sorted backup date directories under :attr:`prefix`.

        Returns:
            Sorted list of date strings in ``YYYY-MM-DD_HHMMSS`` format.
        """
        unique_backup_dates: set[str] = set()
        list_objects_paginator = self.client.get_paginator("list_objects_v2")
        for s3_listing_page in list_objects_paginator.paginate(
            Bucket=self.bucket, Prefix=self.prefix + "/"
        ):
            for s3_object_entry in s3_listing_page.get("Contents", []):
                s3_object_key: str = s3_object_entry["Key"]
                key_path_parts = s3_object_key.split("/")
                if len(key_path_parts) >= 2:
                    candidate_date_str = key_path_parts[1]
                    if self._BACKUP_DIR_RE.match(candidate_date_str):
                        unique_backup_dates.add(candidate_date_str)
        return sorted(unique_backup_dates)

    def get_backup_manifest(self, date_str: str) -> dict | None:
        """Fetch ``manifest.json`` for a backup date (full first, then incremental).

        Args:
            date_str: Backup date string (``YYYY-MM-DD_HHMMSS``).

        Returns:
            Parsed manifest dictionary, or ``None`` if not found.
        """
        for backup_type in ("full", "incremental"):
            manifest_key = f"{self.prefix}/{date_str}/{backup_type}/manifest.json"
            try:
                manifest_response = self.client.get_object(
                    Bucket=self.bucket, Key=manifest_key
                )
                return json.loads(manifest_response["Body"].read().decode("utf-8"))
            except self.client.exceptions.NoSuchKey:
                continue
        return None

    def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
        """Delete backup objects older than ``retention_days``.

        Args:
            prefix: Backup key prefix to scan.
            retention_days: Retention window in days.

        Returns:
            Number of deleted S3 objects.
        """
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted_object_count = 0
        list_objects_paginator = self.client.get_paginator("list_objects_v2")
        for s3_listing_page in list_objects_paginator.paginate(
            Bucket=self.bucket, Prefix=prefix + "/"
        ):
            for s3_object_entry in s3_listing_page.get("Contents", []):
                if s3_object_entry["LastModified"] >= cutoff_dt:
                    continue
                relative_key = s3_object_entry["Key"].removeprefix(prefix + "/")
                first_segment = relative_key.split("/", 1)[0]
                if self._BACKUP_DIR_RE.match(first_segment):
                    self.client.delete_object(
                        Bucket=self.bucket, Key=s3_object_entry["Key"]
                    )
                    deleted_object_count += 1
        return deleted_object_count


def find_s3_key_in_manifest(manifest: dict, filename: str) -> str | None:
    """Look up the S3 key of ``filename`` inside a manifest.

    Args:
        manifest: Parsed manifest dictionary.
        filename: Logical filename to look up (e.g. ``database.sql.gz``).

    Returns:
        S3 key string, or ``None`` if not present.
    """
    for file_entry in manifest.get("files", []):
        if file_entry.get("name") == filename:
            return file_entry.get("s3_key")
    return None


def find_full_backup_before(
    s3_client: S3Client, available_dates: list[str], target_date: str
) -> str | None:
    """Locate the most recent full backup on or before ``target_date``.

    Args:
        s3_client: Configured :class:`S3Client`.
        available_dates: Sorted list of all available backup dates.
        target_date: Date to walk backwards from.

    Returns:
        Date string of the nearest full backup, or ``None`` if none exists.
    """
    if target_date not in available_dates:
        return None
    target_index = available_dates.index(target_date)
    for walk_index in range(target_index, -1, -1):
        candidate_manifest = s3_client.get_backup_manifest(available_dates[walk_index])
        if candidate_manifest and candidate_manifest.get("type") == "full":
            return available_dates[walk_index]
    return None


def build_backup_plan(
    project: str,
    s3_prefix: str,
    timestamp: str,
    backup_type: str,
    include_logs: bool,
    include_resources: bool,
) -> dict[str, Any]:
    """Build a dry-run friendly description of an upcoming backup.

    Args:
        project: Project slug used in the description (not part of the key).
        s3_prefix: Top-level S3 prefix.
        timestamp: Backup timestamp in ``YYYY-MM-DD_HHMMSS`` format.
        backup_type: ``full`` or ``incremental``.
        include_logs: Whether the logs archive is planned.
        include_resources: Whether the resources archive is planned.

    Returns:
        Structured plan with the planned S3 keys and manifest key.
    """
    base_key = f"{s3_prefix}/{timestamp}/{backup_type}"
    planned_files: list[str] = ["database.sql.gz"]
    if include_logs:
        planned_files.append("logs.tar.gz")
    if include_resources:
        planned_files.append("resources.tar.gz")
    return {
        "project": project,
        "timestamp": timestamp,
        "type": backup_type,
        "s3_keys": [f"{base_key}/{file_name}" for file_name in planned_files],
        "manifest_key": f"{base_key}/manifest.json",
    }
