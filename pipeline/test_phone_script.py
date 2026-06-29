"""
Tests for the phone-call script generator (Part 2). Pure logic.
Run with: python3 -m pytest test_phone_script.py
"""

from case_strategy import (
    ARCH_CHARITY_CARE,
    ARCH_INSURED_DENIAL,
    ARCH_SELF_PAY,
    ARCH_SURPRISE_OON,
)
from line_item_audit import LineItemFinding, KIND_DUPLICATE
from phone_script import build_phone_script


def _all_text(script) -> str:
    return " ".join(line for sec in script.sections for line in sec.lines)


def test_script_always_includes_protective_sections():
    script = build_phone_script(ARCH_SELF_PAY, facility_name="Lakeside", account_number="A1")
    titles = [s.title for s in script.sections]
    assert "What NOT to do" in titles
    assert "Get it in writing" in titles
    assert "If you get stuck" in titles


def test_opening_uses_account_and_facility():
    script = build_phone_script(ARCH_SELF_PAY, facility_name="Lakeside General", account_number="ACC-9")
    text = _all_text(script)
    assert "Lakeside General" in text
    assert "ACC-9" in text


def test_surprise_oon_script_mentions_no_surprises_act():
    script = build_phone_script(ARCH_SURPRISE_OON)
    assert "No Surprises Act" in _all_text(script)
    assert "supervisor" in script.who_to_ask_for.lower()


def test_insured_denial_directs_to_insurer():
    script = build_phone_script(ARCH_INSURED_DENIAL)
    assert "insur" in script.who_to_ask_for.lower()
    assert "appeal" in _all_text(script).lower()


def test_charity_script_targets_financial_assistance_office():
    script = build_phone_script(ARCH_CHARITY_CARE)
    assert "financial assistance" in script.who_to_ask_for.lower() or \
           "charity" in script.who_to_ask_for.lower()
    assert "hold" in _all_text(script).lower()  # ask to pause collections


def test_findings_become_spoken_talking_points():
    findings = [
        LineItemFinding(KIND_DUPLICATE, "high", [14, 22], ["70450"], 1200.0, "ps", "pt"),
    ]
    script = build_phone_script(ARCH_SELF_PAY, findings=findings)
    talking = [s for s in script.sections if s.title == "The specific charges to question"]
    assert talking
    assert "line 14, 22" in _all_text(script).lower()


def test_no_findings_omits_the_specific_charges_section():
    script = build_phone_script(ARCH_SELF_PAY, findings=[])
    assert all(s.title != "The specific charges to question" for s in script.sections)


def test_never_do_section_warns_about_paying_under_pressure():
    script = build_phone_script(ARCH_SELF_PAY)
    never = [s for s in script.sections if s.title == "What NOT to do"][0]
    joined = " ".join(never.lines).lower()
    assert "card" in joined or "pay the full" in joined
