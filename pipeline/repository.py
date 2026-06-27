"""
RobinHealth: Postgres-backed repository functions.

This is the only file with raw SQL in it -- bill_pipeline.py and
case_pipeline.py delegate their facility/FAP stub functions here rather
than embedding their own queries, so the SQL surface area stays in one
place. Functions return the same dataclasses already defined elsewhere
(FacilityRecord from bill_pipeline; FapParseResult, EligibilityExtraction,
EligibilityTier, ComplianceFinding from fap_pipeline) so callers don't
need to know whether data came from a mock, a stub, or a real row.

KNOWN GAP: fap_pipeline.DocumentQuality.rationale has no backing column
in financial_assistance_policies -- the schema stores the quality LABEL
only, not the classification reasoning behind it. Rows read back here
carry a placeholder rationale rather than fabricating one; making the
rationale queryable later is a schema migration (a new TEXT column), not
a fix to this file.
"""

from __future__ import annotations

import psycopg2.extras

import db
from compliance_checklist import ComplianceStatus, Severity
from fap_pipeline import (
    ComplianceFinding,
    DocumentQuality,
    EligibilityExtraction,
    EligibilityTier,
    FapParseResult,
)


# ============================================================
# Facilities
# ============================================================

def insert_facility(
    name: str,
    npi: str | None = None,
    state: str | None = None,
    address: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    tax_id: str | None = None,
    health_system_id: str | None = None,
) -> str:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facilities
                    (name, npi, state, address, city, zip, tax_id, health_system_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, npi, state, address, city, zip_code, tax_id, health_system_id),
            )
            facility_id = cur.fetchone()[0]
    return str(facility_id)


def find_facilities_by_npi(npi: str) -> list[dict]:
    """
    Returns raw (id, name, npi, state) rows as plain dicts rather than
    bill_pipeline.FacilityRecord -- importing that type here would create
    a circular import, since bill_pipeline.py needs to import this module
    to delegate its fetch_candidate_facilities stub. Callers (i.e.
    bill_pipeline.fetch_candidate_facilities) wrap these into
    FacilityRecord themselves.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, npi, state FROM facilities WHERE npi = %s", (npi,))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def find_facilities_by_state(state: str) -> list[dict]:
    """Same shape/rationale as find_facilities_by_npi -- see its docstring."""
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, npi, state FROM facilities WHERE state = %s", (state,))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Health systems
# ============================================================

def insert_health_system(
    name: str,
    ein: str | None = None,
    is_nonprofit: bool = True,
    fap_url: str | None = None,
    mrf_url: str | None = None,
    plain_language_summary_url: str | None = None,
    billing_collections_policy_url: str | None = None,
) -> str:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO health_systems
                    (name, ein, is_nonprofit, fap_url, mrf_url,
                     plain_language_summary_url,
                     billing_collections_policy_url, last_verified_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING id
                """,
                (name, ein, is_nonprofit, fap_url, mrf_url,
                 plain_language_summary_url, billing_collections_policy_url),
            )
            health_system_id = cur.fetchone()[0]
    return str(health_system_id)


def fetch_all_health_systems() -> list[dict]:
    """
    All health systems as plain (id, name) dicts -- the candidate list
    bill_pipeline.match_health_system name-matches a new facility's
    provider name against. Real-world scale here is in the thousands at
    most (there are roughly 3,000-6,000 hospital systems in the US total,
    nonprofit ones a meaningful fraction of that), so fetching the whole
    table for in-memory matching is reasonable -- no need for a fuzzier
    DB-side search.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM health_systems ORDER BY name")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_fap_urls_for_facility(facility_id: str) -> dict:
    """
    Resolve fap_url / pls_url / billing_policy_url for a facility via its
    linked health_system, if any. Always returns a dict with all three
    keys -- values are None (not a missing key) when the facility has no
    health_system_id, or the health_system has no URLs on file -- so
    callers (worker.py's _handle_parse_fap) can check `if not
    any(urls.values())` uniformly rather than handling "no linkage" as a
    structurally different case from "linked, but no URLs published."
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT hs.fap_url, hs.plain_language_summary_url, hs.billing_collections_policy_url
                FROM facilities f
                JOIN health_systems hs ON hs.id = f.health_system_id
                WHERE f.id = %s
                """,
                (facility_id,),
            )
            row = cur.fetchone()
    if row is None:
        return {"fap_url": None, "pls_url": None, "billing_policy_url": None}
    return {
        "fap_url": row["fap_url"],
        "pls_url": row["plain_language_summary_url"],
        "billing_policy_url": row["billing_collections_policy_url"],
    }


# ============================================================
# Financial Assistance Policies (write side)
# ============================================================

_DOCUMENT_QUALITY_RATIONALE_PLACEHOLDER = "(rationale not persisted -- see repository.py KNOWN GAP)"


def insert_fap_parse_result(facility_id: str, result: FapParseResult) -> str:
    """
    Persist a FapParseResult: the financial_assistance_policies row, plus
    its fap_eligibility_tiers, fap_eligible_services,
    fap_application_requirements, and fap_compliance_findings rows, in
    one transaction.
    """
    eligibility_basis = result.eligibility.eligibility_basis if result.eligibility else None
    parsing_confidence = result.eligibility.parsing_confidence if result.eligibility else None

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO financial_assistance_policies
                    (facility_id, document_quality, eligibility_basis,
                     parsed_at, parsing_confidence, raw_text, source_doc_hash)
                VALUES (%s, %s, %s, now(), %s, %s, %s)
                RETURNING id
                """,
                (
                    facility_id,
                    result.document_quality.label,
                    eligibility_basis,
                    parsing_confidence,
                    result.raw_text,
                    result.source_doc_hash,
                ),
            )
            fap_id = cur.fetchone()[0]

            if result.eligibility:
                for tier in result.eligibility.tiers:
                    cur.execute(
                        """
                        INSERT INTO fap_eligibility_tiers
                            (fap_id, tier_order, fpl_min_pct, fpl_max_pct,
                             discount_type, discount_value,
                             household_size_adjustment, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            fap_id, tier.tier_order, tier.fpl_min_pct, tier.fpl_max_pct,
                            tier.discount_type, tier.discount_value,
                            psycopg2.extras.Json(tier.household_size_adjustment)
                            if tier.household_size_adjustment is not None else None,
                            tier.notes,
                        ),
                    )

                for service in result.eligibility.eligible_services:
                    if "service_category" not in service or "is_covered" not in service:
                        # Both required per EXTRACTION_PROMPT's schema, but
                        # an LLM occasionally omits a required key anyway.
                        # Skip just this entry rather than letting a
                        # KeyError roll back the whole transaction --
                        # tiers, findings, and the FAP row itself are
                        # already staged here, same reasoning as
                        # fap_pipeline.run_compliance_checklist's
                        # equivalent guard (see bug review notes).
                        continue
                    cur.execute(
                        """
                        INSERT INTO fap_eligible_services
                            (fap_id, service_category, is_covered, notes)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (fap_id, service["service_category"], service["is_covered"], service.get("notes")),
                    )

                req = result.eligibility.application_requirements
                if req is not None:
                    required_documents = req.get("required_documents")
                    presumptive_eligibility_criteria = req.get("presumptive_eligibility_criteria")
                    notification_method_required = req.get("notification_method_required")
                    cur.execute(
                        """
                        INSERT INTO fap_application_requirements
                            (fap_id, application_deadline_days, required_documents,
                             presumptive_eligibility_criteria, notification_method_required)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            fap_id, req.get("application_deadline_days"),
                            psycopg2.extras.Json(required_documents) if required_documents is not None else None,
                            psycopg2.extras.Json(presumptive_eligibility_criteria)
                            if presumptive_eligibility_criteria is not None else None,
                            psycopg2.extras.Json(notification_method_required)
                            if notification_method_required is not None else None,
                        ),
                    )

            for finding in result.findings:
                cur.execute(
                    """
                    INSERT INTO fap_compliance_findings
                        (fap_id, requirement_code, status, evidence_text,
                         severity, argument_template)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fap_id, finding.requirement_code, finding.status.value,
                        finding.evidence_text, finding.severity.value, finding.argument_template,
                    ),
                )

    return str(fap_id)


# ============================================================
# Financial Assistance Policies (read side)
# ============================================================

def fetch_fap_for_facility(facility_id: str) -> FapParseResult | None:
    """
    Read the most recently parsed, active FAP for a facility, assembling
    a FapParseResult from financial_assistance_policies +
    fap_eligibility_tiers + fap_compliance_findings.

    Returns None if no FAP has ever been parsed for this facility -- a
    normal, expected state (see case_pipeline.py's module docstring), not
    an error.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, document_quality, eligibility_basis,
                       parsing_confidence, raw_text, source_doc_hash
                FROM financial_assistance_policies
                WHERE facility_id = %s AND is_active
                ORDER BY parsed_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                (facility_id,),
            )
            fap_row = cur.fetchone()
            if fap_row is None:
                return None

            fap_id = fap_row["id"]

            cur.execute(
                """
                SELECT tier_order, fpl_min_pct, fpl_max_pct, discount_type,
                       discount_value, household_size_adjustment, notes
                FROM fap_eligibility_tiers
                WHERE fap_id = %s
                ORDER BY tier_order
                """,
                (fap_id,),
            )
            tier_rows = cur.fetchall()

            cur.execute(
                """
                SELECT service_category, is_covered, notes
                FROM fap_eligible_services
                WHERE fap_id = %s
                ORDER BY service_category
                """,
                (fap_id,),
            )
            service_rows = cur.fetchall()

            cur.execute(
                """
                SELECT application_deadline_days, required_documents,
                       presumptive_eligibility_criteria, notification_method_required
                FROM fap_application_requirements
                WHERE fap_id = %s
                """,
                (fap_id,),
            )
            requirements_row = cur.fetchone()  # at most one row -- UNIQUE(fap_id), see schema.sql

            cur.execute(
                """
                SELECT requirement_code, status, evidence_text, severity, argument_template
                FROM fap_compliance_findings
                WHERE fap_id = %s
                """,
                (fap_id,),
            )
            finding_rows = cur.fetchall()

    eligibility = None
    if fap_row["eligibility_basis"] is not None or tier_rows:
        eligibility = EligibilityExtraction(
            eligibility_basis=fap_row["eligibility_basis"],
            tiers=[
                EligibilityTier(
                    tier_order=t["tier_order"],
                    fpl_min_pct=t["fpl_min_pct"],
                    fpl_max_pct=t["fpl_max_pct"],
                    discount_type=t["discount_type"],
                    discount_value=float(t["discount_value"]) if t["discount_value"] is not None else None,
                    household_size_adjustment=t["household_size_adjustment"],
                    notes=t["notes"],
                )
                for t in tier_rows
            ],
            eligible_services=[
                {"service_category": s["service_category"], "is_covered": s["is_covered"], "notes": s["notes"]}
                for s in service_rows
            ],
            application_requirements=(
                {
                    "application_deadline_days": requirements_row["application_deadline_days"],
                    "required_documents": requirements_row["required_documents"],
                    "presumptive_eligibility_criteria": requirements_row["presumptive_eligibility_criteria"],
                    "notification_method_required": requirements_row["notification_method_required"],
                }
                if requirements_row is not None else None
            ),
            parsing_confidence=fap_row["parsing_confidence"] or "failed",
        )

    findings = [
        ComplianceFinding(
            requirement_code=f["requirement_code"],
            status=ComplianceStatus(f["status"]),
            evidence_text=f["evidence_text"],
            severity=Severity(f["severity"]),
            argument_template=f["argument_template"],
        )
        for f in finding_rows
    ]

    return FapParseResult(
        facility_id=str(facility_id),
        document_quality=DocumentQuality(
            label=fap_row["document_quality"],
            rationale=_DOCUMENT_QUALITY_RATIONALE_PLACEHOLDER,
        ),
        eligibility=eligibility,
        findings=findings,
        raw_text=fap_row["raw_text"],
        source_doc_hash=fap_row["source_doc_hash"],
    )


# ============================================================
# Patients, cases, and bills
# ============================================================

def insert_patient(
    household_income: float | None = None,
    household_size: int | None = None,
    state: str | None = None,
) -> str:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patients (household_income, household_size, state)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (household_income, household_size, state),
            )
            patient_id = cur.fetchone()[0]
    return str(patient_id)


def insert_case(patient_id: str) -> str:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cases (patient_id) VALUES (%s) RETURNING id",
                (patient_id,),
            )
            case_id = cur.fetchone()[0]
    return str(case_id)


def persist_bill(
    case_id: str,
    bill,  # bill_pipeline.BillExtraction -- not imported as a type to avoid a circular import
    facility_id: str | None = None,
    facility_match_status: str | None = None,
    facility_match_confidence: float | None = None,
    storage_key: str = "",
) -> str:
    """
    Write a BillExtraction (and its line items) to bills/bill_line_items.

    `bill` isn't type-hinted as bill_pipeline.BillExtraction to keep this
    module a leaf dependency -- bill_pipeline.py imports repository.py
    (to delegate fetch_candidate_facilities), so repository.py must not
    import anything back from bill_pipeline.py, even just for a type hint.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bills
                    (case_id, storage_key, facility_id, facility_match_status,
                     facility_match_confidence, provider_name_raw, provider_npi_raw,
                     provider_address_raw, account_number, date_of_service,
                     total_billed_amount, parsed_at, parsing_confidence, raw_text)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
                RETURNING id
                """,
                (
                    case_id, storage_key, facility_id, facility_match_status,
                    facility_match_confidence, bill.provider.name, bill.provider.npi,
                    bill.provider.address, bill.account_number, bill.date_of_service,
                    bill.total_billed_amount, bill.parsing_confidence, bill.raw_text,
                ),
            )
            bill_id = cur.fetchone()[0]

            for item in bill.line_items:
                cur.execute(
                    """
                    INSERT INTO bill_line_items
                        (bill_id, line_number, description, procedure_code,
                         code_type, units, billed_amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bill_id, item.line_number, item.description, item.procedure_code,
                        item.code_type, item.units, item.billed_amount,
                    ),
                )

    return str(bill_id)


def persist_case_synthesis(case_id: str, synthesis: dict) -> None:
    """Store the computed synthesis (JSONB) so the analysis can be restored later."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cases SET synthesis_json = %s, updated_at = now() WHERE id = %s",
                (psycopg2.extras.Json(synthesis), case_id),
            )


def fetch_case_synthesis(case_id: str) -> dict | None:
    """Return the stored synthesis dict for a case, or None if not stored."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT synthesis_json FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
    return row[0] if (row and row[0] is not None) else None


def fetch_bill_for_case(case_id: str) -> dict | None:
    """
    Return the most recent bill for a case as a plain dict (provider, totals,
    line items) -- enough to rebuild the analysis view on resume. None if the
    case has no persisted bill.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, provider_name_raw, provider_npi_raw, provider_address_raw,
                       account_number, date_of_service, total_billed_amount, parsing_confidence
                FROM bills WHERE case_id = %s ORDER BY created_at DESC LIMIT 1
                """,
                (case_id,),
            )
            bill = cur.fetchone()
            if bill is None:
                return None
            cur.execute(
                """
                SELECT line_number, description, procedure_code, code_type, units, billed_amount
                FROM bill_line_items WHERE bill_id = %s ORDER BY line_number
                """,
                (bill["id"],),
            )
            items = cur.fetchall()

    def _f(v):
        return float(v) if v is not None else None

    return {
        "provider": {
            "name": bill["provider_name_raw"],
            "npi": bill["provider_npi_raw"],
            "address": bill["provider_address_raw"],
        },
        "account_number": bill["account_number"],
        "date_of_service": bill["date_of_service"].isoformat() if bill["date_of_service"] else None,
        "total_billed_amount": _f(bill["total_billed_amount"]),
        "parsing_confidence": bill["parsing_confidence"],
        "line_items": [
            {
                "line_number": it["line_number"],
                "description": it["description"],
                "procedure_code": it["procedure_code"],
                "code_type": it["code_type"],
                "units": _f(it["units"]),
                "billed_amount": _f(it["billed_amount"]),
            }
            for it in items
        ],
    }


def fetch_health_system_id_for_facility(facility_id: str) -> str | None:
    """Return health_system_id for a facility row, or None."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT health_system_id FROM facilities WHERE id = %s",
                (facility_id,),
            )
            row = cur.fetchone()
    return str(row[0]) if (row and row[0]) else None


def find_bill_id_for_case(case_id: str) -> str | None:
    """Return the most recently created bill_id for a case, or None."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bills WHERE case_id = %s ORDER BY created_at DESC LIMIT 1",
                (case_id,),
            )
            row = cur.fetchone()
    return str(row[0]) if row else None


def fetch_bill_parsing_confidence(case_id: str) -> str | None:
    """
    Return the parsing_confidence of the most recent bill for a case
    ('high' | 'medium' | 'low' | 'failed'), or None if no bill is persisted.

    Used to gate negotiation-letter drafting: a letter built on a low- or
    failed-confidence extraction can carry a wrong provider, amount, or code
    into formal correspondence, which is a credibility (and liability) risk.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parsing_confidence FROM bills WHERE case_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (case_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


# ============================================================
# EOB persistence
# ============================================================

def persist_eob(
    bill_id: str,
    eob,            # eob_pipeline.EobExtraction -- not type-hinted to avoid circular import
    match_result,   # eob_pipeline.EobMatchResult
    storage_key: str = "",
) -> str:
    """
    Persist an EobExtraction and its match results in one transaction:
    - writes the eobs row
    - writes eob_line_items rows (with match_status and bill_line_item_id)
    - writes eob_adjustment_codes rows per line
    - back-fills bill_line_items.allowed_amount and .patient_responsibility
      for every matched/partial-match pair

    The bill_line_items back-fill is the payoff for all this work: those
    columns are what synthesis uses when building the negotiation argument.
    Only matched/partial-match lines are back-filled -- unmatched EOB lines
    don't have a corresponding bill line to update.
    """
    # Build a lookup from eob_line.line_number -> bill_line_item DB id,
    # so we can link them during insertion. We don't have the DB ids yet --
    # we have the bill_line_item Python objects. We'll need to look up each
    # bill_line_item's DB id by (bill_id, line_number).
    matched_by_eob_line_number = {
        m.eob_line.line_number: m for m in match_result.matched
    }

    with db.connection() as conn:
        with conn.cursor() as cur:
            # 1. Insert the eobs row
            cur.execute(
                """
                INSERT INTO eobs
                    (bill_id, storage_key, insurer_name, member_id, claim_number,
                     date_processed, total_billed_amount, total_allowed_amount,
                     total_insurance_paid, total_patient_responsibility,
                     parsed_at, parsing_confidence, raw_text)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
                RETURNING id
                """,
                (
                    bill_id, storage_key,
                    eob.insurer_name, eob.member_id, eob.claim_number,
                    eob.date_processed,
                    eob.total_billed_amount, eob.total_allowed_amount,
                    eob.total_insurance_paid, eob.total_patient_responsibility,
                    eob.parsing_confidence, eob.raw_text,
                ),
            )
            eob_id = cur.fetchone()[0]

            # 2. Look up existing bill_line_item DB ids by line_number
            #    so we can write the FK and back-fill amounts.
            cur.execute(
                "SELECT id, line_number FROM bill_line_items WHERE bill_id = %s",
                (bill_id,),
            )
            bill_line_id_by_number = {row[1]: row[0] for row in cur.fetchall()}

            # 3. Insert eob_line_items + adjustment codes
            for eob_line in eob.line_items:
                match = matched_by_eob_line_number.get(eob_line.line_number)
                bill_line_item_id = None
                match_status = "unmatched"

                if match is not None:
                    # Find the DB id for the matched bill line
                    bill_line_item_id = bill_line_id_by_number.get(
                        match.bill_line.line_number
                    )
                    match_status = match.match_status

                cur.execute(
                    """
                    INSERT INTO eob_line_items
                        (eob_id, bill_line_item_id, line_number, date_of_service,
                         description, procedure_code, code_type, units,
                         billed_amount, allowed_amount, insurance_paid,
                         patient_responsibility, match_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        eob_id, bill_line_item_id, eob_line.line_number,
                        eob_line.date_of_service, eob_line.description,
                        eob_line.procedure_code, eob_line.code_type,
                        eob_line.units, eob_line.billed_amount,
                        eob_line.allowed_amount, eob_line.insurance_paid,
                        eob_line.patient_responsibility, match_status,
                    ),
                )
                eob_line_item_id = cur.fetchone()[0]

                for code in eob_line.adjustment_codes:
                    cur.execute(
                        """
                        INSERT INTO eob_adjustment_codes
                            (eob_line_item_id, code_type, code, amount, description)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (eob_line_item_id, code.code_type, code.code,
                         code.amount, code.description),
                    )

            # 4. Back-fill bill_line_items for matched pairs.
            #    Only write allowed_amount / patient_responsibility -- do NOT
            #    overwrite billed_amount (the provider's number; the EOB's copy
            #    may differ slightly due to rounding or re-billing).
            for match in match_result.matched:
                db_id = bill_line_id_by_number.get(match.bill_line.line_number)
                if db_id is None:
                    continue
                if match.eob_line.allowed_amount is not None or match.eob_line.patient_responsibility is not None:
                    cur.execute(
                        """
                        UPDATE bill_line_items
                        SET allowed_amount = COALESCE(%s, allowed_amount),
                            patient_responsibility = COALESCE(%s, patient_responsibility)
                        WHERE id = %s
                        """,
                        (match.eob_line.allowed_amount,
                         match.eob_line.patient_responsibility,
                         db_id),
                    )

    return str(eob_id)


def fetch_eob_summary_for_bill(bill_id: str) -> dict | None:
    """
    Return the most recently parsed EOB for a bill as a plain dict with
    aggregated totals, or None if no EOB has been ingested yet.

    Returns the most recent EOB rather than all of them -- multiple EOBs
    for the same bill (resubmissions, appeals) are possible but the latest
    is almost always the authoritative one.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, insurer_name, claim_number, date_processed,
                       total_billed_amount, total_allowed_amount,
                       total_insurance_paid, total_patient_responsibility,
                       parsing_confidence
                FROM eobs
                WHERE bill_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (bill_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None



# ============================================================
# MRF findings
# ============================================================

def upsert_mrf_finding(finding) -> str:
    """
    Persist (insert or update) an MrfFindingResult for a facility.
    UNIQUE index on facility_id means a second lookup for the same facility
    overwrites the first -- last-write-wins is correct here; we always want
    the freshest result.

    `finding` is an mrf_pipeline.MrfFindingResult -- not type-hinted to
    avoid a circular import (same pattern as persist_bill/persist_eob).
    """
    # Serialize rates: MrfCodeRate dataclasses -> plain dicts
    rates_json = {}
    for code, rate in (finding.rates or {}).items():
        rates_json[code] = {
            "code_type": rate.code_type,
            "description": rate.description,
            "gross_charge": rate.gross_charge,
            "discounted_cash_price": rate.discounted_cash_price,
            "min_negotiated_charge": rate.min_negotiated_charge,
            "max_negotiated_charge": rate.max_negotiated_charge,
            "payer_rates": rate.payer_rates,
        }

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mrf_findings
                    (facility_id, mrf_url, mrf_status, status_detail,
                     codes_queried, rates, last_checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (facility_id)
                DO UPDATE SET
                    mrf_url = EXCLUDED.mrf_url,
                    mrf_status = EXCLUDED.mrf_status,
                    status_detail = EXCLUDED.status_detail,
                    codes_queried = EXCLUDED.codes_queried,
                    rates = EXCLUDED.rates,
                    last_checked_at = now()
                RETURNING id
                """,
                (
                    finding.facility_id,
                    finding.mrf_url,
                    finding.status,
                    finding.status_detail,
                    psycopg2.extras.Json(finding.codes_queried),
                    psycopg2.extras.Json(rates_json) if rates_json else None,
                ),
            )
            finding_id = cur.fetchone()[0]
    return str(finding_id)


def fetch_mrf_finding_for_facility(facility_id: str) -> dict | None:
    """
    Return the most recent MRF finding for a facility, or None.
    Returns a plain dict with all columns so callers don't need to
    import mrf_pipeline dataclasses.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT mrf_url, mrf_status, status_detail,
                       codes_queried, rates, last_checked_at
                FROM mrf_findings WHERE facility_id = %s
                """,
                (facility_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def fetch_mrf_url_for_health_system(health_system_id: str) -> str | None:
    """Return health_systems.mrf_url for a given health_system_id, or None."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mrf_url FROM health_systems WHERE id = %s",
                (health_system_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None



# ============================================================
# Job queue
# ============================================================

def enqueue_job(job_type: str, payload: dict) -> str:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (job_type, payload, status) VALUES (%s, %s, 'pending') RETURNING id",
                (job_type, psycopg2.extras.Json(payload)),
            )
            job_id = cur.fetchone()[0]
    return str(job_id)


def claim_next_job() -> dict | None:
    """
    Atomically claim the oldest pending job. Uses SELECT ... FOR UPDATE
    SKIP LOCKED inside the UPDATE's WHERE subquery -- the standard safe
    Postgres pattern for letting multiple workers pull from the same
    queue without two of them claiming the same row (a worker that's
    already locked a candidate row is skipped rather than blocking this
    query). Returns None if the queue is empty.

    attempts is incremented HERE, not in mark_job_failed -- by the time a
    job is being marked failed, this is already the count for the
    attempt that just ran.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'in_progress', claimed_at = now(), attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'pending'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, job_type, payload, attempts
                """
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row["id"]), "job_type": row["job_type"],
        "payload": row["payload"], "attempts": row["attempts"],
    }


def mark_job_complete(job_id: str) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'completed', completed_at = now() WHERE id = %s",
                (job_id,),
            )


def mark_job_failed(job_id: str, error_message: str, max_attempts: int = 3) -> None:
    """
    Requeues for retry (status back to 'pending') while attempts is still
    under max_attempts; permanently fails it once that cap is hit.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= %s THEN 'failed'::job_status ELSE 'pending'::job_status END,
                    last_error = %s
                WHERE id = %s
                """,
                (max_attempts, error_message, job_id),
            )
