"""
RobinHealth: background job worker.

A minimal Postgres-backed job queue, reusing the same database the rest
of this scaffold already talks to, rather than introducing new
infrastructure (Redis, SQS, Celery, ...) this sandboxed environment
can't reach anyway. repository.py's claim_next_job uses `SELECT ... FOR
UPDATE SKIP LOCKED`, the standard safe Postgres pattern for letting
multiple workers pull from the same queue without two of them claiming
the same row -- verified for real with two genuinely concurrent threads
racing against the same queue while building this, not just assumed
from the SQL looking right.

JOB_HANDLERS maps job_type -> a function that does the actual work and
raises on failure; process_next_job claims one job, dispatches it, and
marks it completed/failed (retrying up to MAX_ATTEMPTS before giving up
permanently). Today, the only registered handler (_handle_parse_fap)
will always fail -- not because fetch_fap_documents is unwired (it's
real now: real HTTP fetch, real PDF/HTML extraction) but because there's
still no way to resolve a real fap_url/pls_url/billing_policy_url for a
newly-created facility. _handle_parse_fap deliberately raises rather
than calling fetch_fap_documents with every URL as None -- see its own
comment for why that would otherwise silently write a false claim about
a real hospital to the database. Either way, this is exactly the case
this queue needs to handle correctly: a failing job should retry, then
permanently fail, not silently vanish or retry forever.
"""

from __future__ import annotations

import time
import traceback

import fap_pipeline
import mrf_pipeline
import repository


MAX_ATTEMPTS = 3
POLL_INTERVAL_SECONDS = 5


def _handle_parse_fap(payload: dict) -> None:
    facility_id = payload["facility_id"]
    urls = repository.fetch_fap_urls_for_facility(facility_id)
    fap_url = urls["fap_url"]
    pls_url = urls["pls_url"]
    billing_policy_url = urls["billing_policy_url"]

    if not any(urls.values()):
        # IMPORTANT: do not call parse_fap with every URL unresolved. Now
        # that fetch_fap_documents is real (it used to unconditionally
        # raise, making this scenario unreachable), calling parse_fap
        # this way would silently SUCCEED and write a
        # fap_document_exists/ABSENT finding to Postgres -- whose
        # argument_template is worded for direct use in a letter sent to
        # the provider, asserting no FAP "could be located at the URL
        # provided." That's a false claim about a real hospital's
        # compliance posture when the truth is RobinHealth never had a
        # URL to check, not the same as genuinely checking and finding
        # nothing. Raising here instead routes through the normal
        # retry-then-permanently-fail path (see worker.py's module
        # docstring), which is the honest outcome for a facility with no
        # linked health_system, or a health_system with no URLs on file.
        raise RuntimeError(
            f"no fap_url/pls_url/billing_policy_url resolvable for facility_id={facility_id!r} "
            f"(no linked health_system, or health_system has no URLs on file)"
        )

    result = fap_pipeline.parse_fap(
        facility_id=facility_id, fap_url=fap_url, pls_url=pls_url, billing_policy_url=billing_policy_url,
    )
    repository.insert_fap_parse_result(facility_id, result)


def _handle_fetch_mrf_rates(payload: dict) -> None:
    """
    Fetch and parse the hospital MRF for specific procedure codes.

    Payload shape:
        {
            "facility_id": "...",
            "codes": ["99213", "71046"],     # procedure codes from the bill
            "health_system_id": "..." | null  # used to resolve mrf_url
        }

    All outcomes (rates_found, codes_not_in_mrf, mrf_unreachable, etc.)
    are persisted to mrf_findings -- they're all useful, not just successes.
    This handler therefore never raises: a "failed" result is a legitimate
    result that gets persisted and surfaced to the user.
    """
    facility_id = payload["facility_id"]
    codes = payload.get("codes") or []
    health_system_id = payload.get("health_system_id")

    # Resolve MRF URL from health_system if available
    mrf_url: str | None = None
    if health_system_id:
        mrf_url = repository.fetch_mrf_url_for_health_system(health_system_id)

    # Also get the facility's linked health_system_id if not in payload
    # (handles the case where create_facility_and_queue_fap_parsing set it)
    if not mrf_url and not health_system_id:
        urls = repository.fetch_fap_urls_for_facility(facility_id)
        # We don't have a direct mrf_url fetch on facility, but we can get
        # the health_system_id by checking the DB directly
        import db as db_module
        with db_module.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT health_system_id FROM facilities WHERE id = %s",
                    (facility_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            mrf_url = repository.fetch_mrf_url_for_health_system(str(row[0]))

    finding = mrf_pipeline.fetch_mrf_rates(
        facility_id=facility_id,
        codes=codes,
        mrf_url=mrf_url,
    )
    repository.upsert_mrf_finding(finding)


JOB_HANDLERS = {
    "parse_fap": _handle_parse_fap,
    "fetch_mrf_rates": _handle_fetch_mrf_rates,
}


def process_next_job() -> bool:
    """
    Claim and process one job. Returns False if the queue was empty
    (nothing to do); True if a job was claimed, regardless of whether it
    then succeeded or failed -- the return value tells a caller whether
    to poll again immediately or back off, not whether the job itself
    succeeded.
    """
    job = repository.claim_next_job()
    if job is None:
        return False

    handler = JOB_HANDLERS.get(job["job_type"])
    if handler is None:
        repository.mark_job_failed(
            job["id"], f"No handler registered for job_type={job['job_type']!r}",
            max_attempts=MAX_ATTEMPTS,
        )
        return True

    try:
        handler(job["payload"])
        repository.mark_job_complete(job["id"])
    except Exception:
        repository.mark_job_failed(job["id"], traceback.format_exc(), max_attempts=MAX_ATTEMPTS)

    return True


def run_worker_loop() -> None:
    """The actual long-running process. Not imported anywhere else -- run as `python3 worker.py`."""
    print("RobinHealth worker started, polling for jobs...")
    while True:
        had_job = process_next_job()
        if not had_job:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_worker_loop()
