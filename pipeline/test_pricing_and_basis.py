"""
Tests for P4: stronger/accurate price anchors.

- The pricing reason targets a defensible *fair price* (Medicare multiplier /
  MRF-derived), not the raw Medicare rate.
- Letter wording is grounded in each reduction's actual basis, so a
  pricing/MRF/EOB reduction is never mis-attributed to financial assistance.

Pure logic; run with: python3 -m pytest test_pricing_and_basis.py
"""

from synthesis import findings_to_reasons, PricingBenchmark, Reason, OutcomeType
from letter_pipeline import _argument_for_reason


def test_pricing_reason_anchors_on_fair_price_not_raw_medicare():
    pricing = PricingBenchmark(billed_amount=1000.0, medicare_rate=200.0, fair_price_estimate=360.0)
    reasons = findings_to_reasons([], pricing)
    assert len(reasons) == 1
    r = reasons[0]
    assert r.basis == "pricing"
    assert r.estimated_low == 360.0   # fair price, NOT the raw $200 Medicare rate
    assert r.estimated_high == 1000.0
    assert "360" in r.summary
    assert "1.8x" in r.summary or "x the Medicare" in r.summary


def test_pricing_reason_falls_back_to_medicare_when_no_fair_estimate():
    pricing = PricingBenchmark(billed_amount=1000.0, medicare_rate=200.0, fair_price_estimate=None)
    reasons = findings_to_reasons([], pricing)
    assert reasons[0].estimated_low == 200.0  # falls back to Medicare when no estimate


def test_letter_pricing_basis_uses_benchmark_language_not_fap():
    arg = _argument_for_reason(
        Reason(OutcomeType.PARTIAL_REDUCTION, "s", 360.0, 1000.0, [], basis="pricing")
    )
    assert "benchmark" in arg.text.lower()
    assert "Financial Assistance" not in arg.text
    assert "360.00" in arg.text
    assert arg.requested_amount == 360.0


def test_letter_fap_basis_keeps_financial_assistance_language():
    arg = _argument_for_reason(
        Reason(OutcomeType.PARTIAL_REDUCTION, "s", 1200.0, 2840.0, [], basis="fap")
    )
    assert "Financial Assistance" in arg.text
    assert "1,200.00" in arg.text


def test_letter_mrf_cash_basis_asserts_published_price_without_mislabeled_number():
    # MRF dollar fields are *savings*, not a balance -- the wording must not
    # print them as a target balance.
    arg = _argument_for_reason(
        Reason(OutcomeType.PARTIAL_REDUCTION, "s", 510.0, 600.0, [], basis="mrf_cash")
    )
    assert "cash" in arg.text.lower()
    assert "Financial Assistance" not in arg.text
    assert "510" not in arg.text and "600" not in arg.text
    assert arg.requested_amount is None


def test_letter_eob_basis_references_insurer_allowed_amount():
    arg = _argument_for_reason(
        Reason(OutcomeType.PARTIAL_REDUCTION, "s", 100.0, 200.0, [], basis="eob_allowed")
    )
    assert "insurer" in arg.text.lower() or "payer" in arg.text.lower()
    assert arg.requested_amount is None


def test_letter_none_basis_is_generic_reduction_without_false_fap_claim():
    arg = _argument_for_reason(
        Reason(OutcomeType.PARTIAL_REDUCTION, "s", 1200.0, 2840.0, [], basis=None)
    )
    assert "1,200.00" in arg.text and "2,840.00" in arg.text
    assert "Financial Assistance" not in arg.text  # don't invent an FAP basis
