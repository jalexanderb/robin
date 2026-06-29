"""
Tests for the learning loop (learning.py). Pure logic, no DB.
Run with: python3 -m pytest test_learning.py
"""

from learning import (
    MIN_CASES_FOR_INSIGHT,
    OutcomeRecord,
    insight_text,
    summarize_outcomes,
)


def test_empty_history_has_no_insight():
    insights = summarize_outcomes([])
    assert insights.n_cases == 0
    assert insights.enough_data is False
    assert insight_text(insights) is None


def test_summarize_computes_success_rate_and_avg_reduction():
    records = [
        OutcomeRecord(billed_amount=1000.0, agreed_amount=600.0),   # 40% off
        OutcomeRecord(billed_amount=1000.0, agreed_amount=800.0),   # 20% off
        OutcomeRecord(billed_amount=1000.0, agreed_amount=1000.0),  # no change
    ]
    ins = summarize_outcomes(records)
    assert ins.n_cases == 3
    assert ins.n_reduced == 2
    assert ins.success_rate == round(2 / 3, 3)
    assert ins.avg_reduction_pct == 30.0  # mean of 40 and 20


def test_insight_text_requires_minimum_cases():
    # One reduced case -> not enough to claim a pattern.
    one = summarize_outcomes([OutcomeRecord(1000.0, 500.0)])
    assert one.enough_data is False
    assert insight_text(one) is None

    enough = summarize_outcomes([OutcomeRecord(1000.0, 500.0)] * MIN_CASES_FOR_INSIGHT)
    assert enough.enough_data is True
    assert insight_text(enough) is not None


def test_insight_text_mentions_rate_and_reduction():
    records = [OutcomeRecord(1000.0, 600.0)] * 4
    text = insight_text(summarize_outcomes(records), facility_name="Lakeside General")
    assert "Lakeside General" in text
    assert "%" in text
    assert "4 resolved cases" in text


def test_zero_billed_rows_are_ignored():
    records = [
        OutcomeRecord(billed_amount=0.0, agreed_amount=0.0),
        OutcomeRecord(billed_amount=1000.0, agreed_amount=700.0),
    ]
    ins = summarize_outcomes(records)
    assert ins.n_cases == 1  # the $0-billed row is dropped


def test_days_to_resolve_averaged_when_present():
    records = [
        OutcomeRecord(1000.0, 600.0, days_to_resolve=10.0),
        OutcomeRecord(1000.0, 700.0, days_to_resolve=20.0),
        OutcomeRecord(1000.0, 800.0, days_to_resolve=None),
    ]
    ins = summarize_outcomes(records)
    assert ins.avg_days_to_resolve == 15.0  # mean of 10 and 20; None ignored


def test_eliminated_status_counts_as_reduced_even_without_amount_math():
    records = [
        OutcomeRecord(1000.0, agreed_amount=None, final_status="eliminated"),
        OutcomeRecord(1000.0, 900.0),
        OutcomeRecord(1000.0, 950.0),
    ]
    ins = summarize_outcomes(records)
    # All three usable; the eliminated one counts as reduced.
    assert ins.n_cases == 3
    assert ins.n_reduced >= 1
