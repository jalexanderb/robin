"""
Tests for Tier 1 #6 (deletion + retention) and the storage.delete primitive.
No DB: the DB layer is mocked; storage uses a real temp dir. Run with:
python3 -m pytest test_retention.py
"""

import os
import tempfile
from unittest.mock import patch

import retention
import storage


# ============================================================
# storage.delete
# ============================================================

def test_storage_delete_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    key = storage.save(b"hello bill", "application/pdf")
    assert storage.exists(key)
    assert storage.delete(key) is True
    assert not storage.exists(key)
    # Deleting again is a no-op (idempotent), not an error.
    assert storage.delete(key) is False


def test_storage_delete_handles_empty_key():
    assert storage.delete("") is False


def test_storage_delete_is_path_traversal_safe(tmp_path, monkeypatch):
    # A traversal attempt is reduced to a basename inside STORAGE_DIR, so it
    # can't remove anything outside it.
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("do not delete")
    storage.delete("../secret.txt")
    assert outside.exists()


# ============================================================
# retention.delete_case
# ============================================================

def test_delete_case_removes_blobs_then_row():
    keys = ["k1.pdf", "k2.pdf"]
    with patch.object(retention.repository, "collect_case_storage_keys", return_value=keys) as col, \
         patch.object(retention.repository, "delete_case_row", return_value=True) as drow, \
         patch.object(retention.storage, "delete", return_value=True) as sdel:
        result = retention.delete_case("case-1")

    col.assert_called_once_with("case-1")
    assert sdel.call_count == 2          # both blobs deleted
    drow.assert_called_once_with("case-1")
    assert result["deleted"] is True
    assert result["blobs_deleted"] == 2
    assert result["blobs_found"] == 2


def test_delete_case_continues_when_a_blob_delete_fails():
    with patch.object(retention.repository, "collect_case_storage_keys", return_value=["a", "b"]), \
         patch.object(retention.repository, "delete_case_row", return_value=True), \
         patch.object(retention.storage, "delete", side_effect=[OSError("boom"), True]):
        result = retention.delete_case("case-1")
    # One blob failed, the other succeeded, and the row was still deleted.
    assert result["deleted"] is True
    assert result["blobs_deleted"] == 1


def test_delete_case_reports_missing_case():
    with patch.object(retention.repository, "collect_case_storage_keys", return_value=[]), \
         patch.object(retention.repository, "delete_case_row", return_value=False):
        result = retention.delete_case("nope")
    assert result["deleted"] is False
    assert result["blobs_deleted"] == 0


# ============================================================
# retention.purge_expired
# ============================================================

def test_purge_expired_deletes_each_old_case():
    with patch.object(retention.repository, "fetch_case_ids_older_than", return_value=["c1", "c2", "c3"]), \
         patch.object(retention, "delete_case", side_effect=[
             {"deleted": True, "blobs_deleted": 1},
             {"deleted": True, "blobs_deleted": 0},
             {"deleted": False, "blobs_deleted": 0},
         ]) as dc:
        summary = retention.purge_expired(retention_days=30)
    assert dc.call_count == 3
    assert summary["candidates"] == 3
    assert summary["cases_deleted"] == 2   # the third returned deleted=False
    assert summary["blobs_deleted"] == 1


def test_purge_expired_no_candidates():
    with patch.object(retention.repository, "fetch_case_ids_older_than", return_value=[]):
        summary = retention.purge_expired(retention_days=999)
    assert summary == {"candidates": 0, "cases_deleted": 0, "blobs_deleted": 0}
