"""
RobinHealth: line-item billing-error audit.

Looks INSIDE the bill -- at the individual charge lines -- for the concrete,
hard-to-deny errors that produce the fastest write-offs:

  - Duplicate charges (the same code/service billed more than once)
  - Unbundling (CMS National Correct Coding Initiative procedure-to-procedure
    edits: a component code billed separately alongside the comprehensive code
    that already includes it)
  - Excessive units (a quantity above the CMS Medically Unlikely Edit -- the
    maximum units of a code payable per patient per day)

Unlike the benchmark/eligibility arguments in synthesis.py -- which attack the
*total* ("your bill is 200% of Medicare") -- these attack a specific line with a
specific, verifiable reason a billing department cannot wave away ("line 14, CPT
80048, is a component of the CPT 80053 panel on line 9 per CMS NCCI edits").
Those are the points that get conceded fastest.

Pure logic + static/loadable reference data (no LLM, no DB) so it is
deterministic and unit-testable, exactly like legal_leverage.py. Every finding
is phrased as a verifiable observation and a request to confirm or correct --
never a flat accusation -- because the entire value of this layer is precision,
and a false "you overcharged me" claim is worse than saying nothing.

Reference data
--------------
Duplicate detection needs NO reference data and is always active.

NCCI PTP edits and MUEs are published by CMS quarterly. A small, conservative
set of near-universal entries is embedded so the audit is useful out of the box;
load the full quarterly files for complete coverage (degrades cleanly to the
embedded seed if the files aren't present, the same way pricing_pipeline's OPPS
loader degrades to an empty table):
  - NCCI PTP edits: https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits
  - MUE tables:     https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field


# ============================================================
# Finding model
# ============================================================

# Finding kinds (stable strings; persisted and matched downstream).
KIND_DUPLICATE = "duplicate"
KIND_UNBUNDLING = "ncci_ptp"          # NCCI procedure-to-procedure edit
KIND_EXCESS_UNITS = "mue"             # exceeds Medically Unlikely Edit
KIND_ADDON_NO_PRIMARY = "addon_without_primary"


@dataclass
class LineItemFinding:
    kind: str                       # one of KIND_* above
    severity: str                   # "high" | "medium"
    line_numbers: list[int]         # the implicated bill line numbers
    codes: list[str]                # the implicated procedure codes
    estimated_overcharge: float | None  # dollars likely removable, if computable
    patient_summary: str            # plain-language, for the user
    provider_text: str              # citation-backed, letter-ready (provider-facing)


# ============================================================
# NCCI PTP edit pairs
# ============================================================
# (column_1_code, column_2_code, modifier_indicator)
#   modifier_indicator 0 -> the two codes are never separately payable together
#   modifier_indicator 1 -> separately payable only with an appropriate modifier
#                           and supporting documentation (otherwise the column-2
#                           code is the overcharge)
#   modifier_indicator 9 -> edit not active (ignored)
# When both codes of a pair appear on a bill, the column-2 code is the line we
# flag (it is the one bundled into column 1). We do not have reliable modifier
# data off a scanned bill, so indicator-1 pairs are flagged at "medium" severity
# ("confirm a modifier applies") and indicator-0 at "high".
#
# This embedded set is deliberately tiny and limited to long-standing,
# near-universal lab-panel bundles. Load the full CMS quarterly file via
# load_ncci_ptp_from_csv() for real coverage.
NCCIPair = tuple[str, str, int]

_EMBEDDED_NCCI_PTP: list[NCCIPair] = [
    ("80053", "80048", 1),  # Comprehensive metabolic panel includes basic metabolic panel
    ("80053", "82565", 1),  # CMP includes a standalone creatinine
    ("80053", "84443", 1),  # (commonly bundled) CMP-adjacent TSH double-billing guard
    ("85025", "85027", 1),  # CBC w/ differential includes CBC w/o differential
    ("80050", "80053", 1),  # General health panel includes the CMP
    ("80050", "85025", 1),  # General health panel includes the CBC
]

# Human-readable names for embedded codes, used to make findings legible without
# requiring a full code dictionary. Unknown codes simply render by number.
_CODE_DESCRIPTIONS: dict[str, str] = {
    "80048": "basic metabolic panel",
    "80050": "general health panel",
    "80053": "comprehensive metabolic panel",
    "82565": "creatinine (blood)",
    "84443": "thyroid stimulating hormone (TSH)",
    "85025": "complete blood count with differential",
    "85027": "complete blood count without differential",
    "36415": "routine venipuncture (blood draw)",
    "93000": "electrocardiogram (EKG)",
}


# ============================================================
# Medically Unlikely Edits (max units per patient per day)
# ============================================================
# A few near-universal "1 per day" services. Load the full CMS file via
# load_mue_from_csv() for real coverage.
_EMBEDDED_MUE: dict[str, int] = {
    "80048": 1,
    "80050": 1,
    "80053": 1,
    "85025": 1,
    "85027": 1,
    "93000": 1,
    "36415": 1,
}


# ============================================================
# Reference-data loaders (full CMS quarterly files)
# ============================================================

def load_ncci_ptp_from_csv(path: str) -> list[NCCIPair]:
    """
    Load NCCI procedure-to-procedure edit pairs from a CSV with columns
    column_1, column_2, modifier_indicator. Returns the embedded seed merged
    with the file's pairs. Missing/unreadable file -> embedded seed only
    (clean degradation, same contract as pricing_pipeline's OPPS loader).
    """
    pairs: list[NCCIPair] = list(_EMBEDDED_NCCI_PTP)
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    c1 = (row["column_1"] or "").strip().upper()
                    c2 = (row["column_2"] or "").strip().upper()
                    ind = int(row.get("modifier_indicator", 0) or 0)
                    if c1 and c2:
                        pairs.append((c1, c2, ind))
                except (KeyError, ValueError, AttributeError):
                    continue  # skip malformed rows
    except (FileNotFoundError, PermissionError) as exc:
        print(f"[line_item_audit] NCCI PTP file not loaded ({path}): {exc}")
    return pairs


def load_mue_from_csv(path: str) -> dict[str, int]:
    """
    Load MUE max-units values from a CSV with columns code, max_units. Returns
    the embedded seed merged with (and overridden by) the file's values.
    Missing/unreadable file -> embedded seed only.
    """
    table: dict[str, int] = dict(_EMBEDDED_MUE)
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    code = (row["code"] or "").strip().upper()
                    mx = int(row["max_units"])
                    if code:
                        table[code] = mx
                except (KeyError, ValueError, AttributeError):
                    continue
    except (FileNotFoundError, PermissionError) as exc:
        print(f"[line_item_audit] MUE file not loaded ({path}): {exc}")
    return table


# ============================================================
# Helpers
# ============================================================

def _code(item) -> str | None:
    c = getattr(item, "procedure_code", None)
    if not c:
        return None
    return str(c).strip().upper()


def _describe(code: str) -> str:
    name = _CODE_DESCRIPTIONS.get(code.upper())
    return f"{name} (code {code})" if name else f"code {code}"


def _unit_price(item) -> float | None:
    """Best-effort per-unit price for an estimated overcharge."""
    units = getattr(item, "units", None)
    amt = getattr(item, "billed_amount", None)
    if amt is None:
        return None
    if units and units > 0:
        return amt / units
    return amt


# ============================================================
# Individual checks
# ============================================================

def _find_duplicates(line_items: list) -> list[LineItemFinding]:
    """
    Flag the same billable service appearing on more than one line. Conservative:
    we only group lines that share BOTH a procedure code and an identical billed
    amount (or, when no code is present, an identical description and amount), so
    a legitimately repeated service billed with units isn't mistaken for a
    duplicate. Framed as "please confirm this wasn't billed twice", not an
    accusation.
    """
    findings: list[LineItemFinding] = []
    groups: dict[tuple, list] = {}
    for item in line_items:
        code = _code(item)
        amt = getattr(item, "billed_amount", None)
        if amt is None:
            continue
        if code:
            key = ("code", code, round(float(amt), 2))
        else:
            desc = (getattr(item, "description", "") or "").strip().lower()
            if not desc:
                continue
            key = ("desc", desc, round(float(amt), 2))
        groups.setdefault(key, []).append(item)

    for key, items in groups.items():
        if len(items) < 2:
            continue
        extra = len(items) - 1
        amt = float(getattr(items[0], "billed_amount", 0.0) or 0.0)
        overcharge = round(extra * amt, 2)
        line_nums = [int(getattr(i, "line_number", 0) or 0) for i in items]
        if key[0] == "code":
            label = _describe(key[1])
            codes = [key[1]]
        else:
            label = (getattr(items[0], "description", "") or "this service").strip()
            codes = []
        findings.append(LineItemFinding(
            kind=KIND_DUPLICATE,
            severity="high",
            line_numbers=line_nums,
            codes=codes,
            estimated_overcharge=overcharge,
            patient_summary=(
                f"{label.capitalize()} appears {len(items)} times on your bill at "
                f"${amt:,.2f} each. If it was only done once, you're being charged "
                f"about ${overcharge:,.2f} too much."
            ),
            provider_text=(
                f"The account reflects {len(items)} separate charges for "
                f"{label} at ${amt:,.2f} each (lines "
                f"{', '.join(str(n) for n in line_nums)}). Unless the service was "
                f"genuinely rendered multiple times on this date, this appears to "
                f"be a duplicate charge. Please confirm the medical record supports "
                f"each occurrence or remove the duplicate(s), reducing the balance "
                f"by approximately ${overcharge:,.2f}."
            ),
        ))
    return findings


def _find_unbundling(line_items: list, ncci_pairs: list[NCCIPair]) -> list[LineItemFinding]:
    """
    Flag NCCI procedure-to-procedure edits: a column-2 (component) code billed on
    the same date as the column-1 (comprehensive) code that already includes it.
    The column-2 line is the overcharge.
    """
    findings: list[LineItemFinding] = []
    by_code: dict[str, list] = {}
    for item in line_items:
        c = _code(item)
        if c:
            by_code.setdefault(c, []).append(item)

    seen: set[tuple[str, str]] = set()
    for c1, c2, indicator in ncci_pairs:
        if indicator == 9:
            continue
        if c1 in by_code and c2 in by_code and (c1, c2) not in seen:
            seen.add((c1, c2))
            comp_item = by_code[c2][0]  # the bundled component line
            overcharge = getattr(comp_item, "billed_amount", None)
            line_nums = sorted({
                int(getattr(i, "line_number", 0) or 0)
                for i in (by_code[c1] + by_code[c2])
            })
            severity = "high" if indicator == 0 else "medium"
            confirm = (
                "These codes are never separately payable together"
                if indicator == 0
                else "This code is separately payable only with an appropriate "
                     "modifier and supporting documentation"
            )
            findings.append(LineItemFinding(
                kind=KIND_UNBUNDLING,
                severity=severity,
                line_numbers=line_nums,
                codes=[c1, c2],
                estimated_overcharge=round(float(overcharge), 2) if overcharge else None,
                patient_summary=(
                    f"You were billed separately for {_describe(c2)}, which is "
                    f"already included in {_describe(c1)} on the same day. Billing "
                    f"both is called \"unbundling\""
                    + (
                        f" and may be removable for about ${float(overcharge):,.2f}."
                        if overcharge else "."
                    )
                ),
                provider_text=(
                    f"Per CMS National Correct Coding Initiative procedure-to-procedure "
                    f"edits, {_describe(c2)} is a component of {_describe(c1)}, which "
                    f"appears on the same date of service. {confirm}. Please confirm "
                    f"the edit was correctly applied or remove the separately billed "
                    f"{_describe(c2)} charge"
                    + (
                        f", reducing the balance by approximately ${float(overcharge):,.2f}."
                        if overcharge else "."
                    )
                ),
            ))
    return findings


def _find_excess_units(line_items: list, mue_table: dict[str, int]) -> list[LineItemFinding]:
    """
    Flag lines whose billed units exceed the CMS Medically Unlikely Edit (the
    maximum units of a code payable per patient per day). The excess units are
    the overcharge.
    """
    findings: list[LineItemFinding] = []
    for item in line_items:
        c = _code(item)
        units = getattr(item, "units", None)
        if not c or units is None or c not in mue_table:
            continue
        mue = mue_table[c]
        if units <= mue:
            continue
        excess = units - mue
        unit_price = _unit_price(item)
        overcharge = round(excess * unit_price, 2) if unit_price is not None else None
        line_no = int(getattr(item, "line_number", 0) or 0)
        findings.append(LineItemFinding(
            kind=KIND_EXCESS_UNITS,
            severity="high",
            line_numbers=[line_no],
            codes=[c],
            estimated_overcharge=overcharge,
            patient_summary=(
                f"Your bill charges {units:g} units of {_describe(c)}, but Medicare's "
                f"Medically Unlikely Edit allows at most {mue:g} per day"
                + (
                    f". The {excess:g} extra unit(s) are about ${overcharge:,.2f}."
                    if overcharge else "."
                )
            ),
            provider_text=(
                f"Line {line_no} bills {units:g} units of {_describe(c)}. CMS publishes "
                f"a Medically Unlikely Edit of {mue:g} unit(s) per patient per day for "
                f"this code. Please provide documentation supporting units in excess of "
                f"the MUE or correct the quantity"
                + (
                    f", reducing the balance by approximately ${overcharge:,.2f}."
                    if overcharge else "."
                )
            ),
        ))
    return findings


# ============================================================
# Orchestration
# ============================================================

def audit_line_items(
    line_items: list,
    *,
    ncci_pairs: list[NCCIPair] | None = None,
    mue_table: dict[str, int] | None = None,
) -> list[LineItemFinding]:
    """
    Run every line-item check and return findings ranked by estimated dollar
    impact (highest first); findings without a dollar estimate sort last but
    are still returned.

    `ncci_pairs` / `mue_table` default to the embedded conservative seed when not
    supplied. Callers with the full CMS quarterly files loaded (via
    load_ncci_ptp_from_csv / load_mue_from_csv) pass them in for full coverage.
    """
    if not line_items:
        return []
    pairs = ncci_pairs if ncci_pairs is not None else _EMBEDDED_NCCI_PTP
    mue = mue_table if mue_table is not None else _EMBEDDED_MUE

    findings: list[LineItemFinding] = []
    findings += _find_duplicates(line_items)
    findings += _find_unbundling(line_items, pairs)
    findings += _find_excess_units(line_items, mue)

    findings.sort(key=lambda f: -(f.estimated_overcharge or 0.0))
    return findings


def total_estimated_overcharge(findings: list[LineItemFinding]) -> float:
    """Sum of the dollar-quantified findings (duplicates can't be double-counted
    because each group is emitted once)."""
    return round(sum(f.estimated_overcharge or 0.0 for f in findings), 2)


def finding_from_dict(d: dict) -> LineItemFinding:
    """Rehydrate a LineItemFinding from persisted JSON (see synthesis persistence)."""
    return LineItemFinding(
        kind=d.get("kind", ""),
        severity=d.get("severity", "medium"),
        line_numbers=[int(n) for n in (d.get("line_numbers") or [])],
        codes=[str(c) for c in (d.get("codes") or [])],
        estimated_overcharge=d.get("estimated_overcharge"),
        patient_summary=d.get("patient_summary", ""),
        provider_text=d.get("provider_text", ""),
    )
