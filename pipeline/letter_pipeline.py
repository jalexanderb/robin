"""
RobinHealth: negotiation letter drafting pipeline.

Takes the output of the synthesis layer (synthesis.py) -- a ranked list of
Reasons, each either eligibility-based (dollar estimate) or
procedural-leverage (a 501(r) compliance gap) -- and turns it into the
actual letter sent to the provider.

Two stages:

  1. assemble_context: pure-Python translation of Reasons into
     LetterArgument objects. For PROCEDURAL_LEVERAGE reasons this pulls
     argument_template from compliance_checklist.py (provider-facing,
     conservative phrasing) rather than the patient-facing
     user_facing_summary already used upstream in synthesis -- the two
     audiences need different language for the same finding.

  2. draft_letter: an LLM call that takes the assembled LetterContext and
     produces the formatted letter text, including a requested resolution
     and a response deadline.

Kept separate from synthesis.py because this is the only place
patient-facing language and provider-facing language need to coexist in
the same object (LetterContext.patient_summary vs. arguments[].text) --
mixing that into SynthesisResult would blur a deliberate separation.
"""

from __future__ import annotations

from dataclasses import dataclass

import llm_client
from compliance_checklist import CHECKLIST_BY_CODE
from synthesis import OutcomeType, Reason, SynthesisResult

# How long the provider is given to respond before RobinHealth follows up
# or escalates. Tune empirically once real response-time data exists.
DEFAULT_RESPONSE_DEADLINE_DAYS = 21


# ============================================================
# Data containers
# ============================================================

@dataclass
class RecipientInfo:
    facility_name: str
    facility_address: str | None
    patient_name: str
    account_number: str | None
    date_of_service: str | None  # ISO date string


@dataclass
class LetterArgument:
    outcome_type: OutcomeType
    text: str  # provider-facing phrasing (argument_template, or a pricing statement)
    requested_amount: float | None  # the dollar figure this argument supports, if any
    source_requirement_codes: list[str]


@dataclass
class LetterContext:
    recipient: RecipientInfo
    billed_amount: float
    arguments: list[LetterArgument]
    requested_amount: float | None  # the bottom-line ask; None if requesting full waiver
    requests_full_waiver: bool
    response_deadline_days: int


@dataclass
class DraftedLetter:
    body: str
    requested_amount: float | None
    requests_full_waiver: bool
    response_deadline_days: int


# ============================================================
# Stage 1: context assembly (pure Python -- no LLM call)
# ============================================================

def _argument_for_reason(reason: Reason) -> LetterArgument:
    """
    Translate one Reason into provider-facing letter language.

    PROCEDURAL_LEVERAGE reasons map 1:1 to a single compliance_checklist
    code (synthesis.py only ever populates source_requirement_codes with
    zero or one code per Reason today) -- argument_template is used as-is.

    FULL_ELIMINATION / PARTIAL_REDUCTION reasons don't come from the
    checklist, so their provider-facing text is built directly from the
    Reason's own summary and dollar fields -- already plain-language and
    factual, just reframed as a request rather than a finding.
    """
    if reason.outcome_type == OutcomeType.PROCEDURAL_LEVERAGE:
        # .get(), not direct indexing -- defense in depth. With the fix
        # in synthesis.findings_to_reasons, every PROCEDURAL_LEVERAGE
        # Reason reaching this function should already carry a code
        # that's in CHECKLIST_BY_CODE, but relying on that invariant
        # holding across a module boundary forever is fragile, and this
        # generates text that goes into a real letter to a provider.
        item = CHECKLIST_BY_CODE.get(reason.source_requirement_codes[0]) if reason.source_requirement_codes else None
        text = item.argument_template if item is not None else reason.summary
        return LetterArgument(
            outcome_type=reason.outcome_type,
            text=text,
            requested_amount=None,
            source_requirement_codes=reason.source_requirement_codes,
        )

    if reason.outcome_type == OutcomeType.FULL_ELIMINATION:
        text = (
            "Based on the facility's published Financial Assistance "
            "Policy and the patient's household income relative to the "
            "Federal Poverty Level, this account appears eligible for "
            "full charity care."
        )
        return LetterArgument(
            outcome_type=reason.outcome_type,
            text=text,
            requested_amount=0,
            source_requirement_codes=[],
        )

    # PARTIAL_REDUCTION
    text = (
        f"Based on the facility's published Financial Assistance Policy "
        f"and the patient's household income relative to the Federal "
        f"Poverty Level, this account appears eligible for a reduced "
        f"balance of approximately ${reason.estimated_low:,.2f}, rather "
        f"than the billed ${reason.estimated_high:,.2f}."
        if reason.estimated_low is not None and reason.estimated_high is not None
        else (
            "This charge appears substantially above comparable rates "
            "for the same service."
        )
    )
    return LetterArgument(
        outcome_type=reason.outcome_type,
        text=text,
        requested_amount=reason.estimated_low,
        source_requirement_codes=[],
    )


def assemble_context(
    synthesis_result: SynthesisResult,
    recipient: RecipientInfo,
    billed_amount: float,
    response_deadline_days: int = DEFAULT_RESPONSE_DEADLINE_DAYS,
) -> LetterContext:
    """
    Build a LetterContext from a SynthesisResult.

    The bottom-line ask (requested_amount / requests_full_waiver) follows
    the same precedence synthesis.py already established for the headline
    estimate: a FULL_ELIMINATION reason takes priority over any partial
    reduction, since it's a stronger ask. If no reason carries a dollar
    figure (i.e. only procedural-leverage reasons exist), the letter
    requests a response/review without a specific number -- the procedural
    arguments are the entire ask in that case.
    """
    arguments = [_argument_for_reason(r) for r in synthesis_result.reasons]

    requests_full_waiver = synthesis_result.headline_could_eliminate
    if requests_full_waiver:
        requested_amount = 0.0
    else:
        # lowest estimated_low across PARTIAL_REDUCTION arguments, if any
        amounts = [a.requested_amount for a in arguments if a.requested_amount is not None]
        requested_amount = min(amounts) if amounts else None

    return LetterContext(
        recipient=recipient,
        billed_amount=billed_amount,
        arguments=arguments,
        requested_amount=requested_amount,
        requests_full_waiver=requests_full_waiver,
        response_deadline_days=response_deadline_days,
    )


# ============================================================
# Stage 2: drafting (Claude call)
# ============================================================

LETTER_PROMPT = """\
You are drafting a formal medical-bill dispute/negotiation letter on behalf \
of a patient, sent by RobinHealth (a billing-advocacy service acting as the \
patient's authorized representative) to a healthcare provider's billing \
department. The goal is a letter that is professional and respectful but \
firm and well-supported -- one a billing department must take seriously.

The letter MUST:
- Open by identifying RobinHealth as the patient's authorized representative \
and the account it concerns (use the recipient details).
- Present EACH argument below as its own clearly stated, numbered point. \
Preserve the specific legal citations and dollar figures exactly as given -- \
do not soften them, drop them, or invent any new citations or numbers.
- Frame every point as a factual observation and a lawful request, never a \
threat or an accusation.
- State the requested resolution clearly and prominently as the bottom line.
- Request a written, itemized response within the deadline, and ask that \
collection activity be paused while the account is under review.
- Include one brief, matter-of-fact sentence noting that if the account is \
not resolved the patient may seek review by the appropriate oversight bodies \
(for example, the state attorney general's office, the state insurance \
regulator, or CMS / the federal No Surprises Help Desk) -- stated as the \
patient's available options, not as a threat.
- NOT include a signature block, letterhead, or date -- those are added by \
the calling system. Begin directly with the salutation.

Keep it to a tight single page, and lead with the strongest arguments.

RECIPIENT:
Facility: {facility_name}
Address: {facility_address}
Patient: {patient_name}
Account number: {account_number}
Date of service: {date_of_service}

BILLED AMOUNT: ${billed_amount:,.2f}

ARGUMENTS (use all of these; keep the citations and figures verbatim):
{arguments_block}

REQUESTED RESOLUTION: {resolution_block}

RESPONSE DEADLINE: {response_deadline_days} days from the date of this letter.
"""


def _format_arguments_block(arguments: list[LetterArgument]) -> str:
    return "\n".join(f"{i + 1}. {arg.text}" for i, arg in enumerate(arguments))


def _format_resolution_block(context: LetterContext) -> str:
    if context.requests_full_waiver:
        return "Full waiver of the billed amount under the facility's Financial Assistance Policy."
    if context.requested_amount is not None:
        return (
            f"Reduction of the account balance to approximately "
            f"${context.requested_amount:,.2f}."
        )
    return (
        "A written response addressing the points above, and a revised "
        "account statement reflecting any applicable corrections."
    )


def draft_letter(context: LetterContext) -> DraftedLetter:
    """Render context into a complete letter via the configured LLM (see llm_client.py)."""
    prompt = LETTER_PROMPT.format(
        facility_name=context.recipient.facility_name,
        facility_address=context.recipient.facility_address or "(not provided)",
        patient_name=context.recipient.patient_name,
        account_number=context.recipient.account_number or "(not provided)",
        date_of_service=context.recipient.date_of_service or "(not provided)",
        billed_amount=context.billed_amount,
        arguments_block=_format_arguments_block(context.arguments),
        resolution_block=_format_resolution_block(context),
        response_deadline_days=context.response_deadline_days,
    )
    body = llm_client.complete(prompt, max_tokens=1500)
    return DraftedLetter(
        body=body,
        requested_amount=context.requested_amount,
        requests_full_waiver=context.requests_full_waiver,
        response_deadline_days=context.response_deadline_days,
    )


# ============================================================
# Stage 3: PDF rendering (reportlab)
# ============================================================

from io import BytesIO
from datetime import date as _date

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


_ROBINHEALTH_RED = colors.HexColor("#E03E27")
_DARK = colors.HexColor("#1A1A1A")
_LIGHT_GREY = colors.HexColor("#F5F5F5")


def _build_styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "letterhead_name": ParagraphStyle(
            "letterhead_name",
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=colors.white,
            spaceAfter=2,
        ),
        "letterhead_tagline": ParagraphStyle(
            "letterhead_tagline",
            fontName="Helvetica",
            fontSize=9,
            textColor=colors.white,
            spaceAfter=0,
        ),
        "meta": ParagraphStyle(
            "meta",
            fontName="Helvetica",
            fontSize=9,
            textColor=colors.HexColor("#666666"),
            spaceAfter=4,
        ),
        "address": ParagraphStyle(
            "address",
            fontName="Helvetica",
            fontSize=10,
            textColor=_DARK,
            leading=14,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="Helvetica",
            fontSize=10,
            textColor=_DARK,
            leading=15,
            spaceAfter=10,
        ),
        "subject": ParagraphStyle(
            "subject",
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=_DARK,
            spaceAfter=12,
        ),
        "footer": ParagraphStyle(
            "footer",
            fontName="Helvetica",
            fontSize=8,
            textColor=colors.HexColor("#888888"),
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer",
            fontName="Helvetica-Oblique",
            fontSize=8,
            textColor=colors.HexColor("#888888"),
            leading=11,
        ),
    }


def render_to_pdf(
    letter: DraftedLetter,
    recipient: RecipientInfo,
    reference_number: str,
    sender_name: str = "RobinHealth Patient Advocacy",
    sender_email: str = "advocacy@robinhealth.com",
    sender_phone: str = "(888) ROB-INHL",
) -> bytes:
    """
    Render a DraftedLetter to a formatted PDF with:
      - RobinHealth branded letterhead (red/dark color scheme)
      - Reference number, date, and recipient address block
      - Letter body (from the LLM-drafted text)
      - Signature block
      - Beta disclaimer footer
      - Page numbers

    Returns raw PDF bytes, suitable for saving to storage.save().
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.9 * inch,
    )
    s = _build_styles()
    story = []

    # ---- Letterhead ----
    lh_data = [[
        Paragraph("RobinHealth", s["letterhead_name"]),
        Paragraph("Your AI-enabled health advocate", s["letterhead_tagline"]),
    ]]
    lh_table = Table(lh_data, colWidths=[3.5 * inch, 3.5 * inch])
    lh_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ROBINHEALTH_RED),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 16),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(lh_table)
    story.append(Spacer(1, 0.15 * inch))

    # ---- Sender contact + reference block ----
    today = _date.today().strftime("%B %d, %Y")
    story.append(Paragraph(
        f"{sender_name}&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"{sender_email}&nbsp;&nbsp;|&nbsp;&nbsp;{sender_phone}",
        s["meta"],
    ))
    story.append(Paragraph(
        f"Date: <b>{today}</b>&nbsp;&nbsp;&nbsp;&nbsp;"
        f"Reference: <b>{reference_number}</b>",
        s["meta"],
    ))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.15 * inch))

    # ---- Recipient address block ----
    addr_lines = [f"<b>{recipient.facility_name}</b>"]
    if recipient.facility_address:
        for line in recipient.facility_address.split(","):
            line = line.strip()
            if line:
                addr_lines.append(line)
    story.append(Paragraph("<br/>".join(addr_lines), s["address"]))
    story.append(Spacer(1, 0.1 * inch))

    # ---- RE: subject line ----
    re_parts = [f"RE: Patient Account — {recipient.patient_name}"]
    if recipient.account_number:
        re_parts.append(f"Account #: {recipient.account_number}")
    if recipient.date_of_service:
        re_parts.append(f"Date of Service: {recipient.date_of_service}")
    story.append(Paragraph(" | ".join(re_parts), s["subject"]))

    # ---- Body text (split on double-newlines into paragraphs) ----
    for para in letter.body.split("\n\n"):
        para = para.strip()
        if para:
            # Preserve single newlines within a paragraph as <br/>
            para = para.replace("\n", "<br/>")
            story.append(Paragraph(para, s["body"]))

    # ---- Resolution summary box ----
    if letter.requests_full_waiver:
        res_text = "REQUESTED: Full waiver of the billed amount under the facility's Financial Assistance Policy."
    elif letter.requested_amount is not None:
        res_text = f"REQUESTED: Reduction of account balance to ${letter.requested_amount:,.2f}."
    else:
        res_text = "REQUESTED: Written response addressing all points above."

    res_box = Table(
        [[Paragraph(res_text, ParagraphStyle(
            "res", fontName="Helvetica-Bold", fontSize=9,
            textColor=_ROBINHEALTH_RED, leading=13,
        ))]],
        colWidths=[6.6 * inch],
    )
    res_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _LIGHT_GREY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 1, _ROBINHEALTH_RED),
    ]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(res_box)
    story.append(Spacer(1, 0.25 * inch))

    # ---- Signature block ----
    story.append(Paragraph("Sincerely,", s["body"]))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(
        f"<b>{sender_name}</b><br/>"
        f"Authorized Representative for {recipient.patient_name}<br/>"
        f"{sender_email} | {sender_phone}",
        s["body"],
    ))

    # ---- Footer / disclaimer ----
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "RobinHealth is an AI-enabled patient advocacy service. This letter was prepared on behalf of "
        f"the patient named above under their written authorization (Reference: {reference_number}). "
        "RobinHealth is not a law firm and this letter does not constitute legal advice. "
        "If this account is in active legal proceedings, please contact the patient's legal representative.",
        s["disclaimer"],
    ))

    doc.build(story)
    return buf.getvalue()


def render_followup_letter(
    followup_context: dict,
    recipient: RecipientInfo,
    reference_number: str,
    round_number: int = 2,
) -> bytes:
    """
    Render a follow-up letter from the followup_letter_context dict
    produced by outcome_pipeline.generate_followup_action().

    These have a different structure than initial letters -- they're
    driven by key_points lists and legal_citations rather than
    LLM-drafted prose -- so they get their own simple renderer that
    produces a professional but more direct format.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.9 * inch,
    )
    s = _build_styles()
    story = []

    # Letterhead (same as initial letter)
    lh_data = [[
        Paragraph("RobinHealth", s["letterhead_name"]),
        Paragraph("Your AI-enabled health advocate", s["letterhead_tagline"]),
    ]]
    lh_table = Table(lh_data, colWidths=[3.5 * inch, 3.5 * inch])
    lh_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ROBINHEALTH_RED),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 16),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(lh_table)
    story.append(Spacer(1, 0.15 * inch))

    today = _date.today().strftime("%B %d, %Y")
    urgency = followup_context.get("urgency", "standard")
    urgency_label = " — URGENT" if urgency == "immediate" else (
        " — ESCALATED NOTICE" if urgency == "escalated" else ""
    )
    story.append(Paragraph(
        f"Date: <b>{today}</b>&nbsp;&nbsp;&nbsp;Reference: <b>{reference_number}</b>"
        f"&nbsp;&nbsp;&nbsp;Round: <b>{round_number}</b>{urgency_label}",
        s["meta"],
    ))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.15 * inch))

    # Recipient
    addr_lines = [f"<b>{recipient.facility_name}</b>"]
    if recipient.facility_address:
        for line in recipient.facility_address.split(","):
            line = line.strip()
            if line:
                addr_lines.append(line)
    story.append(Paragraph("<br/>".join(addr_lines), s["address"]))
    story.append(Spacer(1, 0.1 * inch))

    # Subject
    subject = followup_context.get("subject", "Follow-Up: Patient Financial Assistance")
    re_line = f"RE: {subject} | Patient: {recipient.patient_name}"
    if recipient.account_number:
        re_line += f" | Account #: {recipient.account_number}"
    story.append(Paragraph(re_line, s["subject"]))

    # Key points as numbered body paragraphs
    story.append(Paragraph("Dear Billing Department:", s["body"]))
    for i, point in enumerate(followup_context.get("key_points", []), 1):
        # Replace [DATE] placeholder with today
        point = point.replace("[DATE]", today)
        story.append(Paragraph(f"{i}. {point}", s["body"]))

    # Legal citations box (if present)
    citations = followup_context.get("legal_citations", [])
    if citations:
        story.append(Spacer(1, 0.1 * inch))
        cite_text = "<b>Applicable Law and Regulations:</b><br/>" + "<br/>".join(
            f"• {c}" for c in citations
        )
        cite_box = Table(
            [[Paragraph(cite_text, ParagraphStyle(
                "cite", fontName="Helvetica", fontSize=9,
                textColor=_DARK, leading=13,
            ))]],
            colWidths=[6.6 * inch],
        )
        cite_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _LIGHT_GREY),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CCCCCC")),
        ]))
        story.append(cite_box)

    # Documents checklist (for documentation-request letters)
    docs = followup_context.get("documents_checklist", [])
    if docs:
        story.append(Spacer(1, 0.1 * inch))
        docs_text = "<b>Documents Enclosed / To Be Submitted:</b><br/>" + "<br/>".join(
            f"☐ {d.replace('_', ' ').title()}" for d in docs
        )
        story.append(Paragraph(docs_text, s["body"]))

    # Closing
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(
        "We request a written response within <b>14 days</b> of the date of this letter.",
        s["body"],
    ))
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Sincerely,", s["body"]))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(
        f"<b>RobinHealth Patient Advocacy</b><br/>"
        f"Authorized Representative for {recipient.patient_name}<br/>"
        "advocacy@robinhealth.com | (888) ROB-INHL",
        s["body"],
    ))

    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "RobinHealth is an AI-enabled patient advocacy service acting under patient authorization. "
        "Reference: " + reference_number,
        s["disclaimer"],
    ))

    doc.build(story)
    return buf.getvalue()


# How long the insurer is given to respond. Internal appeals on ACA plans are
# generally decided within 30 days (pre-service) / 60 days (post-service); 30 is
# a reasonable, plan-agnostic ask for a post-service claim.
DEFAULT_APPEAL_RESPONSE_DEADLINE_DAYS = 30


def render_insurer_appeal_letter(
    patient_name: str,
    insurer_name: str,
    reference_number: str,
    insurer_address: str | None = None,
    member_id: str | None = None,
    claim_number: str | None = None,
    date_of_service: str | None = None,
    denial_reason: str | None = None,
    response_deadline_days: int = DEFAULT_APPEAL_RESPONSE_DEADLINE_DAYS,
    sender_name: str = "RobinHealth Patient Advocacy",
    sender_email: str = "advocacy@robinhealth.com",
    sender_phone: str = "(888) ROB-INHL",
) -> bytes:
    """
    Render a formal appeal addressed to the patient's *insurer* (not the
    provider), contesting a denied or mis-processed claim and asserting the
    member's internal-appeal / external-review rights.

    Distinct from the provider-directed letters above: the recipient is the
    insurer's appeals department, the leverage is the member's plan benefits
    and appeal rights (45 CFR 147.136 / ERISA), and the ask is to reprocess and
    pay the claim -- not to discount a balance. Fields we don't have (member
    id, claim number, date of service) render as bracketed placeholders the
    patient fills in before sending, rather than being omitted.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.5 * inch, bottomMargin=0.9 * inch,
    )
    s = _build_styles()
    story = []

    # Letterhead
    lh_data = [[
        Paragraph("RobinHealth", s["letterhead_name"]),
        Paragraph("Your AI-enabled health advocate", s["letterhead_tagline"]),
    ]]
    lh_table = Table(lh_data, colWidths=[3.5 * inch, 3.5 * inch])
    lh_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ROBINHEALTH_RED),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 16),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(lh_table)
    story.append(Spacer(1, 0.15 * inch))

    today = _date.today().strftime("%B %d, %Y")
    story.append(Paragraph(
        f"Date: <b>{today}</b>&nbsp;&nbsp;&nbsp;Reference: <b>{reference_number}</b>"
        f"&nbsp;&nbsp;&nbsp;<b>FORMAL APPEAL</b>",
        s["meta"],
    ))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.15 * inch))

    # Recipient (insurer appeals department)
    addr_lines = [f"<b>{insurer_name}</b>", "Appeals Department"]
    if insurer_address:
        for line in insurer_address.split(","):
            line = line.strip()
            if line:
                addr_lines.append(line)
    story.append(Paragraph("<br/>".join(addr_lines), s["address"]))
    story.append(Spacer(1, 0.1 * inch))

    # RE: line
    re_parts = [
        "RE: Appeal of Claim Determination",
        f"Patient: {patient_name}",
        f"Member ID: {member_id or '[Member ID]'}",
        f"Claim #: {claim_number or '[Claim number]'}",
    ]
    if date_of_service:
        re_parts.append(f"Date of Service: {date_of_service}")
    story.append(Paragraph(" | ".join(re_parts), s["subject"]))

    # Body
    story.append(Paragraph("Dear Appeals Department:", s["body"]))
    opening = (
        f"RobinHealth is writing as the authorized representative of {patient_name} to "
        f"formally appeal the determination on the claim referenced above"
    )
    opening += f", for which the stated reason was: {denial_reason}." if denial_reason else "."
    story.append(Paragraph(opening, s["body"]))
    story.append(Paragraph(
        "We respectfully request that you reconsider and reprocess this claim in accordance "
        "with the member's plan benefits, and issue a corrected Explanation of Benefits "
        "reflecting the correct allowed amount, plan payment, and member responsibility.",
        s["body"],
    ))

    # Appeal-rights citations box
    cite_text = (
        "<b>Applicable Appeal Rights:</b><br/>"
        "• 45 CFR §147.136 — the member is entitled to a full and fair internal appeal "
        "and, if the denial is upheld, an independent external review.<br/>"
        "• 29 CFR §2560.503-1 — for plans governed by ERISA, this establishes the member's "
        "appeal rights and the plan's response deadlines."
    )
    cite_box = Table(
        [[Paragraph(cite_text, ParagraphStyle(
            "cite", fontName="Helvetica", fontSize=9, textColor=_DARK, leading=13,
        ))]],
        colWidths=[6.6 * inch],
    )
    cite_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _LIGHT_GREY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CCCCCC")),
    ]))
    story.append(Spacer(1, 0.08 * inch))
    story.append(cite_box)

    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        f"Please provide a written determination of this appeal within "
        f"<b>{response_deadline_days} days</b>. RobinHealth is the member's authorized "
        f"representative for this appeal; please direct correspondence accordingly.",
        s["body"],
    ))

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Sincerely,", s["body"]))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(
        f"<b>{sender_name}</b><br/>"
        f"Authorized Representative for {patient_name}<br/>"
        f"{sender_email} | {sender_phone}",
        s["body"],
    ))

    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#DDDDDD")))
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "RobinHealth is an AI-enabled patient advocacy service acting under patient "
        "authorization, and is not a law firm. This letter does not constitute legal advice. "
        "Reference: " + reference_number,
        s["disclaimer"],
    ))

    doc.build(story)
    return buf.getvalue()
