"""
RobinHealth: state-law leverage arguments.

The federal arguments in legal_leverage.py (No Surprises Act, 501(r), price
transparency) are the floor. Many states layer *stronger* protections on top --
statutory charity-care/fair-pricing programs that cap what eligible patients can
be charged, and bans on reporting medical debt to credit bureaus. Where they
apply, these are frequently more actionable than the federal arguments.

Same shape and discipline as legal_leverage: pure logic + a curated, conservative
reference table, returning legal_leverage.LeverageArgument objects so state and
federal arguments compose in the same letter. Every entry is framed as a request
to apply a statutory protection / confirm compliance, never a threat.

IMPORTANT: this is a deliberately conservative, curated subset of the
highest-impact state laws, not exhaustive legal research, and it is not legal
advice. The phrasing is general enough to remain accurate (e.g. "generally at or
below X% of the Federal Poverty Level"); the precise eligibility thresholds and
procedures are set by each statute and its regulations and change over time.
"""
from __future__ import annotations

import re

from legal_leverage import LeverageArgument


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


# Curated state charity-care / fair-pricing statutes (label, provider-facing text).
STATE_CHARITY_LAWS: dict[str, tuple[str, str]] = {
    "CA": (
        "California Hospital Fair Pricing Act (Cal. Health & Safety Code § 127400 et seq.)",
        "Under California's Hospital Fair Pricing Act, hospitals must offer charity "
        "care and discount-payment programs to eligible patients (generally those at "
        "or below 400% of the Federal Poverty Level) and must screen for eligibility "
        "before referring an account to collections.",
    ),
    "NY": (
        "New York Hospital Financial Assistance Law (N.Y. Public Health Law § 2807-k)",
        "Under New York's Hospital Financial Assistance Law, hospitals receiving "
        "indigent-care funds must offer a financial-assistance program that limits "
        "charges for eligible patients (generally those at or below 300% of the "
        "Federal Poverty Level) and caps collection actions.",
    ),
    "NJ": (
        "New Jersey Hospital Care Payment Assistance / Charity Care (N.J.S.A. 26:2H-18.64)",
        "Under New Jersey's Hospital Care Payment Assistance program, eligible "
        "low-income patients receive free or reduced-charge care, and patients "
        "between roughly 300% and 500% of the Federal Poverty Level may not be "
        "charged more than 115% of the Medicare rate.",
    ),
    "WA": (
        "Washington Charity Care Act (RCW 70.170.060)",
        "Under Washington's Charity Care Act, hospitals must provide free care to "
        "patients generally at or below 300% of the Federal Poverty Level and "
        "discounted care above that, and may not pursue collection before "
        "determining charity-care eligibility.",
    ),
    "CO": (
        "Colorado Hospital Discounted Care (C.R.S. § 25-3-112)",
        "Under Colorado's Hospital Discounted Care law, hospitals must screen "
        "uninsured and low-income patients for public-program and discounted-care "
        "eligibility and limit charges and collection activity for those who qualify.",
    ),
    "IL": (
        "Illinois Hospital Uninsured Patient Discount Act (210 ILCS 89) and Fair Patient Billing Act (210 ILCS 88)",
        "Under Illinois law, hospitals must provide discounts to uninsured patients "
        "(generally below 600% of the Federal Poverty Level), cap the amount such "
        "patients can be charged, and follow fair-billing and collection procedures.",
    ),
    "MD": (
        "Maryland hospital financial-assistance requirements (Md. Code, Health-General § 19-214.1)",
        "Maryland hospitals must maintain a financial-assistance policy with "
        "income-based free and reduced-cost care and offer income-based payment "
        "plans, and are subject to Maryland's all-payer rate-setting system, which "
        "regulates hospital charges.",
    ),
}


# Curated states with statutory limits on reporting medical debt to consumer
# credit agencies (provider-facing label).
STATE_MEDICAL_DEBT_CREDIT_PROTECTIONS: dict[str, str] = {
    "CA": "California's Medical Debt Relief Act (SB 1061)",
    "CO": "Colorado's medical-debt credit-reporting restrictions (SB 23-093)",
    "NY": "New York's Fair Medical Debt Reporting Act",
    "NJ": "New Jersey's Louisa Carman Medical Debt Relief Act",
    "IL": "Illinois's Medical Debt Relief Act",
}


_STATE_IN_ADDRESS = re.compile(r"\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?\s*$")


def extract_state_from_address(address: str | None) -> str | None:
    """
    Best-effort: pull a US two-letter state code from the tail of an address
    string (optionally before a ZIP). Returns None if no valid state is found --
    callers then simply skip state-specific arguments rather than guessing.
    """
    if not address:
        return None
    # Try the regex on the trimmed address first (state, optional ZIP, at end).
    m = _STATE_IN_ADDRESS.search(address.strip().upper())
    if m and m.group(1) in US_STATES:
        return m.group(1)
    # Fallback: scan *whole-word* two-letter tokens for a valid state code (last
    # match wins). The \b...\b bounds matter -- without them "Main" yields the
    # spurious codes "MA"/"IN".
    found = None
    for tok in re.findall(r"\b[A-Za-z]{2}\b", address.upper()):
        if tok in US_STATES:
            found = tok
    return found


def build_state_leverage(
    state: str | None,
    *,
    is_hospital: bool = True,
    self_pay: bool | None = None,
    in_collections: bool | None = None,
) -> list[LeverageArgument]:
    """
    Return the applicable state-law arguments for a given two-letter state.

    Unknown / unsupported states return [] (we never assert a law we don't have
    on file). The charity-care argument is included for hospital bills because a
    "screen this account for eligibility under state law" request is always a
    valid ask; the credit-reporting argument is gated on the bill being in
    collections, where it actually bites.
    """
    if not state:
        return []
    st = state.strip().upper()
    if st not in US_STATES:
        return []

    args: list[LeverageArgument] = []

    if is_hospital and st in STATE_CHARITY_LAWS:
        label, text = STATE_CHARITY_LAWS[st]
        emphasis = (
            " As a self-pay/uninsured patient, the patient is squarely within the "
            "population this law is designed to protect."
            if self_pay else ""
        )
        args.append(LeverageArgument(
            basis=label,
            text=(
                f"{text} We request that this account be screened for eligibility "
                f"under {label} and that any applicable statutory charge limit be "
                f"applied.{emphasis}"
            ),
        ))

    if in_collections and st in STATE_MEDICAL_DEBT_CREDIT_PROTECTIONS:
        law = STATE_MEDICAL_DEBT_CREDIT_PROTECTIONS[st]
        args.append(LeverageArgument(
            basis=f"{law} — medical-debt credit reporting",
            text=(
                f"Please note that {law} restricts reporting this medical debt to "
                f"consumer credit reporting agencies. We request that no such "
                f"reporting occur while this account is in dispute and that any "
                f"prior reporting be corrected as required by law."
            ),
        ))

    return args
