"""The invoice email itself, as opposed to the PDF it links to.

Before this module existed, an invoice was "sent" once a PDF landed on disk and a
link appeared in the customer's spreadsheet. That is document generation, not
delivery. This module builds a real message to the client and reports the delivery
outcome separately, so the audit trail can distinguish:

* the invoice was created and rendered (`INVOICE_GENERATED_AND_SENT`),
* the invoice email was actually delivered (`INVOICE_EMAIL` + delivery_status).
"""
from __future__ import annotations

from src.models import InvoiceRecord, TenantRecord
from src.notifications.mailer import Mailer, OutboundEmail
from src.schemas import currency_symbol, format_major_currency

DELIVERY_SENT = "SENT"
DELIVERY_DRY_RUN = "DRY_RUN"
DELIVERY_FAILED = "FAILED"
DELIVERY_SKIPPED = "SKIPPED"


def render_invoice_email(
    tenant: TenantRecord,
    invoice: InvoiceRecord,
    pdf_url: str,
    payment_url: str,
) -> OutboundEmail:
    """Plain-text invoice email: what is owed, when it is due, and how to pay."""
    symbol = currency_symbol(tenant.currency_code)
    amount = format_major_currency(invoice.total_minor, symbol)

    lines = [
        f"Hello {invoice.client_name},",
        "",
        f"Invoice {invoice.invoice_number} from {tenant.business_name} is attached below.",
        "",
        f"  Amount:  {amount}",
        f"  Issued:  {invoice.issue_date}",
        f"  Due:     {invoice.due_date}",
        f"  Item:    {invoice.item_description}",
        "",
    ]
    if pdf_url:
        lines += [f"Invoice PDF: {pdf_url}", ""]
    if payment_url:
        lines += [f"Pay now: {payment_url}", ""]
    if tenant.upi_vpa and not payment_url:
        lines += [f"Pay by UPI to: {tenant.upi_vpa}", ""]

    lines += [
        f"Questions about this invoice? Reply to this email.",
        "",
        tenant.business_name + (f" | GSTIN {tenant.tax_id}" if tenant.tax_id else ""),
    ]

    return OutboundEmail(
        to=invoice.client_email,
        subject=f"Invoice {invoice.invoice_number} from {tenant.business_name} — {amount}",
        body="\n".join(lines),
    )


def dispatch_invoice_email(
    mailer: Mailer | None,
    tenant: TenantRecord,
    invoice: InvoiceRecord,
    pdf_url: str,
    payment_url: str,
) -> tuple[str, str | None]:
    """Sends (or records) the invoice email. Returns (delivery_status, error)."""
    if mailer is None:
        return DELIVERY_SKIPPED, "mailer not configured"

    message = render_invoice_email(tenant, invoice, pdf_url, payment_url)
    try:
        status, error = mailer.send(message)
    except Exception as exc:  # noqa: BLE001 - delivery must never break invoicing
        return DELIVERY_FAILED, f"{type(exc).__name__}: {exc}"

    # The mailer already speaks the same vocabulary (SENT/DRY_RUN/FAILED).
    return status, error
