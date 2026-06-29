"""
RobinHealth: case-strategy router + triage interview.

A medical bill isn't one kind of fight -- the winning playbook for a surprise
out-of-network ER bill (federal balance-billing protection, near-automatic) is
almost nothing like the playbook for an uninsured patient at a nonprofit
hospital (charity care + correcting the charges) or an insured patient whose
claim was denied (appeal the insurer, don't negotiate the provider).

This module does two things, both pure logic (no LLM, no DB) so they're
deterministic and unit-testable:

  1. triage_questions(facts): given what we already know, return the short,
     prioritized set of plain-language questions still worth asking. We only
     ask what changes the plan -- never a fixed interrogation.

  2. build_strategy(facts, ...): classify the case into an archetype and emit an
     ordered, concrete playbook (StrategyStep list) that reuses the capabilities
     the rest of the pipeline already has -- itemized-bill request, line-item
     dispute, charity-care application, insurer appeal, settlement. The first
     step is always "get the itemized bill" when we don't yet have line-item
     detail (P3: you can't find line-item errors without it).

The strategy is the connective tissue: it tells the patient (and Robin, in
chat) exactly what to do next and why, instead of leaving them with a number
and no plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Archetype identifiers (stable strings; surfaced to the front-end and chat).
ARCH_SURPRISE_OON = "surprise_out_of_network"
ARCH_EMERGENCY_UNINSURED = "emergency_uninsured"
ARCH_INSURED_DENIAL = "insured_claim_denied"
ARCH_CHARITY_CARE = "charity_care"
ARCH_INSURED_BALANCE = "insured_balance"
ARCH_SELF_PAY = "self_pay"
ARCH_GENERAL = "general"


@dataclass
class TriageFacts:
    """
    What we know (or have asked) about the situation. None means "not yet known"
    -- the triage interview fills these in, and build_strategy never asserts a
    fact that's still None.
    """
    coverage: str | None = None        # "insured" | "self_pay" | None
    emergency: bool | None = None       # care was emergency / ER
    out_of_network: bool | None = None  # surprise OON provider at an in-network facility
    claim_denied: bool | None = None    # insurer denied/declined the claim
    received_itemized: bool | None = None
    good_faith_estimate: bool | None = None  # self-pay only: got a GFE before service
    nonprofit: bool | None = None       # facility is a tax-exempt (501(r)) hospital
    is_hospital: bool | None = None
    in_collections: bool | None = None
    # Derived from the analysis, not the patient:
    has_line_items: bool = False        # the bill already has itemized codes
    likely_charity_eligible: bool | None = None  # from FAP/FPL synthesis


@dataclass
class TriageQuestion:
    prompt: str
    field_name: str                     # which TriageFacts field it fills
    kind: str = "yesno"                 # "yesno" | "choice"
    options: list[str] = field(default_factory=list)
    why: str = ""                       # short reason we're asking (builds trust)


@dataclass
class StrategyStep:
    key: str                            # stable id, e.g. "get_itemized"
    title: str                          # short, user-facing
    detail: str                         # what Robin does / what's needed
    leverage: list[str] = field(default_factory=list)       # the basis it relies on
    needs_from_user: list[str] = field(default_factory=list)  # for the evidence pack


@dataclass
class CaseStrategy:
    archetype: str
    headline: str                       # one-line plain summary of the plan
    steps: list[StrategyStep]
    missing_facts: list[TriageQuestion]  # questions still worth asking


# ============================================================
# Triage interview
# ============================================================

def triage_questions(facts: TriageFacts, *, limit: int = 4) -> list[TriageQuestion]:
    """
    Return the prioritized questions still worth asking, capped at `limit` so we
    never overwhelm a stressed patient. Each question is gated so it's only asked
    when it's relevant and not already answered.
    """
    q: list[TriageQuestion] = []

    if facts.coverage is None:
        q.append(TriageQuestion(
            prompt="Do you have health insurance that was (or should have been) billed for this, or are you paying on your own?",
            field_name="coverage", kind="choice",
            options=["I have insurance", "I'm paying on my own (self-pay/uninsured)"],
            why="It decides whether we fight the provider or appeal your insurer.",
        ))

    if facts.emergency is None:
        q.append(TriageQuestion(
            prompt="Was this emergency or urgent care (an ER visit, or care you couldn't reasonably delay)?",
            field_name="emergency",
            why="Emergency care has strong federal billing protections.",
        ))

    # Surprise-OON only matters when there's insurance.
    if facts.out_of_network is None and facts.coverage != "self_pay":
        q.append(TriageQuestion(
            prompt="Were you at an in-network hospital or facility, but later billed by a doctor (like an anesthesiologist, radiologist, or ER physician) who turned out to be out-of-network?",
            field_name="out_of_network",
            why="That's a 'surprise bill' the No Surprises Act usually limits to your in-network share.",
        ))

    if facts.coverage == "insured" and facts.claim_denied is None:
        q.append(TriageQuestion(
            prompt="Did your insurer deny this claim, or process it and leave you a balance?",
            field_name="claim_denied", kind="choice",
            options=["They denied it", "They paid part and left me a balance", "I'm not sure"],
            why="A denial means we appeal the insurer; a balance means we check the charges.",
        ))

    if facts.received_itemized is None:
        q.append(TriageQuestion(
            prompt="Do you have a fully itemized bill (one that lists every charge with its billing codes), or just a summary/balance-due statement?",
            field_name="received_itemized", kind="choice",
            options=["I have the itemized version", "Just a summary", "Not sure"],
            why="An itemized bill is where most billing errors hide -- it's our first move.",
        ))

    if facts.coverage == "self_pay" and facts.good_faith_estimate is None:
        q.append(TriageQuestion(
            prompt="Before your care, did you get a written 'Good Faith Estimate' of the cost?",
            field_name="good_faith_estimate",
            why="If the bill is $400+ over the estimate, you can dispute it federally.",
        ))

    if facts.in_collections is None:
        q.append(TriageQuestion(
            prompt="Has this bill been sent to collections or shown up on your credit report?",
            field_name="in_collections",
            why="If so, we move faster and add specific protections to the letter.",
        ))

    return q[:limit]


# ============================================================
# Archetype classification
# ============================================================

def classify_archetype(facts: TriageFacts) -> str:
    """
    Pick the single most-actionable archetype. Order matters: the earlier checks
    are the higher-leverage, more-specific situations.
    """
    # Surprise out-of-network at an in-network facility -- the strongest leverage
    # that exists (NSA caps it at in-network cost-sharing). Requires coverage.
    if facts.out_of_network is True and facts.coverage != "self_pay":
        return ARCH_SURPRISE_OON

    # Insurer denied the claim -> appeal the insurer, not the provider.
    if facts.coverage == "insured" and facts.claim_denied is True:
        return ARCH_INSURED_DENIAL

    # Emergency care while uninsured -> charity care + correct the charges.
    if facts.emergency is True and facts.coverage == "self_pay":
        return ARCH_EMERGENCY_UNINSURED

    # Strong charity-care signal (income qualifies, or a nonprofit hospital with
    # an unknown income picture worth pursuing).
    if facts.likely_charity_eligible is True:
        return ARCH_CHARITY_CARE

    # Self-pay / uninsured, non-emergency.
    if facts.coverage == "self_pay":
        return ARCH_SELF_PAY

    # Insured, processed, left with a balance (no denial).
    if facts.coverage == "insured":
        return ARCH_INSURED_BALANCE

    return ARCH_GENERAL


# ============================================================
# Step builders
# ============================================================

def _get_itemized_step() -> StrategyStep:
    return StrategyStep(
        key="get_itemized",
        title="Get the itemized bill",
        detail=(
            "Your bill looks like a summary. We request a fully itemized bill "
            "(every charge with its CPT/HCPCS/revenue codes) and ask that "
            "collection activity pause while it's produced. This is the "
            "foundation -- most billing errors only show up once charges are "
            "itemized."
        ),
        leverage=["Itemized-bill request"],
        needs_from_user=["the bill you already received"],
    )


def _review_step() -> StrategyStep:
    return StrategyStep(
        key="review_errors",
        title="Check every line for errors",
        detail=(
            "We scan the itemized charges for duplicates, unbundled codes "
            "(billed separately when one code already includes them), and "
            "quantities above the allowed daily maximum -- the specific, "
            "factual errors a billing department has to correct."
        ),
        leverage=["Line-item audit", "Medicare benchmark"],
    )


def _dispute_letter_step(leverage: list[str]) -> StrategyStep:
    return StrategyStep(
        key="dispute_letter",
        title="Send the dispute letter",
        detail=(
            "Robin drafts a firm, professional letter to the billing department "
            "laying out each error and request, and you review and approve it "
            "before anything is sent."
        ),
        leverage=leverage,
    )


def _charity_step() -> StrategyStep:
    return StrategyStep(
        key="charity_application",
        title="Apply for financial assistance (charity care)",
        detail=(
            "Many patients qualify for free or steeply reduced care without "
            "realizing it. We check the hospital's policy against your household "
            "income and help you apply."
        ),
        leverage=["Hospital Financial Assistance Policy", "501(r) charity care"],
        needs_from_user=["rough household income", "household size", "proof of income (pay stub or tax return)"],
    )


def _insurer_appeal_step() -> StrategyStep:
    return StrategyStep(
        key="insurer_appeal",
        title="Appeal the denial with your insurer",
        detail=(
            "We file a formal internal appeal asserting your plan benefits and "
            "appeal rights; if it's upheld, you're entitled to an independent "
            "external review."
        ),
        leverage=["Plan benefits", "45 CFR 147.136 internal appeal & external review"],
        needs_from_user=["your EOB / denial letter", "member ID and claim number"],
    )


def _settlement_step() -> StrategyStep:
    return StrategyStep(
        key="settlement",
        title="Negotiate the remaining balance",
        detail=(
            "For whatever's left, we negotiate -- a one-time lump-sum settlement "
            "(providers often accept a fraction) or an interest-free payment plan "
            "that fits your budget."
        ),
        leverage=["Cash-price / settlement"],
    )


def _nsa_step(emergency: bool | None) -> StrategyStep:
    basis = "emergency services" if emergency else "care at an in-network facility"
    return StrategyStep(
        key="nsa_dispute",
        title="Invoke your No Surprises Act protection",
        detail=(
            f"For {basis}, federal law generally limits you to your in-network "
            "cost-sharing. We notify both the provider and your insurer to "
            "reprocess the balance at the in-network rate and remove the surprise "
            "charges."
        ),
        leverage=["No Surprises Act (45 CFR Part 149)"],
        needs_from_user=["your EOB", "anything you signed at check-in"],
    )


def _escalation_step() -> StrategyStep:
    return StrategyStep(
        key="escalate",
        title="Escalate if they don't fix it",
        detail=(
            "If the provider or insurer doesn't resolve it, we escalate -- the "
            "CMS No Surprises Help Desk, your state insurance regulator, or your "
            "state attorney general, as appropriate."
        ),
        leverage=["Regulatory escalation"],
    )


# ============================================================
# Strategy assembly
# ============================================================

_HEADLINES = {
    ARCH_SURPRISE_OON: "This looks like a surprise out-of-network bill -- federal law likely caps what you owe at your in-network share.",
    ARCH_INSURED_DENIAL: "Your insurer denied this claim, so the move is to appeal them (internal review, then external) rather than negotiate the provider.",
    ARCH_EMERGENCY_UNINSURED: "For emergency care while uninsured, your strongest paths are charity care and correcting the charges.",
    ARCH_CHARITY_CARE: "You likely qualify for the hospital's financial assistance -- that can wipe out or sharply cut this bill.",
    ARCH_SELF_PAY: "As a self-pay patient, we get the charges corrected and negotiated down to a fair cash price.",
    ARCH_INSURED_BALANCE: "Insurance processed this, but the balance looks high -- we anchor to what your plan allows and check the charges.",
    ARCH_GENERAL: "Here's the plan to push back on this bill.",
}


def build_strategy(
    facts: TriageFacts,
    *,
    has_dollar_findings: bool = False,
) -> CaseStrategy:
    """
    Classify the archetype and assemble the ordered playbook.

    `has_dollar_findings` indicates the line-item audit already found specific
    overcharges; when True the "check every line" step is framed as done/active
    rather than speculative. The itemized-bill step leads whenever we don't yet
    have line-item detail (facts.has_line_items is False).
    """
    archetype = classify_archetype(facts)
    steps: list[StrategyStep] = []

    # P3: itemized bill is the precondition for everything else.
    if not facts.has_line_items:
        steps.append(_get_itemized_step())

    if archetype == ARCH_SURPRISE_OON:
        steps.append(_nsa_step(facts.emergency))
        steps.append(_review_step())
        steps.append(_dispute_letter_step(["No Surprises Act (45 CFR Part 149)", "Line-item audit"]))
        steps.append(_escalation_step())

    elif archetype == ARCH_INSURED_DENIAL:
        steps.append(_insurer_appeal_step())
        steps.append(_escalation_step())
        steps.append(_settlement_step())

    elif archetype == ARCH_EMERGENCY_UNINSURED:
        steps.append(_charity_step())
        steps.append(_review_step())
        steps.append(_dispute_letter_step(["Line-item audit", "Medicare benchmark", "501(r) amount generally billed"]))
        steps.append(_settlement_step())

    elif archetype == ARCH_CHARITY_CARE:
        steps.append(_charity_step())
        steps.append(_dispute_letter_step(["Hospital Financial Assistance Policy", "501(r) charity care"]))
        steps.append(_settlement_step())

    elif archetype == ARCH_SELF_PAY:
        steps.append(_review_step())
        steps.append(_dispute_letter_step(["Line-item audit", "Medicare benchmark", "Hospital price transparency"]))
        steps.append(_charity_step())
        steps.append(_settlement_step())

    elif archetype == ARCH_INSURED_BALANCE:
        steps.append(_review_step())
        steps.append(_dispute_letter_step(["Line-item audit", "Insurer allowed-amount anchor"]))
        steps.append(_settlement_step())

    else:  # ARCH_GENERAL
        steps.append(_review_step())
        steps.append(_dispute_letter_step(["Line-item audit", "Medicare benchmark"]))
        steps.append(_settlement_step())

    return CaseStrategy(
        archetype=archetype,
        headline=_HEADLINES[archetype],
        steps=steps,
        missing_facts=triage_questions(facts),
    )
