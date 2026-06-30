"""
Tests for payments.py (Stripe). The Stripe SDK and the DB are both mocked
(patch payments._stripe and payments.repository.*), so no real Stripe account,
network, or Postgres is needed. Run with: python3 -m pytest test_payments.py
"""

from unittest.mock import MagicMock, patch

import pytest

import payments


def _mock_stripe():
    m = MagicMock()
    m.Customer.create.return_value = {"id": "cus_123"}
    m.checkout.Session.create.return_value = {"id": "cs_test_1", "url": "https://checkout.stripe.test/x"}
    return m


# ============================================================
# Enablement gate
# ============================================================

def test_disabled_when_no_secret_key(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert payments.is_enabled() is False
    with pytest.raises(payments.PaymentsNotConfigured):
        payments._stripe()


# ============================================================
# Customers
# ============================================================

def test_ensure_customer_creates_and_persists():
    m = _mock_stripe()
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "fetch_stripe_customer_id", return_value=None), \
         patch.object(payments.repository, "set_stripe_customer_id") as setc:
        cid = payments.ensure_customer("pat-1", "a@b.com")
    assert cid == "cus_123"
    setc.assert_called_once_with("pat-1", "cus_123")


def test_ensure_customer_returns_existing_without_calling_stripe():
    with patch.object(payments.repository, "fetch_stripe_customer_id", return_value="cus_existing"), \
         patch.object(payments, "_stripe") as st:
        cid = payments.ensure_customer("pat-1")
    assert cid == "cus_existing"
    st.assert_not_called()


# ============================================================
# Membership checkout (subscription)
# ============================================================

def test_membership_checkout_creates_subscription_session(monkeypatch):
    monkeypatch.setenv("STRIPE_MEMBERSHIP_PRICE_ID", "price_123")
    m = _mock_stripe()
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "fetch_stripe_customer_id", return_value="cus_1"):
        res = payments.create_membership_checkout("pat-1", email="a@b.com")
    assert res["checkout_url"].startswith("https://")
    assert res["session_id"] == "cs_test_1"
    kwargs = m.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    assert kwargs["line_items"][0]["price"] == "price_123"
    assert kwargs["metadata"]["kind"] == "membership"


def test_membership_checkout_requires_price_id(monkeypatch):
    monkeypatch.delenv("STRIPE_MEMBERSHIP_PRICE_ID", raising=False)
    with pytest.raises(payments.PaymentsNotConfigured):
        payments.create_membership_checkout("pat-1")


# ============================================================
# Contingency checkout (one-time)
# ============================================================

def test_contingency_checkout_records_pending_payment_in_cents():
    m = _mock_stripe()
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "fetch_stripe_customer_id", return_value="cus_1"), \
         patch.object(payments.repository, "record_payment") as rec:
        res = payments.create_contingency_checkout("pat-1", "case-1", 600.0)
    assert res["session_id"] == "cs_test_1"
    kwargs = m.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "payment"
    assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 60000  # $600 -> cents
    rec.assert_called_once()
    assert rec.call_args.kwargs["amount_cents"] == 60000
    assert rec.call_args.kwargs["status"] == "pending"
    assert rec.call_args.kwargs["kind"] == "contingency"


def test_contingency_checkout_rejects_nonpositive_amount():
    with pytest.raises(ValueError):
        payments.create_contingency_checkout("pat-1", "case-1", 0)


# ============================================================
# Webhooks (source of truth)
# ============================================================

def _webhook_with_event(event, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    m = MagicMock()
    m.Webhook.construct_event.return_value = event
    return m


def test_webhook_membership_completed_sets_active(monkeypatch):
    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "mode": "subscription", "subscription": "sub_1",
        "client_reference_id": "pat-1",
        "metadata": {"kind": "membership", "patient_id": "pat-1"},
    }}}
    m = _webhook_with_event(event, monkeypatch)
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "set_membership_subscription") as setsub:
        summary = payments.handle_webhook(b"{}", "sig")
    setsub.assert_called_once_with("pat-1", "sub_1", "active")
    assert summary["handled"] == "checkout.session.completed"


def test_webhook_contingency_completed_marks_paid(monkeypatch):
    event = {"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_2", "mode": "payment",
        "metadata": {"kind": "contingency", "patient_id": "pat-1", "case_id": "case-1"},
    }}}
    m = _webhook_with_event(event, monkeypatch)
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "mark_payment_status_by_session") as mark:
        payments.handle_webhook(b"{}", "sig")
    mark.assert_called_once_with("cs_2", "paid")


def test_webhook_subscription_deleted_sets_canceled(monkeypatch):
    event = {"type": "customer.subscription.deleted", "data": {"object": {
        "customer": "cus_1", "status": "canceled",
    }}}
    m = _webhook_with_event(event, monkeypatch)
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "set_membership_status_by_customer") as setstat:
        payments.handle_webhook(b"{}", "sig")
    setstat.assert_called_once_with("cus_1", "canceled")


def test_webhook_payment_failed_sets_past_due(monkeypatch):
    event = {"type": "invoice.payment_failed", "data": {"object": {"customer": "cus_1"}}}
    m = _webhook_with_event(event, monkeypatch)
    with patch.object(payments, "_stripe", return_value=m), \
         patch.object(payments.repository, "set_membership_status_by_customer") as setstat:
        payments.handle_webhook(b"{}", "sig")
    setstat.assert_called_once_with("cus_1", "past_due")


def test_webhook_bad_signature_raises_valueerror(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    m = MagicMock()
    m.Webhook.construct_event.side_effect = Exception("bad signature")
    with patch.object(payments, "_stripe", return_value=m):
        with pytest.raises(ValueError):
            payments.handle_webhook(b"{}", "badsig")


def test_webhook_unhandled_event_is_noop(monkeypatch):
    event = {"type": "payment_intent.created", "data": {"object": {}}}
    m = _webhook_with_event(event, monkeypatch)
    with patch.object(payments, "_stripe", return_value=m):
        summary = payments.handle_webhook(b"{}", "sig")
    assert summary["handled"] is None
