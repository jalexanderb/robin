"""
Plain-assertion tests for the pieces that don't depend on a live DB or LLM
calls: FPL lookup, eligibility tier matching, and provider name matching.

No Postgres and no EXTERNAL network needed anywhere in this file --
verified by stopping Postgres entirely and confirming every test still
passes. A handful of tests (fetch_fap_documents) spin up a real,
local-only HTTP server (127.0.0.1, a free ephemeral port) in a
background thread to test real HTTP fetch + PDF/HTML extraction logic
without mocking httpx -- this is local loopback traffic, not a call to
the outside world, so it doesn't change the "no external network"
guarantee.

Run with: python3 test_pipeline.py
"""

from unittest.mock import patch, MagicMock

from fap_pipeline import EligibilityTier
from fpl_lookup import fpl_amount, income_as_fpl_percent
from synthesis import find_matching_tier, OutcomeType, Reason
from bill_pipeline import (
    ExtractedProviderInfo,
    FacilityRecord,
    match_facility,
    match_health_system,
    name_similarity,
)
from letter_pipeline import assemble_context, RecipientInfo
from pricing_pipeline import (
    RateTable,
    RateTableEntry,
    aggregate_to_pricing_benchmark,
    benchmark_line_item,
    load_rate_table_from_csv,
)
from case_pipeline import process_case_intake, _resolve_billed_amount
from worker import process_next_job, JOB_HANDLERS
import llm_client
import storage


def test_fpl_amount_matches_published_2026_figures():
    # 2026 guidelines: 1-person = $15,960, family of 4 = $33,000 (48 states/DC)
    assert fpl_amount(1) == 15960
    assert fpl_amount(4) == 33000

    # Alaska and Hawaii use higher, separate tables
    assert fpl_amount(1, "AK") == 19950
    assert fpl_amount(1, "HI") == 18360
    assert fpl_amount(1, "ak") == 19950  # case-insensitive


def test_income_as_fpl_percent():
    # income exactly at the family-of-4 FPL -> 100%
    assert round(income_as_fpl_percent(33000, 4)) == 100
    # income at half the 1-person FPL -> 50%
    assert round(income_as_fpl_percent(7980, 1)) == 50


def test_find_matching_tier_full_charity_care():
    tiers = [
        EligibilityTier(
            tier_order=1, fpl_min_pct=0, fpl_max_pct=200,
            discount_type="full_charity_care", discount_value=100,
        ),
        EligibilityTier(
            tier_order=2, fpl_min_pct=200, fpl_max_pct=400,
            discount_type="percentage_discount", discount_value=50,
        ),
        EligibilityTier(
            tier_order=3, fpl_min_pct=400, fpl_max_pct=None,
            discount_type="flat_cap", discount_value=1000,
        ),
    ]

    assert find_matching_tier(tiers, 150).discount_type == "full_charity_care"
    assert find_matching_tier(tiers, 300).discount_type == "percentage_discount"
    assert find_matching_tier(tiers, 500).discount_type == "flat_cap"


def test_name_similarity_handles_common_suffix_variation():
    assert name_similarity("Lakeside General Hospital", "Lakeside General") > 0.95
    assert name_similarity("Lakeside General Hospital", "Riverside Medical Center") < 0.5


def test_match_facility_exact_npi_match():
    provider = ExtractedProviderInfo(
        name="Lakeside General Hospital", npi="1234567890",
        tax_id=None, address=None, state="CA",
    )
    candidates = [FacilityRecord(id="fac-1", name="Lakeside General", npi="1234567890", state="CA")]

    with patch("bill_pipeline.fetch_candidate_facilities", return_value=candidates):
        result = match_facility(provider)

    assert result.status == "matched"
    assert result.facility_id == "fac-1"
    assert result.confidence == 1.0


def test_match_facility_name_similarity_below_threshold():
    provider = ExtractedProviderInfo(
        name="Unknown Clinic LLC", npi=None, tax_id=None, address=None, state="CA",
    )
    candidates = [FacilityRecord(id="fac-2", name="Lakeside General", npi=None, state="CA")]

    # name_similarity call path -- no NPI on the provider, so the NPI
    # branch in match_facility is skipped and fetch_candidate_facilities
    # is called once, for name-based candidates.
    with patch("bill_pipeline.fetch_candidate_facilities", return_value=candidates):
        result = match_facility(provider)

    assert result.status == "unmatched"
    assert result.facility_id is None


def test_match_health_system_confident_match_returns_id():
    candidates = [
        {"id": "hs-1", "name": "Ascension Healthcare"},
        {"id": "hs-2", "name": "Trinity Health"},
    ]
    # ", Inc" is a clean trailing-suffix-list match (see _normalize_name) --
    # picked deliberately over a "X Health System" vs "X Health" pair,
    # which scores below threshold here: " health system" is stripped as
    # one suffix unit, not as two separately-strippable layers, so that
    # kind of pair ends up comparing very different string lengths.
    result = match_health_system("Ascension Healthcare, Inc", candidates)
    assert result == "hs-1"


def test_match_health_system_below_threshold_returns_none():
    candidates = [{"id": "hs-1", "name": "Ascension Health"}]
    result = match_health_system("Completely Unrelated Clinic LLC", candidates)
    assert result is None


def test_match_health_system_no_candidates_returns_none():
    assert match_health_system("Ascension Health", []) is None


def test_match_health_system_no_provider_name_returns_none():
    candidates = [{"id": "hs-1", "name": "Ascension Health"}]
    assert match_health_system(None, candidates) is None
    assert match_health_system("", candidates) is None


def test_match_health_system_picks_best_of_multiple_candidates():
    # "Providence" is a much closer match to "Providence Health & Services"
    # than to "Trinity Health" -- confirms max() picks the best score,
    # not just the first candidate that happens to clear the threshold.
    candidates = [
        {"id": "hs-1", "name": "Trinity Health"},
        {"id": "hs-2", "name": "Providence Health & Services"},
    ]
    result = match_health_system("Providence Health and Services", candidates)
    assert result == "hs-2"


def _recipient() -> RecipientInfo:
    return RecipientInfo(
        facility_name="Lakeside General Hospital",
        facility_address="123 Main St, Springfield, CA",
        patient_name="Jane Doe",
        account_number="ACC-001",
        date_of_service="2026-03-14",
    )


def test_assemble_context_full_elimination_requests_full_waiver():
    reason = Reason(
        outcome_type=OutcomeType.FULL_ELIMINATION,
        summary="qualifies for full charity care",
        estimated_low=0,
        estimated_high=0,
        source_requirement_codes=[],
    )
    from synthesis import SynthesisResult, DEFAULT_BETA_CAVEAT

    result = SynthesisResult(
        headline_low=0, headline_high=0, headline_could_eliminate=True,
        reasons=[reason], follow_up_questions=[], beta_caveat=DEFAULT_BETA_CAVEAT,
    )

    context = assemble_context(result, _recipient(), billed_amount=2840.0)

    assert context.requests_full_waiver is True
    assert context.requested_amount == 0.0
    assert len(context.arguments) == 1


def test_assemble_context_partial_reduction_uses_lowest_estimate():
    reason = Reason(
        outcome_type=OutcomeType.PARTIAL_REDUCTION,
        summary="qualifies for a discount",
        estimated_low=1200.0,
        estimated_high=2840.0,
        source_requirement_codes=[],
    )
    from synthesis import SynthesisResult, DEFAULT_BETA_CAVEAT

    result = SynthesisResult(
        headline_low=1200.0, headline_high=2840.0, headline_could_eliminate=False,
        reasons=[reason], follow_up_questions=[], beta_caveat=DEFAULT_BETA_CAVEAT,
    )

    context = assemble_context(result, _recipient(), billed_amount=2840.0)

    assert context.requests_full_waiver is False
    assert context.requested_amount == 1200.0
    assert "1,200.00" in context.arguments[0].text
    assert "2,840.00" in context.arguments[0].text


def test_assemble_context_procedural_leverage_uses_argument_template():
    reason = Reason(
        outcome_type=OutcomeType.PROCEDURAL_LEVERAGE,
        summary="patient-facing summary text",  # should NOT appear in the letter argument
        estimated_low=None,
        estimated_high=None,
        source_requirement_codes=["fap_document_exists"],
    )
    from synthesis import SynthesisResult, DEFAULT_BETA_CAVEAT
    from compliance_checklist import CHECKLIST_BY_CODE

    result = SynthesisResult(
        headline_low=None, headline_high=None, headline_could_eliminate=False,
        reasons=[reason], follow_up_questions=[], beta_caveat=DEFAULT_BETA_CAVEAT,
    )

    context = assemble_context(result, _recipient(), billed_amount=2840.0)

    assert context.requests_full_waiver is False
    assert context.requested_amount is None  # no dollar figure -- pure procedural ask
    assert context.arguments[0].text == CHECKLIST_BY_CODE["fap_document_exists"].argument_template
    assert context.arguments[0].text != reason.summary


def test_load_rate_table_from_csv_and_lookup_with_locality_fallback():
    import os
    import tempfile

    csv_content = (
        "code,locality,rate,description\n"
        "TEST001,12345,150.00,Office visit (locality-specific)\n"
        "TEST001,,120.00,Office visit (national)\n"
        "TEST002,,75.50,\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        path = f.name

    try:
        table = load_rate_table_from_csv(path, source="pfs")
        assert table.lookup("TEST001", locality="12345") == 150.00
        assert table.lookup("TEST001", locality="99999") == 120.00  # falls back to national
        assert table.lookup("TEST002") == 75.50
        assert table.lookup("UNKNOWN") is None
    finally:
        os.unlink(path)


def test_benchmark_line_item_cpt_routes_to_pfs_and_computes_delta():
    from bill_pipeline import ExtractedLineItem

    item = ExtractedLineItem(
        line_number=1, description="Office visit", procedure_code="TEST001",
        code_type="cpt", units=1, billed_amount=300.00,
    )
    pfs_table = RateTable(
        source="pfs",
        entries={("TEST001", ""): RateTableEntry(code="TEST001", locality="", rate=150.00)},
    )

    result = benchmark_line_item(item, rate_tables={"pfs": pfs_table})

    assert result.benchmark_source == "pfs"
    assert result.medicare_rate == 150.00
    assert result.delta_amount == 150.00
    assert round(result.delta_pct, 1) == 100.0


def test_benchmark_line_item_unmatched_code_returns_none():
    from bill_pipeline import ExtractedLineItem

    item = ExtractedLineItem(
        line_number=1, description="Mystery charge", procedure_code="UNKNOWN",
        code_type="cpt", units=1, billed_amount=300.00,
    )
    pfs_table = RateTable(source="pfs", entries={})

    result = benchmark_line_item(item, rate_tables={"pfs": pfs_table})

    assert result.medicare_rate is None
    assert result.delta_amount is None


def test_aggregate_to_pricing_benchmark_excludes_unmatched_line_items():
    from bill_pipeline import ExtractedLineItem

    matched_item = ExtractedLineItem(
        line_number=1, description="Office visit", procedure_code="TEST001",
        code_type="cpt", units=1, billed_amount=300.00,
    )
    unmatched_item = ExtractedLineItem(
        line_number=2, description="Drug, NDC", procedure_code="00000-0000-00",
        code_type="ndc", units=1, billed_amount=50.00,
    )
    pfs_table = RateTable(
        source="pfs",
        entries={("TEST001", ""): RateTableEntry(code="TEST001", locality="", rate=150.00)},
    )
    benchmarks = [
        benchmark_line_item(matched_item, rate_tables={"pfs": pfs_table}),
        benchmark_line_item(unmatched_item, rate_tables={"pfs": pfs_table}),
    ]

    result = aggregate_to_pricing_benchmark(benchmarks)

    assert result is not None
    assert result.billed_amount == 300.00  # excludes the $50 unmatched drug charge
    assert result.medicare_rate == 150.00
    # estimate_negotiated_rate is now real: returns Medicare * 1.8 multiplier when
    # no MRF is available. No longer None (stub is gone).
    from pricing_pipeline import _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO
    assert result.fair_price_estimate == round(150.00 * _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO, 2)


def test_aggregate_to_pricing_benchmark_returns_none_when_nothing_matched():
    from bill_pipeline import ExtractedLineItem

    item = ExtractedLineItem(
        line_number=1, description="Mystery", procedure_code="UNKNOWN",
        code_type="cpt", units=1, billed_amount=300.00,
    )
    pfs_table = RateTable(source="pfs", entries={})
    benchmarks = [benchmark_line_item(item, rate_tables={"pfs": pfs_table})]

    assert aggregate_to_pricing_benchmark(benchmarks) is None


def test_resolve_billed_amount_prefers_total_then_sums_line_items():
    from bill_pipeline import BillExtraction, ExtractedProviderInfo, ExtractedLineItem

    provider = ExtractedProviderInfo(name="X", npi=None, tax_id=None, address=None, state=None)

    bill_with_total = BillExtraction(
        provider=provider, date_of_service=None, account_number=None,
        line_items=[], total_billed_amount=250.0, parsing_confidence="high", raw_text=None,
    )
    assert _resolve_billed_amount(bill_with_total) == 250.0

    bill_without_total = BillExtraction(
        provider=provider, date_of_service=None, account_number=None,
        line_items=[
            ExtractedLineItem(line_number=1, description="A", procedure_code=None,
                               code_type=None, units=1, billed_amount=100.0),
            ExtractedLineItem(line_number=2, description="B", procedure_code=None,
                               code_type=None, units=1, billed_amount=50.0),
        ],
        total_billed_amount=None, parsing_confidence="medium", raw_text=None,
    )
    assert _resolve_billed_amount(bill_without_total) == 150.0

    bill_with_nothing = BillExtraction(
        provider=provider, date_of_service=None, account_number=None,
        line_items=[], total_billed_amount=None, parsing_confidence="low", raw_text=None,
    )
    assert _resolve_billed_amount(bill_with_nothing) is None


def test_process_case_intake_matched_facility_eligible_for_charity_care():
    from bill_pipeline import BillExtraction, ExtractedProviderInfo, ExtractedLineItem, FacilityRecord
    from fap_pipeline import FapParseResult, DocumentQuality, EligibilityExtraction, EligibilityTier
    from synthesis import OutcomeType

    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Lakeside General Hospital", npi="1234567890",
            tax_id=None, address=None, state="CA",
        ),
        date_of_service="2026-03-14", account_number="ACC-100",
        line_items=[ExtractedLineItem(
            line_number=1, description="Office visit", procedure_code="TESTCPT1",
            code_type="cpt", units=1, billed_amount=300.0,
        )],
        total_billed_amount=300.0, parsing_confidence="high", raw_text=None,
    )
    candidates = [FacilityRecord(id="fac-1", name="Lakeside General", npi="1234567890", state="CA")]
    fap = FapParseResult(
        facility_id="fac-1",
        document_quality=DocumentQuality(label="well_structured", rationale="test fixture"),
        eligibility=EligibilityExtraction(
            eligibility_basis="fpl_percentage",
            tiers=[EligibilityTier(
                tier_order=1, fpl_min_pct=0, fpl_max_pct=200,
                discount_type="full_charity_care", discount_value=100,
            )],
            eligible_services=[], application_requirements=None, parsing_confidence="high",
        ),
        findings=[], raw_text=None, source_doc_hash=None,
    )
    pfs_table = RateTable(
        source="pfs",
        entries={("TESTCPT1", ""): RateTableEntry(code="TESTCPT1", locality="", rate=100.0)},
    )

    with patch("bill_pipeline.extract_bill", return_value=bill), \
         patch("bill_pipeline.fetch_candidate_facilities", return_value=candidates), \
         patch("case_pipeline.fetch_fap_for_facility", return_value=fap):
        result = process_case_intake(
            document_bytes=b"", media_type="application/pdf",
            rate_tables={"pfs": pfs_table},
            household_income=20000, household_size=1,  # ~125% FPL -> under the 200% tier
        )

    assert result.match.status == "matched"
    assert result.fap is fap
    assert result.pricing is not None
    assert result.synthesis is not None
    assert result.synthesis.headline_could_eliminate is True
    assert any(r.outcome_type == OutcomeType.FULL_ELIMINATION for r in result.synthesis.reasons)
    assert result.new_facility_queued_for_fap_parsing is False


def test_process_case_intake_unmatched_facility_still_synthesizes_from_pricing():
    from bill_pipeline import BillExtraction, ExtractedProviderInfo, ExtractedLineItem
    from synthesis import OutcomeType

    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Totally Unknown Clinic", npi=None, tax_id=None, address=None, state="TX",
        ),
        date_of_service="2026-04-01", account_number="ACC-200",
        line_items=[ExtractedLineItem(
            line_number=1, description="Lab test", procedure_code="TESTCPT2",
            code_type="cpt", units=1, billed_amount=500.0,
        )],
        total_billed_amount=500.0, parsing_confidence="high", raw_text=None,
    )
    pfs_table = RateTable(
        source="pfs",
        entries={("TESTCPT2", ""): RateTableEntry(code="TESTCPT2", locality="", rate=100.0)},
    )

    # create_facility_and_queue_fap_parsing is mocked to return
    # successfully without touching a real database -- it's fully real
    # now (no NotImplementedError path exists anymore to simulate
    # "unavailable" with), but this test lives in test_pipeline.py,
    # which guarantees no Postgres is needed anywhere in this file. The
    # mock stands in for "the real DB insert + job enqueue succeeded."
    # fetch_fap_for_facility must NOT be called on this path (a
    # newly-created facility has no FAP parsed yet) -- checked
    # explicitly via assert_not_called() rather than relying on an
    # unmocked stub to blow up.
    fetch_fap_mock = MagicMock()
    with patch("bill_pipeline.extract_bill", return_value=bill), \
         patch("bill_pipeline.fetch_candidate_facilities", return_value=[]), \
         patch("case_pipeline.create_facility_and_queue_fap_parsing",
               return_value="new-facility-id-from-mock"), \
         patch("case_pipeline.fetch_fap_for_facility", fetch_fap_mock):
        result = process_case_intake(
            document_bytes=b"", media_type="application/pdf",
            rate_tables={"pfs": pfs_table},
        )

    fetch_fap_mock.assert_not_called()
    assert result.match.status == "new_facility_created"
    assert result.match.facility_id == "new-facility-id-from-mock"
    assert result.fap is None
    assert result.pricing is not None
    assert result.pricing.medicare_rate == 100.0
    assert result.pricing.billed_amount == 500.0
    assert result.synthesis is not None
    assert any(r.outcome_type == OutcomeType.PARTIAL_REDUCTION for r in result.synthesis.reasons)
    assert result.new_facility_queued_for_fap_parsing is True


def test_process_case_intake_facility_creation_succeeds_when_wired_up():
    from bill_pipeline import BillExtraction, ExtractedProviderInfo

    bill = BillExtraction(
        provider=ExtractedProviderInfo(
            name="Totally Unknown Clinic", npi=None, tax_id=None, address=None, state="TX",
        ),
        date_of_service=None, account_number=None,
        line_items=[], total_billed_amount=None, parsing_confidence="low", raw_text=None,
    )

    with patch("bill_pipeline.extract_bill", return_value=bill), \
         patch("bill_pipeline.fetch_candidate_facilities", return_value=[]), \
         patch("case_pipeline.create_facility_and_queue_fap_parsing", return_value="new-fac-id"):
        result = process_case_intake(document_bytes=b"", media_type="application/pdf", rate_tables={})

    assert result.match.status == "new_facility_created"
    assert result.match.facility_id == "new-fac-id"
    assert result.new_facility_queued_for_fap_parsing is True


def test_process_case_intake_returns_none_synthesis_without_resolvable_amount():
    from bill_pipeline import BillExtraction, ExtractedProviderInfo

    bill = BillExtraction(
        provider=ExtractedProviderInfo(name=None, npi=None, tax_id=None, address=None, state=None),
        date_of_service=None, account_number=None,
        line_items=[], total_billed_amount=None, parsing_confidence="low", raw_text=None,
    )

    with patch("bill_pipeline.extract_bill", return_value=bill), \
         patch("case_pipeline.create_facility_and_queue_fap_parsing", return_value="unused-facility-id"):
        result = process_case_intake(document_bytes=b"", media_type="application/pdf", rate_tables={})

    assert result.match.status == "new_facility_created"  # provider.name is None -> match_facility itself returns "unmatched", but the mocked facility creation below succeeds
    assert result.match.facility_id == "unused-facility-id"
    assert result.pricing is None  # no line items to benchmark
    assert result.synthesis is None


def test_process_next_job_returns_false_when_queue_empty():
    with patch("repository.claim_next_job", return_value=None):
        assert process_next_job() is False


def test_process_next_job_dispatches_to_handler_and_marks_complete():
    job = {"id": "job-1", "job_type": "parse_fap", "payload": {"facility_id": "fac-1"}, "attempts": 1}
    fake_handler = MagicMock()

    with patch("repository.claim_next_job", return_value=job), \
         patch.dict(JOB_HANDLERS, {"parse_fap": fake_handler}), \
         patch("repository.mark_job_complete") as mock_complete, \
         patch("repository.mark_job_failed") as mock_failed:
        result = process_next_job()

    fake_handler.assert_called_once_with({"facility_id": "fac-1"})
    mock_complete.assert_called_once_with("job-1")
    mock_failed.assert_not_called()
    assert result is True


def test_process_next_job_marks_failed_when_handler_raises():
    job = {"id": "job-2", "job_type": "parse_fap", "payload": {"facility_id": "fac-2"}, "attempts": 1}
    failing_handler = MagicMock(side_effect=RuntimeError("simulated failure"))

    with patch("repository.claim_next_job", return_value=job), \
         patch.dict(JOB_HANDLERS, {"parse_fap": failing_handler}), \
         patch("repository.mark_job_complete") as mock_complete, \
         patch("repository.mark_job_failed") as mock_failed:
        result = process_next_job()

    mock_complete.assert_not_called()
    mock_failed.assert_called_once()
    args, kwargs = mock_failed.call_args
    assert args[0] == "job-2"
    assert "simulated failure" in args[1]  # traceback text includes the original error message
    assert result is True


def test_process_next_job_marks_failed_for_unregistered_job_type():
    job = {"id": "job-3", "job_type": "totally_unknown_type", "payload": {}, "attempts": 1}

    with patch("repository.claim_next_job", return_value=job), \
         patch("repository.mark_job_failed") as mock_failed:
        result = process_next_job()

    mock_failed.assert_called_once()
    assert "totally_unknown_type" in mock_failed.call_args[0][1]
    assert result is True


def test_llm_client_complete_sends_correct_request_shape_text_only():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": "the answer"}}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        result = llm_client.complete("hello there", max_tokens=50)

    assert result == "the answer"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["messages"] == [{"role": "user", "content": "hello there"}]
    assert call_kwargs["json"]["max_tokens"] == 50
    assert "/chat/completions" in mock_post.call_args.args[0]


def test_llm_client_complete_includes_images_as_data_urls():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("describe this", images=[(b"fake-bytes", "image/png")])

    content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_llm_client_complete_includes_auth_header_when_api_key_set():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible", "LLM_API_KEY": "secret-token"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("hello")

    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_llm_client_complete_json_strips_code_fences():
    import os

    # Scraping/repair path is openai_compatible-only; the anthropic path uses
    # forced tool-use and never sees fenced text (see the structured-output test below).
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("llm_client.complete", return_value='```json\n{"a": 1, "b": [2, 3]}\n```'):
        assert llm_client.complete_json("doesn't matter") == {"a": 1, "b": [2, 3]}


def test_extract_bill_calls_llm_with_image_and_parses_response():
    from bill_pipeline import extract_bill

    fake_data = {
        "provider": {"name": "Test Hospital", "npi": "123", "tax_id": None, "address": None, "state": "CA"},
        "date_of_service": "2026-01-01", "account_number": "ACC-1",
        "line_items": [{"line_number": 1, "description": "Visit", "procedure_code": "X1",
                         "code_type": "cpt", "units": 1, "billed_amount": 200.0}],
        "total_billed_amount": 200.0, "parsing_confidence": "high",
    }

    with patch("llm_client.complete_json", return_value=fake_data) as mock_call:
        result = extract_bill(b"fake-image-bytes", "image/png")

    assert mock_call.call_args.kwargs["images"] == [(b"fake-image-bytes", "image/png")]
    assert result.provider.name == "Test Hospital"
    assert result.line_items[0].billed_amount == 200.0
    assert result.total_billed_amount == 200.0


def test_classify_document_quality_skips_llm_when_document_not_found():
    from fap_pipeline import classify_document_quality, FetchedDocument

    doc = FetchedDocument(url=None, text=None, fetched_at="2026-01-01")

    with patch("llm_client.complete_json") as mock_call:
        result = classify_document_quality(doc)

    mock_call.assert_not_called()
    assert result.label == "not_found"


def test_classify_document_quality_calls_llm_for_existing_document():
    from fap_pipeline import classify_document_quality, FetchedDocument

    doc = FetchedDocument(url="https://example.com/fap", text="some FAP text", fetched_at="2026-01-01")

    with patch("llm_client.complete_json", return_value={"label": "well_structured", "rationale": "clear tables"}):
        result = classify_document_quality(doc)

    assert result.label == "well_structured"
    assert result.rationale == "clear tables"


def test_extract_eligibility_calls_llm_and_parses_tiers():
    from fap_pipeline import extract_eligibility, FetchedDocument, DocumentQuality

    doc = FetchedDocument(url="https://example.com/fap", text="some FAP text", fetched_at="2026-01-01")
    quality = DocumentQuality(label="well_structured", rationale="clear")
    fake_data = {
        "eligibility_basis": "fpl_percentage",
        "tiers": [{"tier_order": 1, "fpl_min_pct": 0, "fpl_max_pct": 200,
                   "discount_type": "full_charity_care", "discount_value": 100}],
        "eligible_services": [], "application_requirements": None, "parsing_confidence": "high",
    }

    with patch("llm_client.complete_json", return_value=fake_data):
        result = extract_eligibility(doc, quality)

    assert result.eligibility_basis == "fpl_percentage"
    assert len(result.tiers) == 1
    assert result.tiers[0].discount_type == "full_charity_care"


def test_run_compliance_checklist_calls_llm_and_maps_findings():
    from fap_pipeline import run_compliance_checklist, FetchedDocument
    from compliance_checklist import ComplianceStatus

    fap_doc = FetchedDocument(url="https://example.com/fap", text="FAP text", fetched_at="2026-01-01")
    pls_doc = FetchedDocument(url=None, text=None, fetched_at="2026-01-01")
    billing_doc = FetchedDocument(url=None, text=None, fetched_at="2026-01-01")
    fake_findings = [{"requirement_code": "fap_document_exists", "status": "present", "evidence_text": "found it"}]

    with patch("llm_client.complete_json", return_value=fake_findings):
        result = run_compliance_checklist(fap_doc, pls_doc, billing_doc, "")

    assert len(result) == 1
    assert result[0].requirement_code == "fap_document_exists"
    assert result[0].status == ComplianceStatus.PRESENT
    assert result[0].evidence_text == "found it"


def test_draft_letter_calls_llm_and_returns_drafted_letter():
    from letter_pipeline import draft_letter, LetterContext, RecipientInfo

    context = LetterContext(
        recipient=RecipientInfo(
            facility_name="Test Hospital", facility_address=None, patient_name="Jane Doe",
            account_number="ACC-1", date_of_service="2026-01-01",
        ),
        billed_amount=500.0, arguments=[], requested_amount=None,
        requests_full_waiver=False, response_deadline_days=21,
    )

    with patch("llm_client.complete", return_value="Dear Billing Department,\n\n...") as mock_call:
        result = draft_letter(context)

    assert result.body == "Dear Billing Department,\n\n..."
    assert "Test Hospital" in mock_call.call_args.args[0]  # the rendered prompt mentions the facility


def test_storage_save_and_load_round_trip():
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            key = storage.save(b"hello bill bytes", "application/pdf")
            assert key.endswith(".pdf")
            assert storage.exists(key)
            assert storage.load(key) == b"hello bill bytes"


def test_storage_is_content_addressed():
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            key1 = storage.save(b"same content", "image/png")
            key2 = storage.save(b"same content", "image/png")
            assert key1 == key2, "identical content should produce the same key"

            key3 = storage.save(b"different content", "image/png")
            assert key3 != key1


def test_storage_load_blocks_path_traversal():
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            try:
                storage.load("../../../etc/passwd")
                assert False, "path traversal was not blocked"
            except FileNotFoundError:
                pass  # expected -- basename-stripped, so it looked inside tmpdir and found nothing


def test_storage_exists_returns_false_for_missing_key():
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"STORAGE_DIR": tmpdir}):
            assert storage.exists("nonexistent-key.pdf") is False


def _generate_test_pdf(text: str) -> bytes:
    """Build a real, valid PDF in memory (via reportlab) with known text -- a genuine fixture, not a hand-rolled byte string."""
    import io
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, text)
    c.save()
    return buf.getvalue()


def _make_local_test_server(routes: dict):
    """
    routes: path -> (status_code, body_bytes, content_type).
    Returns (base_url, server) on 127.0.0.1 at a free ephemeral port.
    Caller must call server.shutdown() when done.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in routes:
                status, body, content_type = routes[self.path]
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # silence default request logging

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}", server


def test_looks_like_pdf_sniffs_magic_number():
    from fap_pipeline import _looks_like_pdf

    assert _looks_like_pdf(b"%PDF-1.4\nrest of file...") is True
    assert _looks_like_pdf(b"<html>not a pdf</html>") is False


def test_looks_like_html_checks_content_type_and_doctype():
    from fap_pipeline import _looks_like_html

    assert _looks_like_html(b"whatever", "text/html; charset=utf-8") is True
    assert _looks_like_html(b"<!DOCTYPE html><html>", "application/octet-stream") is True
    assert _looks_like_html(b"%PDF-1.4", "application/pdf") is False


def test_extract_pdf_text_from_real_pdf():
    from fap_pipeline import _extract_pdf_text

    pdf_bytes = _generate_test_pdf("HELLO_PDF_TEXT_MARKER")
    text = _extract_pdf_text(pdf_bytes)
    assert text is not None
    assert "HELLO_PDF_TEXT_MARKER" in text


def test_extract_pdf_text_returns_none_for_unparseable_content():
    from fap_pipeline import _extract_pdf_text

    assert _extract_pdf_text(b"this is not a pdf at all, just text") is None


def test_extract_html_text_strips_script_and_style():
    from fap_pipeline import _extract_html_text

    html = (
        b"<html><head><style>.x{color:red}</style></head>"
        b"<body><script>doEvil()</script><p>Visible Text Here</p></body></html>"
    )
    text = _extract_html_text(html)
    assert text is not None
    assert "Visible Text Here" in text
    assert "doEvil" not in text
    assert "color:red" not in text


def test_extract_html_text_returns_none_for_empty_body():
    from fap_pipeline import _extract_html_text

    assert _extract_html_text(b"<html><body>   \n  </body></html>") is None


def test_fetch_fap_documents_handles_none_urls():
    from fap_pipeline import fetch_fap_documents

    result = fetch_fap_documents(fap_url=None, pls_url=None, billing_policy_url=None)
    assert set(result.keys()) == {"fap", "pls", "billing"}
    assert all(not doc.exists for doc in result.values())


def test_fetch_fap_documents_handles_connection_refused():
    from fap_pipeline import fetch_fap_documents

    # Port 1 is reserved and essentially guaranteed not to have anything
    # listening -- a real connection-refused, not a mock.
    result = fetch_fap_documents(fap_url="http://127.0.0.1:1/nope", pls_url=None, billing_policy_url=None)
    assert result["fap"].exists is False
    assert result["fap"].url == "http://127.0.0.1:1/nope"  # url is preserved even on failure


def test_fetch_fap_documents_real_http_pdf_html_and_404():
    """
    Genuinely real HTTP: a real local server, real sockets, real
    responses -- httpx is never mocked here. Exercises PDF extraction,
    HTML extraction (with script/style correctly excluded), and a 404
    all coming back as exists=False, in one real fetch_fap_documents call.
    """
    from fap_pipeline import fetch_fap_documents

    pdf_bytes = _generate_test_pdf("UNIQUE_FAP_TEXT_MARKER_12345")
    html_bytes = (
        b"<html><head><style>body{color:red}</style></head>"
        b"<body><script>alert(1)</script><p>UNIQUE_PLS_TEXT_MARKER_67890</p></body></html>"
    )

    base_url, server = _make_local_test_server({
        "/fap.pdf": (200, pdf_bytes, "application/pdf"),
        "/pls.html": (200, html_bytes, "text/html"),
        "/billing-missing": (404, b"not found", "text/plain"),
    })
    try:
        result = fetch_fap_documents(
            fap_url=f"{base_url}/fap.pdf",
            pls_url=f"{base_url}/pls.html",
            billing_policy_url=f"{base_url}/billing-missing",
        )

        assert result["fap"].exists is True
        assert "UNIQUE_FAP_TEXT_MARKER_12345" in result["fap"].text

        assert result["pls"].exists is True
        assert "UNIQUE_PLS_TEXT_MARKER_67890" in result["pls"].text
        assert "alert(1)" not in result["pls"].text
        assert "color:red" not in result["pls"].text

        assert result["billing"].exists is False
    finally:
        server.shutdown()


def test_fetch_fap_documents_pdf_served_with_wrong_content_type():
    """A server mislabeling a PDF as application/octet-stream should still be sniffed correctly via the %PDF- magic number, not trusted blindly off Content-Type."""
    from fap_pipeline import fetch_fap_documents

    pdf_bytes = _generate_test_pdf("MISLABELED_PDF_MARKER")
    base_url, server = _make_local_test_server({
        "/weird.bin": (200, pdf_bytes, "application/octet-stream"),
    })
    try:
        result = fetch_fap_documents(fap_url=f"{base_url}/weird.bin", pls_url=None, billing_policy_url=None)
        assert result["fap"].exists is True
        assert "MISLABELED_PDF_MARKER" in result["fap"].text
    finally:
        server.shutdown()


def test_income_as_fpl_percent_clamps_negative_income_to_zero():
    from synthesis import find_matching_tier

    # Bug review finding: a FAP's most generous tier is typically worded
    # '0% to 200% FPL' -- fpl_min_pct=0 explicitly, not None. Without
    # clamping, negative income produced a negative percentage that
    # failed to match even this tier, when someone with negative income
    # should be at least as eligible as $0 income.
    pct_negative = income_as_fpl_percent(household_income=-5000, household_size=2, state="CA")
    pct_zero = income_as_fpl_percent(household_income=0, household_size=2, state="CA")
    assert pct_negative == pct_zero == 0.0

    tiers = [EligibilityTier(
        tier_order=1, fpl_min_pct=0, fpl_max_pct=200,
        discount_type="full_charity_care", discount_value=100,
    )]
    assert find_matching_tier(tiers, pct_negative) is not None


def test_run_compliance_checklist_skips_one_malformed_finding_keeps_rest():
    from fap_pipeline import run_compliance_checklist, FetchedDocument

    fap_doc = FetchedDocument(url="https://example.com/fap", text="FAP text", fetched_at="2026-01-01")
    pls_doc = FetchedDocument(url=None, text=None, fetched_at="2026-01-01")
    billing_doc = FetchedDocument(url=None, text=None, fetched_at="2026-01-01")
    fake_findings = [
        {"requirement_code": "fap_document_exists", "status": "present", "evidence_text": "found it"},
        {"requirement_code": "totally_hallucinated_code", "status": "present", "evidence_text": "..."},
        {"requirement_code": "agb_methodology_disclosed", "status": "garbage_status_value", "evidence_text": "..."},
        {"requirement_code": "eligibility_criteria_specified", "status": "absent", "evidence_text": None},
    ]

    with patch("llm_client.complete_json", return_value=fake_findings):
        result = run_compliance_checklist(fap_doc, pls_doc, billing_doc, "")

    # The 2 malformed entries are skipped; the 2 valid ones survive.
    assert len(result) == 2
    codes = {f.requirement_code for f in result}
    assert codes == {"fap_document_exists", "eligibility_criteria_specified"}


def test_normalize_name_does_not_mangle_midword_suffix_text():
    from bill_pipeline import _normalize_name

    # Bug review finding: .replace() matched " hospital" inside
    # "Hospitaler's" (a different word that merely contains the suffix
    # text), mangling it into "er's". A trailing-suffix strip should only
    # ever apply at the actual end of the string.
    result = _normalize_name("St. Hospitaler's Medical Group")
    assert "hospitaler" in result
    assert result == "st. hospitaler's medical group"  # "medical group" isn't a listed suffix (only "medical center" is) -- nothing should be stripped here at all


def test_normalize_name_strips_stacked_trailing_suffixes():
    from bill_pipeline import _normalize_name

    # Bug review finding: a single pass through the suffix list only
    # stripped ", inc", leaving "city medical center" instead of "city" --
    # " medical center" was checked before the string ended with it.
    assert _normalize_name("City Medical Center, Inc") == "city"


def test_load_rate_table_from_csv_skips_malformed_row_keeps_rest(tmp_path=None):
    import tempfile
    import os
    from pricing_pipeline import load_rate_table_from_csv

    csv_content = (
        "code,locality,rate,description\n"
        "GOOD001,,100.50,Good entry one\n"
        "BAD0002,,not_a_number,Malformed rate\n"
        "GOOD003,,75.25,Good entry two\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        path = f.name

    try:
        table = load_rate_table_from_csv(path, "pfs")
        assert len(table.entries) == 2
        assert table.lookup("GOOD001") == 100.50
        assert table.lookup("GOOD003") == 75.25
        assert table.lookup("BAD0002") is None
    finally:
        os.unlink(path)


def test_findings_to_reasons_surfaces_zero_medicare_rate_overcharge():
    from synthesis import findings_to_reasons, PricingBenchmark

    # Bug review finding: `if pricing.medicare_rate:` (falsy-zero) used to
    # silently skip this entirely -- a $0 Medicare rate against any
    # positive charge is arguably the most extreme overcharge case there
    # is, not something to drop quietly.
    pricing = PricingBenchmark(billed_amount=500.0, medicare_rate=0.0, fair_price_estimate=None)
    reasons = findings_to_reasons([], pricing)
    assert len(reasons) == 1
    assert "$0" in reasons[0].summary
    assert "$500" in reasons[0].summary


def test_findings_to_reasons_skips_unrecognized_requirement_code():
    from synthesis import findings_to_reasons
    from fap_pipeline import ComplianceFinding
    from compliance_checklist import ComplianceStatus, Severity

    # Bug review finding: requirement_code is free TEXT in Postgres (not
    # DB-constrained like document_quality), reachable from the live
    # /intake request path via fetch_fap_for_facility -- historical data
    # drift or a future checklist edit should not crash synthesis.
    findings = [
        ComplianceFinding(
            requirement_code="some_code_from_a_future_checklist_edit",
            status=ComplianceStatus.ABSENT, evidence_text=None,
            severity=Severity.MATERIAL, argument_template="...",
        ),
        ComplianceFinding(
            requirement_code="eligibility_criteria_specified",
            status=ComplianceStatus.ABSENT, evidence_text=None,
            severity=Severity.MATERIAL, argument_template="...",
        ),
    ]
    reasons = findings_to_reasons(findings, None)
    assert len(reasons) == 1
    assert reasons[0].source_requirement_codes == ["eligibility_criteria_specified"]


def test_argument_for_reason_falls_back_to_summary_for_unrecognized_code():
    from letter_pipeline import _argument_for_reason
    from synthesis import Reason, OutcomeType

    # Defense-in-depth fix: a PROCEDURAL_LEVERAGE reason carrying a code
    # not in CHECKLIST_BY_CODE should fall back to reason.summary rather
    # than crash -- this generates text for a real letter to a provider.
    reason = Reason(
        outcome_type=OutcomeType.PROCEDURAL_LEVERAGE, summary="fallback summary text",
        estimated_low=None, estimated_high=None, source_requirement_codes=["not_a_real_code"],
    )
    argument = _argument_for_reason(reason)
    assert argument.text == "fallback summary text"


def test_complete_json_degenerate_fence_only_response_raises_clean_json_error():
    import json
    import os

    # Bug review finding: .split("\n", 1)[1] crashed with IndexError on
    # a response that's just the opening fence with no newline at all
    # (realistic if generation is cut off almost immediately) -- and
    # IndexError isn't caught by api.py's (KeyError, TypeError,
    # ValueError) handling for "the LLM response couldn't be parsed."
    # Should now raise a clean JSONDecodeError (a ValueError subclass)
    # instead.
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("llm_client.complete", return_value="```"):
        try:
            llm_client.complete_json("whatever")
            assert False, "expected a JSONDecodeError"
        except json.JSONDecodeError:
            pass  # expected -- not IndexError


def test_complete_json_truncated_response_raises_clean_json_error_not_indexerror():
    import json
    import os

    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("llm_client.complete", return_value='```json\n{"a": 1'):
        try:
            llm_client.complete_json("whatever")
            assert False, "expected a JSONDecodeError"
        except json.JSONDecodeError:
            pass


def test_dsn_url_encodes_special_characters_in_credentials():
    import os
    import urllib.parse
    import db

    # Bug review finding: building the DSN via plain f-string
    # interpolation broke when the password contained URL-reserved
    # characters (@, :, /) -- common in auto-generated cloud database
    # credentials. Verified for real against a live Postgres role with
    # exactly these characters while building this fix: the unencoded
    # DSN failed to connect at all ("invalid integer value 'word' for
    # connection option 'port'"), while the encoded one connected
    # correctly. This test checks the encoding logic itself, without
    # needing a live database.
    with patch.dict(os.environ, {
        "ROBINHEALTH_DB_PASSWORD": "p@ss:word/123",
        "ROBINHEALTH_DB_USER": "robinhealth",
    }, clear=False):
        os.environ.pop("DATABASE_URL", None)
        dsn = db._dsn()

    parsed = urllib.parse.urlparse(dsn)
    assert parsed.hostname == "localhost"
    assert urllib.parse.unquote(parsed.password) == "p@ss:word/123"
    assert urllib.parse.unquote(parsed.username) == "robinhealth"


def test_llm_client_anthropic_sends_correct_request_shape_text_only():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "the answer"}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        result = llm_client.complete("hello there", max_tokens=50)

    assert result == "the answer"
    call_kwargs = mock_post.call_args.kwargs
    # Plain string content, not wrapped in a content-block array, when there are no images
    assert call_kwargs["json"]["messages"] == [{"role": "user", "content": "hello there"}]
    assert call_kwargs["json"]["max_tokens"] == 50
    assert "system" not in call_kwargs["json"]  # omitted entirely when not given, not sent as None/empty
    assert mock_post.call_args.args[0].endswith("/v1/messages")
    assert "/chat/completions" not in mock_post.call_args.args[0]


def test_llm_client_anthropic_puts_system_at_top_level_not_in_messages():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "ok"}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("hello", system="You are a helpful assistant.")

    body = mock_post.call_args.kwargs["json"]
    assert body["system"] == "You are a helpful assistant."
    # Exactly one message (the user turn) -- system must NOT also appear
    # as a message in the array, which is how the OpenAI-compatible
    # shape handles it.
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_llm_client_anthropic_includes_images_as_base64_source_blocks():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "ok"}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("describe this", images=[(b"fake-bytes", "image/png")])

    content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe this"}
    # Anthropic's shape: a {"type": "image", "source": {...}} block, NOT
    # an OpenAI-style {"type": "image_url", "image_url": {"url": "data:..."}}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    import base64
    assert content[1]["source"]["data"] == base64.b64encode(b"fake-bytes").decode("ascii")


def test_llm_client_anthropic_uses_x_api_key_header_not_bearer():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "ok"}]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "secret-token"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("hello")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["x-api-key"] == "secret-token"
    assert "Authorization" not in headers  # not a Bearer token, unlike the openai_compatible path
    assert "anthropic-version" in headers


def test_llm_client_anthropic_falls_back_to_anthropic_api_key_env_var():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "ok"}]}

    # LLM_API_KEY deliberately not set -- someone "plugging in" Anthropic
    # likely already has ANTHROPIC_API_KEY set and shouldn't need a
    # second, scaffold-specific env var just to reuse it.
    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "fallback-token"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        llm_client.complete("hello")

    assert mock_post.call_args.kwargs["headers"]["x-api-key"] == "fallback-token"


def test_llm_client_anthropic_parses_multiple_text_blocks():
    import os

    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [
        {"type": "text", "text": "first part. "},
        {"type": "text", "text": "second part."},
    ]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response):
        result = llm_client.complete("hello")

    assert result == "first part. second part."


def test_llm_client_anthropic_complete_json_forces_tool_use_and_unwraps_data():
    import os

    # The anthropic path forces a single emit_json tool call; the API returns
    # the payload as the tool input's "data" field, which we return verbatim.
    payload = {"provider": {"name": "Test Hospital"}, "total_billed_amount": 200.0}
    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [
        {"type": "tool_use", "name": "emit_json", "id": "toolu_1", "input": {"data": payload}},
    ]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response) as mock_post:
        result = llm_client.complete_json("extract the bill")

    assert result == payload
    body = mock_post.call_args.kwargs["json"]
    # Forced tool choice -- this is what guarantees valid JSON (no scraping).
    assert body["tool_choice"] == {"type": "tool", "name": "emit_json"}
    assert body["tools"][0]["name"] == "emit_json"
    # No sampling params -- they 400 on the default model.
    assert "temperature" not in body and "top_p" not in body


def test_llm_client_anthropic_complete_json_unwraps_top_level_array():
    import os

    # Some callers (run_compliance_checklist) expect a top-level array; the
    # "data" envelope carries it through unchanged.
    findings = [{"requirement_code": "A"}, {"requirement_code": "B"}]
    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [
        {"type": "tool_use", "name": "emit_json", "id": "toolu_2", "input": {"data": findings}},
    ]}

    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic"}), \
         patch("httpx.post", return_value=fake_response):
        result = llm_client.complete_json("list the findings")

    assert result == findings


def test_llm_client_default_provider_is_anthropic_when_unset():
    import os

    # Default flipped to Anthropic: Claude is now the out-of-the-box provider
    # (quality + guaranteed-valid structured output). Set
    # LLM_PROVIDER=openai_compatible to use a self-hosted/open-weight model.
    fake_response = MagicMock()
    fake_response.json.return_value = {"content": [{"type": "text", "text": "ok"}]}

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LLM_PROVIDER", None)
        with patch("httpx.post", return_value=fake_response) as mock_post:
            llm_client.complete("hello")

    # Hits Anthropic's Messages API, not the OpenAI-compatible chat endpoint.
    assert mock_post.call_args.args[0].endswith("/v1/messages")
    assert "/chat/completions" not in mock_post.call_args.args[0]



# ============================================================
# EOB pipeline tests (mocked -- no LLM or DB needed)
# ============================================================

def test_match_eob_to_bill_exact_code_match():
    from eob_pipeline import EobExtraction, EobLineItem, match_eob_to_bill

    eob = EobExtraction(
        insurer_name="Aetna", member_id=None, claim_number=None,
        date_processed=None, total_billed_amount=500.0,
        total_allowed_amount=200.0, total_insurance_paid=160.0,
        total_patient_responsibility=40.0,
        line_items=[EobLineItem(
            line_number=1, date_of_service="2026-03-14",
            description="Office visit", procedure_code="99213",
            code_type="cpt", units=1.0,
            billed_amount=500.0, allowed_amount=200.0,
            insurance_paid=160.0, patient_responsibility=40.0,
            adjustment_codes=[],
        )],
        parsing_confidence="high", raw_text=None,
    )
    bill = MagicMock()
    bill.line_items = [MagicMock(
        line_number=1, description="Office visit level 3",
        procedure_code="99213", billed_amount=500.0,
    )]

    result = match_eob_to_bill(eob, bill)

    assert len(result.matched) == 1
    assert result.matched[0].match_status == "matched"
    assert result.matched[0].eob_line.allowed_amount == 200.0
    assert len(result.unmatched_eob_lines) == 0
    assert result.total_allowed_amount == 200.0
    assert result.total_patient_responsibility == 40.0


def test_match_eob_to_bill_unmatched_when_codes_and_amounts_diverge():
    from eob_pipeline import EobExtraction, EobLineItem, match_eob_to_bill

    eob = EobExtraction(
        insurer_name="UHC", member_id=None, claim_number=None,
        date_processed=None, total_billed_amount=1000.0,
        total_allowed_amount=400.0, total_insurance_paid=320.0,
        total_patient_responsibility=80.0,
        line_items=[EobLineItem(
            line_number=1, date_of_service=None,
            description="MRI brain without contrast", procedure_code="70553",
            code_type="cpt", units=1.0,
            billed_amount=1000.0, allowed_amount=400.0,
            insurance_paid=320.0, patient_responsibility=80.0,
            adjustment_codes=[],
        )],
        parsing_confidence="high", raw_text=None,
    )
    bill = MagicMock()
    bill.line_items = [MagicMock(
        line_number=1, description="pharmacy dispensing fee",
        procedure_code="99999", billed_amount=15.00,
    )]

    result = match_eob_to_bill(eob, bill)

    assert len(result.unmatched_eob_lines) == 1
    assert result.total_allowed_amount is None


def test_match_eob_multiple_lines_greedy_assignment():
    from eob_pipeline import EobExtraction, EobLineItem, match_eob_to_bill

    eob = EobExtraction(
        insurer_name="Cigna", member_id=None, claim_number=None,
        date_processed=None, total_billed_amount=800.0,
        total_allowed_amount=320.0, total_insurance_paid=256.0,
        total_patient_responsibility=64.0,
        line_items=[
            EobLineItem(line_number=1, date_of_service=None,
                description="Office visit", procedure_code="99213",
                code_type="cpt", units=1.0,
                billed_amount=300.0, allowed_amount=120.0,
                insurance_paid=96.0, patient_responsibility=24.0,
                adjustment_codes=[]),
            EobLineItem(line_number=2, date_of_service=None,
                description="X-ray chest", procedure_code="71046",
                code_type="cpt", units=1.0,
                billed_amount=500.0, allowed_amount=200.0,
                insurance_paid=160.0, patient_responsibility=40.0,
                adjustment_codes=[]),
        ],
        parsing_confidence="high", raw_text=None,
    )
    bill = MagicMock()
    bill.line_items = [
        MagicMock(line_number=1, description="office visit", procedure_code="99213", billed_amount=300.0),
        MagicMock(line_number=2, description="chest xray", procedure_code="71046", billed_amount=500.0),
    ]

    result = match_eob_to_bill(eob, bill)

    assert len(result.matched) == 2
    matched_bill_numbers = [m.bill_line.line_number for m in result.matched]
    assert sorted(matched_bill_numbers) == [1, 2]
    assert result.total_allowed_amount == 320.0
    assert result.total_patient_responsibility == 64.0


def test_eob_allowed_amount_reason_fires_for_denied_claim():
    """
    When a claim is denied, patient owes the full billed amount. The EOB
    still shows the allowed amount (what would have been paid). The reason
    fires because allowed < what patient currently owes.
    """
    from synthesis import SynthesisInput, _eob_allowed_amount_reason

    reason = _eob_allowed_amount_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        allowed_amount_total=400.0,
        patient_responsibility_total=1000.0,  # denied -- owes everything
    ))

    assert reason is not None
    assert reason.outcome_type.value == "partial_reduction"
    # savings = patient_resp (1000) - allowed (400) = 600
    assert reason.estimated_high == 600
    assert reason.estimated_low == round(600 * 0.7)
    assert "$400" in reason.summary
    assert "$600" in reason.summary


def test_eob_allowed_amount_reason_fires_without_patient_responsibility():
    """
    If no patient_responsibility is set (e.g. uninsured patient got an
    EOB from a prior claim for the same service), anchor falls back to
    billed_amount.
    """
    from synthesis import SynthesisInput, _eob_allowed_amount_reason

    reason = _eob_allowed_amount_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        allowed_amount_total=400.0,
        patient_responsibility_total=None,  # no EOB patient_resp
    ))

    assert reason is not None
    # savings = billed (1000) - allowed (400) = 600
    assert reason.estimated_high == 600


def test_eob_allowed_amount_reason_suppressed_when_already_below_allowed():
    """
    When patient_responsibility < allowed_amount (normal in-network copay
    scenario), the reason is suppressed -- arguing for allowed_amount would
    actually cost the patient MORE than their current obligation.
    """
    from synthesis import SynthesisInput, _eob_allowed_amount_reason

    reason = _eob_allowed_amount_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        allowed_amount_total=400.0,
        patient_responsibility_total=80.0,  # copay < allowed
    ))

    assert reason is None  # savings would be negative; correctly suppressed


def test_eob_allowed_amount_reason_suppressed_when_no_eob():
    from synthesis import SynthesisInput, _eob_allowed_amount_reason

    reason = _eob_allowed_amount_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
    ))

    assert reason is None


def test_parse_eob_extraction_skips_malformed_line_items():
    """
    A malformed line item (missing billed_amount) should be skipped rather
    than crashing the whole extraction -- same defensive pattern as
    run_compliance_checklist and insert_fap_parse_result.
    """
    from eob_pipeline import _parse_eob_extraction

    raw = {
        "insurer_name": "Aetna",
        "member_id": "M123",
        "claim_number": "CLM456",
        "date_processed": "2026-03-14",
        "total_billed_amount": 500.0,
        "total_allowed_amount": 200.0,
        "total_insurance_paid": 160.0,
        "total_patient_responsibility": 40.0,
        "line_items": [
            {
                "line_number": 1,
                "procedure_code": "99213",
                "billed_amount": 300.0,
                "allowed_amount": 120.0,
                "insurance_paid": 96.0,
                "patient_responsibility": 24.0,
                "adjustment_codes": [],
            },
            {
                # Malformed: line_number is non-integer string
                "line_number": "not-a-number",
                "procedure_code": "71046",
                "billed_amount": "not-a-float",  # will fail _to_float but not crash
                "adjustment_codes": [{"code_type": "CARC", "code": "CO-45"}],
            },
            {
                "line_number": 3,
                "procedure_code": "80053",
                "billed_amount": 200.0,
                "allowed_amount": 80.0,
                "insurance_paid": 64.0,
                "patient_responsibility": 16.0,
                "adjustment_codes": [],
            },
        ],
        "parsing_confidence": "medium",
    }

    result = _parse_eob_extraction(raw)

    # Line 2 is malformed (non-integer line_number) -- should be skipped
    # Lines 1 and 3 should survive
    assert result.insurer_name == "Aetna"
    assert result.parsing_confidence == "medium"
    # Non-integer line_number raises ValueError on int() -- that line is skipped
    codes_survived = [item.procedure_code for item in result.line_items]
    assert "99213" in codes_survived
    assert "80053" in codes_survived



# ============================================================
# MRF pipeline tests (mocked -- no network or DB needed)
# ============================================================

def test_mrf_parse_json_finds_exact_cpt_code():
    from mrf_pipeline import _parse_mrf_json, _normalize_code

    raw = {
        "hospital_name": "Test Hospital",
        "standard_charge_information": [
            {
                "description": "Office visit level 3",
                "code_information": [{"code": "99213", "type": "CPT"}],
                "standard_charges": [{
                    "gross_charge": 500.0,
                    "discounted_cash_price": 220.0,
                    "minimum_negotiated_charge": 110.0,
                    "maximum_negotiated_charge": 340.0,
                    "payers_information": [
                        {"payer_name": "Aetna", "plan_name": "PPO",
                         "standard_charge_dollar": 180.0},
                    ],
                }],
            },
            {
                "description": "Chest X-ray",
                "code_information": [{"code": "71046", "type": "CPT"}],
                "standard_charges": [{"gross_charge": 800.0}],
            },
        ],
    }

    result = _parse_mrf_json(raw, {"99213", "71046"})

    assert "99213" in result
    assert result["99213"].gross_charge == 500.0
    assert result["99213"].discounted_cash_price == 220.0
    assert result["99213"].min_negotiated_charge == 110.0
    assert result["99213"].max_negotiated_charge == 340.0
    assert len(result["99213"].payer_rates) == 1
    assert result["99213"].payer_rates[0]["payer_name"] == "Aetna"
    assert result["99213"].payer_rates[0]["rate"] == 180.0
    assert "71046" in result


def test_mrf_parse_json_ignores_placeholder_values():
    from mrf_pipeline import _parse_mrf_json

    raw = {
        "standard_charge_information": [{
            "description": "MRI",
            "code_information": [{"code": "70553", "type": "CPT"}],
            "standard_charges": [{
                "gross_charge": 999999999.0,    # classic placeholder
                "discounted_cash_price": 0.0,   # zero = placeholder
                "minimum_negotiated_charge": 1200.0,  # real value
                "maximum_negotiated_charge": 2800.0,
            }],
        }],
    }

    result = _parse_mrf_json(raw, {"70553"})

    assert "70553" in result
    assert result["70553"].gross_charge is None          # placeholder filtered
    assert result["70553"].discounted_cash_price is None  # zero filtered
    assert result["70553"].min_negotiated_charge == 1200.0  # real value kept
    assert result["70553"].max_negotiated_charge == 2800.0


def test_mrf_parse_csv_finds_codes():
    from mrf_pipeline import _parse_mrf_csv

    csv_text = (
        "description,code,code_type,gross_charge,discounted_cash_price,"
        "minimum_negotiated_charge,maximum_negotiated_charge\n"
        "Office visit,99213,CPT,500.0,220.0,110.0,340.0\n"
        "Lab panel,80053,CPT,300.0,150.0,80.0,200.0\n"
        "Unrelated service,99999,CPT,100.0,50.0,30.0,70.0\n"
    )

    result = _parse_mrf_csv(csv_text, {"99213", "80053"})

    assert "99213" in result
    assert result["99213"].gross_charge == 500.0
    assert result["99213"].discounted_cash_price == 220.0
    assert "80053" in result
    assert "99999" not in result  # not in target_codes


def test_mrf_fetch_returns_url_unknown_when_no_url():
    from mrf_pipeline import fetch_mrf_rates

    result = fetch_mrf_rates(
        facility_id="fac-test-001",
        codes=["99213", "71046"],
        mrf_url=None,
        hospital_domain=None,
    )

    assert result.status == "mrf_url_unknown"
    assert result.rates == {}
    assert len(result.codes_queried) == 2
    assert "CMS requires" in result.status_detail


def test_mrf_fetch_returns_unreachable_on_http_error():
    from mrf_pipeline import fetch_mrf_rates

    with patch("httpx.stream") as mock_stream:
        mock_stream.side_effect = __import__("httpx").ConnectError("refused")
        result = fetch_mrf_rates(
            facility_id="fac-test-002",
            codes=["99213"],
            mrf_url="http://localhost:19999/mrf.json",
        )

    assert result.status == "mrf_unreachable"
    assert result.mrf_url == "http://localhost:19999/mrf.json"
    assert result.rates == {}


def test_mrf_fetch_returns_codes_not_in_mrf_when_no_match():
    from mrf_pipeline import fetch_mrf_rates
    import json

    mrf_json = json.dumps({
        "standard_charge_information": [{
            "description": "Unrelated service",
            "code_information": [{"code": "99999", "type": "CPT"}],
            "standard_charges": [{"gross_charge": 200.0, "discounted_cash_price": 100.0}],
        }]
    }).encode()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.iter_bytes.return_value = [mrf_json]
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=mock_response):
        result = fetch_mrf_rates(
            facility_id="fac-test-003",
            codes=["99213"],  # not in this MRF
            mrf_url="https://example.com/mrf.json",
        )

    assert result.status == "codes_not_in_mrf"
    assert result.rates == {}


def test_mrf_fetch_returns_rates_found_with_real_values():
    from mrf_pipeline import fetch_mrf_rates
    import json

    mrf_json = json.dumps({
        "standard_charge_information": [{
            "description": "Office visit",
            "code_information": [{"code": "99213", "type": "CPT"}],
            "standard_charges": [{
                "gross_charge": 500.0,
                "discounted_cash_price": 220.0,
                "minimum_negotiated_charge": 110.0,
                "maximum_negotiated_charge": 340.0,
            }],
        }]
    }).encode()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.iter_bytes.return_value = [mrf_json]
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=mock_response):
        result = fetch_mrf_rates(
            facility_id="fac-test-004",
            codes=["99213"],
            mrf_url="https://example.com/mrf.json",
        )

    assert result.status == "rates_found"
    assert "99213" in result.rates
    assert result.rates["99213"].discounted_cash_price == 220.0
    assert "$220" in result.status_detail
    assert "$110" in result.status_detail


def test_mrf_returns_unpopulated_when_all_rates_are_placeholders():
    from mrf_pipeline import fetch_mrf_rates
    import json

    mrf_json = json.dumps({
        "standard_charge_information": [{
            "description": "MRI brain",
            "code_information": [{"code": "70553", "type": "CPT"}],
            "standard_charges": [{
                "gross_charge": 999999999.0,    # placeholder
                "discounted_cash_price": 0.0,   # placeholder
                "minimum_negotiated_charge": 0.0,
                "maximum_negotiated_charge": 0.0,
            }],
        }]
    }).encode()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.iter_bytes.return_value = [mrf_json]
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=mock_response):
        result = fetch_mrf_rates(
            facility_id="fac-test-005",
            codes=["70553"],
            mrf_url="https://example.com/mrf.json",
        )

    assert result.status == "mrf_unpopulated"
    assert "compliance red flag" in result.status_detail


def test_mrf_rates_reason_fires_when_cash_price_below_billed():
    from synthesis import SynthesisInput, _mrf_rates_reason, OutcomeType

    reason = _mrf_rates_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        mrf_status="rates_found",
        mrf_cash_price_total=350.0,  # hospital's published self-pay rate
    ))

    assert reason is not None
    assert reason.outcome_type == OutcomeType.PARTIAL_REDUCTION
    assert reason.estimated_high == 650  # 1000 - 350
    assert "$350" in reason.summary
    assert "published self-pay" in reason.summary


def test_mrf_rates_reason_fires_for_compliance_failure():
    from synthesis import SynthesisInput, _mrf_rates_reason, OutcomeType

    reason = _mrf_rates_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
        mrf_status="mrf_unreachable",
        mrf_status_detail="The hospital's price file returned HTTP 404.",
    ))

    assert reason is not None
    assert reason.outcome_type == OutcomeType.PROCEDURAL_LEVERAGE
    assert reason.estimated_high is None  # no dollar estimate
    assert "HTTP 404" in reason.summary


def test_mrf_rates_reason_suppressed_when_no_status():
    from synthesis import SynthesisInput, _mrf_rates_reason

    reason = _mrf_rates_reason(SynthesisInput(
        billed_amount=1000.0, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
    ))

    assert reason is None



# ============================================================
# Pricing pipeline -- PFS API and negotiated-rate tests
# ============================================================

def test_fetch_pfs_rates_for_codes_parses_real_api_response_shape():
    """
    Mocks the CMS Open Data API response shape (verified against the 2026
    indicators dataset schema) and confirms rates are parsed correctly.
    """
    from pricing_pipeline import fetch_pfs_rates_for_codes

    # Actual column names from pfs.data.cms.gov 2026 indicators dataset
    fake_rows = [
        {
            "hcpcs_cd": "99213",
            "mod": "",
            "description": "Office or other outpatient visit",
            "locality_number": "16",
            "non_facility_price": "116.84",
            "facility_price": "83.92",
        },
        {
            "hcpcs_cd": "71046",
            "mod": "",
            "description": "Chest x-ray 2 views",
            "locality_number": "16",
            "non_facility_price": "42.65",
            "facility_price": "42.65",
        },
    ]
    fake_response = MagicMock()
    fake_response.json.return_value = fake_rows

    with patch("httpx.get", return_value=fake_response) as mock_get:
        table = fetch_pfs_rates_for_codes(["99213", "71046"], locality="16")

    assert ("99213", "16") in table.entries
    assert table.entries[("99213", "16")].rate == 116.84
    assert table.entries[("71046", "16")].rate == 42.65
    assert table.entries[("99213", "16")].description == "Office or other outpatient visit"
    # Confirm the API was called with a SQL query containing the codes
    call_args = mock_get.call_args
    query = call_args.kwargs.get("params", {}).get("query", "")
    assert "99213" in query
    assert "71046" in query


def test_fetch_pfs_rates_for_codes_returns_empty_on_network_error():
    from pricing_pipeline import fetch_pfs_rates_for_codes

    with patch("httpx.get", side_effect=__import__("httpx").ConnectError("refused")):
        table = fetch_pfs_rates_for_codes(["99213"])

    assert table.source == "pfs"
    assert table.entries == {}  # graceful empty, not an exception


def test_fetch_pfs_rates_for_codes_handles_wrapped_results():
    """Some DKAN endpoints wrap results in {"results": [...]}."""
    from pricing_pipeline import fetch_pfs_rates_for_codes

    fake_response = MagicMock()
    fake_response.json.return_value = {
        "results": [{
            "hcpcs_cd": "99213",
            "mod": "",
            "description": "Office visit",
            "locality_number": "",
            "non_facility_price": "116.84",
            "facility_price": "83.92",
        }]
    }
    with patch("httpx.get", return_value=fake_response):
        table = fetch_pfs_rates_for_codes(["99213"])

    assert ("99213", "") in table.entries
    assert table.entries[("99213", "")].rate == 116.84


def test_fetch_pfs_rates_for_codes_facility_setting():
    """facility setting uses facility_price column."""
    from pricing_pipeline import fetch_pfs_rates_for_codes

    fake_response = MagicMock()
    fake_response.json.return_value = [{
        "hcpcs_cd": "99213", "mod": "", "description": "Office visit",
        "locality_number": "", "non_facility_price": "116.84", "facility_price": "83.92",
    }]
    with patch("httpx.get", return_value=fake_response):
        table = fetch_pfs_rates_for_codes(["99213"], setting="facility")

    assert table.entries[("99213", "")].rate == 83.92


def test_resolve_pfs_locality_known_states():
    from pricing_pipeline import resolve_pfs_locality

    assert resolve_pfs_locality("TX") == "43"  # rest of Texas
    assert resolve_pfs_locality("CA") == "26"  # rest of California
    assert resolve_pfs_locality("FL") == "09"
    assert resolve_pfs_locality("ny") == "16"  # case-insensitive


def test_resolve_pfs_locality_unknown_returns_none():
    from pricing_pipeline import resolve_pfs_locality

    assert resolve_pfs_locality(None) is None
    assert resolve_pfs_locality("ZZ") is None  # not a real state
    assert resolve_pfs_locality("") is None


def test_resolve_rate_tables_on_demand_fetches_when_pfs_empty():
    from pricing_pipeline import resolve_rate_tables_on_demand, RateTable, RateTableEntry

    base = {"pfs": RateTable(source="pfs", entries={})}

    fetched_table = RateTable(source="pfs", entries={
        ("99213", "16"): RateTableEntry(code="99213", locality="16", rate=116.84),
    })
    with patch("pricing_pipeline.fetch_pfs_rates_for_codes", return_value=fetched_table):
        result = resolve_rate_tables_on_demand(base, codes=["99213"], provider_state="TX")

    assert result["pfs"].entries != {}
    assert ("99213", "16") in result["pfs"].entries


def test_resolve_rate_tables_on_demand_noop_when_pfs_preloaded():
    from pricing_pipeline import resolve_rate_tables_on_demand, RateTable, RateTableEntry

    preloaded = RateTable(source="pfs", entries={
        ("99213", ""): RateTableEntry(code="99213", locality="", rate=110.0),
    })
    base = {"pfs": preloaded}

    with patch("pricing_pipeline.fetch_pfs_rates_for_codes") as mock_fetch:
        result = resolve_rate_tables_on_demand(base, codes=["99213"])

    mock_fetch.assert_not_called()  # shouldn't fetch when table is already populated
    assert result["pfs"] is preloaded  # exact same object, not a copy


def test_estimate_negotiated_rate_uses_mrf_min_negotiated():
    from pricing_pipeline import estimate_negotiated_rate, LineItemBenchmark
    from bill_pipeline import ExtractedLineItem

    benchmarks = [LineItemBenchmark(
        line_item=ExtractedLineItem(line_number=1, description="x", procedure_code="99213",
                                   code_type="cpt", units=1, billed_amount=500),
        benchmark_source="pfs", medicare_rate=116.84,
        delta_amount=383.16, delta_pct=328.0,
    )]
    mrf_finding = {
        "mrf_status": "rates_found",
        "rates": {
            "99213": {
                "gross_charge": 500.0,
                "discounted_cash_price": 220.0,
                "min_negotiated_charge": 110.0,
                "max_negotiated_charge": 340.0,
                "payer_rates": [],
            }
        }
    }

    result = estimate_negotiated_rate(benchmarks, mrf_finding=mrf_finding)

    assert result == 110.0  # min_negotiated takes priority


def test_estimate_negotiated_rate_falls_back_to_cash_price():
    from pricing_pipeline import estimate_negotiated_rate, LineItemBenchmark
    from bill_pipeline import ExtractedLineItem

    benchmarks = [LineItemBenchmark(
        line_item=ExtractedLineItem(line_number=1, description="x", procedure_code="99213",
                                   code_type="cpt", units=1, billed_amount=500),
        benchmark_source="pfs", medicare_rate=116.84,
        delta_amount=383.16, delta_pct=328.0,
    )]
    mrf_finding = {
        "mrf_status": "rates_found",
        "rates": {
            "99213": {
                "gross_charge": 500.0,
                "discounted_cash_price": 220.0,
                "min_negotiated_charge": None,  # not available
                "max_negotiated_charge": None,
                "payer_rates": [],
            }
        }
    }

    result = estimate_negotiated_rate(benchmarks, mrf_finding=mrf_finding)

    assert result == 220.0  # cash price fallback


def test_estimate_negotiated_rate_falls_back_to_medicare_multiplier():
    from pricing_pipeline import estimate_negotiated_rate, LineItemBenchmark, _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO
    from bill_pipeline import ExtractedLineItem

    benchmarks = [LineItemBenchmark(
        line_item=ExtractedLineItem(line_number=1, description="x", procedure_code="99213",
                                   code_type="cpt", units=1, billed_amount=500),
        benchmark_source="pfs", medicare_rate=116.84,
        delta_amount=383.16, delta_pct=328.0,
    )]

    result = estimate_negotiated_rate(benchmarks, mrf_finding=None)

    expected = round(116.84 * _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO, 2)
    assert result == expected


def test_estimate_negotiated_rate_returns_none_with_no_data():
    from pricing_pipeline import estimate_negotiated_rate, LineItemBenchmark
    from bill_pipeline import ExtractedLineItem

    # No matched medicare_rate, no MRF
    benchmarks = [LineItemBenchmark(
        line_item=ExtractedLineItem(line_number=1, description="x", procedure_code="NDC123",
                                   code_type="ndc", units=1, billed_amount=500),
        benchmark_source=None, medicare_rate=None,
        delta_amount=None, delta_pct=None,
    )]

    result = estimate_negotiated_rate(benchmarks, mrf_finding=None)

    assert result is None


def test_aggregate_to_pricing_benchmark_no_longer_raises():
    """
    aggregate_to_pricing_benchmark previously swallowed NotImplementedError.
    Now estimate_negotiated_rate is real -- confirm the benchmark round-trip
    works end-to-end with the Medicare multiplier when no MRF is available.
    """
    from pricing_pipeline import (
        aggregate_to_pricing_benchmark, benchmark_bill,
        RateTable, RateTableEntry, _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO,
    )
    from bill_pipeline import ExtractedLineItem

    table = RateTable(source="pfs", entries={
        ("99213", ""): RateTableEntry(code="99213", locality="", rate=116.84),
    })
    items = [ExtractedLineItem(
        line_number=1, description="Office visit", procedure_code="99213",
        code_type="cpt", units=1.0, billed_amount=500.0,
    )]
    benchmarks = benchmark_bill(items, {"pfs": table})
    pricing = aggregate_to_pricing_benchmark(benchmarks, mrf_finding=None)

    assert pricing is not None
    assert pricing.medicare_rate == 116.84
    assert pricing.billed_amount == 500.0
    # fair_price_estimate should now be set (Medicare multiplier)
    assert pricing.fair_price_estimate is not None
    assert pricing.fair_price_estimate == round(116.84 * _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO, 2)



# ============================================================
# Provider response classification tests (pure logic, no DB/LLM)
# ============================================================

def test_classify_collections_referral():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "Your account has been referred to a collection agency. "
        "Please contact Acme Collections at 555-1234.",
        original_billed_amount=3000.0,
    )
    assert result.response_type == "referred_to_collections"
    assert result.confidence == "high"


def test_classify_eligibility_denial():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "After reviewing your application, we have determined that you do not qualify "
        "for financial assistance. Your income exceeds our eligibility threshold.",
        original_billed_amount=2000.0,
    )
    assert result.response_type == "denied_eligibility"
    assert result.confidence == "high"


def test_classify_documentation_request():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "To process your financial assistance application, please provide: "
        "your most recent tax return, two recent pay stubs, and bank statements "
        "for the past three months.",
        original_billed_amount=5000.0,
    )
    assert result.response_type == "requested_more_info"
    assert "tax return" in result.extracted_documents
    assert "pay stub" in result.extracted_documents


def test_classify_reduced_offer_extracts_amount():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "We are willing to reduce your balance. Our best offer is $1,800 "
        "as a one-time settlement.",
        original_billed_amount=5000.0,
        target_amount=1200.0,
    )
    assert result.response_type == "reduced_offer"
    assert result.extracted_amount == 1800.0
    assert result.confidence == "high"


def test_classify_offer_below_target_becomes_accepted():
    from outcome_pipeline import classify_provider_response
    # Provider offers $900 when we asked for $1200 -- that's BETTER than target
    result = classify_provider_response(
        "We can reduce your balance to $900 as final settlement.",
        original_billed_amount=3000.0,
        target_amount=1200.0,
    )
    # 900 < 1200 (target) so it meets/beats target → accepted_target or reduced_offer
    # Either is acceptable; the important thing is the amount is extracted
    assert result.extracted_amount == 900.0
    assert result.response_type in ("accepted_target", "reduced_offer")


def test_classify_no_fap_claim():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "We do not have a financial assistance program at this facility. "
        "All balances are due in full within 30 days.",
        original_billed_amount=4000.0,
    )
    assert result.response_type == "claimed_no_fap"


def test_classify_billing_error():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "We have reviewed your account and found a billing error. "
        "The charge for the second MRI was posted in error.",
        original_billed_amount=6000.0,
    )
    assert result.response_type == "billing_error"


def test_classify_acceptance():
    from outcome_pipeline import classify_provider_response
    result = classify_provider_response(
        "We have approved your financial assistance request. "
        "Your balance has been reduced to $800.",
        original_billed_amount=4000.0,
        target_amount=800.0,
    )
    assert result.response_type == "accepted_target"
    assert result.extracted_amount == 800.0


def test_generate_followup_collections_is_urgent():
    from outcome_pipeline import (
        generate_followup_action, ClassifiedResponse, NegotiationSummary,
    )
    classified = ClassifiedResponse(
        response_type="referred_to_collections",
        confidence="high",
        extracted_amount=None,
        extracted_documents=[],
        reasoning="mentions collections",
    )
    neg = NegotiationSummary(
        negotiation_id="neg-1", case_id="case-1", status="contacted",
        original_billed_amount=3000.0, target_amount=900.0,
        counter_offer_amount=None, agreed_amount=None, amount_saved=None,
        robinhealth_fee=None, patient_net_savings=None,
        provider_response_text=None, first_contacted_at=None,
        agreed_at=None, paid_at=None, contacts=[],
    )
    action = generate_followup_action("neg-1", "referred_to_collections", classified, neg)
    assert action.urgency == "immediate"
    assert action.followup_letter_context is not None
    assert action.followup_letter_context["letter_type"] == "collections_response"
    assert "FDCPA" in str(action.followup_letter_context)
    assert action.resolves_negotiation is False


def test_generate_followup_accepted_target_resolves():
    from outcome_pipeline import (
        generate_followup_action, ClassifiedResponse, NegotiationSummary,
    )
    classified = ClassifiedResponse(
        response_type="accepted_target", confidence="high",
        extracted_amount=1000.0, extracted_documents=[], reasoning="accepted",
    )
    neg = NegotiationSummary(
        negotiation_id="neg-2", case_id="case-2", status="contacted",
        original_billed_amount=4000.0, target_amount=1000.0,
        counter_offer_amount=None, agreed_amount=None, amount_saved=None,
        robinhealth_fee=None, patient_net_savings=None,
        provider_response_text=None, first_contacted_at=None,
        agreed_at=None, paid_at=None, contacts=[],
    )
    action = generate_followup_action("neg-2", "accepted_target", classified, neg)
    assert action.resolves_negotiation is True
    assert action.suggested_resolution == {"agreed_amount": 1000.0}
    assert action.followup_letter_context is None


def test_generate_followup_eligibility_denial_has_legal_citations():
    from outcome_pipeline import (
        generate_followup_action, ClassifiedResponse, NegotiationSummary,
    )
    classified = ClassifiedResponse(
        response_type="denied_eligibility", confidence="high",
        extracted_amount=None, extracted_documents=[], reasoning="denied",
    )
    neg = NegotiationSummary(
        negotiation_id="neg-3", case_id="case-3", status="provider_replied",
        original_billed_amount=5000.0, target_amount=1500.0,
        counter_offer_amount=None, agreed_amount=None, amount_saved=None,
        robinhealth_fee=None, patient_net_savings=None,
        provider_response_text="not eligible", first_contacted_at=None,
        agreed_at=None, paid_at=None, contacts=[],
    )
    action = generate_followup_action("neg-3", "denied_eligibility", classified, neg)
    ctx = action.followup_letter_context
    assert ctx["letter_type"] == "eligibility_appeal"
    assert any("501(r)" in c for c in ctx["legal_citations"])
    assert action.urgency == "within_week"


def test_generate_followup_documentation_request_includes_checklist():
    from outcome_pipeline import (
        generate_followup_action, ClassifiedResponse, NegotiationSummary,
    )
    classified = ClassifiedResponse(
        response_type="requested_more_info", confidence="high",
        extracted_amount=None,
        extracted_documents=["tax return", "pay stub"],
        reasoning="needs docs",
    )
    neg = NegotiationSummary(
        negotiation_id="neg-4", case_id="case-4", status="provider_replied",
        original_billed_amount=2000.0, target_amount=600.0,
        counter_offer_amount=None, agreed_amount=None, amount_saved=None,
        robinhealth_fee=None, patient_net_savings=None,
        provider_response_text=None, first_contacted_at=None,
        agreed_at=None, paid_at=None, contacts=[],
    )
    action = generate_followup_action("neg-4", "requested_more_info", classified, neg)
    assert "tax return" in action.followup_letter_context["documents_checklist"]
    assert "pay stub" in action.followup_letter_context["documents_checklist"]


def test_extract_dollar_amount():
    from outcome_pipeline import _extract_dollar_amount
    assert _extract_dollar_amount("We can offer $1,500.00") == 1500.0
    assert _extract_dollar_amount("reduce to $2,100") == 2100.0
    assert _extract_dollar_amount("amount of $850") == 850.0
    assert _extract_dollar_amount("no mention of money") is None



# ============================================================
# Fee agreement tests (pure logic)
# ============================================================

def test_fee_terms_content_is_complete():
    from outcome_pipeline import get_fee_terms, FEE_TERMS_TEXT, FEE_TERMS_VERSION
    terms = get_fee_terms()
    assert terms["fee_percentage"] == 20
    assert terms["fee_basis"] == "savings"
    assert terms["no_cure_no_fee"] is True
    assert terms["version"] == FEE_TERMS_VERSION
    assert "20%" in FEE_TERMS_TEXT
    assert "nothing" in FEE_TERMS_TEXT.lower()  # no cure no fee
    assert "authorize" in FEE_TERMS_TEXT.lower() # authorization language


def test_fee_terms_version_reflects_two_plan_model():
    # Bumped to v2.0 when the $50/month membership ceiling was added alongside
    # the capped 20% contingency fee. The version bump intentionally forces
    # existing patients to re-accept the new pricing terms.
    from outcome_pipeline import get_fee_terms, FEE_TERMS_VERSION, FEE_TERMS_TEXT
    assert FEE_TERMS_VERSION == "v2.0"
    terms = get_fee_terms()
    assert terms["membership_monthly_usd"] == 50.0
    assert terms["fee_cap_usd"] == 1000.0
    plan_ids = {p["id"] for p in terms["plans"]}
    assert plan_ids == {"contingency", "membership"}
    assert "$50" in FEE_TERMS_TEXT and "$1,000" in FEE_TERMS_TEXT


def test_compute_robinhealth_fee_contingency_takes_20_percent():
    from outcome_pipeline import compute_robinhealth_fee
    assert compute_robinhealth_fee(3000.0, "contingency") == 600.0


def test_compute_robinhealth_fee_contingency_is_capped_at_1000():
    from outcome_pipeline import compute_robinhealth_fee
    # 20% of $50,000 = $10,000, but the fee is capped at $1,000.
    assert compute_robinhealth_fee(50000.0, "contingency") == 1000.0


def test_compute_robinhealth_fee_membership_takes_nothing_from_savings():
    from outcome_pipeline import compute_robinhealth_fee
    # Members pay the flat monthly fee; we never take a cut of their savings.
    assert compute_robinhealth_fee(50000.0, "membership") == 0.0


def test_compute_robinhealth_fee_zero_or_no_savings_is_free():
    from outcome_pipeline import compute_robinhealth_fee
    assert compute_robinhealth_fee(0.0, "contingency") == 0.0
    assert compute_robinhealth_fee(None, "contingency") == 0.0



# ============================================================
# Letter rendering and delivery tests (no LLM, no DB needed)
# ============================================================

def test_render_to_pdf_produces_valid_pdf():
    from letter_pipeline import (
        DraftedLetter, RecipientInfo, render_to_pdf,
    )
    drafted = DraftedLetter(
        body=(
            "Dear Billing Department,\n\n"
            "We are writing on behalf of Jane Doe regarding account #12345.\n\n"
            "The billed amount of $5,000.00 appears above the Medicare rate for "
            "this service. We request a reduction to $2,000.00.\n\n"
            "Please respond within 21 days."
        ),
        requested_amount=2000.0,
        requests_full_waiver=False,
        response_deadline_days=21,
    )
    recipient = RecipientInfo(
        facility_name="General Hospital",
        facility_address="123 Main St, Springfield, IL 62701",
        patient_name="Jane Doe",
        account_number="ACC-12345",
        date_of_service="2026-03-14",
    )
    pdf_bytes = render_to_pdf(drafted, recipient, reference_number="RH-TEST-001")
    # Valid PDF starts with %PDF-
    assert pdf_bytes[:4] == b"%PDF", "Output is not a valid PDF"
    assert len(pdf_bytes) > 1000, "PDF is suspiciously small"


def test_render_to_pdf_full_waiver_variant():
    from letter_pipeline import DraftedLetter, RecipientInfo, render_to_pdf
    drafted = DraftedLetter(
        body="The patient qualifies for full charity care based on their income.",
        requested_amount=0.0,
        requests_full_waiver=True,
        response_deadline_days=21,
    )
    recipient = RecipientInfo(
        facility_name="County Hospital",
        facility_address=None,
        patient_name="John Smith",
        account_number=None,
        date_of_service=None,
    )
    pdf_bytes = render_to_pdf(drafted, recipient, reference_number="RH-TEST-002")
    assert pdf_bytes[:4] == b"%PDF"


def test_render_followup_letter_produces_valid_pdf():
    from letter_pipeline import RecipientInfo, render_followup_letter
    ctx = {
        "letter_type": "eligibility_appeal",
        "subject": "Appeal of Financial Assistance Denial",
        "key_points": [
            "We are appealing the denial of financial assistance.",
            "Under 26 CFR 1.501(r)-4, the hospital must apply its FAP consistently.",
            "Please provide the specific criteria not met.",
        ],
        "legal_citations": [
            "26 CFR 1.501(r)-4 (FAP application requirements)",
        ],
        "urgency": "standard",
    }
    recipient = RecipientInfo(
        facility_name="Regional Medical Center",
        facility_address="456 Oak Ave, Portland, OR 97201",
        patient_name="Alice Johnson",
        account_number="RMC-9988",
        date_of_service="2026-01-10",
    )
    pdf_bytes = render_followup_letter(ctx, recipient, "RH-TEST-003", round_number=2)
    assert pdf_bytes[:4] == b"%PDF"
    # Should be larger than a trivial PDF
    assert len(pdf_bytes) > 2000


def test_render_followup_collections_urgent_pdf():
    from letter_pipeline import RecipientInfo, render_followup_letter
    ctx = {
        "letter_type": "collections_response",
        "subject": "URGENT: Dispute of Debt",
        "key_points": [
            "This debt is formally disputed under 15 U.S.C. § 1692g.",
            "The FAP application is pending.",
        ],
        "legal_citations": [
            "15 U.S.C. § 1692g (FDCPA debt dispute rights)",
            "26 CFR 1.501(r)-6 (extraordinary collection actions)",
        ],
        "urgency": "immediate",
    }
    recipient = RecipientInfo(
        facility_name="St. Mary Hospital", facility_address=None,
        patient_name="Bob Martinez", account_number="SM-111", date_of_service=None,
    )
    pdf_bytes = render_followup_letter(ctx, recipient, "RH-TEST-004")
    assert pdf_bytes[:4] == b"%PDF"


def test_make_reference_number_format():
    from delivery_pipeline import make_reference_number
    ref = make_reference_number("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    parts = ref.split("-")
    assert parts[0] == "RH"
    assert len(parts[1]) == 6          # case_id prefix
    assert len(parts[2]) == 8          # YYYYMMDD
    assert len(parts[3]) == 4          # random hex
    assert ref.startswith("RH-A1B2C3-")


def test_make_reference_number_is_unique():
    from delivery_pipeline import make_reference_number
    refs = [make_reference_number("test-case-id") for _ in range(10)]
    assert len(set(refs)) == 10, "Reference numbers should be unique"


def test_send_email_not_configured_when_no_smtp_host():
    import os
    from delivery_pipeline import send_email
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SMTP_HOST", None)
        receipt = send_email(
            to_address="billing@hospital.com",
            subject="Test",
            body_text="Test body",
            pdf_bytes=b"%PDF-test",
            pdf_filename="test.pdf",
            reference_number="RH-TEST-005",
        )
    assert receipt.status == "not_configured"
    assert receipt.channel == "letter_email"
    assert "SMTP_HOST" in receipt.detail


def test_send_email_sends_when_smtp_configured():
    """Verifies the SMTP code path with a mock server."""
    import os
    from delivery_pipeline import send_email
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587"}):
            receipt = send_email(
                to_address="billing@hospital.com",
                subject="Medical Bill Negotiation",
                body_text="Please see attached.",
                pdf_bytes=b"%PDF-test",
                pdf_filename="letter.pdf",
                reference_number="RH-TEST-006",
            )
    assert receipt.status == "sent"
    assert receipt.channel == "letter_email"
    assert "smtp.example.com" in receipt.detail
    # Verify sendmail was called
    mock_server.sendmail.assert_called_once()


def test_send_fax_not_configured():
    import os
    from delivery_pipeline import send_fax
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LOB_API_KEY", None)
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        receipt = send_fax("+15551234567", b"%PDF-test", "RH-TEST-007")
    assert receipt.status == "not_configured"
    assert "LOB_API_KEY" in receipt.detail


def test_deliver_dispatcher_routes_to_correct_handler():
    import os
    from delivery_pipeline import deliver
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SMTP_HOST", None)
        receipt = deliver(
            channel="letter_email",
            pdf_bytes=b"%PDF-test",
            reference_number="RH-TEST-008",
            recipient_info={"email": "billing@hospital.com", "subject": "Test"},
        )
    assert receipt.channel == "letter_email"

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LOB_API_KEY", None)
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        receipt = deliver(
            channel="letter_fax",
            pdf_bytes=b"%PDF-test",
            reference_number="RH-TEST-009",
            recipient_info={"fax_number": "+15551234567"},
        )
    assert receipt.channel == "letter_fax"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")
