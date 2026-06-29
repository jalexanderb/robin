"""
Tests for P5: state-law leverage layer. Pure logic, no DB/LLM/network.
Run with: python3 -m pytest test_state_leverage.py
"""

from state_leverage import (
    build_state_leverage,
    extract_state_from_address,
    STATE_CHARITY_LAWS,
)


# ============================================================
# State extraction from address
# ============================================================

def test_extract_state_from_typical_address():
    assert extract_state_from_address("123 Main St, Springfield, CA 95814") == "CA"


def test_extract_state_without_zip():
    assert extract_state_from_address("500 Hospital Way, Seattle, WA") == "WA"


def test_extract_state_returns_none_when_absent():
    assert extract_state_from_address("123 Main St") is None
    assert extract_state_from_address(None) is None


def test_extract_state_ignores_non_state_two_letter_tokens():
    # "St" is not a state; "NY" is.
    assert extract_state_from_address("10 St Marks Pl, New York, NY 10003") == "NY"


# ============================================================
# Charity-care arguments
# ============================================================

def test_charity_argument_present_for_supported_state():
    args = build_state_leverage("CA", is_hospital=True)
    assert len(args) == 1
    assert "Fair Pricing Act" in args[0].basis
    assert "screen" in args[0].text.lower()


def test_self_pay_adds_emphasis():
    args = build_state_leverage("CA", self_pay=True)
    assert "self-pay" in args[0].text.lower()


def test_unsupported_state_returns_empty():
    # A valid state with no curated law on file -> no arguments (never invent one).
    assert build_state_leverage("WY") == []


def test_unknown_state_code_returns_empty():
    assert build_state_leverage("ZZ") == []
    assert build_state_leverage(None) == []


def test_non_hospital_skips_charity_argument():
    args = build_state_leverage("CA", is_hospital=False)
    assert all("Fair Pricing" not in a.basis for a in args)


# ============================================================
# Medical-debt credit-reporting protection
# ============================================================

def test_credit_reporting_argument_only_when_in_collections():
    without = build_state_leverage("NY", in_collections=None)
    with_collections = build_state_leverage("NY", in_collections=True)
    assert all("credit" not in a.basis.lower() for a in without)
    assert any("credit" in a.basis.lower() for a in with_collections)


def test_credit_reporting_argument_text_mentions_reporting():
    args = build_state_leverage("CO", in_collections=True)
    credit = [a for a in args if "credit" in a.basis.lower()]
    assert credit
    assert "credit reporting" in credit[0].text.lower() or "credit reporting agencies" in credit[0].text.lower()


def test_every_curated_charity_law_builds_a_valid_argument():
    for st in STATE_CHARITY_LAWS:
        args = build_state_leverage(st, self_pay=True)
        assert args and args[0].basis and args[0].text
