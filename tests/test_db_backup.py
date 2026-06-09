"""Tests for the backup workflow and the ``zata-ops db backup`` CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zata_ops.cli import app
from zata_ops.db import _backup_impl
from zata_ops.db._s3 import build_backup_plan


def test_build_backup_plan_includes_manifest_key() -> None:
    plan_dict = build_backup_plan(
        project="demo",
        s3_prefix="prefix",
        timestamp="2026-06-08_120000",
        backup_type="full",
        include_logs=True,
        include_resources=False,
    )
    assert plan_dict["manifest_key"] == "prefix/2026-06-08_120000/full/manifest.json"
    assert "prefix/2026-06-08_120000/full/database.sql.gz" in plan_dict["s3_keys"]
    assert "prefix/2026-06-08_120000/full/logs.tar.gz" in plan_dict["s3_keys"]
    assert all("resources" not in key for key in plan_dict["s3_keys"])


def test_determine_backup_type_respects_full_day_and_override() -> None:
    sunday_morning_dt = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    assert (
        _backup_impl.determine_backup_type(
            sunday_morning_dt, full_backup_day=6, force_full=False
        )
        == "full"
    )
    monday_dt = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    assert (
        _backup_impl.determine_backup_type(
            monday_dt, full_backup_day=6, force_full=False
        )
        == "incremental"
    )
    assert (
        _backup_impl.determine_backup_type(
            monday_dt, full_backup_day=6, force_full=True
        )
        == "full"
    )


def test_db_backup_dry_run_prints_planned_keys(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "resources").mkdir()
    (tmp_path / "logs" / "x.log").write_text("x", encoding="utf-8")

    runner = CliRunner()
    dry_run_result = runner.invoke(
        app,
        [
            "db",
            "backup",
            "--dry-run",
            "--project",
            "demo",
            "--db-url",
            "postgresql://u:p@localhost:5432/db",
            "--s3-endpoint",
            "http://localhost:9000",
            "--s3-bucket",
            "demo-backups",
            "--s3-access-key",
            "k",
            "--s3-secret-key",
            "s",
            "--s3-prefix",
            "demo-backups",
            "--logs-dir",
            str(tmp_path / "logs"),
            "--resources-dir",
            str(tmp_path / "resources"),
            "--work-dir",
            str(tmp_path / "work"),
        ],
    )
    assert dry_run_result.exit_code == 0
    assert "dry-run" in dry_run_result.stdout
    assert "demo-backups" in dry_run_result.stdout
    assert "***" in dry_run_result.stdout  # password redacted
    parsed_plan = json.loads(
        dry_run_result.stdout.split("dry-run[/bold green]", 1)[-1].strip()
        if "dry-run[/bold green]" in dry_run_result.stdout
        else dry_run_result.stdout.split("\n", 1)[1]
    )
    assert parsed_plan["bucket"] == "demo-backups"
    assert any("database.sql.gz" in key for key in parsed_plan["s3_keys"])


class _FakeS3Client:
    """Test double that records calls instead of touching the network."""

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
        self.uploaded_files: list[tuple[Path, str]] = []
        self.uploaded_json: list[tuple[dict[str, Any], str]] = []
        self.cleanup_calls: list[tuple[str, int]] = []

    def upload_file(self, file_path: Path, key: str) -> None:
        self.uploaded_files.append((file_path, key))

    def upload_json(self, data: dict, key: str) -> None:
        self.uploaded_json.append((data, key))

    def cleanup_old_backups(self, prefix: str, retention_days: int) -> int:
        self.cleanup_calls.append((prefix, retention_days))
        return 0


def test_run_backup_uploads_database_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir_path = tmp_path / "work"
    work_dir_path.mkdir()

    monkeypatch.setattr(
        _backup_impl,
        "backup_database",
        lambda db_url, output_path: output_path.write_bytes(b"\x1f\x8bfake"),
    )

    captured_clients: list[_FakeS3Client] = []

    def fake_factory(**factory_kwargs: Any) -> _FakeS3Client:
        fake_client = _FakeS3Client(**factory_kwargs)
        captured_clients.append(fake_client)
        return fake_client

    backup_options = _backup_impl.BackupRunOptions(
        project="demo",
        database_url="postgresql://u:p@h:5432/db",
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        logs_dir="/nonexistent",
        resources_dir="/nonexistent",
        work_dir=str(work_dir_path),
        retention_days=7,
        full_backup_day=6,
        force_full=True,
    )

    backup_run_result = _backup_impl.run_backup(
        backup_options,
        s3_client_factory=fake_factory,
        now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
    )

    assert backup_run_result.backup_type == "full"
    assert backup_run_result.timestamp == "2026-06-08_120000"
    assert backup_run_result.errors == {}
    assert len(captured_clients) == 1
    fake_client = captured_clients[0]
    uploaded_keys = [key for _, key in fake_client.uploaded_files]
    assert any("database.sql.gz" in key for key in uploaded_keys)
    assert fake_client.uploaded_json[0][1].endswith("/manifest.json")
    assert fake_client.cleanup_calls == [("p", 7)]


def test_run_backup_validates_required_settings(tmp_path: Path) -> None:
    bad_options = _backup_impl.BackupRunOptions(
        project="demo",
        database_url="",
        s3_endpoint="http://s3",
        s3_access_key="k",
        s3_secret_key="s",
        s3_bucket="b",
        s3_prefix="p",
        s3_addressing_style="path",
        logs_dir="/n",
        resources_dir="/n",
        work_dir=str(tmp_path),
        retention_days=7,
        full_backup_day=6,
    )
    with pytest.raises(ValueError):
        _backup_impl.run_backup(bad_options)
