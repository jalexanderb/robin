"""
RobinHealth: synthesis layer.

Translates backend findings (fap_compliance_findings, fap_eligibility_tiers,
pricing benchmark deltas) into the plain-language, dollar-impact-ranked
output shown to users: a headline estimate, the top reasons behind it, and
any follow-up questions needed to refine the estimate.

This layer never surfaces requirement_code, severity enums, or legal
terminology directly -- those remain available via source_requirement_codes
for an optional "tell me more" expansion. Beta/estimate framing is enforced
structurally (every SynthesisResult carries a beta_caveat), not left to
prompt discipline alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from compliance_checklist import CHECKLIST_BY_CODE, ComplianceStatus
from fap_pipeline import ComplianceFinding, EligibilityTier
from fpl_lookup import income_as_fpl_percent


class OutcomeType(str, Enum):
    FULL_ELIMINATION = "full_elimination"
    PARTIAL_REDUCTION = "partial_reduction"
    PROCEDURAL_LEVERAGE = "procedural_leverage"  # no dollar estimate yet, but relevant


@dataclass
class PricingBenchmark:
    billed_amount: float
    medicare_rate: float | None
    fair_price_estimate: float | None


@dataclass
class SynthesisInput:
    billed_amount: float
    pricing: PricingBenchmark | None
    eligibility_tiers: list[EligibilityTier]
    household_income: float | None
    household_size: int | None
    compliance_findings: list[ComplianceFinding]
    state: str | None = None  # two-letter code; only changes the FPL table for AK/HI
    # EOB-derived fields -- None if no EOB has been uploaded yet.
    # allowed_amount_total: insurer's contracted rate across matched lines.
    #   Used in the negotiation argument: "your insurer pays $X; please
    #   extend the same rate to me."
    # patient_responsibility_total: what the patient actually owes per
    #   the EOB. This is the real target for negotiation, not billed_amount.
    allowed_amount_total: float | None = None
    patient_responsibility_total: float | None = None
    # MRF-derived fields -- set when a hospital's own published price file has
    # been fetched and parsed. cash_price_total is the discounted self-pay rate
    # (strongest negotiation anchor since it's the hospital's own published rate
    # for uninsured/self-pay patients). mrf_status conveys the MRF fetch result
    # to downstream users, including negative results like mrf_unreachable.
    mrf_cash_price_total: float | None = None
    mrf_min_negotiated_total: float | None = None
    mrf_max_negotiated_total: float | None = None
    mrf_status: str | None = None        # mrf_status enum value, for user messaging
    mrf_status_detail: str | None = None # plain-English user-facing explanation


@dataclass
class Reason:
    outcome_type: OutcomeType
    summary: str  # plain-language, dollar-impact framed
    estimated_low: float | None
    estimated_high: float | None
    source_requirement_codes: list[str]  # for optional "tell me more" expansion


@dataclass
class FollowUpQuestion:
    prompt: str  # plain-language ask
    field_name: str  # e.g. 'household_income', 'household_size'


@dataclass
class SynthesisResult:
    headline_low: float | None
    headline_high: float | None
    headline_could_eliminate: bool
    reasons: list[Reason]
    follow_up_questions: list[FollowUpQuestion]
    beta_caveat: str


DEFAULT_BETA_CAVEAT = (
    "This is our AI's best estimate based on the documents you've "
    "provided. RobinHealth is in beta -- actual results depend on your "
    "specific situation and how the provider responds. Please review "
    "these findings before we move forward."
)


# ============================================================
# Step 1: determine which follow-up questions are needed
# ============================================================

def determine_follow_ups(input_data: SynthesisInput) -> list[FollowUpQuestion]:
    """
    Only ask for what's needed for *this* case. Charity-care eligibility
    requires income/household size; only ask if income-based tiers exist
    and the values aren't already known.
    """
    questions: list[FollowUpQuestion] = []

    has_income_based_tiers = any(
        tier.fpl_min_pct is not None or tier.fpl_max_pct is not None
        for tier in input_data.eligibility_tiers
    )

    if has_income_based_tiers and input_data.household_income is None:
        questions.append(FollowUpQuestion(
            prompt=(
                "Roughly what's your household's yearly income? This "
                "helps us check whether you qualify for free or "
                "reduced-cost care -- a lot of people do without "
                "realizing it."
            ),
            field_name="household_income",
        ))

    if has_income_based_tiers and input_data.household_size is None:
        questions.append(FollowUpQuestion(
            prompt="How many people are in your household?",
            field_name="household_size",
        ))

    return questions


# ============================================================
# Step 2: map eligibility tiers -> potential outcome (if income known)
# ============================================================

def find_matching_tier(
    tiers: list[EligibilityTier], fpl_pct: float
) -> EligibilityTier | None:
    """
    Find the tier whose [fpl_min_pct, fpl_max_pct] range contains fpl_pct.
    Open-ended bounds (None) are treated as 0 / +inf.
    """
    for tier in sorted(tiers, key=lambda t: t.tier_order):
        lo = tier.fpl_min_pct if tier.fpl_min_pct is not None else 0
        hi = tier.fpl_max_pct if tier.fpl_max_pct is not None else float("inf")
        if lo <= fpl_pct <= hi:
            return tier
    return None


def estimate_eligibility_outcome(input_data: SynthesisInput) -> Reason | None:
    if input_data.household_income is None or input_data.household_size is None:
        return None
    if not input_data.eligibility_tiers:
        return None

    fpl_pct = income_as_fpl_percent(
        input_data.household_income, input_data.household_size, input_data.state
    )
    tier = find_matching_tier(input_data.eligibility_tiers, fpl_pct)
    if tier is None:
        return None

    # Use patient_responsibility as the anchor amount when an EOB is
    # available -- that's what the patient actually owes, not the inflated
    # billed amount. Fall back to billed_amount when no EOB.
    billed = input_data.patient_responsibility_total or input_data.billed_amount

    if tier.discount_type == "full_charity_care":
        return Reason(
            outcome_type=OutcomeType.FULL_ELIMINATION,
            summary=(
                f"Your household income is about {fpl_pct:.0f}% of the "
                f"federal poverty level, which appears to qualify for full "
                f"charity care under this hospital's financial assistance "
                f"policy."
            ),
            estimated_low=0,
            estimated_high=0,
            source_requirement_codes=[],
        )

    if tier.discount_type in ("percentage_discount", "sliding_scale"):
        discount_pct = tier.discount_value or 0
        reduced = billed * (1 - discount_pct / 100)
        return Reason(
            outcome_type=OutcomeType.PARTIAL_REDUCTION,
            summary=(
                f"Your household income is about {fpl_pct:.0f}% of the "
                f"federal poverty level, which appears to qualify for a "
                f"{discount_pct:.0f}% discount under this hospital's "
                f"financial assistance policy."
            ),
            estimated_low=round(reduced, 2),
            estimated_high=billed,
            source_requirement_codes=[],
        )

    if tier.discount_type == "flat_cap":
        cap = tier.discount_value if tier.discount_value is not None else billed
        capped = min(cap, billed)
        return Reason(
            outcome_type=OutcomeType.PARTIAL_REDUCTION,
            summary=(
                f"Your household income is about {fpl_pct:.0f}% of the "
                f"federal poverty level, which appears to cap what you can "
                f"be charged at around ${capped:,.0f} under this hospital's "
                f"financial assistance policy."
            ),
            estimated_low=round(capped, 2),
            estimated_high=billed,
            source_requirement_codes=[],
        )

    return None


# ============================================================
# Step 3: map compliance findings -> reasons
# ============================================================

def findings_to_reasons(
    findings: list[ComplianceFinding],
    pricing: PricingBenchmark | None,
) -> list[Reason]:
    """
    Translate non-eligibility findings (pricing deltas, procedural
    violations) into Reason objects. Severity from the checklist informs
    ordering but does not solely determine it -- final ranking (by
    estimated dollar impact where available) happens in synthesize().
    """
    reasons: list[Reason] = []

    if pricing and pricing.medicare_rate is not None:
        if pricing.medicare_rate > 0:
            delta_pct = (
                (pricing.billed_amount - pricing.medicare_rate)
                / pricing.medicare_rate
                * 100
            )
            if delta_pct > 50:  # threshold for surfacing; tune empirically
                reasons.append(Reason(
                    outcome_type=OutcomeType.PARTIAL_REDUCTION,
                    summary=(
                        f"This charge is roughly {delta_pct:.0f}% higher than "
                        f"what Medicare pays for the same service -- which is "
                        f"often a starting point for negotiation."
                    ),
                    estimated_low=pricing.medicare_rate,
                    estimated_high=pricing.billed_amount,
                    source_requirement_codes=[],
                ))
        elif pricing.medicare_rate == 0 and pricing.billed_amount > 0:
            # `if pricing.medicare_rate:` (falsy-zero) used to silently
            # skip this -- a real possible value, and arguably the most
            # extreme overcharge case there is (any positive charge
            # against a $0 Medicare rate), not something to drop quietly.
            # Phrased separately from the percentage case above since
            # "X% higher" is meaningless against a $0 denominator.
            reasons.append(Reason(
                outcome_type=OutcomeType.PARTIAL_REDUCTION,
                summary=(
                    f"Medicare's rate for this service is $0, but you were "
                    f"billed ${pricing.billed_amount:,.0f} -- worth raising "
                    f"directly with the provider."
                ),
                estimated_low=0,
                estimated_high=pricing.billed_amount,
                source_requirement_codes=[],
            ))

    for finding in findings:
        if finding.status not in (
            ComplianceStatus.ABSENT,
            ComplianceStatus.CONTRADICTED,
            ComplianceStatus.VAGUE,
        ):
            continue

        # .get() + skip, not direct indexing: requirement_code is free
        # TEXT in Postgres (fap_compliance_findings), not constrained by
        # an enum the way document_quality is -- a code from historical
        # data (written under an older version of the checklist) or a
        # future checklist edit could fail to match CHECKLIST_BY_CODE.
        # This runs in the live, synchronous /intake request path (via
        # fetch_fap_for_facility), so a crash here is more consequential
        # than the equivalent gap already fixed in
        # fap_pipeline.run_compliance_checklist, which only affects the
        # background worker.
        item = CHECKLIST_BY_CODE.get(finding.requirement_code)
        if item is None:
            continue

        reasons.append(Reason(
            outcome_type=OutcomeType.PROCEDURAL_LEVERAGE,
            summary=item.user_facing_summary,
            estimated_low=None,
            estimated_high=None,
            source_requirement_codes=[finding.requirement_code],
        ))

    return reasons


# ============================================================
# Orchestration
# ============================================================

def _mrf_rates_reason(input_data: SynthesisInput) -> Reason | None:
    """
    Generate a reason from hospital-published MRF rates.

    Three sub-cases:
    1. rates_found + cash_price: use the hospital's own self-pay rate as anchor.
       "Your hospital's own published price for this procedure is $X."
    2. rates_found + min/max negotiated but no cash: use the negotiated range.
    3. Negative status (unreachable, unpopulated, not_in_mrf): return a
       PROCEDURAL_LEVERAGE reason documenting the compliance failure --
       no dollar estimate, but valuable for the letter.
    """
    if not input_data.mrf_status:
        return None

    billed = input_data.billed_amount

    if input_data.mrf_status == "rates_found":
        # Sub-case 1: cash price available
        cash = input_data.mrf_cash_price_total
        if cash and cash > 0 and billed and cash < billed:
            savings = billed - cash
            return Reason(
                outcome_type=OutcomeType.PARTIAL_REDUCTION,
                summary=(
                    f"Your hospital's own published self-pay price for these "
                    f"procedures is ${cash:,.0f} — lower than what you were billed. "
                    f"You can ask to be billed at this published rate, which could "
                    f"save approximately ${savings:,.0f}."
                ),
                estimated_low=round(savings * 0.85),  # usually gets most or all
                estimated_high=round(savings),
                source_requirement_codes=[],
            )

        # Sub-case 2: negotiated range available
        min_neg = input_data.mrf_min_negotiated_total
        max_neg = input_data.mrf_max_negotiated_total
        if min_neg and max_neg and billed and min_neg < billed:
            savings_low = billed - max_neg
            savings_high = billed - min_neg
            return Reason(
                outcome_type=OutcomeType.PARTIAL_REDUCTION,
                summary=(
                    f"Your hospital's published negotiated rate range for these "
                    f"procedures is ${min_neg:,.0f}–${max_neg:,.0f}, compared to "
                    f"the ${billed:,.0f} you were billed. Request that your balance "
                    f"be adjusted to the hospital's disclosed negotiated range."
                ),
                estimated_low=max(round(savings_low), 0),
                estimated_high=round(savings_high),
                source_requirement_codes=[],
            )

    # Negative status cases → procedural leverage (no dollar estimate)
    negative_statuses = {"mrf_unreachable", "mrf_unpopulated", "codes_not_in_mrf", "mrf_url_unknown"}
    if input_data.mrf_status in negative_statuses and input_data.mrf_status_detail:
        return Reason(
            outcome_type=OutcomeType.PROCEDURAL_LEVERAGE,
            summary=input_data.mrf_status_detail,
            estimated_low=None,
            estimated_high=None,
            source_requirement_codes=[],
        )

    return None


def _eob_allowed_amount_reason(input_data: SynthesisInput) -> Reason | None:
    """
    If an EOB has been matched, generate a reason using the insurer's
    allowed amount as the leverage anchor. This is often the strongest
    argument available: the provider has already agreed to accept this
    amount from a payer, and it's hard to justify charging an uninsured
    patient significantly more.

    The estimated savings is patient_responsibility minus what charity care
    (or a rate matching allowed_amount) would cost. If no eligibility data
    is available, the estimate is patient_responsibility itself (arguing
    the provider accept the allowed_amount as the ceiling).
    """
    if input_data.allowed_amount_total is None:
        return None
    patient_resp = input_data.patient_responsibility_total
    billed = input_data.billed_amount
    allowed = input_data.allowed_amount_total

    # The savings argument: the gap between what the patient was billed
    # (or owes after insurance) and the insurer's contracted rate.
    anchor = patient_resp if patient_resp is not None else billed
    savings = max(anchor - allowed, 0) if anchor is not None else None

    if savings is None or savings < 1.0:
        # Allowed amount is at or above what the patient owes already --
        # not much leverage here, skip the reason.
        return None

    return Reason(
        outcome_type=OutcomeType.PARTIAL_REDUCTION,
        summary=(
            f"Your insurer's contracted rate for these services is "
            f"${allowed:,.0f} -- the amount your insurer agreed to pay "
            f"the provider. You may be able to negotiate the same rate "
            f"as a self-pay amount, saving approximately "
            f"${savings:,.0f} off your current balance."
        ),
        estimated_low=round(savings * 0.7),   # conservative: provider may not agree fully
        estimated_high=round(savings),
        source_requirement_codes=[],
    )


def synthesize(input_data: SynthesisInput) -> SynthesisResult:
    follow_ups = determine_follow_ups(input_data)

    eligibility_reason = None
    if not follow_ups:  # only attempt once we have enough info
        eligibility_reason = estimate_eligibility_outcome(input_data)

    other_reasons = findings_to_reasons(input_data.compliance_findings, input_data.pricing)
    eob_reason = _eob_allowed_amount_reason(input_data)
    mrf_reason = _mrf_rates_reason(input_data)
    all_reasons = (
        ([eligibility_reason] if eligibility_reason else [])
        + ([mrf_reason] if mrf_reason else [])      # MRF before EOB -- hospital's own rates
        + ([eob_reason] if eob_reason else [])
        + other_reasons
    )

    # Rank: full elimination > partial reduction (by estimated_high desc)
    # > procedural leverage. Starting heuristic -- revisit once real
    # discount_value distributions are available.
    def rank_key(reason: Reason) -> tuple:
        order = {
            OutcomeType.FULL_ELIMINATION: 0,
            OutcomeType.PARTIAL_REDUCTION: 1,
            OutcomeType.PROCEDURAL_LEVERAGE: 2,
        }
        impact = -(reason.estimated_high or 0)
        return (order[reason.outcome_type], impact)

    all_reasons.sort(key=rank_key)

    headline_could_eliminate = any(
        r.outcome_type == OutcomeType.FULL_ELIMINATION for r in all_reasons
    )
    reduction_reasons = [r for r in all_reasons if r.estimated_high is not None]
    headline_low = min(
        (r.estimated_low for r in reduction_reasons if r.estimated_low is not None),
        default=None,
    )
    headline_high = max((r.estimated_high for r in reduction_reasons), default=None)

    return SynthesisResult(
        headline_low=0 if headline_could_eliminate else headline_low,
        headline_high=headline_high,
        headline_could_eliminate=headline_could_eliminate,
        reasons=all_reasons[:3],  # cap at 3 per UX design
        follow_up_questions=follow_ups,
        beta_caveat=DEFAULT_BETA_CAVEAT,
    )
