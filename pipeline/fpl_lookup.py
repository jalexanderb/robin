"""
RobinHealth: Federal Poverty Level (FPL) lookup.

HHS updates poverty guidelines each January (effective ~mid-January).
These figures MUST be reviewed annually -- see
https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines

Figures below are the 2026 guidelines (effective January 13, 2026):
base = amount for a 1-person household, increment = amount added per
additional household member (for households up to size 8 -- HHS uses a
smaller increment, $4,540 for the 48-state/DC table, for each member
beyond 8; not modeled here since the product's household-size input caps
at "5+").
"""

from __future__ import annotations


# {state_code or "default": (base for 1 person, increment per additional member)}
# "default" covers the 48 contiguous states + DC. AK and HI have their own,
# higher tables.
FPL_GUIDELINES_2026: dict[str, tuple[float, float]] = {
    "default": (15960, 5680),
    "AK": (19950, 7100),
    "HI": (18360, 6530),
}


def fpl_amount(household_size: int, state: str | None = None) -> float:
    """
    Dollar amount corresponding to 100% of the FPL for a household of the
    given size in the given state.
    """
    base, increment = FPL_GUIDELINES_2026.get(
        (state or "").upper(), FPL_GUIDELINES_2026["default"]
    )
    extra_members = max(household_size - 1, 0)
    return base + extra_members * increment


def income_as_fpl_percent(
    household_income: float, household_size: int, state: str | None = None
) -> float:
    """
    Household income as a percentage of the FPL for their size/state.

    Negative income (a real scenario -- e.g. self-employed household
    with a business loss for the year) is clamped to 0 before computing
    the percentage. Without this, a negative income produces a negative
    percentage that fails to match even a FAP's most generous tier when
    that tier's fpl_min_pct is an explicit 0 rather than None/open-ended
    -- the typical real-world wording -- even though someone with
    negative income should be at least as eligible as someone with
    exactly $0 income, not less.
    """
    fpl = fpl_amount(household_size, state)
    if fpl <= 0:
        return 0.0
    return max(household_income, 0) / fpl * 100
