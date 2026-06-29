"""
RobinHealth: phone-call script generator.

Most medical bills are actually resolved on the phone, not by mail -- but a
stressed patient on hold with a billing department rarely knows what to say,
what to ask for, or (just as important) what NOT to agree to. This module turns
the case analysis into a concrete, read-it-aloud call script tailored to the
situation: the opening, the specific points to raise (including the exact
line-item errors we found), what to get in writing, the things never to do under
pressure, and how to escalate.

Pure logic (no LLM, no DB), built from the case archetype (case_strategy) and the
line-item findings (line_item_audit), so it's deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from case_strategy import (
    ARCH_CHARITY_CARE,
    ARCH_EMERGENCY_UNINSURED,
    ARCH_INSURED_BALANCE,
    ARCH_INSURED_DENIAL,
    ARCH_SELF_PAY,
    ARCH_SURPRISE_OON,
)


@dataclass
class ScriptSection:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class PhoneScript:
    headline: str                 # one-line "what this call is for"
    who_to_ask_for: str           # the right department/person to request
    sections: list[ScriptSection]


# What the patient should ask to be connected to, by archetype.
_WHO = {
    ARCH_SURPRISE_OON: "the billing department, and ask for a supervisor familiar with No Surprises Act claims",
    ARCH_INSURED_DENIAL: "your insurance company's appeals/member-services line (not the provider, for this call)",
    ARCH_EMERGENCY_UNINSURED: "the financial assistance / charity care office (sometimes called 'patient financial services')",
    ARCH_CHARITY_CARE: "the financial assistance / charity care office",
    ARCH_SELF_PAY: "the billing department, and ask for a financial counselor",
    ARCH_INSURED_BALANCE: "the billing department, and ask for a financial counselor",
}

_HEADLINES = {
    ARCH_SURPRISE_OON: "Call to invoke your surprise-billing protection and get the balance reprocessed.",
    ARCH_INSURED_DENIAL: "Call your insurer to start an appeal of the denied claim.",
    ARCH_EMERGENCY_UNINSURED: "Call to request financial assistance and an itemized, corrected bill.",
    ARCH_CHARITY_CARE: "Call to apply for financial assistance (charity care).",
    ARCH_SELF_PAY: "Call to get an itemized bill, correct the errors, and negotiate a fair price.",
    ARCH_INSURED_BALANCE: "Call to verify the charges against your EOB and correct any errors.",
}


def _opening_section(facility_name: str | None, account_number: str | None) -> ScriptSection:
    acct = f", account number {account_number}" if account_number else ""
    where = f" at {facility_name}" if facility_name else ""
    return ScriptSection(
        "How to open",
        [
            f"\"Hi, my name is [your name]. I'm calling about my bill{where}{acct}.\"",
            "\"I have some questions about the charges and I'd like to get a few things sorted out.\"",
            "Stay calm and friendly -- the person on the phone didn't create the bill, and a "
            "cooperative tone gets you further than an angry one.",
        ],
    )


def _ask_section(archetype: str) -> ScriptSection:
    if archetype == ARCH_SURPRISE_OON:
        return ScriptSection("What to say (your main ask)", [
            "\"I was treated at an in-network facility, but I'm being billed by an out-of-network "
            "provider. Under the federal No Surprises Act, I should only owe my in-network "
            "cost-sharing.\"",
            "\"Please reprocess this so I'm only responsible for the in-network amount, and confirm "
            "you're not balance-billing me for the difference.\"",
            "\"I did not sign a notice-and-consent form waiving those protections. If you believe I "
            "did, please send me a copy.\"",
        ])
    if archetype == ARCH_INSURED_DENIAL:
        return ScriptSection("What to say (your main ask)", [
            "\"My claim was denied and I want to file a formal appeal. Can you tell me the exact "
            "reason for the denial and the deadline to appeal?\"",
            "\"Please send me the denial in writing, the specific plan language it's based on, and "
            "the instructions for both internal appeal and external review.\"",
        ])
    if archetype in (ARCH_EMERGENCY_UNINSURED, ARCH_CHARITY_CARE):
        return ScriptSection("What to say (your main ask)", [
            "\"I'd like to apply for your financial assistance / charity care program. Can you tell "
            "me how to apply and what income documents you need?\"",
            "\"Please put my account on hold so it isn't sent to collections while my application is "
            "being reviewed.\"",
            "\"I'd also like a fully itemized bill with all the billing codes.\"",
        ])
    # SELF_PAY / INSURED_BALANCE / general
    return ScriptSection("What to say (your main ask)", [
        "\"I'd like a fully itemized bill that lists every charge with its billing code -- I only "
        "have a summary right now.\"",
        "\"I want to review the charges before paying, and I'd like to talk about a fair self-pay "
        "price.\"",
    ])


def _findings_section(findings: list) -> ScriptSection | None:
    """Turn the specific line-item errors into spoken talking points."""
    if not findings:
        return None
    lines = [
        "Raise these specific items we found (read the line numbers off your itemized bill):",
    ]
    for f in findings[:5]:
        nums = ", ".join(str(n) for n in (getattr(f, "line_numbers", []) or []))
        loc = f" (line {nums})" if nums else ""
        kind = getattr(f, "kind", "")
        if kind == "duplicate":
            lines.append(f"\"It looks like there's a duplicate charge{loc} -- can you confirm this "
                         "service was actually done more than once, or remove the extra charge?\"")
        elif kind == "ncci_ptp":
            lines.append(f"\"I think these charges{loc} are being billed separately when they should "
                         "be bundled under one code. Can you review the coding?\"")
        elif kind == "mue":
            lines.append(f"\"The number of units billed{loc} looks higher than the daily maximum for "
                         "that code. Can you check the quantity?\"")
        else:
            lines.append(f"\"Can you explain this charge{loc}? It doesn't look right to me.\"")
    return ScriptSection("The specific charges to question", lines)


def _in_writing_section() -> ScriptSection:
    return ScriptSection("Get it in writing", [
        "\"Can you send me that in writing (email or mail) so I have a record?\"",
        "Write down the date, the representative's name, and a reference or call number for the call.",
        "Ask: \"What's your name, and can I get a reference number for this call?\"",
    ])


def _never_do_section() -> ScriptSection:
    return ScriptSection("What NOT to do", [
        "Don't pay the full balance or give a card number just because you're put on the spot -- "
        "you can always pay later once it's corrected.",
        "Don't agree that the full amount is correct or 'admit' you owe it before the charges are "
        "reviewed.",
        "Don't agree to a payment plan amount you can't actually afford -- you can ask for a smaller "
        "monthly amount or an interest-free plan.",
        "If they pressure you, it's completely fine to say: \"I need to review this and I'll call back.\"",
    ])


def _escalate_section() -> ScriptSection:
    return ScriptSection("If you get stuck", [
        "\"Can I speak with a supervisor or a financial counselor / patient advocate?\"",
        "If they won't help, that's okay -- Robin can send a formal written letter that puts the "
        "request (and your rights) on the record.",
    ])


def build_phone_script(
    archetype: str,
    *,
    facility_name: str | None = None,
    account_number: str | None = None,
    findings: list | None = None,
) -> PhoneScript:
    """
    Assemble a tailored call script. `findings` are line_item_audit findings
    (optional); when present, the specific errors become spoken talking points.
    """
    sections: list[ScriptSection] = [
        _opening_section(facility_name, account_number),
        _ask_section(archetype),
    ]
    fsec = _findings_section(findings or [])
    if fsec:
        sections.append(fsec)
    sections.append(_in_writing_section())
    sections.append(_never_do_section())
    sections.append(_escalate_section())

    return PhoneScript(
        headline=_HEADLINES.get(archetype, "Call to question the charges and ask for an itemized bill."),
        who_to_ask_for=_WHO.get(archetype, "the billing department"),
        sections=sections,
    )
