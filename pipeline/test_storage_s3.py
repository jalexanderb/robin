"""
Tests for the S3-compatible (encrypted-at-rest) storage backend — B4.

Uses moto to mock S3, so no real cloud calls. Skipped automatically where boto3
or moto aren't installed (keeps the suite green in minimal environments). The
local-filesystem backend is covered in test_retention.py / test_pipeline.py.

Run with: python3 -m pytest test_storage_s3.py
"""

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws

import storage

BUCKET = "robin-test-bucket"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_SSE", raising=False)
    monkeypatch.delenv("S3_SSE_KMS_KEY_ID", raising=False)
    storage._reset_s3_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client
    storage._reset_s3_client()


def test_s3_save_load_round_trip(s3):
    key = storage.save(b"hello bill", "application/pdf")
    assert key.endswith(".pdf")
    assert storage.exists(key)
    assert storage.load(key) == b"hello bill"


def test_s3_is_content_addressed(s3):
    k1 = storage.save(b"identical", "application/pdf")
    k2 = storage.save(b"identical", "application/pdf")
    assert k1 == k2
    # Only one object actually stored.
    listing = s3.list_objects_v2(Bucket=BUCKET)
    assert listing.get("KeyCount", 0) == 1


def test_s3_objects_are_encrypted_at_rest(s3):
    key = storage.save(b"secret bill", "application/pdf")
    head = s3.head_object(Bucket=BUCKET, Key=key)
    assert head.get("ServerSideEncryption") == "AES256"


def test_s3_kms_encryption(monkeypatch, s3):
    kms = boto3.client("kms", region_name="us-east-1")
    key_id = kms.create_key()["KeyMetadata"]["KeyId"]
    monkeypatch.setenv("S3_SSE", "aws:kms")
    monkeypatch.setenv("S3_SSE_KMS_KEY_ID", key_id)
    key = storage.save(b"kms bill", "application/pdf")
    head = s3.head_object(Bucket=BUCKET, Key=key)
    assert head.get("ServerSideEncryption") == "aws:kms"


def test_s3_delete_round_trip(s3):
    key = storage.save(b"x", "application/pdf")
    assert storage.delete(key) is True
    assert not storage.exists(key)
    # Idempotent: deleting again reports nothing removed.
    assert storage.delete(key) is False


def test_s3_delete_empty_key_is_noop(s3):
    assert storage.delete("") is False


def test_s3_load_missing_raises_filenotfound(s3):
    # Normalized to FileNotFoundError so the /letters endpoint's 404 path is
    # backend-agnostic.
    with pytest.raises(FileNotFoundError):
        storage.load("deadbeef" * 8 + ".pdf")


def test_s3_load_is_path_traversal_safe(s3):
    # A key with traversal segments is reduced to its basename, so it can only
    # ever address an object in the bucket's flat namespace.
    key = storage.save(b"safe", "application/pdf")
    assert storage.load("../../" + key) == b"safe"
