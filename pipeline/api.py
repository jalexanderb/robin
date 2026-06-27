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
import re
import uuid
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import case_pipeline
import db
import delivery_pipeline
import legal_leverage
import letter_pipeline
import llm_client
import outcome_pipeline
import pricing_pipeline
import repository
import storage
import synthesis
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

# storage_keys are content-addressed (sha256 hex + extension). Validate the
# shape before loading so the download endpoint can never be coaxed into
# reading outside the blob store, and so we serve a correct content type.
_STORAGE_KEY_RE = re.compile(r"^[a-f0-9]{64}(\.[a-z0-9]{2,5})?$")
_CONTENT_TYPE_BY_EXT = {
    ".pdf": "application/pdf", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}

_CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

_RATE_LIMIT_PER_MINUTE = os.environ.get("RATE_LIMIT_PER_MINUTE", "20")
_RATE_LIMIT_PER_DAY = os.environ.get("RATE_LIMIT_PER_DAY", "200")


# System prompt for the patient-facing chat (POST /chat). Grounds Robin's
# answers in the patient's own case when context is supplied, and keeps it
# inside its lane (bills, insurance, patient rights, financial assistance).
ROBIN_CHAT_SYSTEM = (
    "You are Robin, an AI-powered patient advocate for RobinHealth. You help "
    "people understand and push back on confusing or excessive medical bills. "
    "You can explain medical bills and the charges/codes on them, how insurance "
    "and EOBs work, a patient's rights, hospital financial assistance policies "
    "(charity care / FAP) and the 501(r) rules for nonprofit hospitals, and how "
    "bill negotiation works.\n\n"
    "Style: warm, direct, and plain-spoken. Short paragraphs. Answer the "
    "question first, then any brief caveat. No jargon without explaining it.\n\n"
    "Ground rules:\n"
    "- RobinHealth is in beta. For anything consequential, remind the user to "
    "review carefully before acting.\n"
    "- You are not a lawyer, doctor, or tax advisor, and you do not give legal, "
    "medical, or tax advice. Say so if a question crosses that line.\n"
    "- Never invent specific dollar amounts, statutes, policy terms, or facts "
    "about the user's bill that you were not given. If you don't know, say so "
    "and explain how to find out.\n"
    "- If a precise answer needs the actual bill and one hasn't been shared, "
    "encourage the user to upload it.\n"
    "- Pricing: the user never pays more than $50/month, or 20% of what "
    "RobinHealth saves them (capped at $1,000) -- whichever they prefer. "
    "Pay-per-win charges nothing if nothing is saved; the $50/month membership "
    "takes 0% of savings and is free until the first win.\n"
    "- If a question is clearly unrelated to medical bills, healthcare costs, "
    "or insurance, gently steer back to what you can help with."
)


def _build_chat_prompt(message: str, context_json: Optional[str]) -> str:
    """
    Assemble the user turn for /chat: the patient's question, plus a compact,
    plain-text summary of their current case (if the front-end supplied one)
    so Robin can answer about *their* bill rather than in generalities.
    """
    if not context_json:
        return message

    import json
    try:
        ctx = json.loads(context_json)
    except (json.JSONDecodeError, TypeError):
        return message
    if not isinstance(ctx, dict):
        return message

    lines: list[str] = []
    provider = ctx.get("provider")
    if provider:
        lines.append(f"- Provider: {provider}")
    if ctx.get("billed_amount") is not None:
        lines.append(f"- Total billed: ${ctx['billed_amount']}")
    if ctx.get("estimated_low") is not None:
        lines.append(f"- Robin's estimated reduced balance: ${ctx['estimated_low']}")
    if ctx.get("household_income") is not None:
        lines.append(f"- Household income: ${ctx['household_income']}")
    if ctx.get("household_size") is not None:
        lines.append(f"- Household size: {ctx['household_size']}")
    for reason in (ctx.get("reasons") or [])[:5]:
        if isinstance(reason, str) and reason.strip():
            lines.append(f"- Finding: {reason.strip()}")

    if not lines:
        return message
    return (
        "Here is the context for this patient's current case:\n"
        + "\n".join(lines)
        + f"\n\nPatient's question: {message}"
    )


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
    # Optional self-setup: apply the DB schema on boot when AUTO_MIGRATE is set.
    # Idempotent (see migrate.py) -- makes first-time cloud deploys one-step.
    if os.environ.get("AUTO_MIGRATE", "").strip().lower() in ("1", "true", "yes"):
        logger.info("AUTO_MIGRATE set -- applying database schema")
        import migrate
        migrate.run_migrations()

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
async def intake(request: Request):
    # EOB document is read from form data manually to avoid FastAPI
    # UploadFile Optional type annotation issues across versions
    eob_file = (await request.form()).get("eob_document")
    request_id = getattr(request.state, "request_id", "-")

    # Parse all fields from the raw multipart form
    form = await request.form()
    bill_document = form.get("bill_document")
    household_income_raw = form.get("household_income")
    household_size_raw = form.get("household_size")
    state = form.get("state") or None
    locality = form.get("locality") or None
    household_income = float(household_income_raw) if household_income_raw else None
    household_size = int(household_size_raw) if household_size_raw else None

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


@app.post("/chat")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
@limiter.limit(f"{_RATE_LIMIT_PER_DAY}/day")
async def chat(
    request: Request,
    message: str = Form(...),
    context_json: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Free-form patient Q&A, answered by the configured LLM (Claude by default).

    This replaces the front-end's old keyword/if-else canned replies: real
    answers about the user's bill, insurance, rights, and financial-assistance
    options. Optional context_json (a small JSON summary of the user's current
    case from the /intake response) lets Robin answer about *their* bill.

    Returns {"reply": "..."}. On LLM error, returns a graceful fallback message
    with HTTP 200 so the chat UI never shows a hard error to a patient.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")

    cleaned = (message or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="message must not be empty")
    if len(cleaned) > 4000:
        cleaned = cleaned[:4000]

    prompt = _build_chat_prompt(cleaned, context_json)
    try:
        reply = llm_client.complete(
            prompt, system=ROBIN_CHAT_SYSTEM, max_tokens=700,
        ).strip()
    except Exception as exc:  # noqa: BLE001 -- patient UX must never hard-fail here
        logger.warning("request_id=%s chat LLM error: %s", request_id, exc)
        return JSONResponse(content={
            "reply": (
                "Sorry — I'm having trouble answering right now. You can still "
                "upload your bill and I'll analyze it, or try your question again "
                "in a moment."
            ),
            "degraded": True,
        })

    logger.info("request_id=%s chat reply_len=%d", request_id, len(reply))
    return JSONResponse(content={"reply": reply})


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
            "current_plan": outcome_pipeline.get_patient_plan(patient_id),
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
                "Fee agreement recorded. You may now proceed with your "
                "medical bills. You'll never pay more than $50/month, or 20% "
                "of what we save you (capped at $1,000) — whichever you "
                "choose. Set your plan at POST /patients/{patient_id}/plan."
            ),
        })
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/patients/{patient_id}/plan")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def set_plan(
    request: Request,
    patient_id: str,
    plan: str = Form(...),  # "contingency" | "membership"
) -> JSONResponse:
    """
    Choose the billing plan: 'contingency' (20% of savings, capped at $1,000,
    nothing if we save nothing) or 'membership' ($50/month flat, 0% of
    savings, free until the first win). The patient picks whichever costs less.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")
    try:
        outcome_pipeline.set_patient_plan(patient_id, plan)
    except ValueError as exc:
        # Unknown plan -> 400; unknown patient -> 404
        if "Unknown plan" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(status_code=404, detail=str(exc))
    logger.info("request_id=%s patient_id=%s plan=%s", request_id, patient_id, plan)
    return JSONResponse(content={
        "patient_id": patient_id,
        "plan": plan,
        "membership_monthly_usd": outcome_pipeline.MEMBERSHIP_MONTHLY_PRICE_USD,
        "contingency_fee_cap_usd": outcome_pipeline.FEE_CAP_USD,
    })


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


@app.get("/cases/{case_id}/full")
async def get_case_full(request: Request, case_id: str) -> JSONResponse:
    """
    Full case state for resuming a session: the bill, the stored synthesis
    (savings estimate + reasons), and any negotiation. Lets the front-end
    rebuild the analysis view without re-uploading the bill.
    """
    _check_auth(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id!r} not found")
    return JSONResponse(content=jsonable_encoder({
        "case_id": case_id,
        "case_status": row[0],
        "bill": repository.fetch_bill_for_case(case_id),
        "synthesis": repository.fetch_case_synthesis(case_id),
        "negotiation": outcome_pipeline.fetch_negotiation_for_case(case_id),
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
    # Optional patient-provided facts that unlock statutory leverage in the
    # initial letter. Each is "yes" | "no" | "unsure"/absent.
    emergency: Optional[str] = Form(None),
    out_of_network: Optional[str] = Form(None),
    received_itemized: Optional[str] = Form(None),
    self_pay: Optional[str] = Form(None),
    good_faith_estimate: Optional[str] = Form(None),
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

    # Confidence gate: refuse to draft a letter when the bill was extracted
    # with low/failed confidence -- a wrong amount, provider, or code in formal
    # correspondence to a provider is a credibility and liability risk. Only
    # blocks when a bill is persisted AND its confidence is poor; cases with no
    # persisted bill (e.g. direct API use) are not blocked.
    confidence = repository.fetch_bill_parsing_confidence(case_id)
    if confidence in ("low", "failed"):
        logger.info("request_id=%s draft-letter blocked: parsing_confidence=%s", request_id, confidence)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "low_confidence_extraction",
                "message": (
                    f"The bill for this case was extracted with '{confidence}' "
                    "confidence, so a negotiation letter can't be generated yet "
                    "-- it could carry a wrong amount, provider, or code into a "
                    "formal letter. Please upload a clearer or itemized bill and "
                    "re-run the analysis first."
                ),
                "parsing_confidence": confidence,
            },
        )

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
            # Initial letter. Build it from the patient's REAL analysis
            # (persisted synthesis: FAP eligibility, 501(r) gaps, pricing
            # benchmarks, EOB allowed-amounts), falling back to a generic
            # reduction argument only if no synthesis was stored.
            from synthesis import SynthesisResult, Reason, OutcomeType
            stored = repository.fetch_case_synthesis(case_id)
            if stored:
                synthesis_result = synthesis.synthesis_from_dict(stored)
            else:
                target = round(billed_amount * 0.40, 2)
                synthesis_result = SynthesisResult(
                    headline_low=target,
                    headline_high=billed_amount,
                    headline_could_eliminate=False,
                    reasons=[Reason(
                        outcome_type=OutcomeType.PARTIAL_REDUCTION,
                        summary=(
                            f"The billed amount of ${billed_amount:,.2f} appears substantially "
                            f"above Medicare and typical negotiated rates for these services."
                        ),
                        estimated_low=target,
                        estimated_high=billed_amount,
                        source_requirement_codes=[],
                    )],
                    follow_up_questions=[],
                    beta_caveat="",
                )

            context = letter_pipeline.assemble_context(
                synthesis_result=synthesis_result,
                recipient=recipient,
                billed_amount=billed_amount,
            )

            # Layer in statutory leverage from the patient's answers (No
            # Surprises Act, price transparency, itemized-bill rights). 501(r)
            # is left to the synthesis above to avoid double-citing.
            def _yn(v):
                if v is None:
                    return None
                return v.strip().lower() in ("yes", "true", "1", "y")
            leverage = legal_leverage.build_leverage_arguments(
                emergency=_yn(emergency),
                out_of_network=_yn(out_of_network),
                received_itemized=_yn(received_itemized),
                self_pay=_yn(self_pay),
                good_faith_estimate=_yn(good_faith_estimate),
            )
            for la in leverage:
                context.arguments.append(letter_pipeline.LetterArgument(
                    outcome_type=OutcomeType.PROCEDURAL_LEVERAGE,
                    text=la.text,
                    requested_amount=None,
                    source_requirement_codes=[],
                ))
            # Try LLM draft; fall back to a template body only if the LLM
            # endpoint is unreachable or errors at the HTTP layer. Other
            # failures (config, parsing, bugs) propagate to the outer handler
            # as a 500 rather than being silently masked by the template.
            try:
                drafted = letter_pipeline.draft_letter(context)
            except httpx.HTTPError:
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


@app.post("/cases/{case_id}/appeal-letter")
@limiter.limit(f"{_RATE_LIMIT_PER_MINUTE}/minute")
async def appeal_letter(
    request: Request,
    case_id: str,
    patient_name: str = Form(...),
    insurer_name: str = Form(...),
    insurer_address: Optional[str] = Form(None),
    member_id: Optional[str] = Form(None),
    claim_number: Optional[str] = Form(None),
    date_of_service: Optional[str] = Form(None),
    denial_reason: Optional[str] = Form(None),
) -> JSONResponse:
    """
    Draft and render an appeal addressed to the patient's insurer (a denied or
    mis-processed claim), as a PDF. Same response shape as draft-letter, so the
    front-end reuses the send-letter / open-PDF flow. Not gated on bill
    extraction confidence -- the appeal is about the insurer's claim handling,
    not the provider's bill.
    """
    _check_auth(request)
    request_id = getattr(request.state, "request_id", "-")
    reference_number = delivery_pipeline.make_reference_number(case_id)
    try:
        pdf_bytes = letter_pipeline.render_insurer_appeal_letter(
            patient_name=patient_name,
            insurer_name=insurer_name,
            reference_number=reference_number,
            insurer_address=insurer_address,
            member_id=member_id,
            claim_number=claim_number,
            date_of_service=date_of_service,
            denial_reason=denial_reason,
        )
        storage_key = storage.save(pdf_bytes, "application/pdf")
        logger.info(
            "request_id=%s case_id=%s appeal reference=%s pdf_size=%d",
            request_id, case_id, reference_number, len(pdf_bytes),
        )
        return JSONResponse(content={
            "reference_number": reference_number,
            "storage_key": storage_key,
            "pdf_size_bytes": len(pdf_bytes),
            "message": (
                "Insurer appeal letter drafted and rendered to PDF. "
                "Call POST /cases/{case_id}/send-letter to deliver it, "
                "or download the PDF using the storage_key."
            ),
        })
    except Exception as exc:
        logger.warning("request_id=%s appeal-letter error: %s", request_id, exc)
        raise HTTPException(status_code=500, detail=f"Appeal letter rendering failed: {exc}")


@app.get("/letters/{storage_key}")
async def get_letter(request: Request, storage_key: str):
    """
    Serve a drafted letter PDF by its storage_key (returned by draft-letter),
    so the patient can view or download it.

    storage_keys are content-addressed sha256 hashes -- effectively
    unguessable capability tokens. Path traversal is doubly prevented:
    storage.load() strips to basename, and the key shape is validated here.
    """
    _check_auth(request)
    if not _STORAGE_KEY_RE.match(storage_key):
        raise HTTPException(status_code=400, detail="Invalid storage_key")
    try:
        data = storage.load(storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No file found for that storage_key")
    ext = os.path.splitext(storage_key)[1].lower()
    media_type = _CONTENT_TYPE_BY_EXT.get(ext, "application/pdf")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="robinhealth-letter{ext or ".pdf"}"'},
    )


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
