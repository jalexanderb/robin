"""
RobinHealth: API layer tests.

Uses FastAPI's TestClient -- this exercises the real ASGI request/
response cycle, real Pydantic/form validation, and real route matching,
just without a literal TCP socket. It needs a live Postgres (the same
DATABASE_URL / ROBINHEALTH_DB_* config as test_repository.py), since
/intake creates real patient/case/bill rows.

bill_pipeline.extract_bill is mocked in most tests below -- it's the one
piece still genuinely blocked in this sandbox (llm_client.py has no
reachable endpoint here), not a design choice to avoid testing it.
test_intake_returns_503_when_llm_endpoint_unreachable deliberately does
NOT mock it, to prove the API's real (current) failure-handling path
against the real (current) unreachable endpoint.

Run: DATABASE_URL=... python3 test_api.py
"""

from __future__ import annotations

import io
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
import repository
import storage
from api import app
from bill_pipeline import BillExtraction, ExtractedLineItem, ExtractedProviderInfo
from compliance_checklist import ComplianceStatus, Severity
from fap_pipeline import ComplianceFinding, DocumentQuality, EligibilityExtraction, FapParseResult


client: TestClient | None = None  # set for real inside the `with` block in __main__ below


def _unique_npi() -> str:
    import uuid
    return str(abs(hash(uuid.uuid4())) % 10_000_000_000).zfill(10)


def _execute(sql: str, params: tuple) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _cleanup_case(patient_id: str | None, case_id: str | None, facility_id: str | None = None) -> None:
    if case_id:
        _execute("DELETE FROM bills WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
    if patient_id:
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))
    if facility_id:
        _execute("DELETE FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,))
        # financial_assistance_policies.facility_id has no ON DELETE
        # clause (verified against schema.sql), so a lingering FAP row
        # would block facility deletion below with a foreign key
        # violation. The four fap_* child tables (tiers,
        # eligible_services, application_requirements,
        # compliance_findings) all cascade from
        # financial_assistance_policies, so deleting it here handles all
        # of them in one step.
        _execute("DELETE FROM financial_assistance_policies WHERE facility_id = %s", (facility_id,))
        _execute("DELETE FROM facilities WHERE id = %s", (facility_id,))


def _fake_bill_upload() -> dict:
    return {"bill_document": ("bill.png", io.BytesIO(b"fake-bill-bytes"), "image/png")}


def test_health_endpoint_reports_ok_when_db_reachable():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_reachable"] is True


def test_health_endpoint_reports_degraded_when_db_unreachable():
    with patch("db.connection", side_effect=Exception("simulated DB outage")):
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database_reachable"] is False


def test_intake_rejects_unsupported_content_type():
    response = client.post(
        "/intake",
        files={"bill_document": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 400


def test_intake_rejects_empty_file():
    response = client.post(
        "/intake",
        files={"bill_document": ("bill.png", io.BytesIO(b""), "image/png")},
    )
    assert response.status_code == 400


def test_intake_matched_facility_creates_patient_case_and_persists_bill():
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="API Test Hospital", npi=npi, state="CA")

    bill = BillExtraction(
        provider=ExtractedProviderInfo(name="API Test Hospital", npi=npi, tax_id=None, address=None, state="CA"),
        date_of_service="2026-06-01", account_number="ACC-API-1",
        line_items=[ExtractedLineItem(
            line_number=1, description="Office visit", procedure_code="API001",
            code_type="cpt", units=1, billed_amount=300.0,
        )],
        total_billed_amount=300.0, parsing_confidence="high", raw_text=None,
    )

    patient_id = case_id = None
    try:
        with patch("bill_pipeline.extract_bill", return_value=bill):
            response = client.post(
                "/intake",
                files=_fake_bill_upload(),
                data={"household_income": "25000", "household_size": "2", "state": "CA"},
            )

        assert response.status_code == 200
        body = response.json()
        patient_id, case_id = body["patient_id"], body["case_id"]

        result = body["result"]
        assert result["bill"]["provider"]["name"] == "API Test Hospital"
        assert result["match"]["status"] == "matched"
        assert result["match"]["facility_id"] == facility_id
        assert result["new_facility_queued_for_fap_parsing"] is False

        # Independently confirm real persistence, not just a plausible-looking response
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT household_income, state FROM patients WHERE id = %s", (patient_id,))
                income, state = cur.fetchone()
                assert float(income) == 25000.0
                assert state == "CA"

                cur.execute(
                    "SELECT facility_id, facility_match_status, total_billed_amount, storage_key FROM bills WHERE case_id = %s",
                    (case_id,),
                )
                stored_facility_id, status, total, storage_key = cur.fetchone()
                assert str(stored_facility_id) == facility_id
                assert status == "matched"
                assert float(total) == 300.0
                assert storage_key.endswith(".png")  # content-addressed key, see storage.py
                assert storage.exists(storage_key)
                assert storage.load(storage_key) == b"fake-bill-bytes"
    finally:
        _cleanup_case(patient_id, case_id, facility_id)


def test_intake_unmatched_provider_creates_new_facility_and_enqueues_job():
    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Brand New API Test Clinic", npi=_unique_npi(), tax_id=None, address=None, state="WA",
        ),
        date_of_service="2026-06-02", account_number="ACC-API-2",
        line_items=[], total_billed_amount=450.0, parsing_confidence="high", raw_text=None,
    )

    patient_id = case_id = facility_id = None
    try:
        with patch("bill_pipeline.extract_bill", return_value=bill):
            response = client.post("/intake", files=_fake_bill_upload(), data={"state": "WA"})

        assert response.status_code == 200
        body = response.json()
        patient_id, case_id = body["patient_id"], body["case_id"]
        result = body["result"]
        assert result["match"]["status"] == "new_facility_created"
        facility_id = result["match"]["facility_id"]
        assert facility_id is not None
        assert result["new_facility_queued_for_fap_parsing"] is True

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, state FROM facilities WHERE id = %s", (facility_id,))
                name, state = cur.fetchone()
                assert name == "Brand New API Test Clinic"
                assert state == "WA"

                cur.execute(
                    "SELECT job_type, status FROM jobs WHERE payload->>'facility_id' = %s", (facility_id,),
                )
                job_type, job_status = cur.fetchone()
                assert job_type == "parse_fap"
                assert job_status == "pending"

                cur.execute("SELECT facility_id, facility_match_status FROM bills WHERE case_id = %s", (case_id,))
                stored_facility_id, status = cur.fetchone()
                assert str(stored_facility_id) == facility_id
                assert status == "new_facility_created"
    finally:
        _cleanup_case(patient_id, case_id, facility_id)


def test_intake_returns_503_when_llm_endpoint_unreachable():
    """
    Deliberately does NOT mock extract_bill -- this exercises the real,
    current state of this sandbox (no reachable LLM_BASE_URL), proving
    /intake degrades to a clean 503 rather than a raw 500/traceback.
    patient_id/case_id are still returned so the caller can retry the
    same case later -- see the DESIGN DECISION comment in api.py.
    """
    patient_id = case_id = None
    try:
        response = client.post("/intake", files=_fake_bill_upload())

        assert response.status_code == 503
        body = response.json()
        assert "patient_id" in body and "case_id" in body
        patient_id, case_id = body["patient_id"], body["case_id"]
        assert "LLM" in body["detail"] or "endpoint" in body["detail"]

        # The case really was created and really has no bill -- confirming
        # the documented "left in 'intake' status for retry" design, not
        # just trusting the response body.
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
                assert cur.fetchone()[0] == "intake"
                cur.execute("SELECT count(*) FROM bills WHERE case_id = %s", (case_id,))
                assert cur.fetchone()[0] == 0
    finally:
        _cleanup_case(patient_id, case_id)


def test_intake_matched_facility_with_persisted_fap_serializes_enum_findings_correctly():
    """
    Bug review gap: the existing matched-facility test exercises a
    freshly-created facility with no FAP data, so it never exercises
    jsonable_encoder against a real, persisted FapParseResult containing
    enum-typed fields (ComplianceFinding.status / .severity, both
    ComplianceStatus / Severity str-Enums). This is the path my
    findings_to_reasons fixes were specifically targeting (the live
    /intake request path, via fetch_fap_for_facility) -- worth a real
    end-to-end check that the full response actually serializes cleanly,
    not just that the underlying Python objects construct without error.
    """
    npi = _unique_npi()
    facility_id = repository.insert_facility(name="Enum Serialization Test Hospital", npi=npi, state="CA")

    fap_result = FapParseResult(
        facility_id=facility_id,
        document_quality=DocumentQuality(label="vague_or_incomplete", rationale="test"),
        eligibility=EligibilityExtraction(
            eligibility_basis=None, tiers=[], eligible_services=[],
            application_requirements=None, parsing_confidence="low",
        ),
        findings=[ComplianceFinding(
            requirement_code="agb_methodology_disclosed", status=ComplianceStatus.ABSENT,
            evidence_text=None, severity=Severity.MATERIAL, argument_template="test template",
        )],
        raw_text="test", source_doc_hash="abc",
    )
    repository.insert_fap_parse_result(facility_id, fap_result)

    bill = BillExtraction(
        provider=ExtractedProviderInfo(name="Enum Serialization Test Hospital", npi=npi, tax_id=None, address=None, state="CA"),
        date_of_service="2026-06-01", account_number="ACC-ENUM-1",
        line_items=[ExtractedLineItem(
            line_number=1, description="Visit", procedure_code="X1",
            code_type="cpt", units=1, billed_amount=100.0,
        )],
        total_billed_amount=100.0, parsing_confidence="high", raw_text=None,
    )

    patient_id = case_id = None
    try:
        with patch("bill_pipeline.extract_bill", return_value=bill):
            response = client.post("/intake", files=_fake_bill_upload(), data={"state": "CA"})

        assert response.status_code == 200
        body = response.json()
        patient_id, case_id = body["patient_id"], body["case_id"]

        findings = body["result"]["fap"]["findings"]
        assert len(findings) == 1
        # Plain string values, not Python enum repr garbage like
        # "ComplianceStatus.ABSENT" or "<ComplianceStatus.ABSENT: 'absent'>"
        assert findings[0]["status"] == "absent"
        assert findings[0]["severity"] == "material"
    finally:
        _cleanup_case(patient_id, case_id, facility_id)



def test_intake_returns_401_when_api_key_set_and_missing():
    import os
    with patch.dict(os.environ, {"API_KEY": "test-secret-key"}):
        # Reimport api to pick up the new env var
        import importlib
        import api as api_module
        importlib.reload(api_module)
        with TestClient(api_module.app) as client:
            response = client.post(
                "/intake",
                files={"bill_document": ("bill.pdf", b"fake", "application/pdf")},
            )
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_intake_returns_200_when_api_key_set_and_correct():
    # 503 expected (no LLM), but never 401 -- auth must pass with correct key
    import os, importlib, db as db_module
    import api as api_module
    with patch.dict(os.environ, {"API_KEY": "test-secret-key"}):
        importlib.reload(api_module)
    with TestClient(api_module.app) as client:
        from bill_pipeline import BillExtraction, ExtractedProviderInfo, ExtractedLineItem
        fake_bill = BillExtraction(
            provider=ExtractedProviderInfo(name="Auth Test Hospital", npi=None,
                                           tax_id=None, address=None, state="CA"),
            date_of_service=None, account_number=None,
            line_items=[ExtractedLineItem(line_number=1, description="x",
                                          procedure_code="99213", code_type="cpt",
                                          units=1, billed_amount=300.0)],
            total_billed_amount=300.0, parsing_confidence="high", raw_text=None,
        )
        with patch("bill_pipeline.extract_bill", return_value=fake_bill),              patch.dict(os.environ, {"API_KEY": "test-secret-key"}):
            response = client.post(
                "/intake",
                headers={"Authorization": "Bearer test-secret-key"},
                files={"bill_document": ("bill.pdf", b"fake", "application/pdf")},
            )
        body = response.json()
        patient_id = body.get("patient_id")
        case_id = body.get("case_id")

    assert response.status_code != 401

    # Clean up any rows this test created
    with db_module.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM facilities WHERE name = %s", ("Auth Test Hospital",))
            row = cur.fetchone()
            fac_id = str(row[0]) if row else None
            if fac_id:
                cur.execute("DELETE FROM jobs WHERE payload->>%s = %s", ("facility_id", fac_id))
            if case_id:
                cur.execute("DELETE FROM bills WHERE case_id = %s", (case_id,))
                cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
            if patient_id:
                cur.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
            if fac_id:
                cur.execute("DELETE FROM facilities WHERE id = %s", (fac_id,))


def test_intake_returns_413_when_bill_too_large():
    import os
    with patch.dict(os.environ, {"MAX_BILL_SIZE_MB": "1"}):
        import importlib
        import api as api_module
        importlib.reload(api_module)
        with TestClient(api_module.app) as client:
            big_bytes = b"x" * (1 * 1024 * 1024 + 1)  # 1 MB + 1 byte
            response = client.post(
                "/intake",
                files={"bill_document": ("bill.pdf", big_bytes, "application/pdf")},
            )
    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"]


def test_intake_request_id_in_response_headers():
    import api as api_module
    with TestClient(api_module.app) as client:
        response = client.get("/health")
    assert "X-Request-ID" in response.headers
    # Should be a valid UUID4
    import uuid
    uuid.UUID(response.headers["X-Request-ID"])  # raises if invalid


def test_db_pool_init_and_close():
    """init_pool() creates a pool; close_pool() cleans it up."""
    import db as db_module
    db_module.close_pool()  # ensure clean state
    assert db_module._pool is None

    db_module.init_pool(min_conn=1, max_conn=2)
    assert db_module._pool is not None

    # Pool should actually work
    with db_module.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)

    db_module.close_pool()
    assert db_module._pool is None


def test_db_direct_connection_when_no_pool():
    """Without init_pool(), connection() uses a direct psycopg2.connect."""
    import db as db_module
    db_module.close_pool()  # ensure no pool
    assert db_module._pool is None

    with db_module.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 42")
            assert cur.fetchone() == (42,)

    # Pool should still be None after a direct connection
    assert db_module._pool is None


def test_health_endpoint_includes_request_id():
    import api as api_module
    with TestClient(api_module.app) as client:
        r1 = client.get("/health")
        r2 = client.get("/health")
    # Each request gets a unique ID
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]



def test_api_negotiate_and_outcome_endpoints_end_to_end():
    """Full round-trip: negotiate -> contact -> outcome -> GET case."""
    patient_id = repository.insert_patient(household_income=40000, household_size=1, state="CA")
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            # Agree to terms first (required before negotiation)
            r = c.post(
                f"/patients/{patient_id}/agree-to-terms",
                data={"affirmed": "true"},
            )
            assert r.status_code == 200, r.json()

            # Start negotiation
            r = c.post(
                f"/cases/{case_id}/negotiate",
                data={"billed_amount": "2000.0", "target_amount": "600.0"},
            )
            assert r.status_code == 200, r.json()
            neg_id = r.json()["negotiation_id"]
            assert neg_id

            # Record contact
            r = c.post(
                f"/cases/{case_id}/contact",
                data={"channel": "letter_fax", "notes": "sent 3/1"},
            )
            assert r.status_code == 200

            # Record outcome
            r = c.post(
                f"/cases/{case_id}/outcome",
                data={"agreed_amount": "700.0", "paid": "false"},
            )
            assert r.status_code == 200, r.json()
            receipt = r.json()
            assert receipt["amount_saved"] == 1300.0
            assert receipt["robinhealth_fee"] == 260.0
            assert receipt["patient_net_savings"] == 1040.0
            assert receipt["savings_pct"] == 65.0

            # GET /cases/{case_id}
            r = c.get(f"/cases/{case_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["case_status"] == "negotiating"
            assert body["negotiation"]["status"] == "agreed"
            assert len(body["negotiation"]["contacts"]) == 1

            # GET /outcomes/summary
            r = c.get("/outcomes/summary")
            assert r.status_code == 200
            summary = r.json()
            assert summary["total_agreed"] >= 1
            assert summary["total_amount_saved"] >= 1300.0

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))



def test_api_response_endpoint_classifies_and_returns_followup():
    """Full API round-trip for POST /cases/{case_id}/response -- reduced offer."""
    patient_id = repository.insert_patient(household_income=35000, household_size=3, state="TX")
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            c.post(f"/patients/{patient_id}/agree-to-terms", data={"affirmed": "true"})
            c.post(f"/cases/{case_id}/negotiate", data={"billed_amount": "3000.0", "target_amount": "900.0"})
            c.post(f"/cases/{case_id}/contact", data={"channel": "letter_mail"})
            r = c.post(
                f"/cases/{case_id}/response",
                data={"response_text": "We can reduce your balance to $1,800 as final settlement."},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["classified"]["response_type"] == "reduced_offer"
        assert body["classified"]["extracted_amount"] == 1800.0
        assert body["followup"]["urgency"] == "within_week"
        assert body["followup"]["followup_letter_context"]["letter_type"] == "counter_offer"
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_response_endpoint_handles_eligibility_denial():
    """Denial → eligibility_appeal context with 501(r) legal citations."""
    patient_id = repository.insert_patient(household_income=28000, household_size=2, state="FL")
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            c.post(f"/patients/{patient_id}/agree-to-terms", data={"affirmed": "true"})
            c.post(f"/cases/{case_id}/negotiate", data={"billed_amount": "6000.0", "target_amount": "1800.0"})
            c.post(f"/cases/{case_id}/contact", data={"channel": "letter_fax"})
            r = c.post(
                f"/cases/{case_id}/response",
                data={"response_text": "Your application has been denied. Your income does not qualify."},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["classified"]["response_type"] == "denied_eligibility"
        ctx = body["followup"]["followup_letter_context"]
        assert ctx["letter_type"] == "eligibility_appeal"
        assert any("501(r)" in cite for cite in ctx["legal_citations"])
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_api_response_endpoint_collections_is_urgent():
    """Collections referral → immediate urgency + FDCPA/501(r) citations."""
    patient_id = repository.insert_patient(household_income=40000, household_size=1, state="CA")
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            c.post(f"/patients/{patient_id}/agree-to-terms", data={"affirmed": "true"})
            c.post(f"/cases/{case_id}/negotiate", data={"billed_amount": "4000.0"})
            c.post(f"/cases/{case_id}/contact", data={"channel": "letter_email"})
            r = c.post(
                f"/cases/{case_id}/response",
                data={"response_text": "This debt has been referred to our collection agency."},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["classified"]["response_type"] == "referred_to_collections"
        assert body["followup"]["urgency"] == "immediate"
        assert body["followup"]["resolves_negotiation"] is False
        ctx = body["followup"]["followup_letter_context"]
        assert "FDCPA" in str(ctx)
    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))



def test_draft_letter_endpoint_produces_pdf():
    """draft-letter returns a storage key and valid reference number."""
    patient_id = repository.insert_patient()
    case_id = repository.insert_case(patient_id)
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/cases/{case_id}/draft-letter",
                data={
                    "patient_name": "Jane Doe",
                    "facility_name": "General Hospital",
                    "facility_address": "123 Main St, City, ST 12345",
                    "account_number": "ACC-999",
                    "date_of_service": "2026-03-14",
                    "billed_amount": "5000.0",
                    "letter_type": "initial",
                },
            )
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["reference_number"].startswith("RH-")
        assert body["storage_key"]
        assert body["pdf_size_bytes"] > 1000
    finally:
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_draft_followup_letter_endpoint():
    """draft-letter with letter_type=followup renders from JSON context."""
    import json
    patient_id = repository.insert_patient()
    case_id = repository.insert_case(patient_id)
    ctx = {
        "letter_type": "eligibility_appeal",
        "subject": "Appeal of Denial",
        "key_points": ["We appeal the denial.", "Please review our application."],
        "legal_citations": ["26 CFR 1.501(r)-4"],
        "urgency": "standard",
    }
    try:
        with TestClient(app) as c:
            r = c.post(
                f"/cases/{case_id}/draft-letter",
                data={
                    "patient_name": "John Smith",
                    "facility_name": "Regional Hospital",
                    "billed_amount": "3000.0",
                    "letter_type": "followup",
                    "followup_context_json": json.dumps(ctx),
                    "round_number": "2",
                },
            )
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["pdf_size_bytes"] > 500
    finally:
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


def test_send_letter_endpoint_records_contact():
    """
    send-letter delivers the PDF (not_configured in sandbox) and records
    a negotiation_contacts row with the letter_storage_key.
    """
    import outcome_pipeline as op
    patient_id = repository.insert_patient()
    case_id = repository.insert_case(patient_id)
    try:
        op.record_fee_agreement(patient_id)
        op.start_negotiation(case_id=case_id, original_billed_amount=4000.0)

        with TestClient(app) as c:
            # First draft a letter to get storage_key
            draft_r = c.post(
                f"/cases/{case_id}/draft-letter",
                data={
                    "patient_name": "Alice Brown",
                    "facility_name": "County Medical",
                    "billed_amount": "4000.0",
                },
            )
            assert draft_r.status_code == 200
            storage_key = draft_r.json()["storage_key"]
            reference_number = draft_r.json()["reference_number"]

            # Now send it
            send_r = c.post(
                f"/cases/{case_id}/send-letter",
                data={
                    "storage_key": storage_key,
                    "reference_number": reference_number,
                    "channel": "letter_email",
                    "recipient_email": "billing@countymedical.org",
                },
            )
        assert send_r.status_code == 200
        body = send_r.json()
        assert body["channel"] == "letter_email"
        # In sandbox: not_configured (no SMTP) -- that's expected and correct
        assert body["delivery_status"] in ("sent", "not_configured")
        assert body["contact_id"] is not None  # contact was recorded regardless
        assert body["reference_number"] == reference_number

        # Verify contact in negotiation history
        summary = op.fetch_negotiation_for_case(case_id)
        assert summary.status == "contacted"
        assert len(summary.contacts) == 1
        assert summary.contacts[0]["channel"] == "letter_email"
        assert summary.contacts[0]["letter_storage_key"] == storage_key

    finally:
        _execute("DELETE FROM negotiations WHERE case_id = %s", (case_id,))
        _execute("DELETE FROM cases WHERE id = %s", (case_id,))
        _execute("DELETE FROM patients WHERE id = %s", (patient_id,))


if __name__ == "__main__":
    import sys
    import traceback

    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    passed = 0
    with TestClient(app) as client:  # required for the lifespan handler (app.state.rate_tables) to run
        for test in tests:
            try:
                test()
                print(f"PASS {test.__name__}")
                passed += 1
            except Exception:
                print(f"FAIL {test.__name__}")
                traceback.print_exc()
                sys.exit(1)
    print(f"\n{passed} tests passed")
