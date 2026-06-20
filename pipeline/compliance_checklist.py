"""
RobinHealth: 501(r) Financial Assistance Policy compliance checklist.

This is the canonical list of requirement codes used by the FAP parsing
pipeline (fap_pipeline.py, Pass B) and stored in fap_compliance_findings.

Each entry defines:
  - code: stable identifier, stored in fap_compliance_findings.requirement_code
  - description: what 501(r) requires, in plain English (internal/dev use)
  - default_severity: 'material' or 'procedural'
  - argument_template: phrasing used when this finding is 'absent' or
    'contradicted'. Kept conservative/verifiable -- e.g. "no X could be
    located at the URL provided", not "this hospital has no X".
  - user_facing_summary: plain-language translation for the synthesis layer.
    Never exposes requirement_code, severity, or legal jargon to the user.
"""

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    MATERIAL = "material"
    PROCEDURAL = "procedural"


class ComplianceStatus(str, Enum):
    PRESENT = "present"
    VAGUE = "vague"
    ABSENT = "absent"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True)
class ChecklistItem:
    code: str
    description: str
    default_severity: Severity
    argument_template: str
    user_facing_summary: str


CHECKLIST: list[ChecklistItem] = [
    ChecklistItem(
        code="fap_document_exists",
        description=(
            "A nonprofit hospital must maintain a written Financial "
            "Assistance Policy (FAP)."
        ),
        default_severity=Severity.MATERIAL,
        argument_template=(
            "No Financial Assistance Policy could be located at the URL "
            "provided for this facility. Under Section 501(r) of the "
            "Internal Revenue Code, nonprofit hospitals are required to "
            "maintain and publish such a policy."
        ),
        user_facing_summary=(
            "We couldn't find a financial assistance policy for this "
            "hospital, even though nonprofit hospitals are required to "
            "have one."
        ),
    ),
    ChecklistItem(
        code="eligibility_criteria_specified",
        description=(
            "The FAP must specify eligibility criteria with enough "
            "precision (e.g. income thresholds relative to the Federal "
            "Poverty Level) that an individual can determine whether they "
            "qualify."
        ),
        default_severity=Severity.MATERIAL,
        argument_template=(
            "The Financial Assistance Policy does not specify income "
            "thresholds or other criteria with enough precision for a "
            "patient to determine eligibility, which appears inconsistent "
            "with the specificity required under Section 501(r)."
        ),
        user_facing_summary=(
            "This hospital's financial assistance rules are too vague to "
            "tell if you qualify -- which itself may be a problem with "
            "their policy."
        ),
    ),
    ChecklistItem(
        code="agb_methodology_disclosed",
        description=(
            "The FAP must disclose the methodology used to calculate "
            "'Amounts Generally Billed' (AGB) -- the cap on what FAP-"
            "eligible patients can be charged."
        ),
        default_severity=Severity.MATERIAL,
        argument_template=(
            "The Financial Assistance Policy does not disclose the "
            "'Amounts Generally Billed' methodology required under Section "
            "501(r). If the patient is FAP-eligible, charges above AGB may "
            "not be permissible."
        ),
        user_facing_summary=(
            "If you qualify for financial assistance, this hospital can't "
            "charge you more than insured patients typically pay -- but "
            "their policy doesn't explain how that cap is calculated."
        ),
    ),
    ChecklistItem(
        code="application_period_defined",
        description=(
            "The FAP must define an application period (deadline for "
            "applying for financial assistance, generally at least 240 "
            "days from the first post-discharge billing statement)."
        ),
        default_severity=Severity.PROCEDURAL,
        argument_template=(
            "The Financial Assistance Policy does not clearly define the "
            "application period during which a patient may apply for "
            "financial assistance, as required under Section 501(r)."
        ),
        user_facing_summary=(
            "This hospital's policy doesn't clearly say how long you have "
            "to apply for financial assistance."
        ),
    ),
    ChecklistItem(
        code="plain_language_summary_exists",
        description=(
            "A separate plain-language summary (PLS) of the FAP must "
            "exist and must be substantively simpler than the full policy."
        ),
        default_severity=Severity.PROCEDURAL,
        argument_template=(
            "No distinct plain-language summary of the Financial "
            "Assistance Policy could be located, or the document provided "
            "as a summary does not appear to differ substantively from the "
            "full policy, as required under Section 501(r)."
        ),
        user_facing_summary=(
            "Hospitals are required to provide a simple summary of their "
            "financial assistance rules -- this one either doesn't exist "
            "or isn't actually simpler than the full policy."
        ),
    ),
    ChecklistItem(
        code="widely_publicized",
        description=(
            "The hospital must take measures to widely publicize the FAP, "
            "per the notification methods it specifies in its own policy."
        ),
        default_severity=Severity.MATERIAL,
        argument_template=(
            "The Financial Assistance Policy specifies notification "
            "methods (e.g. notice on billing statements, signage in "
            "admissions and emergency areas) that do not appear to have "
            "been followed in this patient's case, based on the documents "
            "provided."
        ),
        user_facing_summary=(
            "This hospital is supposed to actively tell patients about "
            "financial assistance -- it doesn't look like that happened "
            "here."
        ),
    ),
    ChecklistItem(
        code="presumptive_eligibility_specified",
        description=(
            "If the FAP claims to use presumptive eligibility (e.g. "
            "automatic qualification based on enrollment in other "
            "assistance programs), the criteria must be specified."
        ),
        default_severity=Severity.PROCEDURAL,
        argument_template=(
            "The Financial Assistance Policy references presumptive "
            "eligibility but does not specify the criteria used, making it "
            "unclear whether the patient automatically qualifies based on "
            "enrollment in other assistance programs."
        ),
        user_facing_summary=(
            "If you're enrolled in programs like Medicaid or SNAP, you "
            "might automatically qualify for help here -- but the policy "
            "doesn't spell out the rules clearly."
        ),
    ),
    ChecklistItem(
        code="billing_collections_policy_consistent",
        description=(
            "A separate billing and collections policy must exist and "
            "must be consistent with the FAP -- in particular, "
            "extraordinary collection actions (ECAs) cannot begin during "
            "the FAP application period."
        ),
        default_severity=Severity.MATERIAL,
        argument_template=(
            "Based on the documents provided, collection activity on this "
            "account appears to have occurred during the period in which "
            "the patient could still have applied for financial "
            "assistance, which Section 501(r) generally prohibits."
        ),
        user_facing_summary=(
            "It looks like this hospital may have started collections on "
            "this bill before the deadline to apply for financial "
            "assistance had even passed -- which isn't allowed."
        ),
    ),
]


CHECKLIST_BY_CODE: dict[str, ChecklistItem] = {item.code: item for item in CHECKLIST}
