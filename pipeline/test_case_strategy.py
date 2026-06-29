"""
Tests for case_strategy (P2: archetype router + triage interview). Pure logic,
no DB/LLM/network. Run with: python3 -m pytest test_case_strategy.py
"""

from case_strategy import (
    ARCH_CHARITY_CARE,
    ARCH_EMERGENCY_UNINSURED,
    ARCH_GENERAL,
    ARCH_INSURED_BALANCE,
    ARCH_INSURED_DENIAL,
    ARCH_SELF_PAY,
    ARCH_SURPRISE_OON,
    TriageFacts,
    build_strategy,
    classify_archetype,
    triage_questions,
)


# ============================================================
# Archetype classification
# ============================================================

def test_surprise_oon_takes_priority():
    facts = TriageFacts(coverage="insured", out_of_network=True, emergency=True)
    assert classify_archetype(facts) == ARCH_SURPRISE_OON


def test_self_pay_out_of_network_is_not_surprise_oon():
    # NSA surprise-billing protection runs through insurance; a self-pay patient
    # isn't in that lane.
    facts = TriageFacts(coverage="self_pay", out_of_network=True)
    assert classify_archetype(facts) != ARCH_SURPRISE_OON


def test_insured_denial_routes_to_appeal():
    facts = TriageFacts(coverage="insured", claim_denied=True)
    assert classify_archetype(facts) == ARCH_INSURED_DENIAL


def test_emergency_uninsured_archetype():
    facts = TriageFacts(coverage="self_pay", emergency=True)
    assert classify_archetype(facts) == ARCH_EMERGENCY_UNINSURED


def test_charity_eligible_routes_to_charity():
    facts = TriageFacts(coverage="self_pay", likely_charity_eligible=True)
    assert classify_archetype(facts) == ARCH_CHARITY_CARE


def test_self_pay_non_emergency_archetype():
    facts = TriageFacts(coverage="self_pay")
    assert classify_archetype(facts) == ARCH_SELF_PAY


def test_insured_balance_archetype():
    facts = TriageFacts(coverage="insured", claim_denied=False)
    assert classify_archetype(facts) == ARCH_INSURED_BALANCE


def test_unknown_facts_fall_back_to_general():
    assert classify_archetype(TriageFacts()) == ARCH_GENERAL


# ============================================================
# Step assembly
# ============================================================

def test_itemized_step_leads_when_no_line_items():
    facts = TriageFacts(coverage="self_pay", has_line_items=False)
    strategy = build_strategy(facts)
    assert strategy.steps[0].key == "get_itemized"


def test_itemized_step_skipped_when_line_items_present():
    facts = TriageFacts(coverage="self_pay", has_line_items=True)
    strategy = build_strategy(facts)
    assert all(s.key != "get_itemized" for s in strategy.steps)


def test_surprise_oon_strategy_includes_nsa_step():
    facts = TriageFacts(coverage="insured", out_of_network=True, has_line_items=True)
    strategy = build_strategy(facts)
    assert strategy.archetype == ARCH_SURPRISE_OON
    assert any(s.key == "nsa_dispute" for s in strategy.steps)


def test_insured_denial_strategy_appeals_and_skips_charity():
    facts = TriageFacts(coverage="insured", claim_denied=True, has_line_items=True)
    strategy = build_strategy(facts)
    keys = [s.key for s in strategy.steps]
    assert "insurer_appeal" in keys
    assert "charity_application" not in keys


def test_charity_strategy_includes_application_step():
    facts = TriageFacts(coverage="self_pay", likely_charity_eligible=True, has_line_items=True)
    strategy = build_strategy(facts)
    assert any(s.key == "charity_application" for s in strategy.steps)


def test_strategy_has_headline_for_every_archetype():
    for facts in [
        TriageFacts(coverage="insured", out_of_network=True),
        TriageFacts(coverage="insured", claim_denied=True),
        TriageFacts(coverage="self_pay", emergency=True),
        TriageFacts(coverage="self_pay", likely_charity_eligible=True),
        TriageFacts(coverage="self_pay"),
        TriageFacts(coverage="insured"),
        TriageFacts(),
    ]:
        strategy = build_strategy(facts)
        assert strategy.headline
        assert len(strategy.steps) >= 2


# ============================================================
# Triage interview
# ============================================================

def test_triage_asks_coverage_first_when_unknown():
    questions = triage_questions(TriageFacts())
    assert questions[0].field_name == "coverage"


def test_triage_skips_oon_for_self_pay():
    questions = triage_questions(TriageFacts(coverage="self_pay"))
    assert all(q.field_name != "out_of_network" for q in questions)


def test_triage_asks_gfe_only_for_self_pay():
    insured = triage_questions(TriageFacts(coverage="insured"))
    self_pay = triage_questions(TriageFacts(coverage="self_pay"), limit=10)
    assert all(q.field_name != "good_faith_estimate" for q in insured)
    assert any(q.field_name == "good_faith_estimate" for q in self_pay)


def test_triage_respects_limit():
    questions = triage_questions(TriageFacts(), limit=2)
    assert len(questions) == 2


def test_triage_skips_already_known_facts():
    facts = TriageFacts(
        coverage="self_pay", emergency=True, received_itemized=True,
        good_faith_estimate=True, in_collections=False,
    )
    # Everything relevant is known -> no questions left.
    assert triage_questions(facts) == []
