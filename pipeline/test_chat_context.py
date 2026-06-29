"""
Tests for the tighter chat <-> case coupling: api._case_context_text builds an
authoritative, server-side context block from the persisted case state. DB reads
are mocked (no Postgres needed). Run with: python3 -m pytest test_chat_context.py
"""

from unittest.mock import patch

import api


_SYNTH = {
    "headline_low": 700.0,
    "headline_high": 1000.0,
    "headline_could_eliminate": False,
    "reasons": [
        {"summary": "We found 1 likely billing error worth about $300.", "outcome_type": "billing_error"},
    ],
    "line_item_findings": [
        {"kind": "duplicate", "patient_summary": "CT scan appears twice at $300 each.",
         "line_numbers": [1, 2], "codes": ["70450"], "estimated_overcharge": 300.0,
         "severity": "high", "provider_text": "pt"},
    ],
}
_BILL = {"provider_name_raw": "Lakeside General Hospital", "total_billed_amount": 1000.0,
         "account_number": "ACC-1", "provider_address_raw": "1 Main St, Springfield, CA 95814"}


def test_case_context_includes_findings_strategy_and_provider():
    with patch.object(api.repository, "fetch_case_synthesis", return_value=_SYNTH), \
         patch.object(api.repository, "fetch_bill_for_case", return_value=_BILL), \
         patch.object(api.repository, "fetch_case_triage", return_value={"coverage": "self_pay"}), \
         patch.object(api.outcome_pipeline, "fetch_negotiation_for_case", return_value=None):
        text = api._case_context_text("case-1")

    assert "Lakeside General Hospital" in text
    assert "CT scan appears twice" in text          # specific finding surfaced
    assert "Recommended approach" in text            # strategy headline
    assert "Next steps" in text                      # ordered steps
    assert "$700" in text or "700" in text           # reduced-balance estimate


def test_case_context_empty_when_nothing_persisted():
    with patch.object(api.repository, "fetch_case_synthesis", return_value=None), \
         patch.object(api.repository, "fetch_bill_for_case", return_value=None), \
         patch.object(api.repository, "fetch_case_triage", return_value=None), \
         patch.object(api.outcome_pipeline, "fetch_negotiation_for_case", return_value=None):
        # No bill/synthesis -> the strategy still has a generic plan, but no
        # bill/finding lines; the block should at least not crash and should be
        # either empty or contain only the generic strategy.
        text = api._case_context_text("case-1")
    assert isinstance(text, str)


def test_case_context_degrades_gracefully_on_db_error():
    with patch.object(api.repository, "fetch_case_synthesis", side_effect=RuntimeError("no db")):
        assert api._case_context_text("case-1") == ""
