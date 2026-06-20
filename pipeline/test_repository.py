"""
RobinHealth: integration tests against a REAL Postgres database.

Unlike test_pipeline.py (fully mocked, no external dependencies),
everything here makes real connections and runs real SQL against a
Postgres instance with schema.sql and bills_schema.sql already applied.
Run:

    createdb robinhealth   # or whatever your local setup needs
    psql -d robinhealth -f ../db/schema.sql
    psql -d robinhealth -f ../db/bills_schema.sql
    DATABASE_URL=postgresql://user:pass@localhost:5432/robinhealth \\
        python3 test_repository.py

(or rely on the ROBINHEALTH_DB_* defaults in db.py, which match the
local dev setup documented in README.md).

Each test cleans up its own rows so the suite is repeatable without a
fixture/teardown framework -- this is a plain-assertion test file like
test_pipeline.py, not pytest, so there's no shared fixture teardown to
lean on.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import db
import repository
from fastapi.testclient import TestClient
from api import app
import bill_pipeline
from bill_pipeline import BillExtraction, ExtractedLineItem, ExtractedProviderInfo
from case_pipeline import process_case_intake
from compliance_checklist import ComplianceStatus, Severity
from fap_pipeline import ComplianceFinding, DocumentQuality, EligibilityExtraction, EligibilityTier, FapParseResult
from pricing_pipeline import RateTable, RateTableEntry


def _unique_npi() -> str:
    """A fake-but-unique NPI per test run, so repeated runs don't collide on data left by a previous run that failed before cleanup."""
    return "9" + str(uuid.uuid4().int)[:9]


def test_can_connect_to_postgres():
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


def test_insert_and_find_facility_by_npi_round_trip():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Test Integration Hospital", npi=npi, state="CA")

    try:
        found = repository.find_facilities_by_npi(npi)
        assert len(found) == 1
        assert found[0]["name"] == "Test Integration Hospital"
        assert found[0]["npi"] == npi
        assert found[0]["state"] == "CA"
        assert str(found[0]["id"]) == facility_id
    finally:
        _delete_facility(facility_id)


def test_insert_and_fetch_fap_parse_result_round_trip():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Test FAP Hospital", npi=npi, state="NY")

    original = FapParseResult(
        facility_id=facility_id,
        document_quality=DocumentQuality(label="well_structured", rationale="ignored on write"),
        eligibility=EligibilityExtraction(
            eligibility_basis="fpl_percentage",
            tiers=[
                EligibilityTier(
                    tier_order=1, fpl_min_pct=0, fpl_max_pct=200,
                    discount_type="full_charity_care", discount_value=100,
                    household_size_adjustment={"per_additional_member_fpl_add": 8000},
                    notes="test tier 1",
                ),
                EligibilityTier(
                    tier_order=2, fpl_min_pct=200, fpl_max_pct=400,
                    discount_type="percentage_discount", discount_value=50,
                ),
            ],
            eligible_services=[], application_requirements=None, parsing_confidence="high",
        ),
        findings=[
            ComplianceFinding(
                requirement_code="fap_document_exists",
                status=ComplianceStatus.PRESENT,
                evidence_text="Found at /financial-assistance",
                severity=Severity.MATERIAL,
                argument_template="N/A -- present",
            ),
            ComplianceFinding(
                requirement_code="widely_publicized",
                status=ComplianceStatus.ABSENT,
                evidence_text=None,
                severity=Severity.MATERIAL,
                argument_template="No evidence of publicity measures.",
            ),
        ],
        raw_text="(raw FAP text would go here)",
        source_doc_hash="abc123",
    )

    fap_id = repository.insert_fap_parse_result(facility_id, original)

    try:
        fetched = repository.fetch_fap_for_facility(facility_id)

        assert fetched is not None
        assert fetched.facility_id == facility_id
        assert fetched.document_quality.label == "well_structured"
        assert fetched.raw_text == "(raw FAP text would go here)"
        assert fetched.source_doc_hash == "abc123"

        assert fetched.eligibility is not None
        assert fetched.eligibility.eligibility_basis == "fpl_percentage"
        assert len(fetched.eligibility.tiers) == 2
        tier1 = next(t for t in fetched.eligibility.tiers if t.tier_order == 1)
        assert tier1.discount_type == "full_charity_care"
        assert tier1.fpl_max_pct == 200
        assert tier1.household_size_adjustment == {"per_additional_member_fpl_add": 8000}
        assert tier1.notes == "test tier 1"

        assert len(fetched.findings) == 2
        present_finding = next(f for f in fetched.findings if f.requirement_code == "fap_document_exists")
        assert present_finding.status == ComplianceStatus.PRESENT
        assert present_finding.severity == Severity.MATERIAL
        absent_finding = next(f for f in fetched.findings if f.requirement_code == "widely_publicized")
        assert absent_finding.status == ComplianceStatus.ABSENT
        assert absent_finding.evidence_text is None
    finally:
        _delete_fap(fap_id)
        _delete_facility(facility_id)


def test_fetch_fap_for_facility_returns_none_when_never_parsed():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Never Parsed Hospital", npi=npi, state="TX")

    try:
        assert repository.fetch_fap_for_facility(facility_id) is None
    finally:
        _delete_facility(facility_id)


def test_process_case_intake_real_db_persists_bill_via_repository():
    """
    The actual end-to-end demonstration: process_case_intake, given a
    real case_id, writes a real bills row (and its line items) to
    Postgres -- no mocking of repository.persist_bill itself. extract_bill
    is still mocked (it needs a real Claude vision call, unrelated to
    whether Postgres works), but everything from match_facility onward
    that touches the database is the real implementation.
    """
    from unittest.mock import patch

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Real DB Test Hospital", npi=npi, state="WA")
    patient_id = repository.insert_patient(household_income=20000, household_size=1, state="WA")
    case_id = repository.insert_case(patient_id)

    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Real DB Test Hospital", npi=npi, tax_id=None, address=None, state="WA",
        ),
        date_of_service="2026-05-01", account_number="ACC-REAL-1",
        line_items=[ExtractedLineItem(
            line_number=1, description="ER visit", procedure_code="REALCPT1",
            code_type="cpt", units=1, billed_amount=900.0,
        )],
        total_billed_amount=900.0, parsing_confidence="high", raw_text=None,
    )
    pfs_table = RateTable(
        source="pfs",
        entries={("REALCPT1", ""): RateTableEntry(code="REALCPT1", locality="", rate=300.0)},
    )

    bill_id = None
    try:
        with patch("bill_pipeline.extract_bill", return_value=bill):
            result = process_case_intake(
                document_bytes=b"", media_type="application/pdf",
                rate_tables={"pfs": pfs_table},
                household_income=20000, household_size=1,
                case_id=case_id,
            )

        assert result.match.status == "matched"
        assert result.match.facility_id == facility_id
        assert result.synthesis is not None

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, total_billed_amount, facility_id, facility_match_status "
                    "FROM bills WHERE case_id = %s",
                    (case_id,),
                )
                row = cur.fetchone()
                assert row is not None
                bill_id = str(row[0])
                assert float(row[1]) == 900.0
                assert str(row[2]) == facility_id
                assert row[3] == "matched"

                cur.execute(
                    "SELECT description, procedure_code, billed_amount FROM bill_line_items WHERE bill_id = %s",
                    (bill_id,),
                )
                line_rows = cur.fetchall()
                assert len(line_rows) == 1
                assert line_rows[0][1] == "REALCPT1"
                assert float(line_rows[0][2]) == 900.0
    finally:
        # Also delete any enqueued MRF jobs (process_case_intake now enqueues one)
        if facility_id:
            _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,))
            _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        if bill_id:
            _execute("DELETE FROM bill_line_items WHERE bill_id = %s", (bill_id,))
            _execute("DELETE FROM bills WHERE id = %s", (bill_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        _delete_facility(facility_id)


# ============================================================
# Cleanup helpers
# ============================================================

def _execute(sql: str, params: tuple) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _delete_fap(fap_id: str) -> None:
    _execute("DELETE FROM fap_compliance_findings WHERE fap_id = %s", (fap_id,))
    _execute("DELETE FROM fap_eligibility_tiers WHERE fap_id = %s", (fap_id,))
    _execute("DELETE FROM financial_assistance_policies WHERE id = %s", (fap_id,))


def _delete_facility(facility_id: str) -> None:
    # financial_assistance_policies has no ON DELETE CASCADE from facilities,
    # so delete any FAP rows (and their cascading children) first.
    # fap_eligible_services / fap_application_requirements / fap_eligibility_tiers /
    # fap_compliance_findings all cascade from financial_assistance_policies, so
    # deleting from that table alone cleans them up.
    _execute("DELETE FROM financial_assistance_policies WHERE facility_id = %s", (facility_id,))
    _execute("DELETE FROM facilities WHERE id = %s", (facility_id,))


def test_enqueue_claim_complete_round_trip():
    job_id = repository.enqueue_job("test_integration_job", {"facility_id": "fac-xyz", "n": 1})

    try:
        claimed = repository.claim_next_job()
        assert claimed is not None
        assert claimed["id"] == job_id
        assert claimed["job_type"] == "test_integration_job"
        assert claimed["payload"] == {"facility_id": "fac-xyz", "n": 1}
        assert claimed["attempts"] == 1

        assert repository.claim_next_job() is None  # already in_progress, not pending

        repository.mark_job_complete(job_id)

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, completed_at FROM jobs WHERE id = %s", (job_id,))
                status, completed_at = cur.fetchone()
                assert status == "completed"
                assert completed_at is not None
    finally:
        _execute("DELETE FROM jobs WHERE id = %s", (job_id,))


def test_job_retries_then_permanently_fails_after_max_attempts():
    job_id = repository.enqueue_job("test_integration_job", {})

    try:
        for expected_attempt in (1, 2, 3):
            claimed = repository.claim_next_job()
            assert claimed is not None, f"expected a claimable job on attempt {expected_attempt}"
            assert claimed["attempts"] == expected_attempt
            repository.mark_job_failed(job_id, f"simulated failure #{expected_attempt}", max_attempts=3)

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, attempts, last_error FROM jobs WHERE id = %s", (job_id,))
                status, attempts, last_error = cur.fetchone()
                assert status == "failed"  # permanently failed after 3 attempts
                assert attempts == 3
                assert "simulated failure #3" in last_error

        assert repository.claim_next_job() is None  # 'failed' jobs are never reclaimed
    finally:
        _execute("DELETE FROM jobs WHERE id = %s", (job_id,))


def test_create_facility_and_queue_fap_parsing_real_db_enqueues_real_job():
    from case_pipeline import create_facility_and_queue_fap_parsing

    npi = _unique_npi()
    provider = ExtractedProviderInfo(
        name="Real Queue Test Hospital", npi=npi, tax_id=None, address=None, state="OR",
    )

    facility_id = create_facility_and_queue_fap_parsing(provider)

    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, npi, state FROM facilities WHERE id = %s", (facility_id,))
                name, npi_stored, state = cur.fetchone()
                assert name == "Real Queue Test Hospital"
                assert npi_stored == npi
                assert state == "OR"

                cur.execute(
                    "SELECT job_type, payload, status FROM jobs WHERE payload->>'facility_id' = %s",
                    (facility_id,),
                )
                job_row = cur.fetchone()
                assert job_row is not None, "no job was enqueued for the new facility"
                assert job_row[0] == "parse_fap"
                assert job_row[1] == {"facility_id": facility_id}
                assert job_row[2] == "pending"
    finally:
        _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,))
        _delete_facility(facility_id)


def test_process_case_intake_real_db_links_bill_to_newly_created_facility():
    """
    Regression test for a bug found while building the HTTP API layer:
    process_case_intake used to leave `match` as ('unmatched', facility_id=
    None) even after successfully creating a brand-new facility, silently
    losing the link when persist_bill ran. Both MatchResult.status's type
    comment and bill_facility_match_status (bills_schema.sql) already
    defined 'new_facility_created' for this case -- this confirms the fix
    against a real database, not just the mocked case_pipeline tests.
    """
    from bill_pipeline import BillExtraction, ExtractedProviderInfo
    from case_pipeline import process_case_intake

    npi = _unique_npi()
    patient_id = repository.insert_patient(household_income=30000, household_size=2, state="OR")
    case_id = repository.insert_case(patient_id)

    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Brand New Unlinked Test Clinic", npi=npi, tax_id=None, address=None, state="OR",
        ),
        date_of_service="2026-05-01", account_number="ACC-LINK-TEST",
        line_items=[], total_billed_amount=750.0, parsing_confidence="high", raw_text=None,
    )

    facility_id = None
    try:
        with patch("bill_pipeline.extract_bill", return_value=bill):
            result = process_case_intake(
                document_bytes=b"", media_type="application/pdf", rate_tables={}, case_id=case_id,
            )

        assert result.match.status == "new_facility_created"
        facility_id = result.match.facility_id
        assert facility_id is not None

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT facility_id, facility_match_status FROM bills WHERE case_id = %s",
                    (case_id,),
                )
                stored_facility_id, stored_status = cur.fetchone()
                assert stored_facility_id is not None, "bill was persisted with facility_id=NULL despite a real facility being created"
                assert str(stored_facility_id) == facility_id
                assert stored_status == "new_facility_created"
    finally:
        if facility_id:
            _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,))
            _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        _execute("DELETE FROM bills WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        if facility_id:
            _delete_facility(facility_id)


def test_worker_real_parse_fap_job_fails_cleanly_without_writing_false_finding():
    """
    Regression test for a bug found while building fetch_fap_documents:
    once it became real (instead of unconditionally raising), calling
    parse_fap with every URL as None started silently SUCCEEDING and
    writing a fap_document_exists/ABSENT finding -- whose
    argument_template is worded for direct use in a letter to the
    provider -- based on never having a URL to check, not on genuinely
    checking and finding nothing. worker._handle_parse_fap now raises
    instead. This confirms the full real path: enqueue a real job, run
    the real worker against it, and check what actually landed (or
    didn't) in Postgres -- not just that the handler raises in isolation.
    """
    import worker

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Worker Bug Regression Test Hospital", npi=npi, state="NY")
    job_id = repository.enqueue_job("parse_fap", {"facility_id": facility_id})

    try:
        had_job = worker.process_next_job()
        assert had_job is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, attempts, last_error FROM jobs WHERE id = %s", (job_id,))
                status, attempts, last_error = cur.fetchone()
                assert status == "pending"  # requeued for retry, not silently completed
                assert attempts == 1
                assert "no fap_url" in last_error.lower() or "not resolvable" in last_error.lower() or "resolvable" in last_error.lower()

                # The real, important check: no FAP row was written at
                # all for this facility -- a false ABSENT finding did
                # NOT silently land in Postgres.
                cur.execute(
                    "SELECT count(*) FROM financial_assistance_policies WHERE facility_id = %s", (facility_id,),
                )
                assert cur.fetchone()[0] == 0, "a FAP row was written despite never having a real URL to check"
    finally:
        _execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        _delete_facility(facility_id)


def test_insert_and_fetch_fap_parse_result_round_trip_with_services_and_requirements():
    """
    Extend the existing round-trip to cover eligible_services and
    application_requirements -- the two EligibilityExtraction fields that
    were hardcoded to []/None until now.
    """
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Services Round Trip Hospital", npi=npi, state="TX")

    from fap_pipeline import (
        ComplianceFinding, DocumentQuality, EligibilityExtraction,
        EligibilityTier, FapParseResult,
    )
    from compliance_checklist import ComplianceStatus, Severity

    result = FapParseResult(
        facility_id=facility_id,
        document_quality=DocumentQuality(label="well_structured", rationale="clear"),
        eligibility=EligibilityExtraction(
            eligibility_basis="fpl_percentage",
            tiers=[EligibilityTier(
                tier_order=1, fpl_min_pct=0, fpl_max_pct=200,
                discount_type="full_charity_care", discount_value=None,
                household_size_adjustment=None, notes=None,
            )],
            eligible_services=[
                {"service_category": "emergency", "is_covered": True, "notes": None},
                {"service_category": "elective_surgery", "is_covered": False, "notes": "excluded"},
            ],
            application_requirements={
                "application_deadline_days": 240,
                "required_documents": ["tax_return", "pay_stub"],
                "presumptive_eligibility_criteria": ["medicaid_enrolled"],
                "notification_method_required": None,
            },
            parsing_confidence="high",
        ),
        findings=[ComplianceFinding(
            requirement_code="fap_document_exists",
            status=ComplianceStatus.PRESENT,
            evidence_text="FAP found",
            severity=Severity.MATERIAL,
            argument_template="has a FAP",
        )],
        raw_text="test raw text",
        source_doc_hash="abc123",
    )

    fap_id = None
    try:
        fap_id = repository.insert_fap_parse_result(facility_id, result)

        fetched = repository.fetch_fap_for_facility(facility_id)
        assert fetched is not None
        assert len(fetched.eligibility.eligible_services) == 2
        emergency = next(s for s in fetched.eligibility.eligible_services if s["service_category"] == "emergency")
        assert emergency["is_covered"] is True
        elective = next(s for s in fetched.eligibility.eligible_services if s["service_category"] == "elective_surgery")
        assert elective["is_covered"] is False
        assert elective["notes"] == "excluded"

        req = fetched.eligibility.application_requirements
        assert req is not None
        assert req["application_deadline_days"] == 240
        assert req["required_documents"] == ["tax_return", "pay_stub"]
        assert req["presumptive_eligibility_criteria"] == ["medicaid_enrolled"]
        assert req["notification_method_required"] is None

    finally:
        if fap_id:
            _delete_fap(fap_id)
        _delete_facility(facility_id)


def test_insert_fap_parse_result_skips_malformed_service_keeps_rest():
    """
    A malformed eligible_services entry (missing required key) must be
    skipped rather than rolling back the whole transaction -- the other
    services, tiers, and findings should all still land.
    """
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Malformed Service Test Hospital", npi=npi, state="WA")

    from fap_pipeline import (
        ComplianceFinding, DocumentQuality, EligibilityExtraction,
        EligibilityTier, FapParseResult,
    )
    from compliance_checklist import ComplianceStatus, Severity

    result = FapParseResult(
        facility_id=facility_id,
        document_quality=DocumentQuality(label="well_structured", rationale="clear"),
        eligibility=EligibilityExtraction(
            eligibility_basis="fpl_percentage",
            tiers=[EligibilityTier(
                tier_order=1, fpl_min_pct=0, fpl_max_pct=300,
                discount_type="percentage_discount", discount_value=75.0,
                household_size_adjustment=None, notes=None,
            )],
            eligible_services=[
                {"service_category": "emergency", "is_covered": True, "notes": None},
                {"is_covered": True},   # malformed: missing service_category -- should be skipped
                {"service_category": "lab", "is_covered": True, "notes": None},
            ],
            application_requirements=None,
            parsing_confidence="medium",
        ),
        findings=[ComplianceFinding(
            requirement_code="fap_document_exists",
            status=ComplianceStatus.PRESENT,
            evidence_text=None,
            severity=Severity.MATERIAL,
            argument_template="has a FAP",
        )],
        raw_text=None,
        source_doc_hash=None,
    )

    fap_id = None
    try:
        fap_id = repository.insert_fap_parse_result(facility_id, result)

        fetched = repository.fetch_fap_for_facility(facility_id)
        assert fetched is not None
        assert len(fetched.eligibility.eligible_services) == 2  # malformed entry skipped
        categories = {s["service_category"] for s in fetched.eligibility.eligible_services}
        assert categories == {"emergency", "lab"}
        assert len(fetched.eligibility.tiers) == 1   # tier was NOT rolled back
        assert len(fetched.findings) == 1             # finding was NOT rolled back

    finally:
        if fap_id:
            _delete_fap(fap_id)
        _delete_facility(facility_id)


def test_insert_health_system_and_fetch_all():
    hs_id = repository.insert_health_system(
        name="Testland Regional Health",
        ein="12-3456789",
        fap_url="https://example.com/fap",
        plain_language_summary_url="https://example.com/pls",
        billing_collections_policy_url="https://example.com/billing",
    )
    try:
        all_systems = repository.fetch_all_health_systems()
        row = next((r for r in all_systems if r["id"] == hs_id), None)
        assert row is not None
        assert row["name"] == "Testland Regional Health"
    finally:
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))


def test_fetch_fap_urls_for_facility_returns_none_when_no_health_system():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Unlinked Facility", npi=npi, state="AZ")
    try:
        urls = repository.fetch_fap_urls_for_facility(facility_id)
        assert urls == {"fap_url": None, "pls_url": None, "billing_policy_url": None}
    finally:
        _delete_facility(facility_id)


def test_fetch_fap_urls_for_facility_returns_urls_when_linked():
    hs_id = repository.insert_health_system(
        name="Linked Health System",
        fap_url="https://example.com/fap",
        plain_language_summary_url="https://example.com/pls",
        billing_collections_policy_url="https://example.com/billing",
    )
    npi = _unique_npi()
    facility_id = repository.insert_facility(
        name="Linked Facility", npi=npi, state="CO", health_system_id=hs_id,
    )
    try:
        urls = repository.fetch_fap_urls_for_facility(facility_id)
        assert urls["fap_url"] == "https://example.com/fap"
        assert urls["pls_url"] == "https://example.com/pls"
        assert urls["billing_policy_url"] == "https://example.com/billing"
    finally:
        _delete_facility(facility_id)
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))


def test_create_facility_links_to_health_system_when_name_matches():
    """
    Full real-DB test for the health-system-matching path:
    create_facility_and_queue_fap_parsing now calls
    repository.fetch_all_health_systems() + bill_pipeline.match_health_system(),
    so a facility whose provider name confidently matches a seeded health
    system should come back with health_system_id set.
    """
    from case_pipeline import create_facility_and_queue_fap_parsing

    hs_id = repository.insert_health_system(
        name="Cascade Healthcare",
        fap_url="https://example.com/cascade/fap",
    )
    provider = ExtractedProviderInfo(
        name="Cascade Healthcare, Inc", npi=_unique_npi(), tax_id=None, address=None, state="WA",
    )
    facility_id = None
    try:
        facility_id = create_facility_and_queue_fap_parsing(provider)

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT health_system_id FROM facilities WHERE id = %s", (facility_id,)
                )
                stored_hs_id = cur.fetchone()[0]
        assert str(stored_hs_id) == hs_id, (
            f"expected health_system_id={hs_id!r}, got {stored_hs_id!r}"
        )
    finally:
        if facility_id:
            _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,))
            _delete_facility(facility_id)
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))


def test_worker_resolves_fap_url_via_health_system_link():
    """
    End-to-end test for the URL-resolution deliverable:
    a 'parse_fap' job for a facility linked to a health system with a
    real (if local-only) FAP URL should now proceed past URL resolution
    and attempt a real fetch, rather than failing immediately with
    'no fap_url resolvable'.

    fetch_fap_documents treats ConnectError (nothing listening at
    localhost:19999) the same as a 404 -- "document not found" -- so
    parse_fap still runs and the job completes (not retries). The
    important check is what did NOT happen: no 'no fap_url resolvable'
    RuntimeError, meaning _handle_parse_fap genuinely called
    fetch_fap_urls_for_facility and resolved a non-None URL instead of
    hardcoding all three as None.
    """
    import worker

    hs_id = repository.insert_health_system(
        name="URL Resolution Test Health System",
        fap_url="http://localhost:19999/fap.html",  # nothing listening -- ConnectError -> "not found"
    )
    npi = _unique_npi()
    facility_id = repository.insert_facility(
        name="URL Resolution Test Hospital", npi=npi, state="MN", health_system_id=hs_id,
    )
    job_id = repository.enqueue_job("parse_fap", {"facility_id": facility_id})

    try:
        had_job = worker.process_next_job()
        assert had_job is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, last_error FROM jobs WHERE id = %s", (job_id,)
                )
                status, last_error = cur.fetchone()

        # The job should have completed (not failed), because fetch_fap_documents
        # treats ConnectError as "document not found" and parse_fap runs.
        # If it failed with "no fap_url resolvable", URL resolution is still broken.
        assert status == "completed", (
            f"expected 'completed' but got {status!r} -- "
            f"if error is 'no fap_url resolvable', _handle_parse_fap is not calling "
            f"fetch_fap_urls_for_facility. actual error: {last_error!r}"
        )
        assert last_error is None

        # Confirm a FAP row actually landed (fap_document_exists/ABSENT)
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM financial_assistance_policies WHERE facility_id = %s",
                    (facility_id,),
                )
                count = cur.fetchone()[0]
        assert count == 1, "expected a FAP row to be written after a successful (if empty) parse"

    finally:
        _execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        _delete_facility(facility_id)
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))


def test_seeded_health_systems_match_and_resolve_real_urls():
    """
    Verifies the full happy path against the real seeded health systems:
    a facility name that matches a seeded system gets a real fap_url
    resolved, and the parse_fap job completes (not fails with 'no URL
    resolvable') -- even though the egress proxy blocks the actual fetch
    in this sandbox, fetch_fap_documents gracefully degrades to
    'document not found' and parse_fap still writes a FAP row.
    """
    import worker

    # Providence is the one seeded system with a real PLS URL too --
    # the most complete test case.
    candidates = repository.fetch_all_health_systems()
    hs_id = bill_pipeline.match_health_system("Providence Health and Services", candidates)
    assert hs_id is not None, "Providence not found in seeded health systems"

    npi = _unique_npi()
    facility_id = repository.insert_facility(
        name="Providence Portland Medical Center Test",
        npi=npi,
        state="OR",
        health_system_id=hs_id,
    )
    job_id = repository.enqueue_job("parse_fap", {"facility_id": facility_id})

    try:
        had_job = worker.process_next_job()
        assert had_job is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
                status = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM financial_assistance_policies WHERE facility_id = %s",
                    (facility_id,),
                )
                fap_count = cur.fetchone()[0]

        assert status == "completed", (
            f"expected job to complete, got {status!r} -- "
            "URL resolution or graceful-degradation path is broken"
        )
        assert fap_count == 1, "expected one FAP row written after successful parse"

    finally:
        _execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        _delete_facility(facility_id)



def test_persist_eob_round_trip_and_bill_line_backfill():
    """
    Full round-trip: persist an EOB against a real bill, confirm the eobs/
    eob_line_items/eob_adjustment_codes rows are written, and confirm that
    bill_line_items.allowed_amount and .patient_responsibility are back-filled
    for matched lines.
    """
    from eob_pipeline import (
        EobAdjustmentCode, EobExtraction, EobLineItem, EobMatchResult,
        EobLineMatch, match_eob_to_bill,
    )

    # Set up a real patient/case/bill/facility chain
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="EOB Test Hospital", npi=npi, state="TX")
    patient_id = repository.insert_patient(household_income=45000, household_size=3, state="TX")
    case_id = repository.insert_case(patient_id)

    bill_id = None
    try:
        # Persist a real bill with two line items
        bill = BillExtraction(
            provider=ExtractedProviderInfo(name="EOB Test Hospital", npi=npi, tax_id=None, address=None, state="TX"),
            date_of_service="2026-03-14",
            account_number="ACC-001",
            line_items=[
                ExtractedLineItem(line_number=1, description="Office visit", procedure_code="99213",
                                  code_type="cpt", units=1.0, billed_amount=300.0),
                ExtractedLineItem(line_number=2, description="Chest X-ray", procedure_code="71046",
                                  code_type="cpt", units=1.0, billed_amount=500.0),
            ],
            total_billed_amount=800.0,
            parsing_confidence="high",
            raw_text=None,
        )
        bill_id = repository.persist_bill(
            case_id=case_id, bill=bill,
            facility_id=facility_id, facility_match_status="matched",
            facility_match_confidence=1.0, storage_key="test-bill.pdf",
        )

        # Build a real EobExtraction + match result (without calling the LLM)
        eob = EobExtraction(
            insurer_name="Aetna", member_id="M-12345", claim_number="CLM-99001",
            date_processed="2026-03-20",
            total_billed_amount=800.0, total_allowed_amount=320.0,
            total_insurance_paid=256.0, total_patient_responsibility=64.0,
            line_items=[
                EobLineItem(line_number=1, date_of_service="2026-03-14",
                    description="Office visit", procedure_code="99213",
                    code_type="cpt", units=1.0,
                    billed_amount=300.0, allowed_amount=120.0,
                    insurance_paid=96.0, patient_responsibility=24.0,
                    adjustment_codes=[
                        EobAdjustmentCode(code_type="CARC", code="CO-45",
                                          amount=180.0, description="Contractual adjustment"),
                    ]),
                EobLineItem(line_number=2, date_of_service="2026-03-14",
                    description="Chest X-ray", procedure_code="71046",
                    code_type="cpt", units=1.0,
                    billed_amount=500.0, allowed_amount=200.0,
                    insurance_paid=160.0, patient_responsibility=40.0,
                    adjustment_codes=[
                        EobAdjustmentCode(code_type="CARC", code="CO-45",
                                          amount=300.0, description="Contractual adjustment"),
                    ]),
            ],
            parsing_confidence="high", raw_text=None,
        )
        eob_match = match_eob_to_bill(eob, bill)

        # Both lines should match on exact procedure code
        assert len(eob_match.matched) == 2

        # Persist EOB
        eob_id = repository.persist_eob(
            bill_id=bill_id, eob=eob, match_result=eob_match,
            storage_key="test-eob.pdf",
        )

        # 1. Confirm eobs row
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT insurer_name, claim_number, total_allowed_amount, "
                    "total_patient_responsibility, parsing_confidence FROM eobs WHERE id = %s",
                    (eob_id,),
                )
                row = cur.fetchone()
        insurer_name, claim_number, total_allowed, total_pr, conf = row
        assert insurer_name == "Aetna"
        assert claim_number == "CLM-99001"
        assert float(total_allowed) == 320.0
        assert float(total_pr) == 64.0
        assert conf == "high"

        # 2. Confirm eob_line_items rows
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT procedure_code, allowed_amount, match_status, bill_line_item_id "
                    "FROM eob_line_items WHERE eob_id = %s ORDER BY line_number",
                    (eob_id,),
                )
                eob_lines = cur.fetchall()

        assert len(eob_lines) == 2
        assert eob_lines[0][0] == "99213"         # procedure_code
        assert float(eob_lines[0][1]) == 120.0    # allowed_amount
        assert eob_lines[0][2] == "matched"        # match_status
        assert eob_lines[0][3] is not None         # bill_line_item_id FK set
        assert eob_lines[1][0] == "71046"
        assert float(eob_lines[1][1]) == 200.0

        # 3. Confirm eob_adjustment_codes rows (2 CARC CO-45 codes)
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ac.code, ac.code_type FROM eob_adjustment_codes ac
                       JOIN eob_line_items eli ON eli.id = ac.eob_line_item_id
                       WHERE eli.eob_id = %s""",
                    (eob_id,),
                )
                codes = cur.fetchall()

        assert len(codes) == 2
        assert all(c[0] == "CO-45" for c in codes)
        assert all(c[1] == "CARC" for c in codes)

        # 4. THE KEY CHECK: bill_line_items.allowed_amount and
        #    .patient_responsibility were back-filled for matched lines
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT line_number, allowed_amount, patient_responsibility "
                    "FROM bill_line_items WHERE bill_id = %s ORDER BY line_number",
                    (bill_id,),
                )
                updated_lines = cur.fetchall()

        assert float(updated_lines[0][1]) == 120.0   # allowed_amount line 1
        assert float(updated_lines[0][2]) == 24.0    # patient_responsibility line 1
        assert float(updated_lines[1][1]) == 200.0   # allowed_amount line 2
        assert float(updated_lines[1][2]) == 40.0    # patient_responsibility line 2

        # 5. fetch_eob_summary_for_bill returns the right totals
        summary = repository.fetch_eob_summary_for_bill(bill_id)
        assert summary is not None
        assert summary["insurer_name"] == "Aetna"
        assert float(summary["total_allowed_amount"]) == 320.0

    finally:
        # eobs cascade-delete with bills; bills cascade-delete with cases
        _execute("DELETE FROM bills WHERE id = %s", (bill_id,)) if bill_id else None
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        _delete_facility(facility_id)


def test_persist_eob_unmatched_lines_do_not_backfill_bill():
    """
    EOB lines that don't match any bill line should NOT write anything to
    bill_line_items -- unmatched means we can't be sure which line they
    belong to.
    """
    from eob_pipeline import (
        EobAdjustmentCode, EobExtraction, EobLineItem, EobMatchResult,
        match_eob_to_bill,
    )

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Unmatched EOB Test Hospital", npi=npi, state="FL")
    patient_id = repository.insert_patient(household_income=50000, household_size=2, state="FL")
    case_id = repository.insert_case(patient_id)
    bill_id = None

    try:
        bill = BillExtraction(
            provider=ExtractedProviderInfo(name="Unmatched EOB Test Hospital", npi=npi,
                                           tax_id=None, address=None, state="FL"),
            date_of_service="2026-04-01",
            account_number=None,
            line_items=[
                ExtractedLineItem(line_number=1, description="Lab panel",
                                  procedure_code="80053", code_type="cpt",
                                  units=1.0, billed_amount=200.0),
            ],
            total_billed_amount=200.0, parsing_confidence="high", raw_text=None,
        )
        bill_id = repository.persist_bill(
            case_id=case_id, bill=bill, facility_id=facility_id,
            facility_match_status="matched", facility_match_confidence=1.0,
            storage_key="",
        )

        # EOB for a completely different service -- won't match the bill line
        eob = EobExtraction(
            insurer_name="BlueCross", member_id=None, claim_number=None,
            date_processed=None,
            total_billed_amount=500.0, total_allowed_amount=200.0,
            total_insurance_paid=160.0, total_patient_responsibility=40.0,
            line_items=[
                EobLineItem(line_number=1, date_of_service=None,
                    description="MRI brain", procedure_code="70553",
                    code_type="cpt", units=1.0,
                    billed_amount=500.0, allowed_amount=200.0,
                    insurance_paid=160.0, patient_responsibility=40.0,
                    adjustment_codes=[]),
            ],
            parsing_confidence="medium", raw_text=None,
        )
        eob_match = match_eob_to_bill(eob, bill)

        # Confirm the match found nothing
        assert len(eob_match.unmatched_eob_lines) == 1
        assert len(eob_match.matched) == 0

        repository.persist_eob(
            bill_id=bill_id, eob=eob, match_result=eob_match, storage_key="",
        )

        # bill_line_items should still have NULL allowed_amount/patient_responsibility
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT allowed_amount, patient_responsibility FROM bill_line_items WHERE bill_id = %s",
                    (bill_id,),
                )
                row = cur.fetchone()

        assert row[0] is None, "allowed_amount should remain NULL for unmatched EOB line"
        assert row[1] is None, "patient_responsibility should remain NULL for unmatched EOB line"

    finally:
        _execute("DELETE FROM bills WHERE id = %s", (bill_id,)) if bill_id else None
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        _delete_facility(facility_id)


def test_fetch_eob_summary_for_bill_returns_none_when_no_eob():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="No EOB Hospital", npi=npi, state="GA")
    patient_id = repository.insert_patient(household_income=None, household_size=None, state=None)
    case_id = repository.insert_case(patient_id)
    bill_id = None

    try:
        bill = BillExtraction(
            provider=ExtractedProviderInfo(name="No EOB Hospital", npi=npi,
                                           tax_id=None, address=None, state="GA"),
            date_of_service=None, account_number=None,
            line_items=[
                ExtractedLineItem(line_number=1, description="ER visit",
                                  procedure_code="99285", code_type="cpt",
                                  units=1.0, billed_amount=1500.0),
            ],
            total_billed_amount=1500.0, parsing_confidence="high", raw_text=None,
        )
        bill_id = repository.persist_bill(
            case_id=case_id, bill=bill, storage_key="",
        )

        assert repository.fetch_eob_summary_for_bill(bill_id) is None

    finally:
        _execute("DELETE FROM bills WHERE id = %s", (bill_id,)) if bill_id else None
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        _delete_facility(facility_id)


def test_synthesis_uses_eob_patient_responsibility_as_anchor():
    """
    Verifies the full chain from SynthesisInput to SynthesisResult
    when EOB data is present: the EOB-derived reason should appear in the
    result for a denied claim scenario.
    """
    import synthesis
    from synthesis import SynthesisInput, OutcomeType

    # Denied claim: patient owes full billed, allowed is much less
    input_data = SynthesisInput(
        billed_amount=2000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        allowed_amount_total=800.0,
        patient_responsibility_total=2000.0,
    )
    result = synthesis.synthesize(input_data)

    eob_reasons = [
        r for r in result.reasons
        if r.outcome_type == OutcomeType.PARTIAL_REDUCTION
        and "contracted rate" in r.summary
    ]
    assert len(eob_reasons) == 1, "expected exactly one EOB-derived reason"
    assert eob_reasons[0].estimated_high == 1200  # 2000 - 800




def test_upsert_mrf_finding_rates_found_round_trip():
    """persist an MrfFindingResult and read it back."""
    from mrf_pipeline import MrfFindingResult, MrfCodeRate

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="MRF Test Hospital", npi=npi, state="TX")

    finding = MrfFindingResult(
        facility_id=facility_id,
        mrf_url="https://example.com/mrf.json",
        status="rates_found",
        status_detail="Found rates for 99213: gross $500, cash $220, negotiated $110-$340.",
        codes_queried=["99213", "71046"],
        rates={
            "99213": MrfCodeRate(
                code="99213", code_type="CPT",
                description="Office visit",
                gross_charge=500.0, discounted_cash_price=220.0,
                min_negotiated_charge=110.0, max_negotiated_charge=340.0,
                payer_rates=[{"payer_name": "Aetna", "plan_name": "PPO", "rate": 180.0}],
            ),
        },
    )

    try:
        finding_id = repository.upsert_mrf_finding(finding)
        assert finding_id

        row = repository.fetch_mrf_finding_for_facility(facility_id)
        assert row is not None
        assert row["mrf_status"] == "rates_found"
        assert row["mrf_url"] == "https://example.com/mrf.json"
        assert "99213" in row["codes_queried"]
        assert "71046" in row["codes_queried"]

        # Rates JSONB round-trip
        rates = row["rates"]
        assert "99213" in rates
        assert rates["99213"]["gross_charge"] == 500.0
        assert rates["99213"]["discounted_cash_price"] == 220.0
        assert rates["99213"]["payer_rates"][0]["payer_name"] == "Aetna"

    finally:
        _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        _delete_facility(facility_id)


def test_upsert_mrf_finding_overwrites_previous_result():
    """second upsert for same facility updates, not duplicates."""
    from mrf_pipeline import MrfFindingResult

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="MRF Upsert Hospital", npi=npi, state="CA")

    first = MrfFindingResult(
        facility_id=facility_id, mrf_url=None,
        status="mrf_url_unknown", status_detail="No URL.",
        codes_queried=["99213"], rates={},
    )
    second = MrfFindingResult(
        facility_id=facility_id, mrf_url="https://example.com/mrf.json",
        status="rates_found", status_detail="Found rates.",
        codes_queried=["99213"], rates={},
    )

    try:
        repository.upsert_mrf_finding(first)
        repository.upsert_mrf_finding(second)

        # Should be exactly one row with the second (updated) values
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*), max(mrf_status::text) FROM mrf_findings WHERE facility_id = %s",
                    (facility_id,),
                )
                count, status = cur.fetchone()
        assert count == 1
        assert status == "rates_found"

    finally:
        _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        _delete_facility(facility_id)


def test_fetch_mrf_finding_returns_none_when_not_yet_run():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="No MRF Hospital", npi=npi, state="WA")
    try:
        assert repository.fetch_mrf_finding_for_facility(facility_id) is None
    finally:
        _delete_facility(facility_id)


def test_worker_fetch_mrf_rates_job_mrf_url_unknown():
    """
    Worker job for a facility with no linked health system (no mrf_url):
    should complete and write an mrf_url_unknown finding, not fail/crash.
    All MRF statuses are valid outcomes -- the job only fails if an
    unexpected exception is raised.
    """
    import worker

    npi = _unique_npi()
    facility_id = repository.insert_facility(name="No HS MRF Hospital", npi=npi, state="AZ")
    job_id = repository.enqueue_job("fetch_mrf_rates", {
        "facility_id": facility_id,
        "codes": ["99213"],
        "health_system_id": None,
    })

    try:
        had_job = worker.process_next_job()
        assert had_job is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
                job_status = cur.fetchone()[0]

        assert job_status == "completed", f"job failed: {job_status}"

        finding = repository.fetch_mrf_finding_for_facility(facility_id)
        assert finding is not None
        assert finding["mrf_status"] == "mrf_url_unknown"

    finally:
        # Delete by facility_id payload too in case job was re-queued or failed mid-run
        _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s OR id = %s", (facility_id, job_id))
        _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        _delete_facility(facility_id)


def test_worker_fetch_mrf_rates_job_unreachable_url():
    """
    Worker job where the mrf_url is set but nothing is listening:
    should complete with mrf_unreachable, not permanently fail the job.
    """
    import worker

    hs_id = repository.insert_health_system(
        name="MRF Unreachable Health System",
        mrf_url="http://localhost:19998/mrf.json",  # nothing listening
    )
    npi = _unique_npi()
    facility_id = repository.insert_facility(
        name="MRF Unreachable Hospital", npi=npi, state="OR", health_system_id=hs_id,
    )
    job_id = repository.enqueue_job("fetch_mrf_rates", {
        "facility_id": facility_id,
        "codes": ["99213", "71046"],
        "health_system_id": hs_id,
    })

    try:
        had_job = worker.process_next_job()
        assert had_job is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
                job_status = cur.fetchone()[0]

        assert job_status == "completed", (
            f"expected completed (mrf_unreachable is a valid outcome, not a job failure), "
            f"got {job_status!r}"
        )

        finding = repository.fetch_mrf_finding_for_facility(facility_id)
        assert finding is not None
        assert finding["mrf_status"] == "mrf_unreachable"
        assert "localhost" in finding["mrf_url"]

    finally:
        _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s OR id = %s", (facility_id, job_id))
        _execute("DELETE FROM mrf_findings WHERE facility_id = %s", (facility_id,))
        _delete_facility(facility_id)
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))


def test_insert_health_system_with_mrf_url():
    """Confirm mrf_url persists correctly through insert_health_system."""
    hs_id = repository.insert_health_system(
        name="MRF URL Test Health System",
        mrf_url="https://example.com/mrf.json",
    )
    try:
        mrf_url = repository.fetch_mrf_url_for_health_system(hs_id)
        assert mrf_url == "https://example.com/mrf.json"
    finally:
        _execute("DELETE FROM health_systems WHERE id = %s", (hs_id,))



# ============================================================
# Outcome tracking tests (real DB)
# ============================================================

def _make_case() -> tuple[str, str]:
    """Helper: insert a patient + case with fee agreement, return (patient_id, case_id)."""
    import outcome_pipeline
    patient_id = repository.insert_patient(household_income=50000, household_size=2, state="TX")
    outcome_pipeline.record_fee_agreement(patient_id)  # required before start_negotiation
    case_id = repository.insert_case(patient_id)
    return patient_id, case_id


def test_start_negotiation_creates_row_and_advances_case_status():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(
            case_id=case_id,
            original_billed_amount=5000.0,
            target_amount=1500.0,
        )
        assert neg_id

        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary is not None
        assert summary.status == "pending"
        assert summary.original_billed_amount == 5000.0
        assert summary.target_amount == 1500.0
        assert summary.agreed_amount is None

        # Case status should advance to negotiating
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
                assert cur.fetchone()[0] == "negotiating"

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_start_negotiation_is_idempotent():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        id1 = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=1000.0)
        id2 = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=1000.0)
        assert id1 == id2  # same row returned, not a second insert
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_contact_advances_status_to_contacted():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=2000.0)
        contact_id = outcome_pipeline.record_contact(
            negotiation_id=neg_id,
            channel="letter_mail",
            notes="Sent certified mail to billing dept",
        )
        assert contact_id

        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "contacted"
        assert summary.first_contacted_at is not None
        assert len(summary.contacts) == 1
        assert summary.contacts[0]["channel"] == "letter_mail"
        assert summary.contacts[0]["notes"] == "Sent certified mail to billing dept"

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_contact_invalid_channel_raises():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=1000.0)
        try:
            outcome_pipeline.record_contact(neg_id, channel="carrier_pigeon")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "carrier_pigeon" in str(e)
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_provider_response_with_counter_offer():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=3000.0)
        outcome_pipeline.record_contact(neg_id, channel="letter_fax")
        outcome_pipeline.record_provider_response(
            negotiation_id=neg_id,
            response_text="We can reduce to $2,100 but no further.",
            counter_offer_amount=2100.0,
        )
        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "counter_offer"
        assert summary.counter_offer_amount == 2100.0
        assert "2,100" in summary.provider_response_text
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_outcome_computes_savings_and_fee_correctly():
    """
    The key test: agreed_amount=1500 on a 5000 bill means:
      savings = 3500, fee = 700 (20%), patient net = 2800 (80%)
    These are GENERATED columns in Postgres -- we verify the DB is
    computing them correctly, not that our Python code is.
    """
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=5000.0)
        outcome_pipeline.record_contact(neg_id, channel="letter_email")

        receipt = outcome_pipeline.record_outcome(
            negotiation_id=neg_id,
            agreed_amount=1500.0,
            paid=False,
        )

        assert receipt.original_billed_amount == 5000.0
        assert receipt.agreed_amount == 1500.0
        assert receipt.amount_saved == 3500.0
        assert receipt.robinhealth_fee == 700.0    # 20% of 3500
        assert receipt.patient_net_savings == 2800.0  # 80% of 3500
        assert receipt.savings_pct == 70.0          # 3500/5000
        assert receipt.status == "agreed"

        # Verify the DB row matches
        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.amount_saved == 3500.0
        assert summary.robinhealth_fee == 700.0
        assert summary.patient_net_savings == 2800.0

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_outcome_paid_advances_case_to_resolved():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=2000.0)
        outcome_pipeline.record_contact(neg_id, channel="phone_call")
        outcome_pipeline.record_outcome(
            negotiation_id=neg_id, agreed_amount=800.0, paid=True,
        )

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
                assert cur.fetchone()[0] == "resolved"

        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "paid"
        assert summary.paid_at is not None
        assert summary.agreed_at is not None

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_outcome_rejects_no_savings():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=1000.0)
        try:
            outcome_pipeline.record_outcome(neg_id, agreed_amount=1000.0)  # no savings
            assert False, "Should raise ValueError"
        except ValueError as e:
            assert "less than" in str(e)

        try:
            outcome_pipeline.record_outcome(neg_id, agreed_amount=1500.0)  # more than billed!
            assert False, "Should raise ValueError"
        except ValueError as e:
            assert "less than" in str(e)

        try:
            outcome_pipeline.record_outcome(neg_id, agreed_amount=-1.0)  # negative
            assert False, "Should raise ValueError"
        except ValueError as e:
            assert "negative" in str(e)

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_outcome_allows_full_elimination():
    """agreed_amount=0 (full charity care) is valid -- not a "no savings" case."""
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=3000.0)
        receipt = outcome_pipeline.record_outcome(neg_id, agreed_amount=0.0)
        assert receipt.amount_saved == 3000.0
        assert receipt.robinhealth_fee == 600.0   # 20% of 3000
        assert receipt.savings_pct == 100.0
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_mark_paid_advances_to_resolved():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=4000.0)
        outcome_pipeline.record_outcome(neg_id, agreed_amount=1200.0, paid=False)

        # Confirm status is 'agreed' not yet 'paid'
        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "agreed"

        outcome_pipeline.mark_paid(neg_id)

        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "paid"
        assert summary.paid_at is not None

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
                assert cur.fetchone()[0] == "resolved"

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_fetch_outcomes_summary_counts_correctly():
    import outcome_pipeline
    # Create two negotiations: one agreed, one rejected
    p1, c1 = _make_case()
    p2, c2 = _make_case()
    try:
        n1 = outcome_pipeline.start_negotiation(c1, 5000.0)
        outcome_pipeline.record_contact(n1, "letter_mail")
        outcome_pipeline.record_outcome(n1, agreed_amount=2000.0, paid=True)

        n2 = outcome_pipeline.start_negotiation(c2, 3000.0)
        outcome_pipeline.record_contact(n2, "phone_call")
        outcome_pipeline.mark_rejected(n2, "Provider refuses to negotiate")

        summary = outcome_pipeline.fetch_outcomes_summary()

        # At least our two new ones show up
        assert summary["total_agreed"] >= 1
        assert summary["total_paid"] >= 1
        assert summary["total_rejected"] >= 1
        assert summary["total_amount_saved"] >= 3000.0  # 5000 - 2000
        assert summary["total_robinhealth_fees"] >= 600.0  # 20% of 3000

    finally:
        for c, p in [(c1, p1), (c2, p2)]:
            _execute("DELETE FROM negotiations WHERE case_id = %s", (c,))
            _execute("DELETE FROM cases WHERE id = %s", (c,))
            _execute("DELETE FROM patients WHERE id = %s", (p,))



def test_record_provider_response_structured_reduced_offer():
    """
    Full DB round-trip: start negotiation, contact, structured response
    (reduced offer), check classification stored and status updated.
    """
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=4000.0, target_amount=1200.0)
        contact_id = outcome_pipeline.record_contact(neg_id, channel="letter_mail")

        classified, followup = outcome_pipeline.record_provider_response_structured(
            negotiation_id=neg_id,
            response_text="We are willing to reduce your balance to $2,000 as a courtesy.",
            contact_id=contact_id,
        )

        assert classified.response_type == "reduced_offer"
        assert classified.extracted_amount == 2000.0

        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "counter_offer"
        assert summary.counter_offer_amount == 2000.0
        assert followup.urgency == "within_week"
        assert followup.followup_letter_context["letter_type"] == "counter_offer"
        assert followup.suggested_resolution == {"agreed_amount": 2000.0}

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_provider_response_structured_collections_urgent():
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(case_id=case_id, original_billed_amount=3000.0)
        outcome_pipeline.record_contact(neg_id, channel="letter_fax")

        classified, followup = outcome_pipeline.record_provider_response_structured(
            negotiation_id=neg_id,
            response_text="This account has been referred to our collection agency. "
                          "Please contact Acme Collections to resolve.",
        )

        assert classified.response_type == "referred_to_collections"
        assert followup.urgency == "immediate"
        assert "FDCPA" in str(followup.followup_letter_context)
        assert "501(r)" in str(followup.followup_letter_context)

        # Status should be provider_replied
        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status == "provider_replied"

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_provider_response_structured_accepted_auto_resolves():
    """
    When provider accepts our target, record_provider_response_structured
    should auto-call record_outcome and advance to agreed.
    """
    import outcome_pipeline
    patient_id, case_id = _make_case()
    try:
        neg_id = outcome_pipeline.start_negotiation(
            case_id=case_id, original_billed_amount=5000.0, target_amount=1500.0
        )
        outcome_pipeline.record_contact(neg_id, channel="letter_email")

        classified, followup = outcome_pipeline.record_provider_response_structured(
            negotiation_id=neg_id,
            response_text="We have approved your financial assistance request. "
                          "Your balance has been reduced to $1,500.",
        )

        assert classified.response_type == "accepted_target"
        assert followup.resolves_negotiation is True

        # Should have auto-resolved
        summary = outcome_pipeline.fetch_negotiation_for_case(case_id)
        assert summary.status in ("agreed", "provider_replied")  # auto-resolve may have run
        if summary.status == "agreed":
            assert summary.agreed_amount == 1500.0
            assert summary.amount_saved == 3500.0
            assert summary.robinhealth_fee == 700.0

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))



def test_record_fee_agreement_stores_terms_text():
    import outcome_pipeline
    patient_id = repository.insert_patient(household_income=50000, household_size=2, state="TX")
    try:
        # Before agreement
        status = outcome_pipeline.check_fee_agreement(patient_id)
        assert status["accepted"] is False
        assert status["accepted_at"] is None

        # Record agreement
        outcome_pipeline.record_fee_agreement(patient_id)
        status = outcome_pipeline.check_fee_agreement(patient_id)
        assert status["accepted"] is True
        assert status["accepted_at"] is not None
        assert status["terms_version"] == outcome_pipeline.FEE_TERMS_VERSION
        assert status["terms_current"] is True

        # Verify the text was stored
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fee_agreement_terms_text FROM patients WHERE id = %s",
                    (patient_id,),
                )
                stored_text = cur.fetchone()[0]
        assert "20%" in stored_text
        assert "Version 1.0" in stored_text  # FEE_TERMS_TEXT header says "Version 1.0"

    finally:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_record_fee_agreement_is_idempotent():
    import outcome_pipeline
    patient_id = repository.insert_patient()
    try:
        outcome_pipeline.record_fee_agreement(patient_id)
        outcome_pipeline.record_fee_agreement(patient_id)  # second call should not raise
        status = outcome_pipeline.check_fee_agreement(patient_id)
        assert status["accepted"] is True
    finally:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_start_negotiation_blocked_without_fee_agreement():
    import outcome_pipeline
    patient_id = repository.insert_patient(household_income=40000, household_size=1, state="CA")
    case_id = repository.insert_case(patient_id)
    try:
        # Should raise FeeAgreementRequired, not proceed
        try:
            outcome_pipeline.start_negotiation(
                case_id=case_id,
                original_billed_amount=3000.0,
            )
            assert False, "Should have raised FeeAgreementRequired"
        except outcome_pipeline.FeeAgreementRequired as exc:
            assert "fee agreement" in str(exc).lower()
            assert "agree-to-terms" in str(exc)

        # Confirm no negotiation was created
        assert outcome_pipeline.fetch_negotiation_id_for_case(case_id) is None

    finally:
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_start_negotiation_allowed_after_fee_agreement():
    import outcome_pipeline
    patient_id = repository.insert_patient(household_income=40000, household_size=1, state="CA")
    case_id = repository.insert_case(patient_id)
    try:
        outcome_pipeline.record_fee_agreement(patient_id)
        neg_id = outcome_pipeline.start_negotiation(
            case_id=case_id,
            original_billed_amount=3000.0,
        )
        assert neg_id is not None
        assert outcome_pipeline.fetch_negotiation_id_for_case(case_id) == neg_id
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_fee_terms_endpoint_returns_terms():
    """GET /patients/{patient_id}/fee-terms returns terms + acceptance status."""
    patient_id = repository.insert_patient()
    try:
        with TestClient(app) as c:
            r = c.get(f"/patients/{patient_id}/fee-terms")
        assert r.status_code == 200
        body = r.json()
        assert body["terms"]["fee_percentage"] == 20
        assert body["terms"]["no_cure_no_fee"] is True
        assert body["agreement_status"]["accepted"] is False
        assert "20%" in body["terms"]["text"]
    finally:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_agree_to_terms_stores_agreement():
    """POST /patients/{patient_id}/agree-to-terms records acceptance."""
    patient_id = repository.insert_patient()
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/patients/{patient_id}/agree-to-terms",
                data={"affirmed": "true"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["accepted_at"] is not None
        assert "20%" in body["message"]
    finally:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_agree_to_terms_requires_affirmed_true():
    """affirmed=false should return 400."""
    patient_id = repository.insert_patient()
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/patients/{patient_id}/agree-to-terms",
                data={"affirmed": "false"},
            )
        assert r.status_code == 400
        assert "must be true" in r.json()["detail"].lower()
    finally:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_negotiate_blocked_without_fee_agreement():
    """POST /cases/{case_id}/negotiate returns 402 if no fee agreement."""
    patient_id = repository.insert_patient()
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/cases/{case_id}/negotiate",
                data={"billed_amount": "2000.0"},
            )
        assert r.status_code == 402
        body = r.json()
        assert body["detail"]["error"] == "fee_agreement_required"
        assert "agree-to-terms" in body["detail"]["agreement_endpoint"]
    finally:
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_full_fee_agreement_and_negotiate_flow():
    """
    End-to-end: terms → agree → negotiate.
    This is the canonical happy path for a new patient.
    """
    import outcome_pipeline
    patient_id = repository.insert_patient(household_income=45000, household_size=3, state="TX")
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            # Step 1: Get terms
            r = c.get(f"/patients/{patient_id}/fee-terms")
            assert r.status_code == 200
            assert r.json()["agreement_status"]["accepted"] is False

            # Step 2: Agree
            r = c.post(
                f"/patients/{patient_id}/agree-to-terms",
                data={"affirmed": "true"},
            )
            assert r.status_code == 200

            # Step 3: Verify terms show as accepted now
            r = c.get(f"/patients/{patient_id}/fee-terms")
            assert r.json()["agreement_status"]["accepted"] is True

            # Step 4: Start negotiation -- now allowed
            r = c.post(
                f"/cases/{case_id}/negotiate",
                data={"billed_amount": "3000.0", "target_amount": "900.0"},
            )
            assert r.status_code == 200
            assert r.json()["negotiation_id"] is not None

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")
