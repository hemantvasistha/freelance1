"""Block 4: Payment Gateway Webhook Signature Verification & Instant Reconciliation."""
import hashlib
import hmac
import sqlite3
from dataclasses import dataclass

from src.models import SheetFlowRepository
from src.schemas import PaymentWebhookPayload
from src.sheets import outbox


@dataclass(frozen=True)
class ReconciliationResult:
    invoice_id: str
    invoice_number: str
    sheet_row_number: int
    status: str
    paid_at: str
    gateway_payment_id: str
    idempotent_replay: bool


def compute_sheet_signature(webhook_secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 of the exact request body, used to authenticate sheet-row webhooks."""
    return hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def compute_payment_signature(
    webhook_secret: str,
    tenant_id: str,
    invoice_number: str,
    gateway_payment_id: str,
    amount_paid_minor: int,
) -> str:
    msg = f"{tenant_id}:{invoice_number}:{gateway_payment_id}:{amount_paid_minor}".encode("utf-8")
    return hmac.new(webhook_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_and_reconcile_payment(
    conn: sqlite3.Connection,
    payload: PaymentWebhookPayload,
) -> ReconciliationResult:
    """
    1. Looks up tenant and verifies HMAC-SHA256 webhook signature (`hmac.compare_digest`).
    2. Verifies `amount_paid_minor` matches `invoice.total_minor`.
    3. Marks invoice `PAID` idempotently and records `WEBHOOK_PAID` in `audit_events`.
    """
    repo = SheetFlowRepository(conn)
    tenant = repo.get_tenant(payload.tenant_id)
    if not tenant:
        raise ValueError(f"Tenant '{payload.tenant_id}' not found.")

    expected_sig = compute_payment_signature(
        webhook_secret=tenant.webhook_secret,
        tenant_id=payload.tenant_id,
        invoice_number=payload.invoice_number,
        gateway_payment_id=payload.gateway_payment_id,
        amount_paid_minor=payload.amount_paid_minor,
    )
    if not hmac.compare_digest(expected_sig, payload.signature):
        raise PermissionError("Invalid payment webhook HMAC signature.")

    inv = repo.get_invoice_by_number(payload.tenant_id, payload.invoice_number)
    if not inv:
        raise ValueError(f"Invoice '{payload.invoice_number}' not found.")

    expected_currency = (tenant.currency_code or "INR").upper()
    provided_currency = (payload.currency or expected_currency).upper()
    if provided_currency != expected_currency:
        raise ValueError(
            f"Currency mismatch: invoice is {expected_currency}, payment claims {provided_currency}."
        )

    if payload.amount_paid_minor < inv.total_minor:
        raise ValueError(
            f"Underpayment rejected: expected {inv.total_minor} minor units, got {payload.amount_paid_minor}."
        )

    event_key = f"paid:{inv.id}:{payload.gateway_payment_id}"
    is_first_time = repo.record_audit_event_if_new(
        invoice_id=inv.id,
        event_type="WEBHOOK_PAID",
        event_key=event_key,
        payload={
            "gateway_payment_id": payload.gateway_payment_id,
            "amount_paid_minor": payload.amount_paid_minor,
            "paid_at_iso": payload.paid_at_iso,
        },
    )

    if inv.status != "PAID":
        inv = repo.mark_invoice_paid(
            tenant_id=payload.tenant_id,
            invoice_number=payload.invoice_number,
            paid_at_iso=payload.paid_at_iso,
            gateway_payment_id=payload.gateway_payment_id,
        )

    # The customer's sheet learns about PAID by pulling this row, keyed on the
    # invoice number (stable) rather than the row position (moves when rows shift).
    outbox.enqueue(
        conn,
        tenant_id=payload.tenant_id,
        invoice_number=payload.invoice_number,
        status="PAID",
        paid_at=inv.paid_at or payload.paid_at_iso,
        payment_ref=payload.gateway_payment_id,
        event_key=f"sheet:{inv.id}:PAID",
    )

    return ReconciliationResult(
        invoice_id=inv.id,
        invoice_number=inv.invoice_number,
        sheet_row_number=inv.sheet_row_number,
        status=inv.status,
        paid_at=inv.paid_at or payload.paid_at_iso,
        gateway_payment_id=payload.gateway_payment_id,
        idempotent_replay=not is_first_time,
    )
