"""
RobinHealth: bill ingestion pipeline.

Given an uploaded medical bill (image or PDF), this pipeline:

  1. Extracts structured data -- provider identity, line items (with
     procedure codes where present), date of service, total billed amount
     -- via Claude vision. Output feeds `bills` / `bill_line_items`.

  2. Matches the extracted provider against the `facilities` table to find
     a `facility_id` (and therefore `fap_id`), connecting this bill to the
     existing FAP pipeline (fap_pipeline.py).

A facility_match_status of 'unmatched' is a valid, expected outcome for
long-tail providers not yet in `facilities` -- the orchestration layer
(not this module) is responsible for creating the facility row and queuing
it for FAP parsing in that case.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

import llm_client
import repository


# Name-similarity score (0-1) above which a candidate facility is treated as
# an automatic match rather than left for manual review.
MATCH_CONFIDENCE_THRESHOLD = 0.82


# ============================================================
# Data containers (mirror the Postgres schema; ORM-agnostic)
# ============================================================

@dataclass
class ExtractedLineItem:
    line_number: int
    description: str
    procedure_code: str | None
    code_type: str | None  # 'cpt' | 'hcpcs' | 'revenue_code' | 'ndc' | 'unknown'
    units: float | None
    billed_amount: float


@dataclass
class ExtractedProviderInfo:
    name: str | None
    npi: str | None
    tax_id: str | None
    address: str | None
    state: str | None  # two-letter code, if determinable from the address


@dataclass
class BillExtraction:
    provider: ExtractedProviderInfo
    date_of_service: str | None  # ISO date string, e.g. "2026-03-14"
    account_number: str | None
    line_items: list[ExtractedLineItem]
    total_billed_amount: float | None
    parsing_confidence: str  # 'high' | 'medium' | 'low' | 'failed'
    raw_text: str | None


@dataclass
class FacilityRecord:
    """Minimal projection of a `facilities` row, as returned by a lookup query."""
    id: str
    name: str
    npi: str | None
    state: str | None


@dataclass
class MatchResult:
    facility_id: str | None
    confidence: float  # 0-1
    status: str  # 'matched' | 'new_facility_created' | 'unmatched'


# ============================================================
# Stage 1: bill extraction (Claude vision)
# ============================================================

EXTRACTION_PROMPT = """\
You are extracting structured data from a patient's medical bill for \
RobinHealth, a billing advocacy service. The bill is provided as an image \
or PDF.

Extract the following as JSON. Use null for any field that cannot be \
determined -- do not guess values that aren't legible or present.

{
  "provider": {
    "name": string or null,
    "npi": string or null,
    "tax_id": string or null,
    "address": string or null,
    "state": <two-letter state code> or null
  },
  "date_of_service": <ISO date YYYY-MM-DD> or null,
  "account_number": string or null,
  "line_items": [
    {
      "line_number": int,
      "description": string,
      "procedure_code": string or null,
      "code_type": one of "cpt" | "hcpcs" | "revenue_code" | "ndc" | "unknown" or null,
      "units": number or null,
      "billed_amount": number
    }
  ],
  "total_billed_amount": number or null,
  "parsing_confidence": one of "high" | "medium" | "low" | "failed"
}

Bills vary widely in format. If the document is a summary/cover page \
without itemized line items, return an empty line_items list and set \
parsing_confidence to "low" -- this is itself a useful signal (it tells \
RobinHealth an itemized bill needs to be requested from the provider), not \
a failure.
"""


def extract_bill(document_bytes: bytes, media_type: str) -> BillExtraction:
    """
    Pass the bill image/PDF to the configured vision-capable model (see
    llm_client.py -- any OpenAI-compatible endpoint, suggested default
    Qwen3-VL) with EXTRACTION_PROMPT, and parse the JSON response into a
    BillExtraction.
    """
    data = llm_client.complete_json(EXTRACTION_PROMPT, images=[(document_bytes, media_type)])
    return BillExtraction(
        provider=ExtractedProviderInfo(**data["provider"]),
        date_of_service=data["date_of_service"],
        account_number=data["account_number"],
        line_items=[ExtractedLineItem(**li) for li in data["line_items"]],
        total_billed_amount=data["total_billed_amount"],
        parsing_confidence=data["parsing_confidence"],
        raw_text=None,  # vision extraction has no separate raw-text fallback
    )


# ============================================================
# Stage 2: provider -> facility matching
# ============================================================

def fetch_candidate_facilities(
    npi: str | None,
    name: str | None,
    state: str | None,
) -> list[FacilityRecord]:
    """
    Query the `facilities` table for candidates, via repository.py.

    Backed by repository.find_facilities_by_npi /
    repository.find_facilities_by_state, which return plain dicts rather
    than FacilityRecord directly -- importing FacilityRecord into
    repository.py would create a circular import, since this function
    needs to import repository.py. Wrapping the dicts here, in the module
    that owns FacilityRecord, is the natural place for that conversion.
    """
    rows: list[dict] = []
    if npi:
        rows = repository.find_facilities_by_npi(npi)
    if not rows and state:
        rows = repository.find_facilities_by_state(state)

    return [
        FacilityRecord(id=str(r["id"]), name=r["name"], npi=r["npi"], state=r["state"])
        for r in rows
    ]


def _normalize_name(name: str) -> str:
    """
    Lowercase and strip common trailing suffixes that cause false
    mismatches. Repeats until nothing more changes, rather than stopping
    after one pass through the suffix list -- a single pass missed
    stacked suffixes (e.g. "X Medical Center, Inc" only had ", inc"
    stripped, since " medical center" was checked before the string
    ended with it).
    """
    name = name.lower().strip()
    changed = True
    while changed:
        changed = False
        for suffix in (
            " hospital",
            " medical center",
            " health system",
            " healthcare",
            ", inc",
            ", llc",
            " inc.",
            " llc.",
        ):
            # endswith + slice, not .replace() -- a suffix is by
            # definition trailing; .replace() would also strip the same
            # text if it happened to appear in the middle of an
            # unrelated word (found via testing: "St. Hospitaler's
            # Medical Group" was getting mangled into "st.er's medical
            # group", since " hospital" matched inside "Hospitaler's").
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                changed = True
    return " ".join(name.split())


def name_similarity(a: str, b: str) -> float:
    """Dependency-free similarity score in [0, 1] between two provider names."""
    return difflib.SequenceMatcher(None, _normalize_name(a), _normalize_name(b)).ratio()


def match_facility(provider: ExtractedProviderInfo) -> MatchResult:
    """
    Match an extracted provider against `facilities`.

      1. If an NPI was extracted and exactly one facility shares it, that's
         an exact match (confidence 1.0).
      2. Otherwise, fetch state-scoped candidates and pick the best
         name-similarity match above MATCH_CONFIDENCE_THRESHOLD.
      3. No sufficiently confident match -> status='unmatched'. Creating a
         new `facilities` row and queuing it for FAP parsing is the
         orchestration layer's responsibility, not this lookup's --
         keeping this function read-only makes it safe to call for
         "preview" matches before a case is committed.
    """
    if provider.npi:
        npi_matches = fetch_candidate_facilities(npi=provider.npi, name=None, state=None)
        if len(npi_matches) == 1:
            return MatchResult(facility_id=npi_matches[0].id, confidence=1.0, status="matched")

    if not provider.name:
        return MatchResult(facility_id=None, confidence=0.0, status="unmatched")

    candidates = fetch_candidate_facilities(npi=None, name=provider.name, state=provider.state)
    if not candidates:
        return MatchResult(facility_id=None, confidence=0.0, status="unmatched")

    best, score = max(
        ((c, name_similarity(provider.name, c.name)) for c in candidates),
        key=lambda pair: pair[1],
    )

    if score >= MATCH_CONFIDENCE_THRESHOLD:
        return MatchResult(facility_id=best.id, confidence=score, status="matched")

    return MatchResult(facility_id=None, confidence=score, status="unmatched")


def match_health_system(provider_name: str | None, candidates: list[dict]) -> str | None:
    """
    Match a provider's name against known health systems, so a newly
    created facility can be linked to one -- the link worker.py's
    _handle_parse_fap needs to resolve a real fap_url/pls_url/
    billing_policy_url, rather than always having nowhere to look.

    candidates is a list of {"id": ..., "name": ...} dicts, e.g. from
    repository.fetch_all_health_systems(). Pure logic, no DB access --
    same split as match_facility/fetch_candidate_facilities, and for the
    same reason: read-only and side-effect-free, so it's safe to call
    for a "preview" match before anything commits.

    Returns the matched health_system_id, or None if no candidate clears
    MATCH_CONFIDENCE_THRESHOLD. Reuses match_facility's threshold and
    name_similarity rather than a separate one -- a health system's name
    and a facility's name aren't the same kind of string (one's often a
    corporate parent, the other a specific hospital), but the underlying
    problem this threshold was tuned for -- "close enough to be the same
    real-world entity, allowing for OCR noise and incidental suffix
    differences" -- is the same problem either way.
    """
    if not provider_name or not candidates:
        return None

    best, score = max(
        ((c, name_similarity(provider_name, c["name"])) for c in candidates),
        key=lambda pair: pair[1],
    )

    if score >= MATCH_CONFIDENCE_THRESHOLD:
        return best["id"]

    return None
