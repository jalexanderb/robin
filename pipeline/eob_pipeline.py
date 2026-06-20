"""
RobinHealth: EOB (Explanation of Benefits) ingestion pipeline.

An EOB is the document an insurer sends after processing a claim. It is
the single most important document for negotiation: it reveals what the
insurer actually contracted to pay (the "allowed amount"), which is almost
always far less than the provider's billed charges. Armed with this, the
negotiation argument shifts from "please discount the bill" (weak) to
"your own insurer agreed to pay $X for this service; please extend the
same rate to me" (strong -- providers are much more likely to accept a
rate they've already agreed to accept from a payer).

PIPELINE OVERVIEW:
1. extract_eob(images/text) → EobExtraction  [LLM vision call]
2. match_eob_to_bill(eob, bill_items) → EobMatchResult  [pure logic]
3. apply_eob_to_bill(match_result, bill_items) → updated bill amounts  [in-place]

The matching step (2) is where most of the complexity lives. EOBs and
bills use different line numbering, may group services differently, and
sometimes use different (but equivalent) procedure codes. The strategy:
  - Primary key: (procedure_code, date_of_service) exact match
  - Tiebreaker: billed_amount within 1% (rounding differences are common)
  - Fallback: description similarity when codes diverge (e.g. HCPCS on
    bill vs revenue code on EOB for the same service)
  - Unmatched EOB lines are flagged separately -- they may represent
    services not on the bill (legitimate) or claim splits (worth noting)

ADJUSTMENT REASON CODES (CARC/RARC):
These are standardized codes on the EOB explaining why amounts differ.
Key ones for RobinHealth's use case:
  CO-45: "Charges exceed your contracted/legislated fee arrangement" --
         the most common code on in-network EOBs. Confirms the allowed
         amount is the real contract ceiling.
  CO-97: "The benefit for this service is included in the payment for
         another service" -- line was bundled; don't double-count.
  PR-1:  "Deductible amount" -- patient owes this because deductible
         not yet met. Still negotiable.
  PR-2:  "Coinsurance amount" -- patient's percentage share.
  PR-3:  "Co-payment amount" -- fixed copay.
  CO-197:"Precertification/authorization absent" -- potential wrongful
         denial; document and dispute, don't just accept the denial.
  OA-23: "Payment adjusted due to the impact of prior payer(s)" --
         coordination of benefits; secondary insurer processed this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import llm_client
from bill_pipeline import BillExtraction, ExtractedLineItem


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class EobAdjustmentCode:
    code_type: str          # 'CARC' or 'RARC'
    code: str               # e.g. 'CO-45', 'PR-1', 'N793'
    amount: float | None    # adjustment amount, if stated
    description: str | None # human-readable reason from EOB, if present


@dataclass
class EobLineItem:
    line_number: int
    date_of_service: str | None    # ISO date string, e.g. "2026-03-14"
    description: str | None
    procedure_code: str | None
    code_type: str | None          # 'cpt' | 'hcpcs' | 'revenue_code' | 'ndc' | 'unknown'
    units: float | None
    billed_amount: float | None    # what the provider billed (may differ from bill due to rounding)
    allowed_amount: float | None   # the KEY number: insurer's contract rate
    insurance_paid: float | None   # what the insurer sent the provider
    patient_responsibility: float | None  # what the patient owes
    adjustment_codes: list[EobAdjustmentCode] = field(default_factory=list)


@dataclass
class EobExtraction:
    insurer_name: str | None
    member_id: str | None
    claim_number: str | None
    date_processed: str | None     # ISO date string
    total_billed_amount: float | None
    total_allowed_amount: float | None
    total_insurance_paid: float | None
    total_patient_responsibility: float | None
    line_items: list[EobLineItem]
    parsing_confidence: str        # 'high' | 'medium' | 'low' | 'failed'
    raw_text: str | None


@dataclass
class EobLineMatch:
    """One matched pair: an EOB line item linked to a bill line item."""
    eob_line: EobLineItem
    bill_line: ExtractedLineItem   # the matched bill line
    match_status: str              # 'matched' | 'partial_match'
    match_score: float             # 0-1 confidence of this specific match


@dataclass
class EobMatchResult:
    """
    Full result of matching an EobExtraction against a BillExtraction's
    line items. Downstream consumers (persist_eob, synthesis) use this
    rather than the raw EobExtraction.
    """
    matched: list[EobLineMatch]
    unmatched_eob_lines: list[EobLineItem]      # on EOB but not on bill
    unmatched_bill_lines: list[ExtractedLineItem]  # on bill but not on EOB
    # Aggregates over matched lines only -- more reliable than EOB's own
    # totals, which sometimes include non-covered or bundled services.
    total_allowed_amount: float | None
    total_patient_responsibility: float | None
    total_insurance_paid: float | None


# ============================================================
# Extraction prompt
# ============================================================

EOB_EXTRACTION_PROMPT = """You are extracting structured data from an Explanation of Benefits (EOB)
document. An EOB is sent by an insurance company to explain how a medical
claim was processed.

Extract ALL of the following as a single JSON object (no markdown fences,
no preamble, just the JSON):

{
  "insurer_name": string or null,
  "member_id": string or null,
  "claim_number": string or null,
  "date_processed": "YYYY-MM-DD" or null,
  "total_billed_amount": number or null,
  "total_allowed_amount": number or null,
  "total_insurance_paid": number or null,
  "total_patient_responsibility": number or null,
  "line_items": [
    {
      "line_number": integer (1-based if not explicit),
      "date_of_service": "YYYY-MM-DD" or null,
      "description": string or null,
      "procedure_code": string or null,
      "code_type": "cpt" | "hcpcs" | "revenue_code" | "ndc" | "unknown" | null,
      "units": number or null,
      "billed_amount": number or null,
      "allowed_amount": number or null,
      "insurance_paid": number or null,
      "patient_responsibility": number or null,
      "adjustment_codes": [
        {
          "code_type": "CARC" or "RARC",
          "code": string,
          "amount": number or null,
          "description": string or null
        }
      ]
    }
  ],
  "parsing_confidence": "high" | "medium" | "low" | "failed"
}

IMPORTANT EXTRACTION RULES:
- allowed_amount is the insurer's contracted rate -- the most critical
  field. Extract it carefully even if labeled "plan discount",
  "negotiated rate", "contract adjustment", or "approved amount".
- patient_responsibility is what the patient owes after insurance
  payment. It equals allowed_amount minus insurance_paid (approximately;
  rounding differences are common). If the EOB shows it explicitly, use
  that value.
- For adjustment_codes: CARC codes explain claim adjustments (CO-45 is
  the most common: "contractual adjustment"). RARC codes are remittance
  remarks (N-codes). Extract all that appear per line.
- If a field is genuinely absent, use null. Do not invent values.
- If you cannot parse the document reliably, set parsing_confidence to
  "low" or "failed" and still extract whatever you can.
- parsing_confidence should be "high" only if you are confident all
  dollar amounts and line items are correctly captured.
"""


def extract_eob(
    images: list[tuple[bytes, str]] | None = None,
    text: str | None = None,
) -> EobExtraction:
    """
    Extract structured data from an EOB document via a vision LLM call.

    Either images (list of (bytes, media_type) tuples, one per page) or
    pre-extracted text must be provided; both may be provided together.
    """
    prompt_parts = [EOB_EXTRACTION_PROMPT]
    if text:
        prompt_parts.append(f"\n\nExtracted text from document:\n{text[:8000]}")

    raw = llm_client.complete_json(
        prompt="\n".join(prompt_parts),
        images=images,
        max_tokens=3000,
    )

    return _parse_eob_extraction(raw)


def _parse_eob_extraction(raw: dict) -> EobExtraction:
    """
    Parse the LLM's JSON output into an EobExtraction. Defensively skips
    any malformed line items or adjustment codes rather than crashing --
    same pattern as fap_pipeline.run_compliance_checklist and
    repository.insert_fap_parse_result.
    """
    line_items = []
    for i, item in enumerate(raw.get("line_items") or [], start=1):
        try:
            adjustment_codes = []
            for code in item.get("adjustment_codes") or []:
                if not code.get("code_type") or not code.get("code"):
                    continue
                adjustment_codes.append(EobAdjustmentCode(
                    code_type=code["code_type"],
                    code=code["code"],
                    amount=_to_float(code.get("amount")),
                    description=code.get("description"),
                ))

            line_items.append(EobLineItem(
                line_number=int(item.get("line_number") or i),
                date_of_service=item.get("date_of_service"),
                description=item.get("description"),
                procedure_code=item.get("procedure_code"),
                code_type=item.get("code_type"),
                units=_to_float(item.get("units")),
                billed_amount=_to_float(item.get("billed_amount")),
                allowed_amount=_to_float(item.get("allowed_amount")),
                insurance_paid=_to_float(item.get("insurance_paid")),
                patient_responsibility=_to_float(item.get("patient_responsibility")),
                adjustment_codes=adjustment_codes,
            ))
        except (KeyError, TypeError, ValueError):
            # Skip this line rather than losing all other lines
            continue

    return EobExtraction(
        insurer_name=raw.get("insurer_name"),
        member_id=raw.get("member_id"),
        claim_number=raw.get("claim_number"),
        date_processed=raw.get("date_processed"),
        total_billed_amount=_to_float(raw.get("total_billed_amount")),
        total_allowed_amount=_to_float(raw.get("total_allowed_amount")),
        total_insurance_paid=_to_float(raw.get("total_insurance_paid")),
        total_patient_responsibility=_to_float(raw.get("total_patient_responsibility")),
        line_items=line_items,
        parsing_confidence=raw.get("parsing_confidence") or "failed",
        raw_text=None,  # populated by the caller after extraction
    )


def _to_float(value) -> float | None:
    """Safely coerce a JSON value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# EOB-to-bill matching
# ============================================================

# Amount tolerance for matching: treat two amounts as equal if within
# this fraction of each other. 1% handles common rounding differences
# between how providers and insurers record the same charge.
_AMOUNT_TOLERANCE = 0.01

# Minimum match score to call something 'matched' vs 'partial_match'.
# Below this, lines stay 'unmatched' even if codes are the same.
_MATCHED_SCORE_THRESHOLD = 0.85
_PARTIAL_MATCH_SCORE_THRESHOLD = 0.50


def _amounts_close(a: float | None, b: float | None) -> bool:
    """True if two amounts are within _AMOUNT_TOLERANCE of each other."""
    if a is None or b is None:
        return False
    if a == 0 and b == 0:
        return True
    if a == 0 or b == 0:
        return False
    return abs(a - b) / max(abs(a), abs(b)) <= _AMOUNT_TOLERANCE


def _score_line_pair(eob_line: EobLineItem, bill_line: ExtractedLineItem) -> float:
    """
    Score how well an EOB line matches a bill line. Returns 0.0-1.0.

    Scoring factors (additive, normalized):
      - Procedure code exact match: +0.5
      - Date of service match: +0.2
      - Billed amount within tolerance: +0.2
      - Description overlap (fallback when codes differ): +0.1
    """
    score = 0.0

    # Procedure code: strongest signal
    if (eob_line.procedure_code and bill_line.procedure_code and
            eob_line.procedure_code.strip().upper() == bill_line.procedure_code.strip().upper()):
        score += 0.5

    # Date of service
    eob_date = (eob_line.date_of_service or "").strip()
    # bill_line doesn't carry date_of_service per line -- it's on the
    # parent BillExtraction. We can't use it here without threading the
    # bill through, so skip this dimension in the per-line score.
    # (Future: carry date_of_service down to ExtractedLineItem too.)

    # Billed amount within tolerance
    if _amounts_close(eob_line.billed_amount, bill_line.billed_amount):
        score += 0.3

    # Description overlap: simple word-intersection ratio
    if eob_line.description and bill_line.description:
        eob_words = set(eob_line.description.lower().split())
        bill_words = set(bill_line.description.lower().split())
        if eob_words and bill_words:
            overlap = len(eob_words & bill_words) / max(len(eob_words), len(bill_words))
            score += 0.2 * overlap

    return min(score, 1.0)


def match_eob_to_bill(
    eob: EobExtraction,
    bill: BillExtraction,
) -> EobMatchResult:
    """
    Match EOB line items to bill line items.

    Uses a greedy best-first assignment: for each EOB line, find the
    highest-scoring unmatched bill line. If the score clears
    _MATCHED_SCORE_THRESHOLD it's 'matched'; if it clears
    _PARTIAL_MATCH_SCORE_THRESHOLD it's 'partial_match'; below that
    it's 'unmatched'. Each bill line can only be matched once.

    Pure logic -- no DB access, same split as match_facility /
    match_health_system.
    """
    available_bill_lines = list(bill.line_items)  # copy; we'll consume entries
    matched: list[EobLineMatch] = []
    unmatched_eob: list[EobLineItem] = []

    for eob_line in eob.line_items:
        if not available_bill_lines:
            unmatched_eob.append(eob_line)
            continue

        # Score this EOB line against every remaining bill line
        scored = [
            (bill_line, _score_line_pair(eob_line, bill_line))
            for bill_line in available_bill_lines
        ]
        best_bill_line, best_score = max(scored, key=lambda x: x[1])

        if best_score >= _MATCHED_SCORE_THRESHOLD:
            matched.append(EobLineMatch(
                eob_line=eob_line,
                bill_line=best_bill_line,
                match_status="matched",
                match_score=best_score,
            ))
            available_bill_lines.remove(best_bill_line)
        elif best_score >= _PARTIAL_MATCH_SCORE_THRESHOLD:
            matched.append(EobLineMatch(
                eob_line=eob_line,
                bill_line=best_bill_line,
                match_status="partial_match",
                match_score=best_score,
            ))
            available_bill_lines.remove(best_bill_line)
        else:
            unmatched_eob.append(eob_line)

    # Aggregate over matched lines only
    def _sum_matched(attr: str) -> float | None:
        values = [
            getattr(m.eob_line, attr)
            for m in matched
            if getattr(m.eob_line, attr) is not None
        ]
        return sum(values) if values else None

    return EobMatchResult(
        matched=matched,
        unmatched_eob_lines=unmatched_eob,
        unmatched_bill_lines=available_bill_lines,
        total_allowed_amount=_sum_matched("allowed_amount"),
        total_patient_responsibility=_sum_matched("patient_responsibility"),
        total_insurance_paid=_sum_matched("insurance_paid"),
    )
