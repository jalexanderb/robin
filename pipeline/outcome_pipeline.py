"""
RobinHealth: outcome tracking pipeline.

Manages the full negotiation lifecycle from first outreach to final
settlement, and computes the savings/fee figures that make the
"20% of savings" business model auditable.

LIFECYCLE:
  1. Intake completes -> synthesis produces estimate -> case is ready
  2. Patient reviews, approves sending the letter
  3. start_negotiation() creates a negotiations row (status=pending)
  4. record_contact() logs each outreach attempt (letter_mail, phone, etc.)
     and auto-advances status to 'contacted'
  5. record_provider_response() captures what the provider said
  6. record_outcome() captures the final agreed amount and computes savings
     The database computes amount_saved / robinhealth_fee / patient_net_savings
     via generated columns -- no floating-point business logic in Python

WHAT GETS RECORDED:
  - Every outreach attempt (channel, timestamp, what was sent)
  - Provider responses (free text + timestamp)
  - Final agreed amount (the number that drives the fee calculation)
  - Whether the patient paid (completes the case)

WHY GENERATED COLUMNS FOR FEES:
  amount_saved, robinhealth_fee, and patient_net_savings are GENERATED
  ALWAYS columns in Postgres -- they're computed from original_billed_amount
  and agreed_amount at write time, not in Python. This prevents a class of
  bugs where the Python fee calculation drifts from what the database stores,
  and makes the audit trail authoritative: the database's own arithmetic,
  not application code, determines the fee.

VALIDATION:
  record_outcome() validates:
  - agreed_amount < original_billed_amount (savings must be positive)
  - agreed_amount >= 0 (full elimination is valid; negative is not)
  - The negotiation exists and belongs to the given case
  These are the minimum checks for an auditable "20% of savings" claim.
"""

from __future__ import annotations

from dataclasses import dataclass

import db
import psycopg2.extras
import repository


# ============================================================
# Exceptions
# ============================================================

class FeeAgreementRequired(Exception):
    """
    Raised when a patient tries to start a negotiation without having
    first accepted the fee agreement. The caller (API layer) should
    surface this as a 402 Payment Required with a clear message and
    a link to the terms endpoint.
    """


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class NegotiationSummary:
    """
    Full state of a negotiation, returned by fetch_negotiation_for_case.
    All monetary amounts are floats (None if not yet set).
    """
    negotiation_id: str
    case_id: str
    status: str
    original_billed_amount: float
    target_amount: float | None
    counter_offer_amount: float | None
    agreed_amount: float | None
    amount_saved: float | None
    robinhealth_fee: float | None
    patient_net_savings: float | None
    provider_response_text: str | None
    first_contacted_at: str | None   # ISO datetime string
    agreed_at: str | None
    paid_at: str | None
    contacts: list[dict]             # list of negotiation_contacts rows


@dataclass
class OutcomeReceipt:
    """
    Returned by record_outcome() -- a summary of the agreed deal for
    display to the patient and for the RobinHealth fee calculation.
    """
    negotiation_id: str
    original_billed_amount: float
    agreed_amount: float
    amount_saved: float
    savings_pct: float              # amount_saved / original * 100
    robinhealth_fee: float          # 20% of savings
    patient_net_savings: float      # 80% of savings
    status: str                     # 'agreed' or 'paid'


# ============================================================
# Negotiation lifecycle
# ============================================================

def start_negotiation(
    case_id: str,
    original_billed_amount: float,
    target_amount: float | None = None,
) -> str:
    """
    Create a negotiations row for a case (status=pending).
    Returns the negotiation_id.

    `target_amount` is what we're asking for -- typically the
    synthesis estimated_low, which is the most conservative figure
    that still represents a meaningful reduction. If None, it can be
    set later via update_target().

    Idempotent: if a negotiation already exists for this case, returns
    its ID rather than raising (a second intake shouldn't wipe the
    existing negotiation history).
    """
    existing = fetch_negotiation_id_for_case(case_id)
    if existing:
        return existing

    if original_billed_amount <= 0:
        raise ValueError(
            f"original_billed_amount must be positive, got {original_billed_amount}"
        )

    # Gate: patient must have accepted the fee agreement before we can
    # start negotiating on their behalf. This is both a legal requirement
    # (we need authorization to act as their representative) and a product
    # requirement (the business model only works if patients know the fee).
    # Fetch the patient_id from the case to check.
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
    if row is None:
        raise ValueError(f"No case found with id={case_id!r}")
    patient_id = str(row[0])

    agreement = check_fee_agreement(patient_id)
    if not agreement["accepted"]:
        raise FeeAgreementRequired(
            "Patient has not accepted the RobinHealth fee agreement. "
            "Please call GET /patients/{patient_id}/fee-terms to retrieve "
            "the terms, then POST /patients/{patient_id}/agree-to-terms "
            "to record acceptance before starting a negotiation."
        )
    if not agreement["terms_current"]:
        raise FeeAgreementRequired(
            f"Patient accepted fee terms version {agreement['terms_version']!r} "
            f"but current version is {FEE_TERMS_VERSION!r}. "
            f"Please have them re-accept the updated terms."
        )

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO negotiations
                    (case_id, original_billed_amount, target_amount, status)
                VALUES (%s, %s, %s, 'pending')
                RETURNING id
                """,
                (case_id, original_billed_amount, target_amount),
            )
            negotiation_id = str(cur.fetchone()[0])

    _update_case_status(case_id, "negotiating")
    return negotiation_id


def record_contact(
    negotiation_id: str,
    channel: str,
    letter_storage_key: str | None = None,
    notes: str | None = None,
) -> str:
    """
    Record an outreach attempt and advance the negotiation to 'contacted'
    if it's still 'pending'.

    `channel` must be one of the contact_channel enum values:
    'letter_mail', 'letter_fax', 'letter_email',
    'phone_call', 'patient_portal', 'in_person'.

    Returns the contact_id.
    """
    valid_channels = {
        "letter_mail", "letter_fax", "letter_email",
        "phone_call", "patient_portal", "in_person",
    }
    if channel not in valid_channels:
        raise ValueError(
            f"Invalid channel {channel!r}. Must be one of: {sorted(valid_channels)}"
        )

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO negotiation_contacts
                    (negotiation_id, channel, letter_storage_key, notes)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (negotiation_id, channel, letter_storage_key, notes),
            )
            contact_id = str(cur.fetchone()[0])

            # Advance to 'contacted' and stamp first_contacted_at if still pending
            cur.execute(
                """
                UPDATE negotiations SET
                    status = CASE WHEN status = 'pending' THEN 'contacted'::negotiation_status
                                  ELSE status END,
                    first_contacted_at = COALESCE(first_contacted_at, now()),
                    updated_at = now()
                WHERE id = %s
                """,
                (negotiation_id,),
            )

    return contact_id


def record_provider_response(
    negotiation_id: str,
    response_text: str,
    counter_offer_amount: float | None = None,
    contact_id: str | None = None,
) -> None:
    """
    Record a provider response.

    If `counter_offer_amount` is set, status advances to 'counter_offer'.
    Otherwise it advances to 'provider_replied'.

    `contact_id`: if provided, also updates that specific contact's
    provider_response field (links the response to a specific outreach).
    """
    if counter_offer_amount is not None and counter_offer_amount < 0:
        raise ValueError("counter_offer_amount cannot be negative")

    new_status = "counter_offer" if counter_offer_amount is not None else "provider_replied"

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE negotiations SET
                    status = %s::negotiation_status,
                    provider_response_text = %s,
                    counter_offer_amount = COALESCE(%s, counter_offer_amount),
                    updated_at = now()
                WHERE id = %s
                """,
                (new_status, response_text, counter_offer_amount, negotiation_id),
            )

            if contact_id:
                cur.execute(
                    """
                    UPDATE negotiation_contacts SET
                        provider_responded_at = now(),
                        provider_response = %s
                    WHERE id = %s AND negotiation_id = %s
                    """,
                    (response_text, contact_id, negotiation_id),
                )


def record_outcome(
    negotiation_id: str,
    agreed_amount: float,
    paid: bool = False,
    notes: str | None = None,
) -> OutcomeReceipt:
    """
    Record the final agreed amount and return an OutcomeReceipt.

    `paid`: True if the patient has already paid (status='paid');
            False (default) sets status='agreed' -- payment can be
            confirmed later via mark_paid().

    Validates that:
    - agreed_amount >= 0 (full elimination is valid)
    - agreed_amount < original_billed_amount (must save something)

    The database computes amount_saved / robinhealth_fee / patient_net_savings
    as generated columns -- we read them back after the UPDATE rather than
    computing them in Python.
    """
    if agreed_amount < 0:
        raise ValueError(f"agreed_amount cannot be negative, got {agreed_amount}")

    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Read original to validate
            cur.execute(
                "SELECT original_billed_amount FROM negotiations WHERE id = %s",
                (negotiation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"No negotiation found with id={negotiation_id!r}")

            original = float(row["original_billed_amount"])
            if agreed_amount >= original:
                raise ValueError(
                    f"agreed_amount ({agreed_amount}) must be less than "
                    f"original_billed_amount ({original}) -- "
                    f"outcome tracking requires a real saving to record"
                )

            final_status = "paid" if paid else "agreed"
            cur.execute(
                """
                UPDATE negotiations SET
                    agreed_amount = %s,
                    status = %s::negotiation_status,
                    agreed_at = COALESCE(agreed_at, now()),
                    paid_at = CASE WHEN %s THEN now() ELSE paid_at END,
                    updated_at = now()
                WHERE id = %s
                RETURNING
                    original_billed_amount, agreed_amount,
                    amount_saved, robinhealth_fee, patient_net_savings,
                    status, case_id
                """,
                (agreed_amount, final_status, paid, negotiation_id),
            )
            result = dict(cur.fetchone())

    case_id = str(result["case_id"])
    if paid:
        _update_case_status(case_id, "resolved")

    original_f = float(result["original_billed_amount"])
    agreed_f = float(result["agreed_amount"])
    saved_f = float(result["amount_saved"])
    fee_f = float(result["robinhealth_fee"])
    net_f = float(result["patient_net_savings"])

    return OutcomeReceipt(
        negotiation_id=negotiation_id,
        original_billed_amount=original_f,
        agreed_amount=agreed_f,
        amount_saved=saved_f,
        savings_pct=round(saved_f / original_f * 100, 1),
        robinhealth_fee=fee_f,
        patient_net_savings=net_f,
        status=result["status"],
    )


def mark_paid(negotiation_id: str) -> None:
    """
    Advance a negotiation from 'agreed' to 'paid' once the patient has paid.
    Also advances the case to 'resolved'.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE negotiations SET
                    status = 'paid'::negotiation_status,
                    paid_at = COALESCE(paid_at, now()),
                    updated_at = now()
                WHERE id = %s AND status = 'agreed'
                RETURNING case_id
                """,
                (negotiation_id,),
            )
            row = cur.fetchone()
            if row:
                _update_case_status(str(row[0]), "resolved")


def mark_rejected(negotiation_id: str, provider_response: str | None = None) -> None:
    """Record that the provider refused any reduction."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE negotiations SET
                    status = 'rejected'::negotiation_status,
                    provider_response_text = COALESCE(%s, provider_response_text),
                    updated_at = now()
                WHERE id = %s
                """,
                (provider_response, negotiation_id),
            )


def mark_withdrawn(negotiation_id: str, reason: str | None = None) -> None:
    """Record that the patient chose not to pursue the negotiation."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE negotiations SET
                    status = 'withdrawn'::negotiation_status,
                    provider_response_text = COALESCE(%s, provider_response_text),
                    updated_at = now()
                WHERE id = %s
                """,
                (reason, negotiation_id),
            )


# ============================================================
# Read side
# ============================================================

def fetch_negotiation_id_for_case(case_id: str) -> str | None:
    """Return the negotiation_id for a case, or None if none exists."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM negotiations WHERE case_id = %s",
                (case_id,),
            )
            row = cur.fetchone()
    return str(row[0]) if row else None


def fetch_negotiation_for_case(case_id: str) -> NegotiationSummary | None:
    """
    Return the full negotiation state for a case, including all contact
    attempts. Returns None if no negotiation has been started.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    n.id, n.case_id, n.status,
                    n.original_billed_amount, n.target_amount,
                    n.counter_offer_amount, n.agreed_amount,
                    n.amount_saved, n.robinhealth_fee, n.patient_net_savings,
                    n.provider_response_text,
                    n.first_contacted_at, n.agreed_at, n.paid_at
                FROM negotiations n
                WHERE n.case_id = %s
                """,
                (case_id,),
            )
            neg_row = cur.fetchone()
            if neg_row is None:
                return None

            cur.execute(
                """
                SELECT
                    id, channel, sent_at, letter_storage_key, notes,
                    provider_responded_at, provider_response
                FROM negotiation_contacts
                WHERE negotiation_id = %s
                ORDER BY sent_at
                """,
                (str(neg_row["id"]),),
            )
            contacts = [dict(r) for r in cur.fetchall()]

    def _f(v):
        return float(v) if v is not None else None

    def _s(v):
        return v.isoformat() if v is not None else None

    return NegotiationSummary(
        negotiation_id=str(neg_row["id"]),
        case_id=str(neg_row["case_id"]),
        status=neg_row["status"],
        original_billed_amount=float(neg_row["original_billed_amount"]),
        target_amount=_f(neg_row["target_amount"]),
        counter_offer_amount=_f(neg_row["counter_offer_amount"]),
        agreed_amount=_f(neg_row["agreed_amount"]),
        amount_saved=_f(neg_row["amount_saved"]),
        robinhealth_fee=_f(neg_row["robinhealth_fee"]),
        patient_net_savings=_f(neg_row["patient_net_savings"]),
        provider_response_text=neg_row["provider_response_text"],
        first_contacted_at=_s(neg_row["first_contacted_at"]),
        agreed_at=_s(neg_row["agreed_at"]),
        paid_at=_s(neg_row["paid_at"]),
        contacts=[
            {
                "contact_id": str(c["id"]),
                "channel": c["channel"],
                "sent_at": c["sent_at"].isoformat() if c["sent_at"] else None,
                "letter_storage_key": c["letter_storage_key"],
                "notes": c["notes"],
                "provider_responded_at": c["provider_responded_at"].isoformat()
                    if c["provider_responded_at"] else None,
                "provider_response": c["provider_response"],
            }
            for c in contacts
        ],
    )


def fetch_outcomes_summary() -> dict:
    """
    Aggregate outcome statistics across all cases -- the top-level
    metrics that show whether RobinHealth's negotiation approach works.
    """
    with db.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status NOT IN ('pending', 'withdrawn')) AS total_active,
                    COUNT(*) FILTER (WHERE status IN ('agreed', 'paid')) AS total_agreed,
                    COUNT(*) FILTER (WHERE status = 'paid') AS total_paid,
                    COUNT(*) FILTER (WHERE status = 'rejected') AS total_rejected,
                    SUM(amount_saved) FILTER (WHERE status IN ('agreed', 'paid')) AS total_saved,
                    SUM(robinhealth_fee) FILTER (WHERE status IN ('agreed', 'paid')) AS total_fees,
                    AVG(amount_saved / NULLIF(original_billed_amount, 0) * 100)
                        FILTER (WHERE status IN ('agreed', 'paid')) AS avg_savings_pct,
                    AVG(
                        EXTRACT(EPOCH FROM (agreed_at - first_contacted_at)) / 86400
                    ) FILTER (WHERE agreed_at IS NOT NULL AND first_contacted_at IS NOT NULL)
                        AS avg_days_to_agreement
                FROM negotiations
            """)
            row = dict(cur.fetchone())

    def _f(v): return float(v) if v is not None else None
    def _i(v): return int(v) if v is not None else 0

    return {
        "total_active_negotiations": _i(row["total_active"]),
        "total_agreed": _i(row["total_agreed"]),
        "total_paid": _i(row["total_paid"]),
        "total_rejected": _i(row["total_rejected"]),
        "total_amount_saved": _f(row["total_saved"]),
        "total_robinhealth_fees": _f(row["total_fees"]),
        "avg_savings_pct": round(_f(row["avg_savings_pct"]) or 0, 1),
        "avg_days_to_agreement": round(_f(row["avg_days_to_agreement"]) or 0, 1),
    }


# ============================================================
# Internal helpers
# ============================================================

def _update_case_status(case_id: str, new_status: str) -> None:
    """Advance a case's status, never regress it."""
    status_order = [
        "intake", "reviewing", "awaiting_user_input",
        "ready_for_action", "negotiating", "resolved",
    ]
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
            if row is None:
                return
            current = row[0]
            current_idx = status_order.index(current) if current in status_order else 0
            new_idx = status_order.index(new_status) if new_status in status_order else 0
            if new_idx > current_idx:
                cur.execute(
                    "UPDATE cases SET status = %s::case_status, updated_at = now() WHERE id = %s",
                    (new_status, case_id),
                )


# ============================================================
# Provider response classification and follow-up generation
# ============================================================

# Maps each response type to a plain-English description of what the
# provider said -- used for user-facing messaging and LLM classification.
RESPONSE_TYPE_DESCRIPTIONS = {
    "reduced_offer": "Provider offered a reduced amount, but more than our target",
    "accepted_target": "Provider accepted our requested amount",
    "denied_eligibility": "Provider says patient doesn't qualify for financial assistance",
    "requested_more_info": "Provider is requesting additional documentation",
    "referred_to_collections": "Account has been referred to a collections agency",
    "claimed_no_fap": "Provider claims they have no financial assistance program",
    "billing_error": "Provider acknowledges there was a billing error",
    "insurance_issue": "Provider says this should have been covered by insurance",
    "no_response": "Provider has not responded within the deadline",
    "other": "Other response -- see notes for details",
}

# Which response types can be auto-resolved without a follow-up letter
_AUTO_RESOLVE_TYPES = {"accepted_target", "billing_error"}

# Which response types require urgent action
_URGENT_TYPES = {"referred_to_collections"}


@dataclass
class FollowupAction:
    """
    The recommended next step after a provider response.
    Returned by generate_followup_action().
    """
    response_type: str
    negotiation_id: str
    # What to do
    action: str              # short imperative: "Send appeal letter", "Accept offer", etc.
    urgency: str             # "immediate", "within_week", "when_ready"
    explanation: str         # 2-3 sentence plain-language explanation for the patient
    # The pre-built follow-up letter context (None if no letter needed)
    followup_letter_context: dict | None   # serializable dict, not LetterContext directly
    # Whether this action closes the negotiation
    resolves_negotiation: bool
    suggested_resolution: dict | None      # {"agreed_amount": X} if known


@dataclass
class ClassifiedResponse:
    """Result of classifying a provider response text."""
    response_type: str
    confidence: str          # "high", "medium", "low"
    extracted_amount: float | None    # if response mentions a specific dollar figure
    extracted_documents: list[str]    # if response requests specific documents
    reasoning: str           # why we classified it this way


def classify_provider_response(
    response_text: str,
    original_billed_amount: float,
    target_amount: float | None = None,
) -> ClassifiedResponse:
    """
    Classify a provider's free-text response into a structured response_type.

    Uses heuristic keyword matching first (fast, no LLM call, works for the
    most common clear-cut cases). Falls back to an LLM call via llm_client
    for ambiguous responses. Designed to be called before record_provider_response
    so the response_type can be stored alongside the free text.

    Amount extraction: scans for dollar figures in the response text and
    interprets them as the offered amount for reduced_offer classifications.
    """
    text_lower = response_text.lower()

    # --- Heuristic classification (fast path) ---

    # Collections referral -- most urgent, check first
    if any(kw in text_lower for kw in [
        "collect", "collection agency", "referred to", "third party", "debt collector"
    ]):
        return ClassifiedResponse(
            response_type="referred_to_collections",
            confidence="high",
            extracted_amount=None,
            extracted_documents=[],
            reasoning="Response mentions collections or debt collection agency",
        )

    # Billing error
    if any(kw in text_lower for kw in [
        "billing error", "incorrect charge", "billing mistake",
        "duplicate charge", "posted in error", "adjust the bill",
    ]):
        return ClassifiedResponse(
            response_type="billing_error",
            confidence="high",
            extracted_amount=None,
            extracted_documents=[],
            reasoning="Response acknowledges a billing error",
        )

    # No FAP claimed
    if any(kw in text_lower for kw in [
        "no financial assistance", "don't have a financial assistance",
        "do not have a program", "no charity care program", "does not offer charity",
        "do not have a financial assistance", "does not have a financial assistance",
        "don't offer financial assistance",
    ]):
        return ClassifiedResponse(
            response_type="claimed_no_fap",
            confidence="high",
            extracted_amount=None,
            extracted_documents=[],
            reasoning="Response claims no financial assistance program exists",
        )

    # Request for more info -- check before denial to catch "need docs to determine eligibility"
    doc_keywords = ["tax return", "pay stub", "w-2", "bank statement", "proof of income",
                    "documentation", "please provide", "submit the following", "need to verify"]
    found_docs = [kw for kw in doc_keywords if kw in text_lower]
    if found_docs or any(kw in text_lower for kw in [
        "additional information", "more information", "please send", "please submit",
        "we need", "require documentation",
    ]):
        return ClassifiedResponse(
            response_type="requested_more_info",
            confidence="high",
            extracted_amount=None,
            extracted_documents=found_docs,
            reasoning=f"Response requests documentation or additional information ({', '.join(found_docs) or 'general request'})",
        )

    # Eligibility denial
    if any(kw in text_lower for kw in [
        "not qualify", "does not qualify", "don't qualify", "ineligible",
        "not eligible", "income exceeds", "above the limit", "denied",
        "not approved", "does not meet",
    ]):
        return ClassifiedResponse(
            response_type="denied_eligibility",
            confidence="high",
            extracted_amount=None,
            extracted_documents=[],
            reasoning="Response indicates patient denied financial assistance eligibility",
        )

    # Insurance-related
    if any(kw in text_lower for kw in [
        "your insurance", "should be covered", "insurance company", "file a claim",
        "contact your insurer", "insurance adjustment",
    ]):
        return ClassifiedResponse(
            response_type="insurance_issue",
            confidence="high",
            extracted_amount=None,
            extracted_documents=[],
            reasoning="Response redirects to insurance",
        )

    # Acceptance
    if any(kw in text_lower for kw in [
        "accept", "approved", "approved your request", "agree to", "we will honor",
        "agreed to reduce", "approved the reduction",
    ]):
        extracted = _extract_dollar_amount(response_text)
        return ClassifiedResponse(
            response_type="accepted_target",
            confidence="high" if extracted else "medium",
            extracted_amount=extracted,
            extracted_documents=[],
            reasoning="Response indicates acceptance",
        )

    # Reduced offer -- look for a dollar amount being offered
    extracted = _extract_dollar_amount(response_text)
    if extracted is not None:
        if any(kw in text_lower for kw in [
            "offer", "can reduce", "willing to", "settle for",
            "best we can do", "final offer", "reduce to",
        ]):
            # Make sure it's less than billed and more than target
            is_reduced = extracted < original_billed_amount
            is_counter = target_amount is None or extracted > target_amount
            if is_reduced and is_counter:
                return ClassifiedResponse(
                    response_type="reduced_offer",
                    confidence="high",
                    extracted_amount=extracted,
                    extracted_documents=[],
                    reasoning=f"Response offers ${extracted:,.2f} (less than billed ${original_billed_amount:,.2f})",
                )
            elif is_reduced:
                return ClassifiedResponse(
                    response_type="accepted_target",
                    confidence="medium",
                    extracted_amount=extracted,
                    extracted_documents=[],
                    reasoning=f"Response offers ${extracted:,.2f} which meets or beats our target",
                )

    # Ambiguous -- use LLM
    return _classify_with_llm(response_text, original_billed_amount, target_amount)


def _extract_dollar_amount(text: str) -> float | None:
    """Extract the first plausible dollar amount from text."""
    import re
    # Match $1,234.56 or $1234 or 1,234.56 preceded by $ or "amount"
    patterns = [
        r'\$[\s]*([\d,]+(?:\.\d{2})?)',
        r'amount\s+of\s+\$?[\s]*([\d,]+(?:\.\d{2})?)',
        r'reduce\s+to\s+\$?[\s]*([\d,]+(?:\.\d{2})?)',
        r'settle\s+for\s+\$?[\s]*([\d,]+(?:\.\d{2})?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _classify_with_llm(
    response_text: str,
    original_billed_amount: float,
    target_amount: float | None,
) -> ClassifiedResponse:
    """LLM fallback for ambiguous responses."""
    import llm_client

    valid_types = list(RESPONSE_TYPE_DESCRIPTIONS.keys())
    prompt = (
        f"Classify this healthcare provider billing response into exactly one of these types:\n"
        + "\n".join(f"  {t}: {d}" for t, d in RESPONSE_TYPE_DESCRIPTIONS.items())
        + f"\n\nOriginal billed amount: ${original_billed_amount:,.2f}\n"
        + (f"Our requested amount: ${target_amount:,.2f}\n" if target_amount else "")
        + f"\nProvider response:\n{response_text}\n\n"
        + "Respond only with JSON (no markdown):\n"
        + '{"response_type": "<type>", "confidence": "high"|"medium"|"low", '
        + '"extracted_amount": <number or null>, '
        + '"extracted_documents": [<list of strings or empty>], '
        + '"reasoning": "<one sentence>"}'
    )
    try:
        result = llm_client.complete_json(prompt, max_tokens=300)
        rt = result.get("response_type", "other")
        if rt not in valid_types:
            rt = "other"
        return ClassifiedResponse(
            response_type=rt,
            confidence=result.get("confidence", "low"),
            extracted_amount=result.get("extracted_amount"),
            extracted_documents=result.get("extracted_documents") or [],
            reasoning=result.get("reasoning", "LLM classification"),
        )
    except Exception as exc:
        return ClassifiedResponse(
            response_type="other",
            confidence="low",
            extracted_amount=None,
            extracted_documents=[],
            reasoning=f"Classification failed: {exc}",
        )


def generate_followup_action(
    negotiation_id: str,
    response_type: str,
    classified: ClassifiedResponse,
    negotiation_summary: "NegotiationSummary",
) -> FollowupAction:
    """
    Given a classified provider response, determine the recommended
    next action and pre-build a follow-up letter context if applicable.

    This is where "RobinHealth advocates for the patient" concretely
    means something: instead of leaving the patient to figure out
    how to respond to a denial or counter-offer, we tell them exactly
    what to do and hand them the letter to send.
    """
    original = negotiation_summary.original_billed_amount
    target = negotiation_summary.target_amount
    extracted = classified.extracted_amount

    if response_type == "accepted_target":
        amount = extracted or target or original
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Accept and confirm the agreed amount",
            urgency="when_ready",
            explanation=(
                f"The provider has accepted our requested amount"
                + (f" of ${amount:,.2f}" if amount else "")
                + ". Record this as the agreed outcome and arrange payment."
            ),
            followup_letter_context=None,
            resolves_negotiation=True,
            suggested_resolution={"agreed_amount": amount},
        )

    if response_type == "billing_error":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Confirm corrected bill amount in writing",
            urgency="within_week",
            explanation=(
                "The provider has acknowledged a billing error. "
                "Request a corrected statement in writing before making any payment, "
                "and confirm the corrected amount once received."
            ),
            followup_letter_context=_make_billing_error_confirmation_context(negotiation_summary),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "reduced_offer":
        offered = extracted
        savings_if_accepted = (original - offered) if offered else None
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Counter with our original target or accept their offer",
            urgency="within_week",
            explanation=(
                f"The provider offered ${offered:,.2f}" if offered else "The provider made a reduced offer"
            ) + (
                f", which saves ${savings_if_accepted:,.2f} off the original bill. "
                if savings_if_accepted else ". "
            ) + (
                f"We asked for ${target:,.2f}, so we can either accept their offer "
                "or send a counter-letter pushing for our original target."
                if target and offered and offered > target
                else "This meets or beats our target — you can accept."
            ),
            followup_letter_context=_make_counter_offer_context(negotiation_summary, offered),
            resolves_negotiation=False,
            suggested_resolution={"agreed_amount": offered} if offered else None,
        )

    if response_type == "denied_eligibility":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Send eligibility appeal letter",
            urgency="within_week",
            explanation=(
                "The provider has denied financial assistance eligibility. "
                "This is frequently incorrect or based on an incomplete review. "
                "We'll send an appeal letter citing the specific FAP criteria and "
                "your documentation. Hospitals must follow their own published policy."
            ),
            followup_letter_context=_make_eligibility_appeal_context(
                negotiation_summary, classified
            ),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "requested_more_info":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Gather and submit requested documentation",
            urgency="within_week",
            explanation=(
                "The provider is requesting additional documentation before making "
                "a decision. This is a good sign — they're actively reviewing the "
                "request rather than denying it outright. We'll prepare a cover "
                "letter with the documentation checklist."
            ),
            followup_letter_context=_make_documentation_cover_context(
                negotiation_summary, classified.extracted_documents
            ),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "referred_to_collections":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Send urgent cease-and-desist and FAP rights letter",
            urgency="immediate",
            explanation=(
                "The account has been referred to collections. This is time-sensitive: "
                "you have rights under the No Surprises Act, the Fair Debt Collection "
                "Practices Act (FDCPA), and the hospital's own 501(r) obligations. "
                "We'll send an urgent letter invoking these rights and demanding the "
                "account be returned for FAP review before any collection activity."
            ),
            followup_letter_context=_make_collections_response_context(negotiation_summary),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "claimed_no_fap":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Send 501(r) compliance letter",
            urgency="within_week",
            explanation=(
                "The provider claims they have no financial assistance program. "
                "If they are a nonprofit hospital, this is false — 501(r) requires "
                "all nonprofit hospitals to maintain and publicize a FAP. We'll send "
                "a letter citing this requirement and requesting their written policy."
            ),
            followup_letter_context=_make_fap_compliance_demand_context(negotiation_summary),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "insurance_issue":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Clarify insurance status and redirect to self-pay negotiation",
            urgency="within_week",
            explanation=(
                "The provider is redirecting to your insurance. If the claim was "
                "denied by insurance or you are uninsured/underinsured, send a letter "
                "clarifying your situation and re-requesting direct financial assistance."
            ),
            followup_letter_context=_make_insurance_clarification_context(negotiation_summary),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    if response_type == "no_response":
        return FollowupAction(
            response_type=response_type,
            negotiation_id=negotiation_id,
            action="Send follow-up letter with escalation notice",
            urgency="within_week",
            explanation=(
                "The provider has not responded within the deadline. "
                "A follow-up letter reiterating the request and noting the lack "
                "of response will be sent, with a shorter deadline and notice "
                "that the matter may be escalated to the state insurance commissioner "
                "or hospital accreditation body."
            ),
            followup_letter_context=_make_no_response_escalation_context(negotiation_summary),
            resolves_negotiation=False,
            suggested_resolution=None,
        )

    # "other" / unknown
    return FollowupAction(
        response_type=response_type,
        negotiation_id=negotiation_id,
        action="Review provider response and decide next step",
        urgency="within_week",
        explanation=(
            "The provider sent a response that requires manual review. "
            "Please read it carefully and choose whether to accept, counter, "
            "or appeal."
        ),
        followup_letter_context=None,
        resolves_negotiation=False,
        suggested_resolution=None,
    )


# ============================================================
# Follow-up letter context builders (one per response type)
# ============================================================

def _make_billing_error_confirmation_context(neg: "NegotiationSummary") -> dict:
    return {
        "letter_type": "billing_error_confirmation",
        "subject": "Request for Corrected Statement of Account",
        "key_points": [
            "Thank you for acknowledging the billing error on this account.",
            "Please provide a corrected itemized statement showing the accurate charges.",
            "Confirm in writing the amount we are now responsible for.",
            "We will arrange payment promptly upon receipt of the corrected statement.",
        ],
        "urgency": "standard",
    }


def _make_counter_offer_context(neg: "NegotiationSummary", offered_amount: float | None) -> dict:
    target = neg.target_amount
    return {
        "letter_type": "counter_offer",
        "subject": "Response to Offer — Counter-Proposal",
        "key_points": [
            f"We have received your offer" + (f" of ${offered_amount:,.2f}" if offered_amount else "") + ".",
            f"Our financial circumstances require a balance closer to ${target:,.2f}." if target else
            "We respectfully request a further reduction.",
            "We are committed to resolving this account and appreciate your flexibility.",
            "Please confirm whether you can accommodate this request.",
        ],
        "requested_amount": target,
        "urgency": "standard",
    }


def _make_eligibility_appeal_context(
    neg: "NegotiationSummary", classified: "ClassifiedResponse"
) -> dict:
    return {
        "letter_type": "eligibility_appeal",
        "subject": "Appeal of Financial Assistance Eligibility Determination",
        "key_points": [
            "We are writing to appeal the denial of financial assistance for this account.",
            "Under 26 CFR 1.501(r)-4, your hospital is required to apply its FAP consistently "
            "to all patients who apply and meet the eligibility criteria.",
            "We respectfully request a review of this determination and a written explanation "
            "of the specific criteria that were not met.",
            "We are prepared to provide any additional documentation required.",
        ],
        "legal_citations": [
            "26 CFR 1.501(r)-4 (FAP application requirements)",
            "IRS Notice 2014-2 (written FAP application requirements)",
        ],
        "urgency": "standard",
    }


def _make_documentation_cover_context(
    neg: "NegotiationSummary", documents_requested: list[str]
) -> dict:
    return {
        "letter_type": "documentation_submission",
        "subject": "Financial Assistance Application — Documentation Submission",
        "key_points": [
            "Please find enclosed the documentation you requested in support of "
            "our financial assistance application.",
            "Enclosed documents: " + (", ".join(documents_requested) if documents_requested
                                      else "income verification and supporting materials"),
            "We request a decision on our application within 30 days of receipt.",
            "Please contact us if additional information is required.",
        ],
        "documents_checklist": documents_requested or [
            "Proof of income (pay stubs or tax return)",
            "Household size verification",
            "Bank statements (last 3 months)",
        ],
        "urgency": "standard",
    }


def _make_collections_response_context(neg: "NegotiationSummary") -> dict:
    return {
        "letter_type": "collections_response",
        "subject": "URGENT: Dispute of Debt — Cease Collection Activity Pending FAP Review",
        "key_points": [
            "NOTICE: This debt is formally disputed under 15 U.S.C. § 1692g (FDCPA).",
            "Under IRS 501(r) regulations, nonprofit hospitals must apply their Financial "
            "Assistance Policy before engaging in extraordinary collection actions, "
            "including referral to collections agencies.",
            "Our financial assistance application was submitted and is pending review. "
            "Referral to collections prior to FAP determination may violate 26 CFR 1.501(r)-6.",
            "We demand: (1) immediate return of this account for FAP review, "
            "(2) cessation of all collection activity, "
            "(3) written confirmation of FAP status within 10 days.",
        ],
        "legal_citations": [
            "15 U.S.C. § 1692g (FDCPA debt dispute rights)",
            "26 CFR 1.501(r)-6 (prohibition on extraordinary collection actions before FAP)",
            "No Surprises Act, 42 U.S.C. § 300gg-111 (balance billing protections)",
        ],
        "urgency": "immediate",
        "cc_recipients": ["State Insurance Commissioner", "Hospital Accreditation Body"],
    }


def _make_fap_compliance_demand_context(neg: "NegotiationSummary") -> dict:
    return {
        "letter_type": "fap_compliance_demand",
        "subject": "Request for Financial Assistance Policy — 501(r) Compliance",
        "key_points": [
            "We are writing to request a copy of your hospital's Financial Assistance Policy (FAP).",
            "Under 26 CFR 1.501(r)-4, all nonprofit hospitals exempt under IRC § 501(c)(3) "
            "are required to have a written FAP and make it widely available to patients.",
            "If your facility has tax-exempt nonprofit status, a FAP must exist. "
            "Please provide: (1) a copy of the FAP, (2) the plain language summary, "
            "and (3) a FAP application form.",
            "Failure to provide this information may indicate a 501(r) compliance violation, "
            "which we may report to the IRS.",
        ],
        "legal_citations": [
            "26 CFR 1.501(r)-4 (FAP requirements for 501(c)(3) hospitals)",
            "IRS Form 990, Schedule H (hospital FAP reporting)",
        ],
        "urgency": "standard",
    }


def _make_insurance_clarification_context(neg: "NegotiationSummary") -> dict:
    return {
        "letter_type": "insurance_clarification",
        "subject": "Clarification of Insurance Status — Financial Assistance Request",
        "key_points": [
            "Thank you for your response regarding our account.",
            "To clarify our situation: [INSERT: uninsured / insurance denied this claim / "
            "high-deductible plan with insufficient coverage].",
            "We are not able to resolve this through insurance and are requesting "
            "direct financial assistance under your hospital's published FAP.",
            "Please review our application on its merits as a self-pay patient.",
        ],
        "urgency": "standard",
    }


def _make_no_response_escalation_context(neg: "NegotiationSummary") -> dict:
    return {
        "letter_type": "no_response_followup",
        "subject": "Second Notice — Financial Assistance Application Pending Response",
        "key_points": [
            "This is a follow-up to our financial assistance application submitted on [DATE].",
            "We have not received a response within the requested timeframe.",
            "Under 26 CFR 1.501(r)-4(b)(2), your hospital is required to process FAP "
            "applications in a timely manner.",
            "Please respond within 14 days. If no response is received, we may file a "
            "complaint with the IRS, your state Attorney General, or your hospital "
            "accreditation body (e.g. The Joint Commission).",
        ],
        "legal_citations": [
            "26 CFR 1.501(r)-4(b)(2) (FAP application processing requirements)",
        ],
        "urgency": "escalated",
        "escalation_notice": True,
    }


# ============================================================
# Updated record_provider_response with structured classification
# ============================================================

def record_provider_response_structured(
    negotiation_id: str,
    response_text: str,
    contact_id: str | None = None,
    response_type: str | None = None,
    response_data: dict | None = None,
) -> tuple["ClassifiedResponse", "FollowupAction"]:
    """
    Enhanced version of record_provider_response that:
    1. Classifies the response (via heuristics or LLM)
    2. Generates the recommended follow-up action
    3. Persists everything in one call
    4. Returns (ClassifiedResponse, FollowupAction) so the caller can
       immediately show the patient what to do next

    If response_type is provided, skips classification and uses it directly.
    """
    # Fetch negotiation context for classification and follow-up generation
    neg = _fetch_negotiation_by_id(negotiation_id)
    if neg is None:
        raise ValueError(f"No negotiation found with id={negotiation_id!r}")

    # Classify
    if response_type is None:
        classified = classify_provider_response(
            response_text=response_text,
            original_billed_amount=neg.original_billed_amount,
            target_amount=neg.target_amount,
        )
        response_type = classified.response_type
    else:
        classified = ClassifiedResponse(
            response_type=response_type,
            confidence="high",  # caller-provided, assumed authoritative
            extracted_amount=response_data.get("offered_amount") if response_data else None,
            extracted_documents=response_data.get("documents_requested", []) if response_data else [],
            reasoning="Manually specified by caller",
        )

    # Generate follow-up action
    followup = generate_followup_action(
        negotiation_id=negotiation_id,
        response_type=response_type,
        classified=classified,
        negotiation_summary=neg,
    )

    # Determine new negotiation status
    status_map = {
        "reduced_offer": "counter_offer",
        "accepted_target": "agreed",
        "denied_eligibility": "provider_replied",
        "requested_more_info": "provider_replied",
        "referred_to_collections": "provider_replied",
        "claimed_no_fap": "provider_replied",
        "billing_error": "provider_replied",
        "insurance_issue": "provider_replied",
        "no_response": "provider_replied",
        "other": "provider_replied",
    }
    new_status = status_map.get(response_type, "provider_replied")
    counter_amount = classified.extracted_amount if response_type == "reduced_offer" else None

    with db.connection() as conn:
        with conn.cursor() as cur:
            # Update negotiation
            cur.execute(
                """
                UPDATE negotiations SET
                    status = %s::negotiation_status,
                    provider_response_text = %s,
                    counter_offer_amount = COALESCE(%s, counter_offer_amount),
                    updated_at = now()
                WHERE id = %s
                """,
                (new_status, response_text, counter_amount, negotiation_id),
            )

            # Update the contact if provided
            if contact_id:
                import psycopg2.extras as _extras
                cur.execute(
                    """
                    UPDATE negotiation_contacts SET
                        response_type = %s::provider_response_type,
                        response_data = %s,
                        provider_responded_at = now(),
                        provider_response = %s
                    WHERE id = %s AND negotiation_id = %s
                    """,
                    (
                        response_type,
                        psycopg2.extras.Json(response_data or {
                            "extracted_amount": classified.extracted_amount,
                            "extracted_documents": classified.extracted_documents,
                            "confidence": classified.confidence,
                            "reasoning": classified.reasoning,
                        }),
                        response_text, contact_id, negotiation_id,
                    ),
                )

    # Auto-resolve if accepted or billing error
    if followup.resolves_negotiation and followup.suggested_resolution:
        try:
            record_outcome(
                negotiation_id=negotiation_id,
                agreed_amount=followup.suggested_resolution["agreed_amount"],
            )
        except (ValueError, KeyError):
            pass  # don't crash if amount is ambiguous

    return classified, followup


def _fetch_negotiation_by_id(negotiation_id: str) -> "NegotiationSummary | None":
    """Fetch NegotiationSummary by negotiation_id (not case_id)."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT case_id FROM negotiations WHERE id = %s",
                (negotiation_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return fetch_negotiation_for_case(str(row[0]))


# ============================================================
# Fee agreement
# ============================================================

# The canonical fee terms text shown to patients. Versioned so we can
# tell exactly what a patient agreed to even if terms change later.
# This is the authoritative copy; the API endpoint returns it verbatim.
FEE_TERMS_VERSION = "v1.0"

FEE_TERMS_TEXT = """RobinHealth Fee Agreement — Version 1.0

WHAT WE DO
RobinHealth analyzes your medical bills and, if you choose to proceed,
negotiates with the provider on your behalf to reduce what you owe.

OUR FEE
If we successfully reduce your bill, you pay RobinHealth 20% of the
amount saved.

Example: If your bill is $5,000 and we negotiate it down to $2,000,
you save $3,000. Our fee is 20% of $3,000 = $600. You pay the
provider $2,000 and pay RobinHealth $600. Your total cost is $2,600
instead of $5,000 — a net saving of $2,400.

IF WE DON'T SAVE YOU ANYTHING
You owe us nothing. Our fee only applies when we achieve a real,
documented reduction in your bill. If the provider refuses to
negotiate or doesn't reduce the amount, there is no fee.

WHEN YOU PAY
The fee becomes due once you and the provider have agreed on a
reduced amount. We will send you an invoice at that time.

YOUR AUTHORIZATION
By accepting these terms, you authorize RobinHealth to communicate
with your healthcare provider on your behalf regarding this bill.
You remain responsible for reviewing any agreements we reach and
confirming them before payment.

To proceed, you must confirm that you have read and understood
these terms. You can withdraw from the negotiation at any time
before an agreement is reached, with no fee owed.
"""


def get_fee_terms() -> dict:
    """Return the current fee terms for display to the patient."""
    return {
        "version": FEE_TERMS_VERSION,
        "text": FEE_TERMS_TEXT,
        "fee_percentage": 20,
        "fee_basis": "savings",
        "fee_due_when": "upon_agreement",
        "no_cure_no_fee": True,
    }


def record_fee_agreement(patient_id: str) -> None:
    """
    Record that a patient has read and accepted the fee terms.
    Stores the exact terms text and version so the agreement is
    auditable even if terms change in the future.

    Idempotent: a second call for the same patient updates the
    timestamp and version but does not raise.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE patients SET
                    fee_agreement_accepted = TRUE,
                    fee_agreement_accepted_at = now(),
                    fee_agreement_terms_version = %s,
                    fee_agreement_terms_text = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (FEE_TERMS_VERSION, FEE_TERMS_TEXT, patient_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"No patient found with id={patient_id!r}")


def check_fee_agreement(patient_id: str) -> dict:
    """
    Return the patient's fee agreement status.
    Used by start_negotiation to gate access.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fee_agreement_accepted,
                       fee_agreement_accepted_at,
                       fee_agreement_terms_version
                FROM patients WHERE id = %s
                """,
                (patient_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise ValueError(f"No patient found with id={patient_id!r}")

    accepted, accepted_at, version = row
    return {
        "accepted": bool(accepted),
        "accepted_at": accepted_at.isoformat() if accepted_at else None,
        "terms_version": version,
        "current_terms_version": FEE_TERMS_VERSION,
        "terms_current": version == FEE_TERMS_VERSION if version else False,
    }
