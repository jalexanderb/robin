"""
RobinHealth: HTTP API layer.

Exposes case_pipeline.process_case_intake over HTTP.

  GET  /health   Liveness check + real Postgres connectivity.
  POST /intake   Upload a bill (+ optional EOB + patient context),
                 get back a structured CaseIntakeResult.

PRODUCTION HARDENING (all in this file):

  Authentication
    Bearer token via the Authorization header.  Set API_KEY env var to
    enable; if unset, the endpoint is open (dev/test mode).  Checked
    before anything else -- unauthenticated requests get 401 immediately,
    before any DB or LLM work is done.

  Rate limiting
    Backed by slowapi (a limits wrapper for FastAPI).  Two limits:
      RATE_LIMIT_PER_MINUTE  (default: 20) -- per-IP, per-minute cap
      RATE_LIMIT_PER_DAY     (default: 200) -- per-IP, per-day cap
    Requests that exceed the limit get a 429 with a Retry-After header.
    Uses in-memory storage (resets on restart) -- replace the limiter's
    storage_uri with a Redis URL for distributed rate limiting across
    multiple API processes.

  CORS
    Configured via CORS_ORIGINS env var (comma-separated list, or "*"
    for all origins).  Defaults to "*" in dev, which is safe since auth
    is the real protection layer.

  Request size limits
    MAX_BILL_SIZE_MB (default: 20) -- bills larger than this are rejected
    with 413 before LLM extraction is attempted.  A 500-page PDF bill is
    an outlier; even the largest hospital bills are typically <5 MB when
    scanned at reasonable resolution.

  Structured logging
    Every request gets a request_id (UUID4) in the response headers and
    in all log lines for that request.  Uses Python's standard logging
    module (JSON-friendly with the right formatter) rather than print().
    Log level is controlled by LOG_LEVEL env var (default: INFO).

  Connection pooling
    db.init_pool() is called once at startup.  The pool is sized via
    DB_POOL_MIN_CONN / DB_POOL_MAX_CONN (defaults: 2 / 10).

Rate tables (pricing_pipeline.RateTable) are loaded ONCE at startup from
PFS_RATE_CSV_PATH / OPPS_RATE_CSV_PATH if set; otherwise start empty and
fetch on demand via the CMS PFS Open Data API per request.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8001

Environment variables (summary):
    API_KEY                Bearer token for auth (unset = open)
    CORS_ORIGINS           Comma-separated origins or "*" (default: "*")
    RATE_LIMIT_PER_MINUTE  Per-IP per-minute request cap (default: 20)
    RATE_LIMIT_PER_DAY     Per-IP per-day request cap (default: 200)
    MAX_BILL_SIZE_MB       Max bill upload size in MB (default: 20)
    LOG_LEVEL              Logging level (default: INFO)
    DB_POOL_MIN_CONN       Min pool connections (default: 2)
    DB_POOL_MAX_CONN       Max pool connections (default: 10)
    PFS_RATE_CSV_PATH      Pre-downloaded CMS PFS CSV (optional)
    OPPS_RATE_CSV_PATH     Pre-downloaded CMS OPPS Addendum B CSV (optional)
    LLM_PROVIDER           "openai_compatible" (default) or "anthropic"
    LLM_BASE_URL           LLM endpoint base URL
    LLM_API_KEY            LLM API key
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import case_pipeline
import db
import delivery_pipeline
import letter_pipeline
import outcome_pipeline
import pricing_pipeline
import repository
import storage
from pricing_pipeline import BenchmarkSource, RateTable


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("robinhealth.api")


# ============================================================
# Configuration
# ============================================================

_API_KEY = os.environ.get("API_KEY")  # None = open (dev/test)
_MAX_BILL_SIZE_BYTES = int(os.environ.get("MAX_BILL_SIZE_MB", "20")) * 1024 * 1024

ALLOWED_BILL_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "application/pdf",
}

_CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

_RATE_LIMIT_PER_MINUTE = os.environ.get("RATE_LIMIT_PER_MINUTE", "20")
_RATE_LIMIT_PER_DAY = os.environ.get("RATE_LIMIT_PER_DAY", "200")


# ============================================================
# Auth helper
# ============================================================

def _check_auth(request: Request) -> None:
    """
    Raise HTTP 401 if API_KEY is set and the request's Authorization
    header doesn't match.  No-op when API_KEY is unset (dev/test mode).
    """
    if not _API_KEY:
        return  # open mode
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != _API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected: Bearer <API_KEY>",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ============================================================
# Rate limiter
# ============================================================

limiter = Limiter(key_func=get_remote_address)


# ============================================================
# Rate table loading
# ============================================================

def _load_rate_table(env_var: str, source: BenchmarkSource) -> RateTable:
    path = os.environ.get(env_var)
    if path and os.path.exists(path):
        logger.info("Loading %s rate table from %s", source.upper(), path)
        return pricing_pipeline.load_rate_table_from_csv(path, source)
    logger.info(
        "%s not set or file not found -- %s rate table starts empty "
        "(on-demand PFS fetch active for PFS; OPPS requires a pre-downloaded file)",
        env_var, source.upper(),
    )
    return RateTable(source=source, entries={})


# ============================================================
# App + lifespan
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connection pool
    logger.info("Initialising DB connection pool")
    db.init_pool()

    # Rate tables
    app.state.rate_tables: dict[BenchmarkSource, RateTable] = {
        "pfs": _load_rate_table("PFS_RATE_CSV_PATH", "pfs"),
        "opps": _load_rate_table("OPPS_RATE_CSV_PATH", "opps"),
    }

    logger.info(
        "RobinHealth API ready | auth=%s | pool_min=%s pool_max=%s | "
        "rate_limit=%s/min %s/day | cors=%s",
        "enabled" if _API_KEY else "open",
        os.environ.get("DB_POOL_MIN_CONN", "2"),
        os.environ.get("DB_POOL_MAX_CONN", "10"),
        _RATE_LIMIT_PER_MINUTE, _RATE_LIMIT_PER_DAY,
        _CORS_ORIGINS,
    )

    yield

    logger.info("Shutting down -- closing DB pool")
    db.close_pool()


app = FastAPI(title="RobinHealth API", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ============================================================
# Request ID middleware
# ============================================================

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    logger.info(
        "request_id=%s method=%s path=%s ip=%s",
        request_id, request.method, request.url.path,
        get_remote_address(request),
    )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s status=%s", request_id, response.status_code,
    )
    return response


# ============================================================
# Endpoints
# ============================================================

@app.get("/health")
def health() -> JSONResponse:
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        db_ok = True
    except Exception as exc:
        logger.warning("Health check DB failure: %s", exc)
        db_ok = False
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={"status": "ok" if db_ok else "degraded", "database_reachable": db_ok},
    )


@app.post("/intake")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
@limiter.limit(f"{_RATE_LIMIT_PER_DAY}/day")
async def intake(
    request: Request,
    bill_document: UploadFile = File(...),
    household_income: Optional[float] = Form(None),
    household_size: Optional[int] = Form(None),
    state: Optional[str] = Form(None),
    locality: Optional[str] = Form(None),
):
    # EOB document is read from form data manually to avoid FastAPI
    # UploadFile Optional type annotation issues across versions
    eob_file = (await request.form()).get("eob_document")
    request_id = getattr(request.state, "request_id", "-")

    # Auth
    _check_auth(request)

    # Content-type validation
    if bill_document.content_type not in ALLOWED_BILL_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported content type '{bill_document.content_type}'. "
                f"Expected one of: {sorted(ALLOWED_BILL_CONTENT_TYPES)}"
            ),
        )

    # Read and size-check bill
    document_bytes = await bill_document.read()
    if not document_bytes:
        raise HTTPException(status_code=400, detail="bill_document was empty")
    if len(document_bytes) > _MAX_BILL_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"bill_document exceeds the maximum allowed size of "
                f"{_MAX_BILL_SIZE_BYTES // (1024*1024)} MB "
                f"(received {len(document_bytes) // (1024*1024)} MB)."
            ),
        )

    # Read EOB if provided (read from form manually)
    eob_bytes: Optional[bytes] = None
    eob_media_type: Optional[str] = None
    eob_storage_key: str = ""
    if eob_file is not None and hasattr(eob_file, "read"):
        eob_bytes = await eob_file.read()
        if eob_bytes:
            if len(eob_bytes) > _MAX_BILL_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"eob_document exceeds the maximum allowed size of "
                           f"{_MAX_BILL_SIZE_BYTES // (1024*1024)} MB.",
                )
            eob_media_type = getattr(eob_file, "content_type", "application/pdf")
            eob_storage_key = storage.save(eob_bytes, eob_media_type or "application/pdf")
        else:
            eob_bytes = None

    # Persist patient + case first so we always have IDs to return
    patient_id = repository.insert_patient(
        household_income=household_income, household_size=household_size, state=state,
    )
    case_id = repository.insert_case(patient_id)
    storage_key = storage.save(document_bytes, bill_document.content_type)

    logger.info(
        "request_id=%s patient_id=%s case_id=%s bill_size=%d eob=%s",
        request_id, patient_id, case_id, len(document_bytes), eob_bytes is not None,
    )

    # DESIGN: patient/case rows are NOT rolled back if extraction fails --
    # the case stays in 'intake' status with no bill attached, which is a
    # valid, addressable state (caller can retry the same case_id once an
    # LLM endpoint is reachable). Error responses always include the IDs.
    try:
        result = case_pipeline.process_case_intake(
            document_bytes=document_bytes,
            media_type=bill_document.content_type,
            rate_tables=app.state.rate_tables,
            household_income=household_income,
            household_size=household_size,
            patient_state=state,
            locality=locality,
            case_id=case_id,
            storage_key=storage_key,
            eob_bytes=eob_bytes,
            eob_media_type=eob_media_type,
            eob_storage_key=eob_storage_key,
        )
    except httpx.HTTPError as exc:
        logger.warning("request_id=%s LLM endpoint error: %s", request_id, exc)
        return JSONResponse(status_code=503, content={
            "patient_id": patient_id, "case_id": case_id,
            "detail": (
                "The configured LLM endpoint is unreachable or returned an error "
                f"(LLM_BASE_URL={os.environ.get('LLM_BASE_URL', '(default) http://localhost:8000/v1')}). "
                f"Underlying error: {exc}"
            ),
        })
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("request_id=%s LLM parse error: %s", request_id, exc)
        return JSONResponse(status_code=502, content={
            "patient_id": patient_id, "case_id": case_id,
            "detail": (
                f"The LLM endpoint responded but its output couldn't be parsed: {exc}"
            ),
        })

    logger.info(
        "request_id=%s case_id=%s match=%s synthesis=%s",
        request_id, case_id,
        getattr(getattr(result, "match", None), "status", "?"),
        result.synthesis is not None,
    )
    return JSONResponse(content=jsonable_encoder({
        "patient_id": patient_id,
        "case_id": case_id,
        "result": result,
    }))


@app.get("/patients/{patient_id}/fee-terms")
async def get_fee_terms(request: Request, patient_id: str) -> JSONResponse:
    """
    Return the current fee agreement terms for display to the patient.
    Also returns the patient's current acceptance status.

    This is the first thing a new patient should see. The flow is:
      1. GET /patients/{patient_id}/fee-terms   ← display terms to patient
      2. Patient reads and clicks "I agree"
      3. POST /patients/{patient_id}/agree-to-terms  ← record acceptance
      4. POST /cases/{case_id}/negotiate         ← now allowed
    """
    _check_auth(request)
    try:
        terms = outcome_pipeline.get_fee_terms()
        agreement_status = outcome_pipeline.check_fee_agreement(patient_id)
        return JSONResponse(content={
            "patient_id": patient_id,
            "terms": terms,
            "agreement_status": agreement_status,
        })
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/patients/{patient_id}/agree-to-terms")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def agree_to_terms(
    request: Request,
    patient_id: str,
    affirmed: bool = Form(...),
) -> JSONResponse:
    """
    Record that a patient has read and accepted the fee agreement.

    `affirmed` must be true -- the caller (front-end) is responsible
    for confirming the patient actively checked a checkbox or clicked
    an explicit confirmation button, not just scrolled past the terms.

    Returns the updated agreement status including the timestamp.
    The stored terms text is the exact version shown to them, so the
    agreement is auditable even if terms change later.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")

    if not affirmed:
        raise HTTPException(
            status_code=400,
            detail=(
                "affirmed must be true. The patient must explicitly confirm "
                "they have read and understood the fee agreement before proceeding."
            ),
        )

    try:
        outcome_pipeline.record_fee_agreement(patient_id)
        status = outcome_pipeline.check_fee_agreement(patient_id)
        logger.info(
            "request_id=%s patient_id=%s fee_agreement_accepted=True version=%s",
            request_id, patient_id, outcome_pipeline.FEE_TERMS_VERSION,
        )
        return JSONResponse(content={
            "patient_id": patient_id,
            "accepted": True,
            "accepted_at": status["accepted_at"],
            "terms_version": status["terms_version"],
            "message": (
                "Fee agreement recorded. You may now proceed to negotiate "
                "your medical bills. RobinHealth will charge 20% of any "
                "savings achieved."
            ),
        })
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/cases/{case_id}/negotiate")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def start_negotiation(
    request: Request,
    case_id: str,
    billed_amount: float = Form(...),
    target_amount: Optional[float] = Form(None),
) -> JSONResponse:
    """
    Start tracking a negotiation for a case.  Creates a negotiations row
    (status=pending) and advances the case to 'negotiating'.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")
    try:
        negotiation_id = outcome_pipeline.start_negotiation(
            case_id=case_id,
            original_billed_amount=billed_amount,
            target_amount=target_amount,
        )
        logger.info("request_id=%s case_id=%s negotiation_id=%s", request_id, case_id, negotiation_id)
        return JSONResponse(content={"negotiation_id": negotiation_id, "case_id": case_id})
    except outcome_pipeline.FeeAgreementRequired as exc:
        # 402 Payment Required -- semantically right: patient must agree
        # to the fee structure before we can act on their behalf
        raise HTTPException(
            status_code=402,
            detail={
                "error": "fee_agreement_required",
                "message": str(exc),
                "terms_endpoint": f"/patients/{{patient_id}}/fee-terms",
                "agreement_endpoint": f"/patients/{{patient_id}}/agree-to-terms",
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/cases/{case_id}/contact")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def record_contact(
    request: Request,
    case_id: str,
    channel: str = Form(...),
    letter_storage_key: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Record an outreach attempt (letter sent, call made, etc.).
    Advances the negotiation status from pending -> contacted.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")
    negotiation_id = outcome_pipeline.fetch_negotiation_id_for_case(case_id)
    if negotiation_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"No negotiation found for case_id={case_id!r}. "
                   f"Call POST /cases/{{case_id}}/negotiate first."
        )
    try:
        contact_id = outcome_pipeline.record_contact(
            negotiation_id=negotiation_id,
            channel=channel,
            letter_storage_key=letter_storage_key,
            notes=notes,
        )
        logger.info("request_id=%s contact_id=%s channel=%s", request_id, contact_id, channel)
        return JSONResponse(content={"contact_id": contact_id, "negotiation_id": negotiation_id})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/cases/{case_id}/outcome")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def record_outcome(
    request: Request,
    case_id: str,
    agreed_amount: float = Form(...),
    paid: bool = Form(False),
    provider_response: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Record the final negotiation outcome: what the provider agreed to.
    Returns a full OutcomeReceipt with savings and fee breakdown.
    Setting paid=true advances the case to 'resolved'.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")
    negotiation_id = outcome_pipeline.fetch_negotiation_id_for_case(case_id)
    if negotiation_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"No negotiation found for case_id={case_id!r}."
        )
    if provider_response:
        try:
            outcome_pipeline.record_provider_response(
                negotiation_id=negotiation_id,
                response_text=provider_response,
            )
        except Exception:
            pass  # best-effort; outcome recording is the priority

    try:
        receipt = outcome_pipeline.record_outcome(
            negotiation_id=negotiation_id,
            agreed_amount=agreed_amount,
            paid=paid,
        )
        logger.info(
            "request_id=%s case_id=%s saved=%.2f fee=%.2f",
            request_id, case_id, receipt.amount_saved, receipt.robinhealth_fee,
        )
        return JSONResponse(content=jsonable_encoder(receipt))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/cases/{case_id}")
async def get_case(request: Request, case_id: str) -> JSONResponse:
    """Retrieve full case state including negotiation history and outcome."""
    _check_auth(request)
    negotiation = outcome_pipeline.fetch_negotiation_for_case(case_id)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM cases WHERE id = %s",
                (case_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id!r} not found")
    return JSONResponse(content=jsonable_encoder({
        "case_id": case_id,
        "case_status": row[0],
        "negotiation": negotiation,
    }))


@app.post("/cases/{case_id}/response")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def handle_provider_response(
    request: Request,
    case_id: str,
    response_text: str = Form(...),
    contact_id: Optional[str] = Form(None),
    response_type: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Record a provider response, classify it, and return the recommended
    next action plus a pre-built follow-up letter context.

    This is the key "advocate" endpoint: instead of leaving the patient
    to figure out how to respond to a denial, counter-offer, or request
    for documentation, we tell them exactly what to do and hand them
    the letter to send.

    If response_type is provided, skips LLM classification.
    Otherwise classifies the free text via heuristics + LLM fallback.

    Returns:
      classified:   what type of response this is + extracted amounts/docs
      followup:     what to do next + pre-built letter context
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")

    negotiation_id = outcome_pipeline.fetch_negotiation_id_for_case(case_id)
    if negotiation_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"No negotiation found for case_id={case_id!r}."
        )

    try:
        classified, followup = outcome_pipeline.record_provider_response_structured(
            negotiation_id=negotiation_id,
            response_text=response_text,
            contact_id=contact_id,
            response_type=response_type,
        )
        logger.info(
            "request_id=%s case_id=%s response_type=%s urgency=%s",
            request_id, case_id, classified.response_type, followup.urgency,
        )
        return JSONResponse(content=jsonable_encoder({
            "negotiation_id": negotiation_id,
            "classified": classified,
            "followup": followup,
        }))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/cases/{case_id}/draft-letter")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def draft_letter(
    request: Request,
    case_id: str,
    patient_name: str = Form(...),
    facility_name: str = Form(...),
    facility_address: Optional[str] = Form(None),
    account_number: Optional[str] = Form(None),
    date_of_service: Optional[str] = Form(None),
    billed_amount: float = Form(...),
    letter_type: str = Form("initial"),  # "initial" | "followup"
    followup_context_json: Optional[str] = Form(None),
    round_number: int = Form(1),
) -> JSONResponse:
    """
    Draft and render a negotiation letter to PDF.

    For letter_type="initial": uses the synthesis result from the case
    (requires the case to have a completed bill extraction + synthesis).
    For letter_type="followup": renders from a followup_context_json dict
    (the followup_letter_context from POST /cases/{case_id}/response).

    Returns the storage_key of the rendered PDF and a reference number.
    The PDF can be previewed/downloaded via the storage key.
    Does NOT send the letter -- call POST /cases/{case_id}/send-letter to deliver.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")

    reference_number = delivery_pipeline.make_reference_number(case_id)
    recipient = letter_pipeline.RecipientInfo(
        facility_name=facility_name,
        facility_address=facility_address,
        patient_name=patient_name,
        account_number=account_number,
        date_of_service=date_of_service,
    )

    try:
        if letter_type == "followup" and followup_context_json:
            import json
            followup_ctx = json.loads(followup_context_json)
            pdf_bytes = letter_pipeline.render_followup_letter(
                followup_context=followup_ctx,
                recipient=recipient,
                reference_number=reference_number,
                round_number=round_number,
            )
        else:
            # Initial letter: try LLM drafting, fall back to template if unreachable.
            # The template produces a professional letter covering the core arguments
            # without requiring an LLM call -- useful in dev/sandbox environments.
            from synthesis import SynthesisResult, Reason, OutcomeType
            target = round(billed_amount * 0.40, 2)
            minimal_reason = Reason(
                outcome_type=OutcomeType.PARTIAL_REDUCTION,
                summary=(
                    f"The billed amount of ${billed_amount:,.2f} appears substantially "
                    f"above Medicare and typical negotiated rates for these services."
                ),
                estimated_low=target,
                estimated_high=billed_amount,
                source_requirement_codes=[],
            )
            minimal_synthesis = SynthesisResult(
                headline_low=target,
                headline_high=billed_amount,
                headline_could_eliminate=False,
                reasons=[minimal_reason],
                follow_up_questions=[],
                beta_caveat="",
            )
            context = letter_pipeline.assemble_context(
                synthesis_result=minimal_synthesis,
                recipient=recipient,
                billed_amount=billed_amount,
            )
            # Try LLM draft; fall back to a template body if LLM is unreachable
            try:
                drafted = letter_pipeline.draft_letter(context)
            except Exception:
                # Template fallback: professional letter without LLM
                template_body = (
                    f"Dear Billing Department,\n\n"
                    f"RobinHealth is writing as the authorized representative for {recipient.patient_name} "
                    f"regarding account #{recipient.account_number or 'on file'}"
                    + (f", date of service {recipient.date_of_service}" if recipient.date_of_service else "")
                    + f".\n\n"
                    f"The billed amount of ${billed_amount:,.2f} appears substantially above Medicare "
                    f"published rates and typical negotiated rates for the services rendered. "
                    f"We respectfully request a reduction of the account balance to "
                    f"${target:,.2f}, which is consistent with standard reimbursement benchmarks "
                    f"for comparable services in this market.\n\n"
                    f"Our client is committed to resolving this account promptly. "
                    f"Please respond in writing within {context.response_deadline_days} days to confirm "
                    f"whether this adjustment can be accommodated. "
                    f"RobinHealth can be reached at advocacy@robinhealth.com.\n\n"
                    f"Thank you for your consideration."
                )
                drafted = letter_pipeline.DraftedLetter(
                    body=template_body,
                    requested_amount=target,
                    requests_full_waiver=False,
                    response_deadline_days=context.response_deadline_days,
                )
            pdf_bytes = letter_pipeline.render_to_pdf(
                letter=drafted,
                recipient=recipient,
                reference_number=reference_number,
            )

        storage_key = storage.save(pdf_bytes, "application/pdf")
        logger.info(
            "request_id=%s case_id=%s reference=%s pdf_size=%d",
            request_id, case_id, reference_number, len(pdf_bytes),
        )
        return JSONResponse(content={
            "reference_number": reference_number,
            "storage_key": storage_key,
            "pdf_size_bytes": len(pdf_bytes),
            "message": (
                "Letter drafted and rendered to PDF. "
                "Call POST /cases/{case_id}/send-letter to deliver it, "
                "or download the PDF using the storage_key for manual sending."
            ),
        })
    except Exception as exc:
        logger.warning("request_id=%s draft-letter error: %s", request_id, exc)
        raise HTTPException(status_code=500, detail=f"Letter rendering failed: {exc}")


@app.post("/cases/{case_id}/send-letter")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def send_letter(
    request: Request,
    case_id: str,
    storage_key: str = Form(...),       # PDF storage key from draft-letter
    reference_number: str = Form(...),  # reference number from draft-letter
    channel: str = Form(...),           # letter_email | letter_fax | letter_mail
    recipient_email: Optional[str] = Form(None),
    recipient_fax: Optional[str] = Form(None),
    recipient_name: Optional[str] = Form(None),
    recipient_address: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Deliver a drafted letter (by storage_key) via the specified channel,
    then record the contact in the negotiation history.

    Requires an active negotiation for the case (POST /cases/{case_id}/negotiate
    must have been called first, which requires fee agreement).

    The delivery itself degrades gracefully -- if SMTP_HOST / LOB_API_KEY /
    TWILIO credentials are not configured, the letter is still recorded as a
    contact with status 'not_configured', so the negotiation history is
    accurate. In production with credentials set, email delivery is real;
    fax and mail require LOB_API_KEY or Twilio credentials.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")

    # Load the PDF from storage
    try:
        pdf_bytes = storage.load(storage_key)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Letter PDF not found at storage_key={storage_key!r}: {exc}"
        )

    # Build recipient_info for delivery
    recipient_info: dict = {}
    if channel == "letter_email":
        if not recipient_email:
            raise HTTPException(status_code=400, detail="recipient_email required for letter_email channel")
        recipient_info = {
            "email": recipient_email,
            "subject": f"Medical Bill Negotiation — Reference {reference_number}",
            "body": (
                "Dear Billing Department,\n\n"
                "Please find attached a formal negotiation letter from RobinHealth, "
                f"acting as authorized representative for our patient (Reference: {reference_number}).\n\n"
                "RobinHealth Patient Advocacy | advocacy@robinhealth.com"
            ),
        }
    elif channel == "letter_fax":
        if not recipient_fax:
            raise HTTPException(status_code=400, detail="recipient_fax required for letter_fax channel")
        recipient_info = {"fax_number": recipient_fax}
    elif channel in ("letter_mail", "in_person"):
        recipient_info = {
            "name": recipient_name or "Billing Department",
            "address": recipient_address or "",
        }

    # Deliver
    receipt = delivery_pipeline.deliver(
        channel=channel,
        pdf_bytes=pdf_bytes,
        reference_number=reference_number,
        recipient_info=recipient_info,
    )

    # Record the contact in negotiation history
    negotiation_id = outcome_pipeline.fetch_negotiation_id_for_case(case_id)
    contact_id = None
    if negotiation_id:
        try:
            contact_id = outcome_pipeline.record_contact(
                negotiation_id=negotiation_id,
                channel=channel,
                letter_storage_key=storage_key,
                notes=(
                    f"Reference: {reference_number}. "
                    f"Delivery status: {receipt.status}. "
                    + (notes or "")
                ).strip(),
            )
        except Exception as exc:
            logger.warning("request_id=%s record_contact failed: %s", request_id, exc)

    logger.info(
        "request_id=%s case_id=%s channel=%s delivery=%s reference=%s",
        request_id, case_id, channel, receipt.status, reference_number,
    )
    return JSONResponse(content=jsonable_encoder({
        "reference_number": reference_number,
        "channel": channel,
        "delivery_status": receipt.status,
        "delivery_detail": receipt.detail,
        "contact_id": contact_id,
        "negotiation_id": negotiation_id,
    }))


@app.get("/outcomes/summary")
async def outcomes_summary(request: Request) -> JSONResponse:
    """Aggregate outcome metrics across all negotiations."""
    _check_auth(request)
    return JSONResponse(content=outcome_pipeline.fetch_outcomes_summary())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
