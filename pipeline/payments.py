"""
RobinHealth: payments (Stripe).

Collects the two fee options the product offers:

  membership   -- a $50/month subscription (Stripe Subscription via a Checkout
                  Session in subscription mode, against STRIPE_MEMBERSHIP_PRICE_ID).
  contingency  -- a one-time charge equal to 20% of the documented savings,
                  capped at $1,000 (Checkout Session in payment mode for the
                  computed amount). The amount is computed by
                  outcome_pipeline.compute_robinhealth_fee, not here.

Design mirrors storage.py's S3 backend: the `stripe` SDK is imported lazily, and
everything no-ops/raises a clear PaymentsNotConfigured when STRIPE_SECRET_KEY
isn't set -- so dev, tests, and the local path need no Stripe dependency or
account. Card data never touches our servers: we use Stripe-hosted Checkout, so
we stay out of PCI scope.

State lives in the DB (repository): the patient's stripe_customer_id +
membership subscription id/status, and a `payments` row per one-time charge.
Webhooks (verified with STRIPE_WEBHOOK_SECRET) are the source of truth for when
money actually moved -- we never mark something paid from the redirect alone.

Environment:
    STRIPE_SECRET_KEY            sk_live_... / sk_test_...   (enables payments)
    STRIPE_WEBHOOK_SECRET        whsec_...                   (verifies webhooks)
    STRIPE_MEMBERSHIP_PRICE_ID   price_... for the $50/mo recurring price
    STRIPE_SUCCESS_URL           where Checkout returns on success
    STRIPE_CANCEL_URL            where Checkout returns on cancel
"""
from __future__ import annotations

import os

import repository


class PaymentsNotConfigured(RuntimeError):
    """Raised when a payment action is attempted but STRIPE_SECRET_KEY is unset."""


def is_enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _stripe():
    """Lazy-import and configure the Stripe SDK. Patched in tests."""
    if not is_enabled():
        raise PaymentsNotConfigured(
            "Stripe is not configured (set STRIPE_SECRET_KEY to enable payments)."
        )
    import stripe  # lazy: only needed when payments are actually used
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe


def _success_url() -> str:
    return os.environ.get("STRIPE_SUCCESS_URL", "https://robinhealth.com/billing/success")


def _cancel_url() -> str:
    return os.environ.get("STRIPE_CANCEL_URL", "https://robinhealth.com/billing/cancel")


# ============================================================
# Customers
# ============================================================

def ensure_customer(patient_id: str, email: str | None = None) -> str:
    """
    Return the patient's Stripe customer id, creating (and persisting) one on
    first use. Idempotent.
    """
    existing = repository.fetch_stripe_customer_id(patient_id)
    if existing:
        return existing
    customer = _stripe().Customer.create(
        email=email or None,
        metadata={"patient_id": patient_id},
    )
    repository.set_stripe_customer_id(patient_id, customer["id"])
    return customer["id"]


# ============================================================
# Checkout sessions
# ============================================================

def create_membership_checkout(
    patient_id: str, email: str | None = None,
    success_url: str | None = None, cancel_url: str | None = None,
) -> dict:
    """
    Start the $50/month membership subscription. Returns {checkout_url, session_id}.
    The subscription isn't active until the webhook confirms it.
    """
    price_id = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID")
    if not price_id:
        raise PaymentsNotConfigured("STRIPE_MEMBERSHIP_PRICE_ID is not set.")
    customer_id = ensure_customer(patient_id, email)
    session = _stripe().checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url or _success_url(),
        cancel_url=cancel_url or _cancel_url(),
        client_reference_id=patient_id,
        metadata={"patient_id": patient_id, "kind": "membership"},
    )
    return {"checkout_url": session["url"], "session_id": session["id"]}


def create_contingency_checkout(
    patient_id: str, case_id: str, amount_usd: float, email: str | None = None,
    success_url: str | None = None, cancel_url: str | None = None,
) -> dict:
    """
    Start a one-time contingency-fee charge (20% of savings, already computed and
    capped by the caller). Records a pending `payments` row and returns
    {checkout_url, session_id}. Marked paid only when the webhook confirms.
    """
    if amount_usd is None or amount_usd <= 0:
        raise ValueError("contingency amount must be positive")
    amount_cents = int(round(amount_usd * 100))
    customer_id = ensure_customer(patient_id, email)
    session = _stripe().checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": amount_cents,
                "product_data": {"name": "RobinHealth fee — 20% of your savings"},
            },
            "quantity": 1,
        }],
        success_url=success_url or _success_url(),
        cancel_url=cancel_url or _cancel_url(),
        client_reference_id=patient_id,
        metadata={"patient_id": patient_id, "case_id": case_id, "kind": "contingency"},
    )
    repository.record_payment(
        patient_id=patient_id, case_id=case_id, kind="contingency",
        amount_cents=amount_cents, stripe_session_id=session["id"], status="pending",
    )
    return {"checkout_url": session["url"], "session_id": session["id"]}


# ============================================================
# Webhooks (source of truth for money actually moving)
# ============================================================

def handle_webhook(payload: bytes, sig_header: str | None) -> dict:
    """
    Verify and process a Stripe webhook. Returns a small summary dict. Raises
    ValueError on a bad signature (the caller should return 400).
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise PaymentsNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:  # signature/parse failure -> 400
        raise ValueError(f"invalid webhook signature: {exc}") from exc

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        kind = (obj.get("metadata") or {}).get("kind")
        patient_id = (obj.get("metadata") or {}).get("patient_id") or obj.get("client_reference_id")
        if kind == "membership" and patient_id:
            repository.set_membership_subscription(
                patient_id, obj.get("subscription"), "active",
            )
        elif kind == "contingency" and obj.get("id"):
            repository.mark_payment_status_by_session(obj["id"], "paid")
        return {"handled": etype, "kind": kind}

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        status = "canceled" if etype.endswith("deleted") else obj.get("status")
        if customer_id:
            repository.set_membership_status_by_customer(customer_id, status)
        return {"handled": etype, "status": status}

    if etype == "invoice.payment_failed":
        customer_id = obj.get("customer")
        if customer_id:
            repository.set_membership_status_by_customer(customer_id, "past_due")
        return {"handled": etype, "status": "past_due"}

    return {"handled": None, "type": etype}  # event we don't act on
