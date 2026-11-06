"""Razorpay: card/UPI payment links and native webhook reconciliation.

Two independent pieces:

1. **Payment links** (`create_payment_link`) — a hosted Razorpay page a customer can
   pay with a card. Only used when `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are set;
   otherwise the invoice keeps its zero-fee UPI deep link.
2. **Webhook reconciliation** (`verify_webhook_signature` + `record_razorpay_payment`) —
   verifies `X-Razorpay-Signature` with the dashboard webhook secret and marks the
   invoice PAID. This needs no API keys, only `RAZORPAY_WEBHOOK_SECRET`.

Matching is done through the `notes` we attach to the link (`tenant_id`,
`invoice_number`), so no extra mapping table is required.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone

import httpx

from src.models import SheetFlowRepository
from src.reconciliation.webhooks import ReconciliationResult

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

PAID_EVENTS = ("payment.captured", "payment_link.paid", "order.paid")


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the exact request body with the dashboard webhook secret."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _epoch_to_iso(value: object) -> str:
    try:
        timestamp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_payment_event(raw_body: bytes) -> dict | None:
    """Normalises a Razorpay webhook body into the fields reconciliation needs."""
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Razorpay webhook JSON: {exc}") from exc

    event_name = event.get("event") or ""
    if event_name not in PAID_EVENTS:
        return None

    payload = event.get("payload") or {}
    payment = ((payload.get("payment") or {}).get("entity")) or {}
    link = ((payload.get("payment_link") or {}).get("entity")) or {}

    notes = payment.get("notes") or link.get("notes") or {}
    tenant_id = notes.get("tenant_id")
    invoice_number = notes.get("invoice_number")
    if not tenant_id or not invoice_number:
        raise ValueError("Razorpay event has no tenant_id/invoice_number in notes.")

    payment_id = payment.get("id") or link.get("payment_id") or link.get("id")
    amount_minor = payment.get("amount") or link.get("amount_paid") or link.get("amount") or 0
    paid_at = _epoch_to_iso(payment.get("created_at") or link.get("created_at"))

    return {
        "event": event_name,
        "tenant_id": str(tenant_id),
        "invoice_number": str(invoice_number),
        "gateway_payment_id": str(payment_id or ""),
        "amount_paid_minor": int(amount_minor),
        "currency": (payment.get("currency") or link.get("currency") or "INR"),
        "paid_at_iso": paid_at,
    }


def record_razorpay_payment(conn: sqlite3.Connection, event: dict) -> ReconciliationResult:
    """Marks the matching invoice PAID. Replays are idempotent; underpayment is rejected."""
    repo = SheetFlowRepository(conn)
    tenant = repo.get_tenant(event["tenant_id"])
    if not tenant:
        raise ValueError(f"Tenant '{event['tenant_id']}' not found.")

    invoice = repo.get_invoice_by_number(event["tenant_id"], event["invoice_number"])
    if not invoice:
        raise ValueError(f"Invoice '{event['invoice_number']}' not found.")

    if event["amount_paid_minor"] < invoice.total_minor:
        raise ValueError(
            f"Underpayment rejected: expected {invoice.total_minor} minor units, "
            f"got {event['amount_paid_minor']}."
        )

    event_key = f"paid:{invoice.id}:{event['gateway_payment_id'] or 'razorpay'}"
    is_first_time = repo.record_audit_event_if_new(
        invoice_id=invoice.id,
        event_type="RAZORPAY_PAID",
        event_key=event_key,
        payload={key: event[key] for key in ("event", "gateway_payment_id", "amount_paid_minor", "paid_at_iso")},
    )

    if invoice.status != "PAID":
        invoice = repo.mark_invoice_paid(
            tenant_id=event["tenant_id"],
            invoice_number=event["invoice_number"],
            paid_at_iso=event["paid_at_iso"],
            gateway_payment_id=event["gateway_payment_id"] or "razorpay",
        )

    return ReconciliationResult(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        sheet_row_number=invoice.sheet_row_number,
        status=invoice.status,
        paid_at=invoice.paid_at or event["paid_at_iso"],
        gateway_payment_id=event["gateway_payment_id"] or "razorpay",
        idempotent_replay=not is_first_time,
    )


def create_payment_link(
    *,
    key_id: str,
    key_secret: str,
    invoice_number: str,
    tenant_id: str,
    amount_minor: int,
    currency: str,
    description: str,
) -> str:
    """Creates a hosted Razorpay page and returns its short_url."""
    response = httpx.post(
        f"{RAZORPAY_API_BASE}/payment_links",
        auth=(key_id, key_secret),
        json={
            "amount": amount_minor,
            "currency": currency,
            "description": description[:200],
            "reference_id": f"{tenant_id}:{invoice_number}",
            "reminder_enable": True,
            "notes": {"tenant_id": tenant_id, "invoice_number": invoice_number},
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["short_url"]


def build_payment_link_provider(settings) -> callable | None:
    """Returns a callable for the invoice pipeline, or None when Razorpay is not configured."""
    key_id = (settings.razorpay_key_id or "").strip()
    key_secret = (settings.razorpay_key_secret or "").strip()
    if not key_id or not key_secret:
        return None

    def provide(tenant, invoice) -> str | None:
        return create_payment_link(
            key_id=key_id,
            key_secret=key_secret,
            invoice_number=invoice.invoice_number,
            tenant_id=tenant.id,
            amount_minor=invoice.total_minor,
            currency=tenant.currency_code or "INR",
            description=f"Invoice {invoice.invoice_number} — {tenant.business_name}",
        )

    return provide
