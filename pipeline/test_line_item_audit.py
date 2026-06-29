"""
Tests for line_item_audit (P1: line-item billing-error detection) and its
integration into synthesis.py and letter_pipeline.py.

No DB, no LLM, no network -- the audit is pure deterministic logic, the same
way legal_leverage is. Run with: python3 -m pytest test_line_item_audit.py
"""

import os
import tempfile
from dataclasses import asdict

from bill_pipeline import ExtractedLineItem
import line_item_audit
from line_item_audit import (
    KIND_DUPLICATE,
    KIND_EXCESS_UNITS,
    KIND_UNBUNDLING,
    audit_line_items,
    finding_from_dict,
    load_mue_from_csv,
    load_ncci_ptp_from_csv,
    total_estimated_overcharge,
    LineItemFinding,
)


def _li(line_number, description, code, units, amount, code_type="cpt"):
    return ExtractedLineItem(
        line_number=line_number, description=description, procedure_code=code,
        code_type=code_type, units=units, billed_amount=amount,
    )


# ============================================================
# Duplicate detection (no reference data needed)
# ============================================================

def test_audit_detects_exact_duplicate_by_code_and_amount():
    items = [
        _li(1, "CT scan head", "70450", 1, 1200.0),
        _li(2, "CT scan head", "70450", 1, 1200.0),
    ]
    findings = audit_line_items(items)
    dupes = [f for f in findings if f.kind == KIND_DUPLICATE]
    assert len(dupes) == 1
    assert dupes[0].estimated_overcharge == 1200.0
    assert dupes[0].severity == "high"
    assert set(dupes[0].line_numbers) == {1, 2}
    assert "70450" in dupes[0].codes


def test_audit_no_duplicate_when_amounts_differ():
    # Same code but different amounts -> not flagged (conservative).
    items = [
        _li(1, "Office visit", "99213", 1, 150.0),
        _li(2, "Office visit", "99213", 1, 95.0),
    ]
    assert [f for f in audit_line_items(items) if f.kind == KIND_DUPLICATE] == []


def test_audit_duplicate_uses_description_when_no_code():
    items = [
        _li(1, "Sterile supply kit", None, 1, 75.0, code_type=None),
        _li(2, "Sterile supply kit", None, 1, 75.0, code_type=None),
        _li(3, "Sterile supply kit", None, 1, 75.0, code_type=None),
    ]
    dupes = [f for f in audit_line_items(items) if f.kind == KIND_DUPLICATE]
    assert len(dupes) == 1
    # Three occurrences -> two are extra -> 2 * 75.
    assert dupes[0].estimated_overcharge == 150.0


# ============================================================
# Unbundling (NCCI PTP edits)
# ============================================================

def test_audit_detects_unbundling_from_embedded_seed():
    # 80053 (CMP) and 80048 (BMP) are an embedded NCCI pair; 80048 is the
    # bundled component and should be flagged as the overcharge.
    items = [
        _li(1, "Comprehensive metabolic panel", "80053", 1, 120.0),
        _li(2, "Basic metabolic panel", "80048", 1, 80.0),
    ]
    unb = [f for f in audit_line_items(items) if f.kind == KIND_UNBUNDLING]
    assert len(unb) == 1
    assert unb[0].codes == ["80053", "80048"]
    assert unb[0].estimated_overcharge == 80.0  # the component line
    assert "Correct Coding Initiative" in unb[0].provider_text


def test_audit_unbundling_indicator_0_is_high_severity():
    items = [
        _li(1, "Procedure A", "11111", 1, 500.0),
        _li(2, "Procedure B", "22222", 1, 300.0),
    ]
    custom_pairs = [("11111", "22222", 0)]  # 0 == never separately payable
    findings = audit_line_items(items, ncci_pairs=custom_pairs, mue_table={})
    unb = [f for f in findings if f.kind == KIND_UNBUNDLING]
    assert len(unb) == 1
    assert unb[0].severity == "high"
    assert "never separately payable" in unb[0].provider_text


def test_audit_unbundling_indicator_9_is_ignored():
    items = [
        _li(1, "Procedure A", "11111", 1, 500.0),
        _li(2, "Procedure B", "22222", 1, 300.0),
    ]
    findings = audit_line_items(items, ncci_pairs=[("11111", "22222", 9)], mue_table={})
    assert [f for f in findings if f.kind == KIND_UNBUNDLING] == []


# ============================================================
# Excess units (MUE)
# ============================================================

def test_audit_detects_excess_units():
    # 10 units of a code whose MUE is 1 -> 9 excess units * (1000/10) = 900.
    items = [_li(1, "Lab panel", "85025", 10, 1000.0)]
    findings = audit_line_items(items)
    mue = [f for f in findings if f.kind == KIND_EXCESS_UNITS]
    assert len(mue) == 1
    assert mue[0].estimated_overcharge == 900.0
    assert mue[0].severity == "high"


def test_audit_no_excess_when_within_mue():
    items = [_li(1, "EKG", "93000", 1, 200.0)]  # MUE for 93000 is 1
    assert [f for f in audit_line_items(items) if f.kind == KIND_EXCESS_UNITS] == []


# ============================================================
# Orchestration
# ============================================================

def test_audit_empty_input_returns_empty():
    assert audit_line_items([]) == []


def test_audit_ranks_findings_by_overcharge_desc():
    items = [
        _li(1, "Small dupe", "10000", 1, 50.0),
        _li(2, "Small dupe", "10000", 1, 50.0),
        _li(3, "Big dupe", "20000", 1, 900.0),
        _li(4, "Big dupe", "20000", 1, 900.0),
    ]
    findings = audit_line_items(items)
    overcharges = [f.estimated_overcharge for f in findings]
    assert overcharges == sorted(overcharges, reverse=True)
    assert findings[0].estimated_overcharge == 900.0


def test_total_estimated_overcharge_sums_quantified_findings():
    findings = [
        LineItemFinding(KIND_DUPLICATE, "high", [1], ["x"], 100.0, "", ""),
        LineItemFinding(KIND_UNBUNDLING, "medium", [2], ["y"], 50.0, "", ""),
        LineItemFinding(KIND_DUPLICATE, "high", [3], ["z"], None, "", ""),
    ]
    assert total_estimated_overcharge(findings) == 150.0


def test_finding_from_dict_roundtrips():
    original = LineItemFinding(
        kind=KIND_UNBUNDLING, severity="medium", line_numbers=[1, 2],
        codes=["80053", "80048"], estimated_overcharge=80.0,
        patient_summary="ps", provider_text="pt",
    )
    restored = finding_from_dict(asdict(original))
    assert restored == original


# ============================================================
# Reference-data loaders (degrade gracefully; merge files)
# ============================================================

def test_loaders_degrade_to_embedded_seed_when_file_missing():
    pairs = load_ncci_ptp_from_csv("/nonexistent/ncci.csv")
    mue = load_mue_from_csv("/nonexistent/mue.csv")
    assert len(pairs) >= 1          # embedded seed survives
    assert "85025" in mue           # embedded MUE survives


def test_loaders_merge_file_rows():
    ncci_csv = "column_1,column_2,modifier_indicator\nAAAAA,BBBBB,0\n"
    mue_csv = "code,max_units\nAAAAA,3\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f1:
        f1.write(ncci_csv)
        ncci_path = f1.name
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f2:
        f2.write(mue_csv)
        mue_path = f2.name
    try:
        pairs = load_ncci_ptp_from_csv(ncci_path)
        mue = load_mue_from_csv(mue_path)
        assert ("AAAAA", "BBBBB", 0) in pairs
        assert mue["AAAAA"] == 3
    finally:
        os.unlink(ncci_path)
        os.unlink(mue_path)


# ============================================================
# Synthesis integration
# ============================================================

def _minimal_synthesis_input(billed):
    from synthesis import SynthesisInput
    return SynthesisInput(
        billed_amount=billed, pricing=None, eligibility_tiers=[],
        household_income=None, household_size=None, compliance_findings=[],
    )


def test_synthesize_adds_billing_error_reason_ranked_first():
    from synthesis import synthesize, OutcomeType
    findings = [
        LineItemFinding(KIND_DUPLICATE, "high", [1, 2], ["70450"], 200.0,
                        "dup summary", "dup provider text"),
        LineItemFinding(KIND_UNBUNDLING, "medium", [3, 4], ["80053", "80048"], 100.0,
                        "unb summary", "unb provider text"),
    ]
    result = synthesize(_minimal_synthesis_input(1000.0), line_item_findings=findings)

    assert result.reasons[0].outcome_type == OutcomeType.BILLING_ERROR
    # headline reflects total removable overcharge (200 + 100 = 300).
    assert result.headline_low == 700.0
    assert result.headline_high == 1000.0
    # individual findings are carried through for the letter.
    assert len(result.line_item_findings) == 2


def test_synthesize_without_findings_has_no_billing_error_reason():
    from synthesis import synthesize, OutcomeType
    result = synthesize(_minimal_synthesis_input(1000.0))
    assert all(r.outcome_type != OutcomeType.BILLING_ERROR for r in result.reasons)
    assert result.line_item_findings == []


def test_synthesis_from_dict_roundtrips_findings():
    from synthesis import synthesize, synthesis_from_dict, OutcomeType
    findings = [
        LineItemFinding(KIND_DUPLICATE, "high", [1, 2], ["70450"], 200.0,
                        "dup summary", "dup provider text"),
    ]
    result = synthesize(_minimal_synthesis_input(1000.0), line_item_findings=findings)
    restored = synthesis_from_dict(asdict(result))
    assert len(restored.line_item_findings) == 1
    assert restored.line_item_findings[0].provider_text == "dup provider text"
    assert restored.reasons[0].outcome_type == OutcomeType.BILLING_ERROR


# ============================================================
# Letter integration
# ============================================================

def _recipient():
    from letter_pipeline import RecipientInfo
    return RecipientInfo(
        facility_name="Lakeside General Hospital",
        facility_address="123 Main St, Springfield, CA",
        patient_name="Jane Doe", account_number="ACC-001",
        date_of_service="2026-03-14",
    )


def test_assemble_context_leads_with_line_item_findings():
    from synthesis import SynthesisResult, Reason, OutcomeType, DEFAULT_BETA_CAVEAT
    from letter_pipeline import assemble_context

    finding = LineItemFinding(
        KIND_DUPLICATE, "high", [1, 2], ["70450"], 200.0,
        "dup summary", "DUPLICATE PROVIDER TEXT",
    )
    # Result carries both the aggregate BILLING_ERROR reason AND a partial
    # reduction reason; the letter should itemize the finding (not the
    # aggregate) and lead with it.
    be_reason = Reason(OutcomeType.BILLING_ERROR, "aggregate summary", 800.0, 1000.0, [])
    pr_reason = Reason(OutcomeType.PARTIAL_REDUCTION, "discount", 600.0, 1000.0, [])
    result = SynthesisResult(
        headline_low=600.0, headline_high=1000.0, headline_could_eliminate=False,
        reasons=[be_reason, pr_reason], follow_up_questions=[],
        beta_caveat=DEFAULT_BETA_CAVEAT, line_item_findings=[finding],
    )

    context = assemble_context(result, _recipient(), billed_amount=1000.0)

    # Finding leads.
    assert context.arguments[0].text == "DUPLICATE PROVIDER TEXT"
    assert context.arguments[0].outcome_type == OutcomeType.BILLING_ERROR
    # The aggregate BILLING_ERROR reason is NOT duplicated as its own argument.
    aggregate_texts = [a.text for a in context.arguments if a.text == "aggregate summary"]
    assert aggregate_texts == []
    # The partial-reduction reason still becomes an argument.
    assert any("600.00" in a.text for a in context.arguments)


def test_assemble_context_folds_overcharge_into_requested_amount():
    from synthesis import SynthesisResult, DEFAULT_BETA_CAVEAT
    from letter_pipeline import assemble_context

    finding = LineItemFinding(
        KIND_DUPLICATE, "high", [1, 2], ["70450"], 300.0, "ps", "pt",
    )
    result = SynthesisResult(
        headline_low=700.0, headline_high=1000.0, headline_could_eliminate=False,
        reasons=[], follow_up_questions=[], beta_caveat=DEFAULT_BETA_CAVEAT,
        line_item_findings=[finding],
    )
    context = assemble_context(result, _recipient(), billed_amount=1000.0)
    # Bottom-line ask = billed - total overcharge = 1000 - 300.
    assert context.requested_amount == 700.0
