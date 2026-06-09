"""Tests for the ``zata-ops db check`` command (moto-backed S3)."""

from __future__ import annotations


import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from zata_ops.cli import app


@pytest.fixture()
def _moto_s3_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket="diag-bucket")
        yield


def test_db_check_round_trip_against_moto(
    _moto_s3_environment, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    check_result = runner.invoke(
        app,
        [
            "db",
            "check",
            "--s3-endpoint",
            "https://s3.amazonaws.com",
            "--s3-bucket",
            "diag-bucket",
            "--s3-access-key",
            "test",
            "--s3-secret-key",
            "test",
            "--s3-prefix",
            "diag",
        ],
    )
    assert check_result.exit_code == 0, check_result.stdout
    assert "OK" in check_result.stdout
