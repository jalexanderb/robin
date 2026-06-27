"""
RobinHealth: statutory leverage arguments for negotiation/dispute letters.

Turns a small set of patient-provided facts (was it emergency care? an
out-of-network "surprise" bill? did they get a Good Faith Estimate? an
itemized bill?) plus what we already know about the bill into concrete,
citation-backed arguments a provider's billing department has to take
seriously. These are layered on top of the data-driven arguments synthesis.py
already produces (FAP eligibility, 501(r) compliance gaps, pricing benchmarks,
EOB allowed-amounts).

Every argument is framed as a lawful request/observation, never a threat, and
the citations are real federal authorities:
  - No Surprises Act -- 45 C.F.R. Part 149 (balance-billing protections;
    Good Faith Estimate at  149.610)
  - Hospital price transparency -- 45 C.F.R. Part 180
  - Tax-exempt hospital billing limits -- 26 U.S.C. 501(r); 26 C.F.R.
    1.501(r)-5 (amounts generally billed) and 1.501(r)-6 (collections)

This module is pure logic (no LLM, no DB) so it is unit-testable and
deterministic; the drafting LLM is told to preserve these citations verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LeverageArgument:
    basis: str   # short label, e.g. "No Surprises Act — emergency care"
    text: str    # provider-facing paragraph, citation-backed


def build_leverage_arguments(
    *,
    is_hospital: bool = True,
    emergency: bool | None = None,
    out_of_network: bool | None = None,
    received_itemized: bool | None = None,
    self_pay: bool | None = None,
    good_faith_estimate: bool | None = None,
    nonprofit: bool | None = None,
    prices_published: bool | None = None,
) -> list[LeverageArgument]:
    """
    Build the applicable statutory arguments. Unknown facts (None) are
    skipped rather than asserted, so the letter never claims something the
    patient didn't tell us. `True`/`False` drive the fact-specific arguments.
    """
    args: list[LeverageArgument] = []

    if emergency:
        args.append(LeverageArgument(
            "No Surprises Act — emergency care",
            "Federal law (the No Surprises Act, 45 C.F.R. Part 149) prohibits "
            "balance billing for emergency services. The patient's financial "
            "responsibility is limited to the in-network cost-sharing amount, "
            "regardless of the provider's or facility's network status. We "
            "request that this account be reprocessed so any amount above the "
            "permitted in-network cost-sharing is removed.",
        ))

    if out_of_network:
        args.append(LeverageArgument(
            "No Surprises Act — surprise out-of-network billing",
            "Under the No Surprises Act, a patient treated at an in-network "
            "facility may not be balance-billed by an out-of-network provider "
            "unless the patient gave informed written consent in advance on "
            "the CMS-required notice-and-consent form. Please provide a copy "
            "of any signed notice; absent compliant notice and consent, the "
            "out-of-network balance must be removed and the claim reprocessed "
            "at in-network cost-sharing.",
        ))

    # Good Faith Estimate only applies to self-pay/uninsured patients.
    if self_pay and good_faith_estimate is False:
        args.append(LeverageArgument(
            "No Surprises Act — Good Faith Estimate",
            "As a self-pay/uninsured patient, the patient was entitled under "
            "45 C.F.R. § 149.610 to a Good Faith Estimate of expected charges "
            "before service, and we have no record that one was provided. "
            "Where a bill exceeds the Good Faith Estimate by $400 or more, the "
            "patient may invoke the federal patient-provider dispute "
            "resolution process. We request the charges be revised to a "
            "reasonable good-faith amount for these services.",
        ))

    if received_itemized is False:
        args.append(LeverageArgument(
            "Itemized bill",
            "We formally request a fully itemized statement listing every "
            "charge with its CPT/HCPCS/revenue code, units, and price. "
            "Collection activity on this account should be paused while the "
            "itemized bill is produced and reviewed, as itemization commonly "
            "reveals duplicate charges, services not rendered, and coding "
            "errors.",
        ))

    if is_hospital:
        if prices_published is False:
            args.append(LeverageArgument(
                "Hospital price transparency",
                "Federal hospital price-transparency rules (45 C.F.R. Part "
                "180) require hospitals to publish their standard charges — "
                "including payer-specific negotiated rates and the discounted "
                "cash price — for these services, and we were unable to locate "
                "a compliant published file. We request the applicable "
                "standard charges for the billed codes and that the account be "
                "adjusted to reflect them.",
            ))
        else:
            args.append(LeverageArgument(
                "Hospital price transparency",
                "Under federal hospital price-transparency rules (45 C.F.R. "
                "Part 180), please provide the published standard charges — "
                "including the payer-specific negotiated rates and the "
                "discounted cash price — for the billed codes, so the charges "
                "can be compared against the facility's own published prices.",
            ))

    if nonprofit:
        args.append(LeverageArgument(
            "501(r) charity care and billing limits",
            "As a tax-exempt hospital, the facility must maintain a Financial "
            "Assistance Policy and may not bill a patient eligible for "
            "assistance more than the amounts generally billed to insured "
            "patients (26 U.S.C. § 501(r); 26 C.F.R. § 1.501(r)-5), and must "
            "make reasonable efforts to determine financial-assistance "
            "eligibility before any extraordinary collection action "
            "(26 C.F.R. § 1.501(r)-6). We request a financial-assistance "
            "determination and that the balance be capped at the amount "
            "generally billed.",
        ))

    return args
