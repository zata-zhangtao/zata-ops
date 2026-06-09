"""Tests for the restore workflow (mocked S3 + DB)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zata_ops.db import _restore_impl


class _FakeRestoreS3Client:
    """In-memory fake of :class:`zata_ops.db._s3.S3Client` for restore tests."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str,
        addressing_style: str,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.prefix = prefix
        self.dates: list[str] = ["2026-06-07_180000", "2026-06-08_180000"]
        self.manifests: dict[str, dict[str, Any]] = {
            "2026-06-07_180000": {
                "type": "full",
                "files": [
                    {
                        "name": "database.sql.gz",
                        "s3_key": "p/2026-06-07_180000/full/database.sql.gz",
                    },
                ],
            },
            "2026-06-08_180000": {
                "type": "incremental",
                "files": [
                    {
                        "name": "database.sql.gz",
                        "s3_key": "p/2026-06-08_180000/incremental/database.sql.gz",
                    },
                ],
            },
        }
        self.downloaded_keys: list[str] = []

    def list_backup_dates(self) -> list[str]:
        return list(self.dates)

    def get_backup_manifest(self, date_str: str) -> dict[str, Any] | None:
        return self.manifests.get(date_str)

    def download_file(self, s3_key: str, output_path: Path) -> None:
        self.downloaded_keys.append(s3_key)
        output_path.write_bytes(b"\x1f\x8bfake")


def test_run_restore_db_only_calls_download_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _restore_impl,
        "restore_database",
        lambda db_url, sql_gz_path, sanitize_invalid_utf8=False: None,
    )

    restore_options = _restore_impl.RestoreRunOptions(
        target_date="2026-06-07_180000",
        database_url="postgresql://u:p@h:5432/db",
        logs_dir=str(tmp_path / "logs"),
        resources_dir=str(tmp_path / "resources"),
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        restore_db=True,
    )

    restore_result = _restore_impl.run_restore(
        restore_options, s3_client_factory=_FakeRestoreS3Client
    )
    assert restore_result.success is True
    assert "database" in restore_result.restored_targets


def test_run_restore_rejects_no_targets(tmp_path: Path) -> None:
    bad_restore_options = _restore_impl.RestoreRunOptions(
        target_date="2026-06-07_180000",
        database_url="postgresql://u:p@h:5432/db",
        logs_dir=str(tmp_path),
        resources_dir=str(tmp_path),
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
    )
    with pytest.raises(ValueError):
        _restore_impl.run_restore(bad_restore_options)


def test_run_restore_rejects_mutually_exclusive_flags(tmp_path: Path) -> None:
    bad_restore_options = _restore_impl.RestoreRunOptions(
        target_date="2026-06-07_180000",
        database_url="postgresql://u:p@h:5432/db",
        logs_dir=str(tmp_path),
        resources_dir=str(tmp_path),
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        restore_db=True,
        clean_target_schema=True,
        drop_target_db=True,
    )
    with pytest.raises(ValueError):
        _restore_impl.run_restore(bad_restore_options)


def test_run_restore_chain_walks_full_then_incremental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_restore_impl, "restore_archive", lambda *a, **kw: None)

    # Add a logs key to both manifests so we have something to chain through.
    fake_client_holder: dict[str, _FakeRestoreS3Client] = {}

    def fake_factory(**factory_kwargs: Any) -> _FakeRestoreS3Client:
        chain_fake_client = _FakeRestoreS3Client(**factory_kwargs)
        chain_fake_client.manifests["2026-06-07_180000"]["files"].append(
            {"name": "logs.tar.gz", "s3_key": "p/2026-06-07_180000/full/logs.tar.gz"}
        )
        chain_fake_client.manifests["2026-06-08_180000"]["files"].append(
            {
                "name": "logs.tar.gz",
                "s3_key": "p/2026-06-08_180000/incremental/logs.tar.gz",
            }
        )
        fake_client_holder["client"] = chain_fake_client
        return chain_fake_client

    chain_restore_options = _restore_impl.RestoreRunOptions(
        target_date="2026-06-08_180000",
        database_url="postgresql://u:p@h:5432/db",
        logs_dir=str(tmp_path / "logs"),
        resources_dir=str(tmp_path / "resources"),
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        restore_logs=True,
        use_chain=True,
    )

    chain_restore_result = _restore_impl.run_restore(
        chain_restore_options, s3_client_factory=fake_factory
    )

    fake_client = fake_client_holder["client"]
    assert chain_restore_result.success is True
    assert "logs" in chain_restore_result.restored_targets
    assert any("2026-06-07" in k for k in fake_client.downloaded_keys)
    assert any("2026-06-08" in k for k in fake_client.downloaded_keys)
