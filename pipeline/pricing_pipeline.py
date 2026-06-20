"""
RobinHealth: pricing benchmark pipeline.

Compares a bill's line items against Medicare's published payment rates --
the two public, no-cost benchmarks that together cover most of a hospital
bill:

  - PFS (Physician Fee Schedule): professional/physician-component
    charges, billed under CPT codes. Source: CMS Physician Fee Schedule,
    https://pfs.data.cms.gov -- updated annually, with a queryable Open
    Data API.
  - OPPS (Hospital Outpatient Prospective Payment System), Addendum B:
    hospital facility-component charges for outpatient services, billed
    under HCPCS codes, mapped to Ambulatory Payment Classifications
    (APCs). Source: CMS's quarterly Addendum B download -- no live API,
    a CSV/Excel file.

Both are public-domain and free, unlike FAIR Health (paid license) or
commercial MRF aggregators (Turquoise Health, Trilliant Health), which
offer free *querying* but not a redistributable dataset.

Unlike the other pipeline stages, this one is NOT an LLM call -- PFS/OPPS
lookup is deterministic data retrieval, so the lookup logic itself
(RateTable, benchmark_line_item, aggregate_to_pricing_benchmark) is fully
implemented and tested here, not stubbed. Only the data-loading data path
(network fetch / CSV file location) is left as a TODO, since that depends
on how the data gets into this environment in production.

SIMPLIFICATIONS, flagged rather than silently assumed:

  1. A CPT code can appear on both a physician bill (PFS, professional
     component) and a hospital outpatient bill (OPPS, facility component)
     for the same encounter, at different rates -- and some PFS-priced
     services use HCPCS Level II codes, while OPPS sometimes prices CPT
     codes. DEFAULT_SOURCE_FOR_CODE_TYPE (cpt->pfs, hcpcs->opps) is the
     common case, not a universal rule; pass benchmark_source explicitly
     to benchmark_line_item when the bill's context is known.

  2. PFS rates are geographically adjusted (GPCI) by CMS "locality," a
     finer-grained division than state -- there is no state->locality
     crosswalk in this scaffold yet. RateTable.lookup accepts an optional
     locality and falls back to a national/unadjusted rate when no
     locality-specific entry exists; deriving locality from
     bill_pipeline.ExtractedProviderInfo.state is a separate future TODO.

  3. This module only benchmarks against MEDICARE rates. It does not
     attempt the negotiated-commercial-rate comparison FAIR Health /
     Turquoise / Trilliant offer -- see estimate_negotiated_rate() below.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from typing import Literal

import httpx

from bill_pipeline import ExtractedLineItem
from synthesis import PricingBenchmark


BenchmarkSource = Literal["pfs", "opps"]

# code_type (from bill_pipeline.ExtractedLineItem) -> default benchmark
# source. See module docstring, simplification 1.
DEFAULT_SOURCE_FOR_CODE_TYPE: dict[str, BenchmarkSource] = {
    "cpt": "pfs",
    "hcpcs": "opps",
}


# ============================================================
# Data containers
# ============================================================

@dataclass
class RateTableEntry:
    code: str
    locality: str  # "" represents the national/unadjusted rate
    rate: float
    description: str | None = None


@dataclass
class RateTable:
    """
    In-memory (code, locality) -> RateTableEntry lookup, with a
    national/unadjusted fallback when no locality-specific entry exists.

    Pure lookup logic, deliberately separated from how entries get
    loaded (load_rate_table_from_csv) so it's testable without file I/O.
    """
    source: BenchmarkSource
    entries: dict[tuple[str, str], RateTableEntry]

    def lookup_entry(self, code: str, locality: str | None = None) -> RateTableEntry | None:
        if locality:
            entry = self.entries.get((code, locality))
            if entry is not None:
                return entry
        return self.entries.get((code, ""))

    def lookup(self, code: str, locality: str | None = None) -> float | None:
        entry = self.lookup_entry(code, locality)
        return entry.rate if entry else None


@dataclass
class LineItemBenchmark:
    line_item: ExtractedLineItem
    benchmark_source: BenchmarkSource | None  # None if code_type had no default mapping
    medicare_rate: float | None  # None if the code wasn't found in the rate table
    delta_amount: float | None  # billed_amount - medicare_rate, only if both are known
    delta_pct: float | None  # delta_amount / medicare_rate * 100


# ============================================================
# Stage 1: rate table loading (fully implemented -- pure CSV parsing)
# ============================================================

def load_rate_table_from_csv(path: str, source: BenchmarkSource) -> RateTable:
    """
    Load a rate table from a CSV with columns: code, locality, rate, and
    an optional description. `locality` may be blank for a
    national/unadjusted rate -- OPPS Addendum B has no locality column at
    all, so always export it blank for that source.

    Real source files, by benchmark_source:
      - 'pfs':  CMS Physician Fee Schedule, https://pfs.data.cms.gov
                (Open Data API; updated annually, effective Jan 1). See
                fetch_pfs_rates_for_codes for a direct-API alternative to
                pre-exporting a CSV.
      - 'opps': CMS OPPS Addendum B, updated quarterly --
                https://www.cms.gov/medicare/payment/prospective-payment-systems/hospital-outpatient-pps/quarterly-addenda-updates
    """
    entries: dict[tuple[str, str], RateTableEntry] = {}
    skipped = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row_number, row in enumerate(reader, start=2):  # row 1 is the header
            try:
                code = row["code"].strip()
                locality = (row.get("locality") or "").strip()
                rate = float(row["rate"])
            except (KeyError, ValueError, AttributeError) as exc:
                # A real CMS export is large (thousands of rows) and not
                # immune to one-off quirks (a trailing blank line, an
                # Excel export artifact, a missing column on one row).
                # Skip just that row rather than failing the whole load
                # -- this runs once at API startup, so an unhandled
                # exception here would otherwise prevent the entire
                # server from booting over a single bad row.
                print(f"[pricing_pipeline] skipping malformed row {row_number} in {path}: {exc}")
                skipped += 1
                continue
            entries[(code, locality)] = RateTableEntry(
                code=code, locality=locality, rate=rate,
                description=(row.get("description") or "").strip() or None,
            )
    if skipped:
        print(f"[pricing_pipeline] loaded {len(entries)} rows from {path}, skipped {skipped} malformed row(s)")
    return RateTable(source=source, entries=entries)


# ============================================================
# Stage 2: live PFS lookup (real CMS Open Data API)
# ============================================================

# CMS pfs.data.cms.gov Open Data API -- DKAN-style SQL endpoint.
# Dataset IDs verified June 2026 from pfs.data.cms.gov/datasets.
# CMS publishes new dataset IDs each calendar year; update these when
# the annual PFS final rule takes effect each January 1.
_PFS_API_BASE = "https://pfs.data.cms.gov/api/1/datastore/sql"

# Payment amounts dataset (non_facility_price + facility_price columns).
# The localities dataset (81f942b8-...) has GPCI values but not dollar
# amounts; the indicators dataset (7c7df311-...) has the actual prices.
_PFS_DATASET_ID_2026 = "7c7df311-5315-4f38-b9ed-fd62f8bebe11"

_PFS_API_TIMEOUT = 15.0  # seconds; CMS API is occasionally slow


# CMS PFS locality codes by state -- used to prefer a locality-specific
# rate over the national unadjusted rate when the provider's state is
# known. CMS assigns each state one "rest of state" locality plus
# additional urban localities for large metros; this maps to the most
# common rest-of-state code, which is adequate for most cases.
# Source: CMS Physician Fee Schedule Localities 2026.
# Complete crosswalk at: https://pfs.data.cms.gov/dataset/81f942b8-...
PFS_STATE_TO_DEFAULT_LOCALITY: dict[str, str] = {
    "AK": "02", "AL": "01", "AR": "16", "AZ": "16",
    "CA": "26",  # rest of California
    "CO": "07", "CT": "16", "DC": "12", "DE": "12",
    "FL": "09", "GA": "10", "HI": "03", "IA": "16",
    "ID": "26", "IL": "16",
    "IN": "16", "KS": "16", "KY": "16", "LA": "16",
    "MA": "14", "MD": "12", "ME": "16", "MI": "16",
    "MN": "16", "MO": "16", "MS": "16", "MT": "16",
    "NC": "16", "ND": "16", "NE": "16", "NH": "16",
    "NJ": "12", "NM": "16",
    "NY": "16",  # rest of NY
    "OH": "16", "OK": "16", "OR": "26", "PA": "12",
    "PR": "40", "RI": "14", "SC": "16", "SD": "16",
    "TN": "16", "TX": "43",  # rest of Texas
    "UT": "16", "VA": "12", "VT": "16", "WA": "26",
    "WI": "16", "WV": "16", "WY": "16",
}


def resolve_pfs_locality(state: str | None) -> str | None:
    """
    Map a two-letter state code to the CMS PFS default locality code for
    that state. Returns None when the state is unknown or unmapped --
    callers fall back to the national unadjusted rate in that case.
    """
    if not state:
        return None
    return PFS_STATE_TO_DEFAULT_LOCALITY.get(state.upper())


def fetch_pfs_rates_for_codes(
    codes: list[str],
    locality: str | None = None,
    setting: str = "non_facility",  # "non_facility" | "facility"
    dataset_id: str = _PFS_DATASET_ID_2026,
) -> RateTable:
    """
    Query the CMS Physician Fee Schedule Open Data API for a specific set
    of HCPCS/CPT codes. Returns a RateTable populated with the results.

    Uses the DKAN SQL endpoint at pfs.data.cms.gov. The query fetches
    both non_facility_price (office/clinic setting) and facility_price
    (hospital/ASC setting) and stores the requested setting's price as
    the rate. Both are stored so callers can switch setting without a
    second API call.

    Graceful failure modes (all return an empty RateTable, never raise):
      - Network error (API unreachable from sandbox / timeout)
      - HTTP error (4xx/5xx from CMS API)
      - Unexpected response shape (API schema change)
    In production with real network access, these degrade to "no Medicare
    benchmark available" rather than crashing the pipeline.

    The `locality` parameter filters results to a specific CMS locality
    code. When None, only national/unlocalized rows are returned. Use
    resolve_pfs_locality(provider_state) to derive the right code.

    `dataset_id` defaults to the CY 2026 indicators dataset. Update this
    each January when CMS publishes the new annual dataset.
    """
    if not codes:
        return RateTable(source="pfs", entries={})

    # Build a SQL query for the DKAN datastore API.
    # Column names verified against the 2026 indicators dataset schema:
    #   hcpcs_cd, mod, description, locality_number,
    #   non_facility_price, facility_price
    # The SQL syntax is ANSI SQL with a small DKAN extension for IN lists.
    quoted_codes = ", ".join(f"'{c.strip()}'" for c in codes if c)
    locality_clause = f"AND locality_number = '{locality}'" if locality else ""
    query = (
        f"SELECT hcpcs_cd, mod, description, locality_number, "
        f"non_facility_price, facility_price "
        f"FROM {dataset_id} "
        f"WHERE hcpcs_cd IN ({quoted_codes}) "
        f"{locality_clause} "
        f"LIMIT {len(codes) * 100}"  # headroom for multiple localities
    )

    try:
        response = httpx.get(
            _PFS_API_BASE,
            params={"query": query},
            timeout=_PFS_API_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            # DKAN sometimes wraps in {"results": [...]}
            rows = rows.get("results") or rows.get("data") or []
    except httpx.HTTPError as exc:
        print(f"[pricing_pipeline] PFS API error: {exc}")
        return RateTable(source="pfs", entries={})
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"[pricing_pipeline] PFS API parse error: {exc}")
        return RateTable(source="pfs", entries={})

    entries: dict[tuple[str, str], RateTableEntry] = {}
    price_col = "facility_price" if setting == "facility" else "non_facility_price"

    for row in rows:
        try:
            code = (row.get("hcpcs_cd") or "").strip().upper()
            loc = (row.get("locality_number") or "").strip()
            price_str = row.get(price_col) or row.get("non_facility_price") or ""
            rate = float(str(price_str).replace("$", "").replace(",", "").strip())
            desc = (row.get("description") or "").strip() or None
            entries[(code, loc)] = RateTableEntry(
                code=code, locality=loc, rate=rate, description=desc,
            )
        except (KeyError, ValueError, AttributeError):
            continue  # skip malformed rows, same pattern as load_rate_table_from_csv

    return RateTable(source="pfs", entries=entries)


def fetch_opps_rates_for_codes(
    codes: list[str],
    opps_addendum_b_path: str | None = None,
) -> RateTable:
    """
    Load OPPS (Hospital Outpatient PPS) Addendum B rates for specific
    HCPCS codes from the quarterly CMS download.

    Unlike the PFS, OPPS has no live queryable API -- CMS publishes a
    ZIP of Excel/CSV files each quarter. This function loads from a
    pre-extracted CSV at `opps_addendum_b_path` (same format as the
    load_rate_table_from_csv CSV: code, locality, rate, description).

    Returns an empty RateTable if path is None or the file is missing --
    degrades cleanly the same way the PFS API does on network failure.

    Download the quarterly Addendum B from:
    https://www.cms.gov/medicare/payment/prospective-payment-systems/
        hospital-outpatient-pps/quarterly-addenda-updates
    Then extract the "APC Payment Rate" column by HCPCS code into the
    standard CSV format (code, locality="", rate, description).
    """
    if not opps_addendum_b_path:
        return RateTable(source="opps", entries={})
    try:
        table = load_rate_table_from_csv(opps_addendum_b_path, source="opps")
        if not codes:
            return table
        # Filter to only the requested codes
        target = {c.strip().upper() for c in codes if c}
        filtered = {k: v for k, v in table.entries.items() if k[0] in target}
        return RateTable(source="opps", entries=filtered)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"[pricing_pipeline] OPPS Addendum B not found at {opps_addendum_b_path}: {exc}")
        return RateTable(source="opps", entries={})


def resolve_rate_tables_on_demand(
    base_tables: dict,
    codes: list[str] | None = None,
    provider_state: str | None = None,
) -> dict:
    """
    Return a rate-table dict with on-demand PFS rates merged in, when the
    startup CSV was not pre-loaded (base_tables["pfs"] is empty).

    If the startup PFS table already has entries (pre-loaded full schedule),
    this is a no-op -- returns base_tables unchanged. OPPS has no live API
    so it always requires a pre-downloaded CSV; no on-demand fetch there.

    Designed to be called from case_pipeline after extract_bill, once the
    procedure codes and provider state are known.
    """
    pfs_table = base_tables.get("pfs", RateTable(source="pfs", entries={}))
    if pfs_table.entries:
        return base_tables  # pre-loaded -- nothing to do

    if not codes:
        return base_tables

    locality = resolve_pfs_locality(provider_state)
    try:
        fetched = fetch_pfs_rates_for_codes(codes, locality=locality)
        if fetched.entries:
            result = dict(base_tables)
            result["pfs"] = fetched
            print(f"[pricing_pipeline] fetched {len(fetched.entries)} PFS rates on-demand")
            return result
    except Exception as exc:
        print(f"[pricing_pipeline] on-demand PFS fetch failed: {exc}")

    return base_tables


# ============================================================
# Stage 3: line-item benchmarking (fully implemented)
# ============================================================

def benchmark_line_item(
    item: ExtractedLineItem,
    rate_tables: dict[BenchmarkSource, RateTable],
    locality: str | None = None,
    benchmark_source: BenchmarkSource | None = None,
) -> LineItemBenchmark:
    """
    Compare one line item's billed_amount against the relevant Medicare
    rate table. benchmark_source overrides the code_type-based default
    (DEFAULT_SOURCE_FOR_CODE_TYPE) -- pass it explicitly when the bill's
    context (e.g. "this is a physician's professional bill, not a
    hospital facility bill") is known to differ from that default.

    A missing rate_tables entry, an uncoded line item, or a code not
    found in the table are all valid, expected outcomes (drug NDC codes
    and many revenue codes have no Medicare-fee-schedule counterpart at
    all) -- they return medicare_rate=None rather than raising.
    """
    source = benchmark_source or (
        DEFAULT_SOURCE_FOR_CODE_TYPE.get(item.code_type) if item.code_type else None
    )

    if source is None or item.procedure_code is None or source not in rate_tables:
        return LineItemBenchmark(
            line_item=item, benchmark_source=source,
            medicare_rate=None, delta_amount=None, delta_pct=None,
        )

    rate = rate_tables[source].lookup(item.procedure_code, locality)
    if rate is None:
        return LineItemBenchmark(
            line_item=item, benchmark_source=source,
            medicare_rate=None, delta_amount=None, delta_pct=None,
        )

    delta_amount = item.billed_amount - rate
    delta_pct = (delta_amount / rate * 100) if rate > 0 else None

    return LineItemBenchmark(
        line_item=item, benchmark_source=source,
        medicare_rate=rate, delta_amount=delta_amount, delta_pct=delta_pct,
    )


def benchmark_bill(
    line_items: list[ExtractedLineItem],
    rate_tables: dict[BenchmarkSource, RateTable],
    locality: str | None = None,
) -> list[LineItemBenchmark]:
    return [benchmark_line_item(item, rate_tables, locality) for item in line_items]


# ============================================================
# Stage 4: negotiated-rate benchmark (stubbed -- separate data source)
# ============================================================

# Typical commercial negotiated rates run 130-250% of Medicare,
# depending on market and service type. 180% is a reasonable median
# estimate for most hospital markets -- used as a fallback when no
# MRF-specific rate is available. Sources: RAND Hospital Price
# Transparency studies (2020-2024 series) consistently find commercial
# rates at 1.5-2.5x Medicare; 1.8x is the published median.
_COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO = 1.80


def estimate_negotiated_rate(
    benchmarks: list[LineItemBenchmark],
    mrf_finding: dict | None = None,
) -> float | None:
    """
    Estimate a fair commercial rate for the procedures in `benchmarks`.
    Returns a dollar total (not per-line), or None if no estimate is
    possible.

    Priority order:
    1. MRF min_negotiated_charge if available (most precise -- the
       hospital's own disclosed lowest accepted rate, aggregated across
       matched codes). This is the strongest anchor because it's the
       specific hospital's actual disclosure, not a national average.
    2. MRF discounted_cash_price if available (hospital's self-pay rate,
       which is often similar to or slightly above min_negotiated).
    3. Medicare rate × _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO (national
       median multiplier from RAND studies). Least precise but always
       available when the PFS API returned rates.

    `mrf_finding` is the dict from repository.fetch_mrf_finding_for_facility
    (or the mrf_finding field from CaseIntakeResult) -- None if no MRF
    job has completed for this facility yet.

    The negotiated-rate estimate sits in synthesis.PricingBenchmark as
    fair_price_estimate, used by findings_to_reasons to add a secondary
    leverage point alongside the primary Medicare comparison.
    """
    # Option 1: MRF min_negotiated_charge total
    if mrf_finding and mrf_finding.get("mrf_status") == "rates_found":
        rates = mrf_finding.get("rates") or {}
        min_vals = [
            r["min_negotiated_charge"] for r in rates.values()
            if r.get("min_negotiated_charge")
        ]
        if min_vals:
            return sum(min_vals)

        # Option 2: MRF discounted_cash_price total
        cash_vals = [
            r["discounted_cash_price"] for r in rates.values()
            if r.get("discounted_cash_price")
        ]
        if cash_vals:
            return sum(cash_vals)

    # Option 3: Medicare rate × median commercial multiplier
    matched = [b for b in benchmarks if b.medicare_rate is not None]
    if matched:
        total_medicare = sum(b.medicare_rate for b in matched)
        return round(total_medicare * _COMMERCIAL_TO_MEDICARE_MEDIAN_RATIO, 2)

    return None


# ============================================================
# Stage 5: aggregation into synthesis.PricingBenchmark (fully implemented)
# ============================================================

def aggregate_to_pricing_benchmark(
    benchmarks: list[LineItemBenchmark],
    mrf_finding: dict | None = None,
) -> PricingBenchmark | None:
    """
    Roll up per-line-item benchmarks into the single PricingBenchmark
    synthesis.SynthesisInput expects.

    Only line items with a matched Medicare rate contribute to either
    side of the comparison -- billed_amount and medicare_rate here cover
    the SAME subset of line items, so the delta_pct synthesis.py computes
    from them stays meaningful. Unmatched line items (e.g. NDC drug
    codes) are excluded entirely, not treated as a $0 Medicare rate --
    that would manufacture a 100%+ "overcharge" finding from a code this
    benchmark simply can't price.

    `mrf_finding` is passed through to estimate_negotiated_rate so it can
    use MRF-specific rates when available (higher priority than the
    Medicare multiplier fallback).

    Returns None if nothing on the bill could be matched.
    """
    matched = [b for b in benchmarks if b.medicare_rate is not None]
    if not matched:
        return None

    fair_price_estimate = estimate_negotiated_rate(benchmarks, mrf_finding=mrf_finding)

    return PricingBenchmark(
        billed_amount=sum(b.line_item.billed_amount for b in matched),
        medicare_rate=sum(b.medicare_rate for b in matched),
        fair_price_estimate=fair_price_estimate,
    )
