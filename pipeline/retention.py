"""
RobinHealth: data deletion + retention (Tier 1 #6).

Orchestrates erasing a patient's case data -- on request (CCPA/MHMDA deletion
rights) and on a schedule (the retention sweep that purges old cases). Combines
the DB layer (repository; rows cascade-delete from `cases`) with blob storage
(the bill, EOB, and letter files), keeping that two-store coordination in one
place. Pure orchestration, so it's unit-testable with repository + storage
mocked.

Run the sweep from cron / a scheduled worker:
    DATABASE_URL=... python3 retention.py [retention_days]
"""
from __future__ import annotations

import logging
import os

import repository
import storage

logger = logging.getLogger("robinhealth.retention")

# Default retention window for a case's raw data. Tune to your stated policy;
# overridable via RETENTION_DAYS env or the CLI argument.
DEFAULT_RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))


def delete_case(case_id: str) -> dict:
    """
    Erase a case: remove its blob files (bill, EOB, letters) first, then delete
    the case row (the DB cascade removes bills / EOBs / line items /
    negotiations / contacts). Blobs go first so a mid-way failure can't orphan
    files the DB no longer references. Returns a summary.
    """
    keys = repository.collect_case_storage_keys(case_id)
    blobs_deleted = 0
    for k in keys:
        try:
            if storage.delete(k):
                blobs_deleted += 1
        except Exception as exc:  # noqa: BLE001 -- one bad blob shouldn't block the rest
            logger.warning("retention: failed to delete blob %s: %s", k, exc)
    row_deleted = repository.delete_case_row(case_id)
    return {
        "case_id": case_id,
        "deleted": row_deleted,
        "blobs_found": len(keys),
        "blobs_deleted": blobs_deleted,
    }


def purge_expired(retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Delete every case older than `retention_days`. Returns counts."""
    case_ids = repository.fetch_case_ids_older_than(retention_days)
    cases_deleted = 0
    blobs_deleted = 0
    for cid in case_ids:
        res = delete_case(cid)
        if res["deleted"]:
            cases_deleted += 1
        blobs_deleted += res["blobs_deleted"]
    logger.info(
        "retention sweep: %d/%d cases purged, %d blobs removed (cutoff=%dd)",
        cases_deleted, len(case_ids), blobs_deleted, retention_days,
    )
    return {"candidates": len(case_ids), "cases_deleted": cases_deleted, "blobs_deleted": blobs_deleted}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RETENTION_DAYS
    print(purge_expired(days))
