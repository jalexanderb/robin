"""
RobinHealth: Hospital Price Transparency MRF pipeline.

Fetches and parses a hospital's CMS-mandated Machine-Readable File (MRF)
to find what the hospital actually charges (and accepts) for specific
procedure codes. This is qualitatively different from Medicare benchmarks:
it shows the *specific hospital's own disclosed prices*, including their
negotiated rates with individual payers, giving RobinHealth the strongest
possible negotiation anchor: "Your hospital's own published price
transparency file shows you accepted $X from [payer] for this service."

HOW MRF DISCOVERY WORKS:
Since January 2021, every US hospital must publish a `cms-hpt.txt` file
at their website root containing the URL of their machine-readable file.
Format:
    https://hospitaldomain.com/path/to/mrf.json   <- the MRF
    https://hospitaldomain.com/price-transparency  <- the webpage

We try to discover the MRF URL in this order:
  1. health_systems.mrf_url if already on file (seeded or previously discovered)
  2. Fetch {hospital_domain}/cms-hpt.txt and parse the first URL from it
  3. If neither works, status = mrf_url_unknown

MRF FILE FORMAT (CMS standard, January 2025+):
The CMS template requires JSON or CSV. The JSON schema has this structure
at top level:
  {
    "hospital_name": "...",
    "hospital_location": "...",
    "standard_charge_information": [
      {
        "description": "Office visit",
        "code_information": [{"code": "99213", "type": "CPT"}],
        "standard_charges": [
          {
            "setting": "outpatient",
            "gross_charge": 500.0,
            "discounted_cash_price": 280.0,
            "minimum_negotiated_charge": 120.0,
            "maximum_negotiated_charge": 350.0,
            "payers_information": [
              {"payer_name": "Aetna", "plan_name": "PPO", "standard_charge_dollar": 200.0}
            ]
          }
        ]
      }
    ]
  }

WHAT WE SURFACE TO THE USER:
All four statuses are meaningful and explicitly communicated:
  rates_found         -- "We found your hospital's published rates: the
                          negotiated range for this procedure is $X-$Y."
  codes_not_in_mrf    -- "Your hospital published a price transparency file
                          but didn't include this procedure. This may indicate
                          the hospital is non-compliant with CMS requirements."
  mrf_unpopulated     -- "Your hospital's price file lists this procedure
                          but left the rates blank or used placeholder values.
                          CMS now requires real dollar amounts; this is a
                          compliance red flag."
  mrf_unreachable     -- "We couldn't access your hospital's price file at
                          the published URL. This is worth noting when
                          negotiating."
  mrf_url_unknown     -- "We don't have a price transparency URL on file for
                          this hospital yet."

WHY ALL STATUSES MATTER FOR NEGOTIATION:
Even a negative result (unreachable, unpopulated) is a negotiation point:
CMS requires hospitals to publish this data, and failure to comply is a
pattern that supports the argument that the hospital is engaging in opaque
billing practices. A letter that says "your hospital has not published
complete price transparency data as required by 45 CFR 180" is often just
as useful as one that cites specific rates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class MrfCodeRate:
    """Rates for one procedure code found in the MRF."""
    code: str
    code_type: str              # 'CPT', 'HCPCS', 'MS-DRG', etc.
    description: str | None
    gross_charge: float | None
    discounted_cash_price: float | None
    min_negotiated_charge: float | None
    max_negotiated_charge: float | None
    # Payer-specific rates: [{"payer_name": ..., "plan_name": ..., "rate": ...}]
    payer_rates: list[dict] = field(default_factory=list)


@dataclass
class MrfFindingResult:
    """
    Result of an MRF lookup for a set of procedure codes.
    This is what gets persisted to mrf_findings and surfaced to the user.
    """
    facility_id: str
    mrf_url: str | None
    status: str          # mrf_status enum value
    status_detail: str   # plain-English explanation, user-facing
    codes_queried: list[str]
    rates: dict[str, MrfCodeRate]   # code -> rates; empty if no rates found


# ============================================================
# MRF URL discovery
# ============================================================

_HTP_TXT_PATH = "/cms-hpt.txt"
_REQUEST_TIMEOUT = 20.0  # MRFs can be large; fetch gets a longer timeout
_MRF_FETCH_MAX_BYTES = 50 * 1024 * 1024  # 50 MB -- partial fetch for large files


def discover_mrf_url(hospital_domain: str) -> str | None:
    """
    Fetch {hospital_domain}/cms-hpt.txt and extract the MRF URL from it.
    Returns None if the file is missing or unparseable.

    The CMS spec requires the first non-blank line to be the MRF URL.
    Some hospitals put the webpage URL second and some add extra metadata
    lines, so we look for the first http(s):// line that looks like a
    data file (json, csv, or contains 'mrf' / 'machine' in the path).
    """
    domain = hospital_domain.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    txt_url = f"{domain}{_HTP_TXT_PATH}"
    try:
        response = httpx.get(txt_url, timeout=10.0, follow_redirects=True)
        if response.status_code >= 400:
            return None
        text = response.text
    except httpx.HTTPError:
        return None

    # Parse lines looking for the MRF URL (data file, not the webpage)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("http"):
            continue
        lower = line.lower()
        # Prefer lines that look like a machine-readable data file
        if any(ext in lower for ext in (".json", ".csv", ".json.gz", ".csv.gz")):
            return line
        if any(kw in lower for kw in ("mrf", "machine-readable", "chargemaster", "standard-charge")):
            return line
    # Fall back to the first https:// line that isn't obviously a webpage
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("http") and not any(
            kw in line.lower() for kw in ("html", "price-transparency", "billing")
        ):
            return line

    return None


# ============================================================
# MRF fetching and parsing
# ============================================================

_PLACEHOLDER_VALUES = {0.0, 999999.0, 9999999.0, 99999999.0, 999999999.0}
_MIN_REAL_RATE = 1.0  # anything below $1 is treated as a placeholder


def _looks_like_placeholder(value: float | None) -> bool:
    """True if a rate value is a known placeholder rather than a real dollar amount."""
    if value is None:
        return True
    if value < _MIN_REAL_RATE:
        return True
    return value in _PLACEHOLDER_VALUES


def _extract_float(obj: dict, *keys: str) -> float | None:
    """Try multiple key names, return the first non-None float found."""
    for key in keys:
        val = obj.get(key)
        if val is None:
            continue
        try:
            f = float(val)
            return f
        except (TypeError, ValueError):
            continue
    return None


def _normalize_code(code: str) -> str:
    """Normalize a procedure code for lookup: strip spaces, uppercase."""
    return code.strip().upper()


def _parse_mrf_json(raw_json: dict, target_codes: set[str]) -> dict[str, MrfCodeRate]:
    """
    Parse the CMS-standard MRF JSON structure and extract rates for
    target_codes. Returns a dict of normalized_code -> MrfCodeRate.

    Handles the 2024/2025 CMS JSON template format:
    standard_charge_information[].code_information[].code
    and the older "items_and_services" format some hospitals still use.
    """
    found: dict[str, MrfCodeRate] = {}

    # CMS 2024+ template
    items = raw_json.get("standard_charge_information") or []
    # Some older hospitals use "items_and_services" or "ChargeDescription"
    if not items:
        items = raw_json.get("items_and_services") or []

    for item in items:
        # Extract codes for this line item
        codes_in_item: list[tuple[str, str]] = []  # (normalized_code, code_type)

        for code_info in item.get("code_information") or []:
            code = code_info.get("code") or code_info.get("billing_code") or ""
            code_type = code_info.get("type") or code_info.get("billing_code_type") or "UNKNOWN"
            normalized = _normalize_code(str(code))
            if normalized in target_codes:
                codes_in_item.append((normalized, code_type))

        # Also check top-level code fields (older format)
        for top_key in ("cpt_code", "hcpcs_code", "billing_code", "code"):
            code = item.get(top_key)
            if code:
                normalized = _normalize_code(str(code))
                if normalized in target_codes and not any(c[0] == normalized for c in codes_in_item):
                    code_type = "CPT" if top_key == "cpt_code" else "HCPCS"
                    codes_in_item.append((normalized, code_type))

        if not codes_in_item:
            continue

        description = (
            item.get("description") or item.get("item_or_service") or
            item.get("ChargeDescription") or None
        )

        # Extract standard charges
        charges = item.get("standard_charges") or item.get("standard_charge") or [item]
        if isinstance(charges, dict):
            charges = [charges]

        for charge in charges:
            gross = _extract_float(charge,
                "gross_charge", "gross_charges", "chargemaster_rate", "Charges")
            cash = _extract_float(charge,
                "discounted_cash_price", "cash_price", "self_pay")
            min_neg = _extract_float(charge,
                "minimum_negotiated_charge", "min_negotiated", "min")
            max_neg = _extract_float(charge,
                "maximum_negotiated_charge", "max_negotiated", "max")

            # Collect payer-specific rates
            payer_rates = []
            for pi in charge.get("payers_information") or charge.get("payer_specific_negotiated_charge") or []:
                payer_name = pi.get("payer_name") or pi.get("payer") or ""
                plan_name = pi.get("plan_name") or pi.get("plan") or ""
                rate = _extract_float(pi,
                    "standard_charge_dollar", "negotiated_rate", "rate", "amount")
                if rate is not None and not _looks_like_placeholder(rate):
                    payer_rates.append({
                        "payer_name": payer_name,
                        "plan_name": plan_name,
                        "rate": rate,
                    })

            for normalized_code, code_type in codes_in_item:
                if normalized_code not in found:
                    found[normalized_code] = MrfCodeRate(
                        code=normalized_code,
                        code_type=code_type,
                        description=description,
                        gross_charge=gross if not _looks_like_placeholder(gross) else None,
                        discounted_cash_price=cash if not _looks_like_placeholder(cash) else None,
                        min_negotiated_charge=min_neg if not _looks_like_placeholder(min_neg) else None,
                        max_negotiated_charge=max_neg if not _looks_like_placeholder(max_neg) else None,
                        payer_rates=payer_rates,
                    )

    return found


def _parse_mrf_csv(csv_text: str, target_codes: set[str]) -> dict[str, MrfCodeRate]:
    """
    Parse the CMS wide-form CSV MRF format. The CSV has a fixed header row
    followed by data rows. Column names vary; we match on common variants.
    Returns same shape as _parse_mrf_json.
    """
    import csv
    import io

    found: dict[str, MrfCodeRate] = {}
    reader = csv.DictReader(io.StringIO(csv_text))

    # Normalize header names for fuzzy matching
    def _find_col(row: dict, *candidates: str) -> str | None:
        keys_lower = {k.lower().replace(" ", "_").replace("-", "_"): k for k in row}
        for c in candidates:
            if c.lower() in keys_lower:
                return keys_lower[c.lower()]
        return None

    for row in reader:
        code_col = _find_col(row, "code", "cpt_code", "hcpcs_code", "billing_code")
        if not code_col:
            continue
        code_val = row.get(code_col, "").strip()
        normalized = _normalize_code(code_val)
        if normalized not in target_codes:
            continue

        type_col = _find_col(row, "code_type", "billing_code_type", "type")
        desc_col = _find_col(row, "description", "item_or_service", "charge_description")
        gross_col = _find_col(row, "gross_charge", "chargemaster_rate", "gross_charges")
        cash_col = _find_col(row, "discounted_cash_price", "cash_price", "self_pay")
        min_col = _find_col(row, "minimum_negotiated_charge", "min_negotiated")
        max_col = _find_col(row, "maximum_negotiated_charge", "max_negotiated")

        def _safe_float(col):
            if not col:
                return None
            try:
                v = float(row.get(col, "") or 0)
                return v if not _looks_like_placeholder(v) else None
            except (TypeError, ValueError):
                return None

        if normalized not in found:
            found[normalized] = MrfCodeRate(
                code=normalized,
                code_type=(row.get(type_col, "UNKNOWN") if type_col else "UNKNOWN"),
                description=(row.get(desc_col) if desc_col else None),
                gross_charge=_safe_float(gross_col),
                discounted_cash_price=_safe_float(cash_col),
                min_negotiated_charge=_safe_float(min_col),
                max_negotiated_charge=_safe_float(max_col),
                payer_rates=[],  # wide-form CSV doesn't carry payer-specific rates
            )

    return found


# ============================================================
# Main entry point
# ============================================================

def fetch_mrf_rates(
    facility_id: str,
    codes: list[str],
    mrf_url: str | None = None,
    hospital_domain: str | None = None,
) -> MrfFindingResult:
    """
    Fetch and parse the MRF for a facility, returning rates for `codes`.

    mrf_url: use this if already known (from health_systems.mrf_url).
    hospital_domain: if mrf_url is None, try to discover it from cms-hpt.txt
                     at this domain.

    All four failure modes are returned as structured results, not exceptions,
    because every status is user-facing information:
      - mrf_url_unknown  → no URL to try
      - mrf_unreachable  → URL exists but fetch failed
      - mrf_unpopulated  → MRF parsed but rates are placeholders
      - codes_not_in_mrf → MRF parsed but these codes aren't listed
      - rates_found      → real dollar amounts found for at least one code
    """
    target_codes = {_normalize_code(c) for c in codes if c}

    # Step 1: resolve MRF URL
    resolved_url = mrf_url
    if not resolved_url and hospital_domain:
        resolved_url = discover_mrf_url(hospital_domain)

    if not resolved_url:
        return MrfFindingResult(
            facility_id=facility_id,
            mrf_url=None,
            status="mrf_url_unknown",
            status_detail=(
                "No machine-readable file URL is on file for this hospital, and "
                "no hospital domain was provided to attempt discovery via cms-hpt.txt. "
                "This is worth noting when negotiating: CMS requires all hospitals to "
                "publish price transparency data."
            ),
            codes_queried=list(codes),
            rates={},
        )

    # Step 2: fetch the MRF (partial fetch for large files)
    try:
        with httpx.stream(
            "GET", resolved_url,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"Accept-Encoding": "gzip, deflate"},
        ) as response:
            if response.status_code >= 400:
                return MrfFindingResult(
                    facility_id=facility_id,
                    mrf_url=resolved_url,
                    status="mrf_unreachable",
                    status_detail=(
                        f"The hospital's price transparency file at {resolved_url} "
                        f"returned HTTP {response.status_code}. This is a compliance "
                        f"concern: CMS requires hospitals to keep this URL accessible."
                    ),
                    codes_queried=list(codes),
                    rates={},
                )

            content_type = response.headers.get("content-type", "").lower()
            raw_bytes = b""
            for chunk in response.iter_bytes():
                raw_bytes += chunk
                if len(raw_bytes) >= _MRF_FETCH_MAX_BYTES:
                    # Partial fetch -- enough to cover most small-to-medium MRFs
                    break

    except httpx.HTTPError as exc:
        return MrfFindingResult(
            facility_id=facility_id,
            mrf_url=resolved_url,
            status="mrf_unreachable",
            status_detail=(
                f"Could not reach the hospital's price transparency file at "
                f"{resolved_url}: {type(exc).__name__}. This may be a temporary "
                f"network issue, or the hospital may have removed the file."
            ),
            codes_queried=list(codes),
            rates={},
        )

    # Step 3: parse -- try JSON first, fall back to CSV
    found_rates: dict[str, MrfCodeRate] = {}
    parse_error: str | None = None

    url_lower = resolved_url.lower()
    is_csv = "csv" in url_lower or ("csv" in content_type)

    if not is_csv:
        try:
            # Handle gzip-compressed JSON
            text = raw_bytes
            if resolved_url.endswith(".gz") or "gzip" in content_type:
                import gzip
                text = gzip.decompress(raw_bytes)
            raw_json = json.loads(text)
            found_rates = _parse_mrf_json(raw_json, target_codes)
        except (json.JSONDecodeError, Exception) as exc:
            # Try CSV as fallback
            try:
                found_rates = _parse_mrf_csv(raw_bytes.decode("utf-8", errors="replace"), target_codes)
            except Exception as csv_exc:
                parse_error = f"JSON parse failed ({exc}); CSV fallback also failed ({csv_exc})"

    else:
        try:
            found_rates = _parse_mrf_csv(raw_bytes.decode("utf-8", errors="replace"), target_codes)
        except Exception as exc:
            parse_error = f"CSV parse failed: {exc}"

    if parse_error:
        return MrfFindingResult(
            facility_id=facility_id,
            mrf_url=resolved_url,
            status="mrf_unreachable",
            status_detail=(
                f"The hospital's price transparency file was fetched but could not "
                f"be parsed ({parse_error}). The file may be in a non-standard format."
            ),
            codes_queried=list(codes),
            rates={},
        )

    # Step 4: classify the result
    if not found_rates:
        return MrfFindingResult(
            facility_id=facility_id,
            mrf_url=resolved_url,
            status="codes_not_in_mrf",
            status_detail=(
                f"The hospital's price transparency file was found at {resolved_url} "
                f"but did not list prices for the procedure codes on your bill "
                f"({', '.join(sorted(target_codes))}). Hospitals are required to "
                f"publish standard charges for all items and services they provide."
            ),
            codes_queried=list(codes),
            rates={},
        )

    # Check if all found rates are placeholders
    all_rates = list(found_rates.values())
    all_populated = any(
        r.gross_charge or r.discounted_cash_price or
        r.min_negotiated_charge or r.max_negotiated_charge or r.payer_rates
        for r in all_rates
    )

    if not all_populated:
        return MrfFindingResult(
            facility_id=facility_id,
            mrf_url=resolved_url,
            status="mrf_unpopulated",
            status_detail=(
                f"The hospital's price transparency file lists the procedure codes "
                f"from your bill but all rate fields are blank or contain placeholder "
                f"values. As of May 2025, CMS requires hospitals to encode actual "
                f"dollar amounts — this is a compliance red flag worth raising in "
                f"your negotiation."
            ),
            codes_queried=list(codes),
            rates=found_rates,
        )

    codes_with_rates = [code for code, rate in found_rates.items() if
                        any([rate.gross_charge, rate.discounted_cash_price,
                             rate.min_negotiated_charge, rate.payer_rates])]
    codes_missing = sorted(target_codes - set(found_rates.keys()))

    detail_parts = [
        f"Found published rates for {len(codes_with_rates)} of "
        f"{len(target_codes)} procedure code(s) on your bill."
    ]
    if codes_missing:
        detail_parts.append(
            f"Not found in MRF: {', '.join(codes_missing)}."
        )
    for code, rate in found_rates.items():
        parts = []
        if rate.gross_charge:
            parts.append(f"gross ${rate.gross_charge:,.0f}")
        if rate.discounted_cash_price:
            parts.append(f"cash ${rate.discounted_cash_price:,.0f}")
        if rate.min_negotiated_charge and rate.max_negotiated_charge:
            parts.append(
                f"negotiated ${rate.min_negotiated_charge:,.0f}–"
                f"${rate.max_negotiated_charge:,.0f}"
            )
        if parts:
            detail_parts.append(f"{code}: {', '.join(parts)}.")

    return MrfFindingResult(
        facility_id=facility_id,
        mrf_url=resolved_url,
        status="rates_found",
        status_detail=" ".join(detail_parts),
        codes_queried=list(codes),
        rates=found_rates,
    )
