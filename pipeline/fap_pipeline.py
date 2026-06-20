"""
RobinHealth: FAP document parsing pipeline.

Given a facility's published Financial Assistance Policy (and related
documents: plain-language summary, billing/collections policy), this
pipeline produces two outputs:

  1. Structured eligibility data (Pass A) -> fap_eligibility_tiers,
     fap_eligible_services, fap_application_requirements
  2. A 501(r) compliance gap analysis (Pass B) -> fap_compliance_findings,
     run against the canonical checklist in compliance_checklist.py,
     REGARDLESS of document quality.

Document quality classification determines confidence levels, not whether
parsing is attempted -- Pass B in particular is most valuable on
'vague_or_incomplete' or 'not_found' documents, where the *absence* of
required elements is itself the finding.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

import llm_client
from compliance_checklist import CHECKLIST, CHECKLIST_BY_CODE, ComplianceStatus, Severity


REQUEST_TIMEOUT_SECONDS = 15.0


# ============================================================
# Data containers (mirror the Postgres schema; ORM-agnostic)
# ============================================================

@dataclass
class FetchedDocument:
    url: str | None
    text: str | None
    fetched_at: str

    @property
    def exists(self) -> bool:
        return bool(self.text)


@dataclass
class DocumentQuality:
    label: str  # 'well_structured' | 'prose_with_data' | 'vague_or_incomplete' | 'not_found'
    rationale: str


@dataclass
class EligibilityTier:
    tier_order: int
    fpl_min_pct: int | None
    fpl_max_pct: int | None
    discount_type: str
    discount_value: float | None
    household_size_adjustment: dict | None = None
    notes: str | None = None


@dataclass
class EligibilityExtraction:
    eligibility_basis: str | None
    tiers: list[EligibilityTier]
    eligible_services: list[dict]
    application_requirements: dict | None
    parsing_confidence: str  # 'high' | 'medium' | 'low' | 'failed'


@dataclass
class ComplianceFinding:
    requirement_code: str
    status: ComplianceStatus
    evidence_text: str | None
    severity: Severity
    argument_template: str


@dataclass
class FapParseResult:
    facility_id: str
    document_quality: DocumentQuality
    eligibility: EligibilityExtraction | None
    findings: list[ComplianceFinding]
    raw_text: str | None
    source_doc_hash: str | None


# ============================================================
# Stage 1: document acquisition & classification
# ============================================================

def _looks_like_pdf(content: bytes) -> bool:
    """Sniff the magic number rather than trusting Content-Type alone -- servers mislabel this often enough that it's not safe to trust."""
    return content[:5] == b"%PDF-"


def _looks_like_html(content: bytes, content_type: str) -> bool:
    if "html" in content_type.lower():
        return True
    head = content[:1000].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def _extract_pdf_text(content: bytes) -> str | None:
    """
    Extract text from PDF bytes via pypdf. None if the PDF has no
    extractable text layer (e.g. scanned/image-only -- OCR is a possible
    future enhancement, not implemented here) or can't be parsed at all.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        # A malformed/corrupted PDF is functionally the same as "no
        # usable document" for everything downstream -- classify_
        # document_quality already treats absent text as 'not_found'.
        return None
    text = text.strip()
    return text or None


def _extract_html_text(content: bytes) -> str | None:
    """Strip tags via BeautifulSoup (dropping <script>/<style> first), collapsing blank lines. None if nothing visible remains."""
    try:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        return None
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text or None


def _fetch_one_document(url: str | None) -> FetchedDocument:
    fetched_at = datetime.now(timezone.utc).isoformat()
    if not url:
        return FetchedDocument(url=None, text=None, fetched_at=fetched_at)

    try:
        response = httpx.get(url, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError:
        # Connection refused, timeout, DNS failure, etc. -- functionally
        # "document not found" for every downstream consumer, not a
        # pipeline error to propagate.
        return FetchedDocument(url=url, text=None, fetched_at=fetched_at)

    if response.status_code >= 400:
        return FetchedDocument(url=url, text=None, fetched_at=fetched_at)

    content = response.content
    content_type = response.headers.get("content-type", "")

    if _looks_like_pdf(content):
        text = _extract_pdf_text(content)
    elif _looks_like_html(content, content_type):
        text = _extract_html_text(content)
    else:
        # Plain text or an unrecognized type -- best-effort decode rather
        # than discarding it outright.
        try:
            text = content.decode("utf-8", errors="replace").strip() or None
        except Exception:
            text = None

    return FetchedDocument(url=url, text=text, fetched_at=fetched_at)


def fetch_fap_documents(
    fap_url: str | None,
    pls_url: str | None,
    billing_policy_url: str | None,
) -> dict[str, FetchedDocument]:
    """
    Fetch the FAP, plain-language summary, and billing/collections policy.

    Each URL is fetched independently -- one failing doesn't affect the
    others. A missing URL, a fetch failure (timeout, connection refused,
    4xx/5xx), or content that yields no extractable text are all treated
    identically: the resulting FetchedDocument.exists is False, and
    downstream stages treat document_quality as 'not_found' -- itself a
    finding (fap_document_exists), not a pipeline error.

    Handles PDF and HTML, the two formats real hospital FAP pages
    overwhelmingly use -- see _extract_pdf_text / _extract_html_text.
    """
    return {
        "fap": _fetch_one_document(fap_url),
        "pls": _fetch_one_document(pls_url),
        "billing": _fetch_one_document(billing_policy_url),
    }


def hash_document(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_document_quality(fap_doc: FetchedDocument) -> DocumentQuality:
    """
    Classify the FAP document into one of:
      - not_found: fap_doc.exists is False
      - well_structured / prose_with_data / vague_or_incomplete: via LLM

    Classification informs parsing_confidence defaults and how much weight
    Pass A structured output should carry vs. raw_text fallback -- it does
    NOT gate whether Pass B runs (Pass B always runs).
    """
    if not fap_doc.exists:
        return DocumentQuality(
            label="not_found",
            rationale="No FAP document could be fetched from the provided URL.",
        )

    prompt = (
        "Classify this hospital Financial Assistance Policy document into "
        "one of: well_structured, prose_with_data, vague_or_incomplete.\n"
        "well_structured = clear tables of income tiers and discounts.\n"
        "prose_with_data = legal prose but specific numbers/thresholds are "
        "extractable.\n"
        "vague_or_incomplete = lacks specific thresholds, AGB methodology, "
        "or other required elements.\n\n"
        'Return JSON: {"label": ..., "rationale": ...}\n\n'
        f"DOCUMENT:\n{fap_doc.text}"
    )
    data = llm_client.complete_json(prompt, max_tokens=300)
    return DocumentQuality(label=data["label"], rationale=data["rationale"])


# ============================================================
# Stage 2a: Pass A -- eligibility extraction
# ============================================================

EXTRACTION_PROMPT = """\
You are extracting structured data from a hospital's Financial Assistance \
Policy (FAP) document for RobinHealth, a service that helps patients \
identify financial assistance eligibility.

Extract the following as JSON. Use null for any field that cannot be \
determined from the document -- do not guess or infer values that aren't \
stated or clearly implied.

{{
  "eligibility_basis": one of "fpl_percentage" | "flat_income" | \
"asset_test" | "combination" | null,
  "tiers": [
    {{
      "tier_order": int,
      "fpl_min_pct": int or null,
      "fpl_max_pct": int or null,
      "discount_type": one of "full_charity_care" | "percentage_discount" \
| "sliding_scale" | "flat_cap",
      "discount_value": number or null,
      "household_size_adjustment": object or null,
      "notes": string or null
    }}
  ],
  "eligible_services": [
    {{"service_category": string, "is_covered": bool, "notes": string or null}}
  ],
  "application_requirements": {{
    "application_deadline_days": int or null,
    "required_documents": [string] or null,
    "presumptive_eligibility_criteria": [string] or null,
    "notification_method_required": [string] or null
  }} or null,
  "parsing_confidence": one of "high" | "medium" | "low" | "failed"
}}

DOCUMENT:
{document_text}
"""


def extract_eligibility(
    fap_doc: FetchedDocument, quality: DocumentQuality
) -> EligibilityExtraction:
    """
    Pass A: extract income tiers, discount structure, service exclusions,
    and procedural requirements.

    Even for 'vague_or_incomplete' documents, this runs and returns
    whatever partial data is extractable, with parsing_confidence set
    accordingly -- the gaps are what Pass B is for.
    """
    if quality.label == "not_found":
        return EligibilityExtraction(
            eligibility_basis=None,
            tiers=[],
            eligible_services=[],
            application_requirements=None,
            parsing_confidence="failed",
        )

    data = llm_client.complete_json(EXTRACTION_PROMPT.format(document_text=fap_doc.text))
    return EligibilityExtraction(
        eligibility_basis=data["eligibility_basis"],
        tiers=[EligibilityTier(**t) for t in data["tiers"]],
        eligible_services=data["eligible_services"],
        application_requirements=data["application_requirements"],
        parsing_confidence=data["parsing_confidence"],
    )


# ============================================================
# Stage 2b: Pass B -- 501(r) compliance checklist
# ============================================================

COMPLIANCE_PROMPT = """\
You are reviewing a hospital's Financial Assistance Policy (FAP) and \
related documents for compliance with Section 501(r) of the Internal \
Revenue Code, on behalf of RobinHealth, a patient billing advocacy service.

For EACH requirement below, determine its status:
  - "present": the document clearly addresses this requirement
  - "vague": the document references this but without enough specificity \
to act on
  - "absent": the document does not address this requirement at all
  - "contradicted": this document contradicts another provided document \
on this point

Return JSON: a list of objects, one per requirement code, each with:
  {{"requirement_code": ..., "status": ..., "evidence_text": <short quote \
or null>}}

REQUIREMENTS:
{requirements}

DOCUMENTS:
--- Financial Assistance Policy ---
{fap_text}

--- Plain-Language Summary ---
{pls_text}

--- Billing & Collections Policy ---
{billing_text}

--- Patient's Bill / Account Documents (for cross-document checks) ---
{patient_docs_text}
"""


def run_compliance_checklist(
    fap_doc: FetchedDocument,
    pls_doc: FetchedDocument,
    billing_doc: FetchedDocument,
    patient_docs_text: str,
) -> list[ComplianceFinding]:
    """
    Pass B: run the full 501(r) checklist regardless of document quality.

    For 'not_found' FAPs, this still returns a finding for
    'fap_document_exists' with status=ABSENT and severity=MATERIAL. Every
    other requirement_code is effectively moot in that case; the synthesis
    layer treats fap_document_exists as the headline issue rather than
    enumerating the rest.
    """
    if not fap_doc.exists:
        item = CHECKLIST_BY_CODE["fap_document_exists"]
        return [
            ComplianceFinding(
                requirement_code=item.code,
                status=ComplianceStatus.ABSENT,
                evidence_text=None,
                severity=item.default_severity,
                argument_template=item.argument_template,
            )
        ]

    requirements_block = "\n".join(
        f"- {item.code}: {item.description}" for item in CHECKLIST
    )

    prompt = COMPLIANCE_PROMPT.format(
        requirements=requirements_block,
        fap_text=fap_doc.text or "(not provided)",
        pls_text=pls_doc.text or "(not provided)",
        billing_text=billing_doc.text or "(not provided)",
        patient_docs_text=patient_docs_text or "(not provided)",
    )
    raw_findings = llm_client.complete_json(prompt)
    findings = []
    for rf in raw_findings:
        # .get() rather than direct indexing, and skip rather than raise,
        # on purpose: an LLM occasionally returns a requirement_code or
        # status value that doesn't match the canonical checklist
        # (hallucination/formatting drift). Unlike extract_eligibility's
        # tiers (order-sensitive, where dropping one could silently shift
        # which tier a real income matches -- failing loudly there is
        # safer), these 8 findings are independent yes/no items, so
        # losing one to a formatting glitch shouldn't cost the other 7.
        item = CHECKLIST_BY_CODE.get(rf.get("requirement_code"))
        if item is None:
            continue
        try:
            status = ComplianceStatus(rf.get("status"))
        except ValueError:
            continue
        findings.append(ComplianceFinding(
            requirement_code=item.code,
            status=status,
            evidence_text=rf.get("evidence_text"),
            severity=item.default_severity,
            argument_template=item.argument_template,
        ))
    return findings


# ============================================================
# Orchestration
# ============================================================

def parse_fap(
    facility_id: str,
    fap_url: str | None,
    pls_url: str | None,
    billing_policy_url: str | None,
    patient_docs_text: str = "",
) -> FapParseResult:
    """
    Full pipeline entry point: fetch -> classify -> Pass A -> Pass B.

    Returns a FapParseResult ready to be persisted across
    financial_assistance_policies, fap_eligibility_tiers,
    fap_eligible_services, fap_application_requirements, and
    fap_compliance_findings.
    """
    docs = fetch_fap_documents(fap_url, pls_url, billing_policy_url)
    fap_doc, pls_doc, billing_doc = docs["fap"], docs["pls"], docs["billing"]

    quality = classify_document_quality(fap_doc)
    eligibility = extract_eligibility(fap_doc, quality)
    findings = run_compliance_checklist(fap_doc, pls_doc, billing_doc, patient_docs_text)

    return FapParseResult(
        facility_id=facility_id,
        document_quality=quality,
        eligibility=eligibility,
        findings=findings,
        raw_text=fap_doc.text,
        source_doc_hash=hash_document(fap_doc.text) if fap_doc.exists else None,
    )
