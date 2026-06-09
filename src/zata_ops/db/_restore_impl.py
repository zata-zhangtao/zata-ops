"""Pure restore workflow: download manifest, restore DB/logs/resources.

Mirrors ``_backup_impl.py``: the side-effecting orchestration lives here so
the typer wrapper layer stays thin and testable.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from zata_ops.db._archive import restore_archive
from zata_ops.db._database import (
    clean_target_schema,
    drop_and_recreate_target_db,
    get_table_row_count,
    restore_database,
)
from zata_ops.db._s3 import (
    S3Client,
    find_full_backup_before,
    find_s3_key_in_manifest,
)


@dataclass
class RestoreRunOptions:
    """Inputs required to run one restore cycle.

    Attributes:
        target_date: Backup date string (``YYYY-MM-DD_HHMMSS``) to restore.
        database_url: Target database URL.
        logs_dir: Target directory for logs restore.
        resources_dir: Target directory for resources restore.
        s3_endpoint: S3 endpoint URL.
        s3_access_key: S3 access key.
        s3_secret_key: S3 secret key.
        s3_bucket: S3 bucket name.
        s3_prefix: S3 key prefix.
        s3_addressing_style: ``path``, ``virtual``, or ``auto``.
        restore_db: Whether to restore the database.
        restore_logs: Whether to restore logs.
        restore_resources: Whether to restore resources.
        use_chain: For file restores, apply the full + incrementals chain.
        clean_target_schema: Drop+recreate the ``public`` schema first.
        drop_target_db: Drop+recreate the target database first.
        sanitize_invalid_utf8: Replace invalid UTF-8 bytes before piping to psql.
        verify_tables: Tables that must exist with >0 rows after restore.
    """

    target_date: str
    database_url: str
    logs_dir: str
    resources_dir: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_prefix: str
    s3_addressing_style: str
    restore_db: bool = False
    restore_logs: bool = False
    restore_resources: bool = False
    use_chain: bool = False
    clean_target_schema: bool = False
    drop_target_db: bool = False
    sanitize_invalid_utf8: bool = False
    verify_tables: list[str] = field(default_factory=list)


@dataclass
class RestoreRunResult:
    """Outcome of one restore cycle.

    Attributes:
        backup_type: ``full`` / ``incremental`` / ``unknown``.
        downloaded_keys: S3 keys actually downloaded.
        restored_targets: List of restored target labels (``database``/``logs``/
            ``resources``).
        warnings: Non-fatal warnings raised during restore.
        success: Overall success flag.
    """

    backup_type: str
    downloaded_keys: list[str] = field(default_factory=list)
    restored_targets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    success: bool = True


def run_restore(
    options: RestoreRunOptions,
    s3_client_factory=S3Client,
) -> RestoreRunResult:
    """Execute one restore cycle end-to-end.

    Args:
        options: Restore configuration.
        s3_client_factory: Callable returning an :class:`S3Client`-compatible
            object. Tests override this to inject a fake.

    Returns:
        :class:`RestoreRunResult` describing what was restored.

    Raises:
        ValueError: If both ``clean_target_schema`` and ``drop_target_db`` are
            set, or no restore target is selected.
    """
    if options.clean_target_schema and options.drop_target_db:
        raise ValueError(
            "clean_target_schema and drop_target_db are mutually exclusive"
        )

    selected_restore_targets: list[str] = []
    if options.restore_db:
        selected_restore_targets.append("database")
    if options.restore_logs:
        selected_restore_targets.append("logs")
    if options.restore_resources:
        selected_restore_targets.append("resources")
    if not selected_restore_targets:
        raise ValueError("at least one restore target must be selected")

    s3_client = s3_client_factory(
        endpoint=options.s3_endpoint,
        access_key=options.s3_access_key,
        secret_key=options.s3_secret_key,
        bucket=options.s3_bucket,
        prefix=options.s3_prefix,
        addressing_style=options.s3_addressing_style,
    )

    available_backup_dates = s3_client.list_backup_dates()
    if options.target_date not in available_backup_dates:
        raise ValueError(f"backup {options.target_date} not found in S3")

    target_manifest = s3_client.get_backup_manifest(options.target_date)
    if not target_manifest:
        raise ValueError(f"manifest missing for {options.target_date}")

    backup_type_str = target_manifest.get("type", "unknown")
    restore_run_result = RestoreRunResult(backup_type=backup_type_str)

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir_path = Path(tmp_dir_str)

        if options.restore_db:
            db_s3_key = find_s3_key_in_manifest(target_manifest, "database.sql.gz")
            if not db_s3_key:
                restore_run_result.success = False
                restore_run_result.warnings.append(
                    "database.sql.gz missing from manifest"
                )
                return restore_run_result

            db_dump_local_path = tmp_dir_path / "database.sql.gz"
            s3_client.download_file(db_s3_key, db_dump_local_path)
            restore_run_result.downloaded_keys.append(db_s3_key)

            if options.clean_target_schema:
                clean_target_schema(options.database_url)
            if options.drop_target_db:
                drop_and_recreate_target_db(options.database_url)

            restore_database(
                options.database_url,
                db_dump_local_path,
                sanitize_invalid_utf8=options.sanitize_invalid_utf8,
            )
            restore_run_result.restored_targets.append("database")

        if options.restore_logs or options.restore_resources:
            if options.use_chain:
                _apply_restore_chain(
                    s3_client=s3_client,
                    available_dates=available_backup_dates,
                    target_date=options.target_date,
                    options=options,
                    tmp_dir_path=tmp_dir_path,
                    restore_run_result=restore_run_result,
                )
            else:
                _apply_single_restore(
                    s3_client=s3_client,
                    target_manifest=target_manifest,
                    backup_type_str=backup_type_str,
                    options=options,
                    tmp_dir_path=tmp_dir_path,
                    restore_run_result=restore_run_result,
                )

    if options.restore_db and options.verify_tables:
        for required_table_name in options.verify_tables:
            verified_row_count = get_table_row_count(
                options.database_url, required_table_name
            )
            if verified_row_count is None:
                restore_run_result.warnings.append(
                    f"required table {required_table_name} does not exist"
                )
                restore_run_result.success = False
            elif verified_row_count == 0:
                restore_run_result.warnings.append(
                    f"required table {required_table_name} has 0 rows"
                )
                restore_run_result.success = False

    return restore_run_result


def _apply_single_restore(
    *,
    s3_client: S3Client,
    target_manifest: dict,
    backup_type_str: str,
    options: RestoreRunOptions,
    tmp_dir_path: Path,
    restore_run_result: RestoreRunResult,
) -> None:
    """Restore logs/resources from a single backup date."""
    if options.restore_logs:
        logs_s3_key = find_s3_key_in_manifest(target_manifest, "logs.tar.gz")
        if logs_s3_key:
            logs_local_path = tmp_dir_path / "logs.tar.gz"
            s3_client.download_file(logs_s3_key, logs_local_path)
            restore_run_result.downloaded_keys.append(logs_s3_key)
            restore_archive(
                logs_local_path,
                options.logs_dir,
                use_incremental=(backup_type_str == "incremental"),
            )
            restore_run_result.restored_targets.append("logs")
        else:
            restore_run_result.warnings.append("logs.tar.gz missing from manifest")

    if options.restore_resources:
        resources_s3_key = find_s3_key_in_manifest(target_manifest, "resources.tar.gz")
        if resources_s3_key:
            resources_local_path = tmp_dir_path / "resources.tar.gz"
            s3_client.download_file(resources_s3_key, resources_local_path)
            restore_run_result.downloaded_keys.append(resources_s3_key)
            restore_archive(
                resources_local_path,
                options.resources_dir,
                use_incremental=(backup_type_str == "incremental"),
            )
            restore_run_result.restored_targets.append("resources")
        else:
            restore_run_result.warnings.append("resources.tar.gz missing from manifest")


def _apply_restore_chain(
    *,
    s3_client: S3Client,
    available_dates: list[str],
    target_date: str,
    options: RestoreRunOptions,
    tmp_dir_path: Path,
    restore_run_result: RestoreRunResult,
) -> None:
    """Restore the full + incremental chain leading up to ``target_date``."""
    chain_anchor_full_date = find_full_backup_before(
        s3_client, available_dates, target_date
    )
    if not chain_anchor_full_date:
        restore_run_result.success = False
        restore_run_result.warnings.append(
            "no full backup found before target date; chain restore not possible"
        )
        return

    chain_full_index = available_dates.index(chain_anchor_full_date)
    chain_target_index = available_dates.index(target_date)
    chain_dates_list = available_dates[chain_full_index : chain_target_index + 1]

    for chain_step_index, chain_date_str in enumerate(chain_dates_list):
        chain_step_manifest = s3_client.get_backup_manifest(chain_date_str)
        if not chain_step_manifest:
            restore_run_result.warnings.append(f"manifest missing for {chain_date_str}")
            continue

        is_chain_first = chain_step_index == 0
        chain_use_g = not is_chain_first

        if options.restore_logs:
            logs_s3_key = find_s3_key_in_manifest(chain_step_manifest, "logs.tar.gz")
            if logs_s3_key:
                chain_logs_local_path = tmp_dir_path / f"{chain_date_str}_logs.tar.gz"
                s3_client.download_file(logs_s3_key, chain_logs_local_path)
                restore_run_result.downloaded_keys.append(logs_s3_key)
                restore_archive(
                    chain_logs_local_path,
                    options.logs_dir,
                    use_incremental=chain_use_g,
                )

        if options.restore_resources:
            resources_s3_key = find_s3_key_in_manifest(
                chain_step_manifest, "resources.tar.gz"
            )
            if resources_s3_key:
                chain_resources_local_path = (
                    tmp_dir_path / f"{chain_date_str}_resources.tar.gz"
                )
                s3_client.download_file(resources_s3_key, chain_resources_local_path)
                restore_run_result.downloaded_keys.append(resources_s3_key)
                restore_archive(
                    chain_resources_local_path,
                    options.resources_dir,
                    use_incremental=chain_use_g,
                )

    if options.restore_logs:
        restore_run_result.restored_targets.append("logs")
    if options.restore_resources:
        restore_run_result.restored_targets.append("resources")


__all__ = [
    "RestoreRunOptions",
    "RestoreRunResult",
    "run_restore",
    "subprocess",  # re-exported for tests that patch shared subprocess usage
]
