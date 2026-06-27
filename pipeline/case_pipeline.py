"""
RobinHealth: case intake orchestration.

Every other pipeline file (bill_pipeline, fap_pipeline, pricing_pipeline,
synthesis, letter_pipeline) is correct and tested in isolation, but
nothing wires them together -- synthesis.synthesize() still requires a
hand-built SynthesisInput. This module is that connective layer: given an
uploaded bill, it produces the full SynthesisResult a real case would
need, by composing the pieces built so far.

process_case_intake is pure orchestration (no new LLM/DB logic of its
own) -- it calls bill_pipeline.extract_bill, bill_pipeline.match_facility,
pricing_pipeline.benchmark_bill/aggregate_to_pricing_benchmark, and
synthesis.synthesize, in that order, and assembles their outputs into one
result.

DESIGN DECISION -- FAP lookup is a READ, not a parse:
fap_pipeline.parse_fap is an LLM-driven pipeline meant to run ONCE per
facility, as a background job, not synchronously on every bill upload
from every patient at that facility (too slow and too costly to run
inline). This module instead defines fetch_fap_for_facility, a fast read
of whatever FAP data has already been parsed and persisted for a given
facility_id -- returning None is a normal, expected state (a brand-new
facility, or one still queued), not an error.

DESIGN DECISION -- unmatched facilities don't block synthesis:
pricing benchmarks (Medicare-rate comparison) don't depend on facility
identity at all, so a patient at a not-yet-seen hospital still gets a
useful result today, while that facility is queued for FAP parsing in
the background via create_facility_and_queue_fap_parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import bill_pipeline
import eob_pipeline
import pricing_pipeline
import repository
import synthesis

from bill_pipeline import BillExtraction, ExtractedProviderInfo, MatchResult
from eob_pipeline import EobExtraction, EobMatchResult, match_eob_to_bill
from mrf_pipeline import MrfFindingResult
from fap_pipeline import FapParseResult
from pricing_pipeline import BenchmarkSource, RateTable
from synthesis import PricingBenchmark, SynthesisResult


@dataclass
class CaseIntakeResult:
    bill: BillExtraction
    match: MatchResult
    fap: FapParseResult | None  # None if unmatched, or matched but not yet parsed
    pricing: PricingBenchmark | None
    synthesis: SynthesisResult | None  # None if the bill had no resolvable billed amount
    new_facility_queued_for_fap_parsing: bool
    eob: EobExtraction | None = None          # None if no EOB was uploaded
    eob_match: EobMatchResult | None = None   # None if no EOB or match not run
    mrf_finding: dict | None = None           # None if no MRF lookup has completed yet


# ============================================================
# Helpers (fully implemented)
# ============================================================

def _resolve_billed_amount(bill: BillExtraction) -> float | None:
    """
    Prefer the bill's stated total; fall back to summing line items when
    no total was extracted (e.g. a bill where only itemized charges were
    legible). None if neither is available -- synthesis can't run on an
    unknown billed amount, and treating that as $0 would silently corrupt
    every downstream dollar calculation rather than surfacing the gap.
    """
    if bill.total_billed_amount is not None:
        return bill.total_billed_amount
    if bill.line_items:
        return sum(item.billed_amount for item in bill.line_items)
    return None


# ============================================================
# Facility lookup / creation (real, backed by repository.py)
# ============================================================

def fetch_fap_for_facility(facility_id: str) -> FapParseResult | None:
    """
    Read a facility's most recently parsed FAP data from storage, via
    repository.fetch_fap_for_facility.

    See the module docstring re: why this is a separate, fast read path
    rather than calling fap_pipeline.parse_fap inline. None is a valid
    result -- it means this facility has no parsed FAP on file yet.
    """
    return repository.fetch_fap_for_facility(facility_id)


def create_facility_and_queue_fap_parsing(provider: ExtractedProviderInfo) -> str:
    """
    Insert a new `facilities` row for a provider that matched nothing in
    bill_pipeline.match_facility, and enqueue a 'parse_fap' job for it.
    Returns the new facility_id.

    Both halves are real: the DB insert (repository.insert_facility) and
    the job enqueue (repository.enqueue_job). Before inserting, this also
    attempts to match the provider's name against known health systems
    (bill_pipeline.match_health_system, against
    repository.fetch_all_health_systems()'s candidate list) -- a
    confident match links the new facility via health_system_id, which
    is what lets worker.py's _handle_parse_fap resolve a real fap_url/
    pls_url/billing_policy_url for it instead of having nowhere to look.
    No match (the common case until more health systems are on file) ->
    health_system_id stays None, and that 'parse_fap' job will still
    correctly fail (not silently) for the same reason it always has: see
    _handle_parse_fap's RuntimeError. The queue mechanics themselves
    (claim, retry, eventual permanent failure) are real and tested either
    way -- see test_repository.py.
    """
    health_system_id = bill_pipeline.match_health_system(provider.name, repository.fetch_all_health_systems())
    facility_id = repository.insert_facility(
        name=provider.name, npi=provider.npi, state=provider.state, health_system_id=health_system_id,
    )
    repository.enqueue_job("parse_fap", {"facility_id": facility_id})
    # MRF job is enqueued separately -- the codes come from the bill, which
    # isn't available here. The caller (process_case_intake) enqueues it
    # after extract_bill runs with the actual procedure codes.
    return facility_id


# ============================================================
# Orchestration
# ============================================================

def process_case_intake(
    document_bytes: bytes,
    media_type: str,
    rate_tables: dict[BenchmarkSource, RateTable],
    household_income: float | None = None,
    household_size: int | None = None,
    patient_state: str | None = None,
    locality: str | None = None,
    case_id: str | None = None,
    storage_key: str = "",
    eob_bytes: bytes | None = None,
    eob_media_type: str | None = None,
    eob_storage_key: str = "",
) -> CaseIntakeResult:
    """
    Full intake pipeline: bill upload (+ optional EOB) -> structured case
    data -> SynthesisResult.

    eob_bytes / eob_media_type: if provided, extract_eob() is called on
    the EOB document, the result is matched against the bill's line items,
    and the EOB-derived allowed_amount / patient_responsibility are threaded
    into SynthesisInput. The EOB is also persisted (alongside the bill) if
    case_id is given.

    patient_state, if not given, falls back to the bill's provider state
    -- a reasonable proxy (FPL geographic variation only matters for
    AK/HI) but not necessarily where the patient actually lives. Pass it
    explicitly once that's available from a `patients` record.

    case_id, if given, persists the extracted bill (and its line items)
    via repository.persist_bill -- a `cases` row the caller already
    created (e.g. via repository.insert_case after repository.
    insert_patient). Left as None by default so calling this without a
    database available (as every existing unit test in test_pipeline.py
    does) still works -- persistence is additive, not required to get a
    SynthesisResult back.

    storage_key is passed straight through to persist_bill (default ""
    matches persist_bill's own default). bills.storage_key is meant to
    reference the uploaded file in real object storage, which this
    scaffold doesn't have -- a caller with nowhere real to put the bytes
    can safely leave this as "".
    """
    bill = bill_pipeline.extract_bill(document_bytes, media_type)
    match = bill_pipeline.match_facility(bill.provider)

    fap: FapParseResult | None = None
    new_facility_queued = False

    if match.status == "matched" and match.facility_id:
        fap = fetch_fap_for_facility(match.facility_id)
    elif match.status == "unmatched":
        new_facility_id = create_facility_and_queue_fap_parsing(bill.provider)
        # Reflect the newly created facility in match itself -- both
        # MatchResult.status's type comment and bill_facility_match_status
        # (bills_schema.sql) already define 'new_facility_created' for
        # exactly this case. Leaving match as ('unmatched', facility_id=None)
        # here would silently lose the new facility's id from both the
        # CaseIntakeResult returned to callers and the persist_bill call
        # below.
        match = MatchResult(facility_id=new_facility_id, confidence=match.confidence, status="new_facility_created")
        new_facility_queued = True

    if case_id is not None:
        repository.persist_bill(
            case_id=case_id,
            bill=bill,
            facility_id=match.facility_id,
            facility_match_status=match.status,
            facility_match_confidence=match.confidence,
            storage_key=storage_key,
        )

    # MRF: fetch cached finding first (synchronous DB read, no network),
    # then enqueue a background job to refresh it. Moved before benchmarking
    # so the cached finding can feed into aggregate_to_pricing_benchmark.
    facility_id_for_mrf = match.facility_id
    mrf_finding: dict | None = None
    if facility_id_for_mrf and case_id is not None:
        # Return any already-cached MRF finding synchronously --
        # fast DB read; None if this facility is new (job hasn't run yet)
        mrf_finding = repository.fetch_mrf_finding_for_facility(facility_id_for_mrf)

        codes_for_mrf = [
            item.procedure_code for item in bill.line_items
            if item.procedure_code
        ]
        if codes_for_mrf:
            hs_id = repository.fetch_health_system_id_for_facility(facility_id_for_mrf)
            repository.enqueue_job("fetch_mrf_rates", {
                "facility_id": facility_id_for_mrf,
                "codes": codes_for_mrf,
                "health_system_id": hs_id,
            })

    # On-demand PFS rate fetch: if startup CSV wasn't loaded, fetch rates for
    # just this bill's codes now that we know what they are. No-op when the
    # startup table is already populated (full schedule pre-loaded).
    bill_codes = [
        item.procedure_code for item in bill.line_items if item.procedure_code
    ]
    effective_rate_tables = pricing_pipeline.resolve_rate_tables_on_demand(
        rate_tables,
        codes=bill_codes,
        provider_state=getattr(bill.provider, "state", None) if bill.provider else None,
    )

    benchmarks = pricing_pipeline.benchmark_bill(bill.line_items, effective_rate_tables, locality)
    pricing = pricing_pipeline.aggregate_to_pricing_benchmark(benchmarks, mrf_finding=mrf_finding)

    # EOB ingestion -- optional, runs only if eob_bytes was supplied.
    eob: EobExtraction | None = None
    eob_match: EobMatchResult | None = None
    if eob_bytes is not None:
        eob = eob_pipeline.extract_eob(
            images=[(eob_bytes, eob_media_type or "application/pdf")],
        )
        eob.raw_text = None  # raw bytes not worth storing twice
        eob_match = match_eob_to_bill(eob, bill)

        # Persist the EOB (alongside the bill) if we have a case_id.
        # We need the bill_id for the FK -- fetch it from the bills table.
        if case_id is not None:
            bill_id = repository.find_bill_id_for_case(case_id)
            if bill_id:
                repository.persist_eob(
                    bill_id=bill_id,
                    eob=eob,
                    match_result=eob_match,
                    storage_key=eob_storage_key,
                )

    result: SynthesisResult | None = None
    billed_amount = _resolve_billed_amount(bill)
    if billed_amount is not None:
        eligibility_tiers = fap.eligibility.tiers if (fap and fap.eligibility) else []
        compliance_findings = fap.findings if fap else []
        # Aggregate MRF rates across matched codes for synthesis
        mrf_cash_total: float | None = None
        mrf_min_total: float | None = None
        mrf_max_total: float | None = None
        if mrf_finding and mrf_finding.get("mrf_status") == "rates_found":
            rates = mrf_finding.get("rates") or {}
            cash_vals = [r["discounted_cash_price"] for r in rates.values()
                         if r.get("discounted_cash_price")]
            min_vals = [r["min_negotiated_charge"] for r in rates.values()
                        if r.get("min_negotiated_charge")]
            max_vals = [r["max_negotiated_charge"] for r in rates.values()
                        if r.get("max_negotiated_charge")]
            mrf_cash_total = sum(cash_vals) if cash_vals else None
            mrf_min_total = sum(min_vals) if min_vals else None
            mrf_max_total = sum(max_vals) if max_vals else None

        synthesis_input = synthesis.SynthesisInput(
            billed_amount=billed_amount,
            pricing=pricing,
            eligibility_tiers=eligibility_tiers,
            household_income=household_income,
            household_size=household_size,
            compliance_findings=compliance_findings,
            state=patient_state or bill.provider.state,
            allowed_amount_total=eob_match.total_allowed_amount if eob_match else None,
            patient_responsibility_total=eob_match.total_patient_responsibility if eob_match else None,
            mrf_cash_price_total=mrf_cash_total,
            mrf_min_negotiated_total=mrf_min_total,
            mrf_max_negotiated_total=mrf_max_total,
            mrf_status=mrf_finding.get("mrf_status") if mrf_finding else None,
            mrf_status_detail=mrf_finding.get("status_detail") if mrf_finding else None,
        )
        result = synthesis.synthesize(synthesis_input)

    # Persist the synthesis (as JSONB) so the analysis can be restored later
    # (e.g. when a patient resumes their case). OutcomeType is a str-Enum, so
    # asdict() produces a JSON-serializable dict directly.
    if result is not None and case_id is not None:
        repository.persist_case_synthesis(case_id, asdict(result))

    return CaseIntakeResult(
        bill=bill,
        match=match,
        fap=fap,
        pricing=pricing,
        synthesis=result,
        new_facility_queued_for_fap_parsing=new_facility_queued,
        eob=eob,
        eob_match=eob_match,
        mrf_finding=mrf_finding,
    )
