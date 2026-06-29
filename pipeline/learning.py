"""
RobinHealth: the learning loop.

Every negotiation outcome is already recorded (outcome_pipeline.record_outcome),
but until now nothing fed that back. This module turns the history of resolved
cases at a facility into insight: how often pushing back produced a reduction,
the typical reduction size, and how long it took. Over time this compounds into
something competitors can't easily copy -- a real, data-backed sense of "what
works here" that informs the strategy and what Robin tells the patient.

Pure logic (no DB) so it's deterministic and testable; the raw rows are fetched
by repository.fetch_facility_outcomes and passed in. Insights are only surfaced
once there's enough history to be meaningful (MIN_CASES_FOR_INSIGHT), so we never
present a single anecdote as a pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


# Below this, we don't claim a "pattern" -- too few cases to be meaningful.
MIN_CASES_FOR_INSIGHT = 3


@dataclass
class OutcomeRecord:
    billed_amount: float
    agreed_amount: float | None
    final_status: str | None = None      # e.g. 'reduced', 'eliminated', 'no_change'
    days_to_resolve: float | None = None


@dataclass
class FacilityInsights:
    n_cases: int                          # resolved cases with a usable outcome
    n_reduced: int                        # cases that got any reduction
    success_rate: float | None            # n_reduced / n_cases (0..1)
    avg_reduction_pct: float | None       # mean % off the bill, among reduced cases
    avg_days_to_resolve: float | None
    enough_data: bool                     # n_cases >= MIN_CASES_FOR_INSIGHT


def _reduction_pct(rec: OutcomeRecord) -> float | None:
    if rec.agreed_amount is None or not rec.billed_amount or rec.billed_amount <= 0:
        return None
    pct = (rec.billed_amount - rec.agreed_amount) / rec.billed_amount * 100.0
    return max(pct, 0.0)


def _is_reduced(rec: OutcomeRecord) -> bool:
    if rec.final_status in ("reduced", "eliminated"):
        return True
    pct = _reduction_pct(rec)
    return pct is not None and pct > 0.5  # treat sub-dollar rounding as "no change"


def summarize_outcomes(records: list[OutcomeRecord]) -> FacilityInsights:
    """Aggregate resolved-case outcomes into facility-level insight."""
    usable = [r for r in records if r.billed_amount and r.billed_amount > 0]
    n = len(usable)
    if n == 0:
        return FacilityInsights(0, 0, None, None, None, enough_data=False)

    reduced = [r for r in usable if _is_reduced(r)]
    reduction_pcts = [p for p in (_reduction_pct(r) for r in reduced) if p is not None]
    days = [r.days_to_resolve for r in usable if r.days_to_resolve is not None]

    return FacilityInsights(
        n_cases=n,
        n_reduced=len(reduced),
        success_rate=round(len(reduced) / n, 3),
        avg_reduction_pct=round(mean(reduction_pcts), 1) if reduction_pcts else None,
        avg_days_to_resolve=round(mean(days), 1) if days else None,
        enough_data=n >= MIN_CASES_FOR_INSIGHT,
    )


def insight_text(insights: FacilityInsights, facility_name: str | None = None) -> str | None:
    """
    Plain-language, honest summary for the patient -- or None when there isn't
    enough history to responsibly claim a pattern.
    """
    if not insights.enough_data:
        return None
    where = f" at {facility_name}" if facility_name else ""
    rate_pct = round((insights.success_rate or 0) * 100)
    parts = [
        f"Across {insights.n_cases} resolved cases{where}, pushing back produced a "
        f"reduction about {rate_pct}% of the time"
    ]
    if insights.avg_reduction_pct is not None:
        parts.append(f", averaging roughly {insights.avg_reduction_pct:.0f}% off the bill")
    if insights.avg_days_to_resolve is not None:
        parts.append(f", typically resolving in about {insights.avg_days_to_resolve:.0f} days")
    return "".join(parts) + "."
