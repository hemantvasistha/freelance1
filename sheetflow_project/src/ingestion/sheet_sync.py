"""Block 3: Google Sheets Ingestion & End-to-End Invoice Dispatch Pipeline."""
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from src.billing.document_links import build_invoice_pdf_url
from src.billing.payment_links import generate_payment_artifacts
from src.billing.pdf_generator import generate_invoice_pdf
from src.models import SheetFlowRepository
from src.schemas import (
    SheetRowWebhookPayload,
    calculate_gst_breakdown,
    compute_idempotency_key,
)


@dataclass(frozen=True)
class SheetRowSyncResult:
    invoice_id: str
    invoice_number: str
    sheet_row_number: int
    created_new: bool
    status: str
    subtotal_minor: int
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    total_minor: int
    total_major: str
    payment_url: str
    upi_uri: str | None
    pdf_path: str
    html_path: str
    pdf_url: str


def process_sheet_row_webhook(
    conn: sqlite3.Connection,
    payload: SheetRowWebhookPayload,
    output_pdf_dir: str,
    public_base_url: str = "",
    payment_link_provider: Callable[[Any, Any], str | None] | None = None,
) -> SheetRowSyncResult:
    """
    Processes an incoming `READY_TO_BILL` spreadsheet row:
    1. Verifies the tenant exists and is active.
    2. Computes deterministic GST split (CGST/SGST vs IGST) in integer minor units.
    3. Creates or retrieves the invoice idempotently.
    4. Generates UPI & checkout payment links and renders the A4 PDF invoice.
    5. Updates invoice status to `SENT` and logs an immutable audit event.

    `public_base_url` (e.g. `https://billing.example.com`) turns the rendered PDF into a
    client-openable signed URL; without it `pdf_url` is empty and only the disk path is
    returned, which the spreadsheet cannot use.
    """
    repo = SheetFlowRepository(conn)
    tenant = repo.get_tenant(payload.tenant_id)
    if not tenant or not tenant.is_active:
        raise ValueError(f"Active tenant '{payload.tenant_id}' not found.")

    tax = calculate_gst_breakdown(
        subtotal_major=payload.subtotal_major,
        tax_rate_pct=payload.tax_rate_pct,
        tenant_home_state=tenant.home_state,
        client_state=payload.client_state,
    )

    idem_key = compute_idempotency_key(
        tenant_id=tenant.id,
        invoice_number=payload.invoice_number,
        issue_date=payload.issue_date,
    )

    invoice, created_new = repo.create_or_get_invoice(
        tenant_id=tenant.id,
        sheet_row_number=payload.sheet_row_number,
        invoice_number=payload.invoice_number,
        idempotency_key=idem_key,
        client_name=payload.client_name,
        client_email=payload.client_email,
        client_phone=payload.client_phone,
        client_state=payload.client_state,
        item_description=payload.item_description,
        subtotal_minor=tax.subtotal_minor,
        tax_rate_bps=tax.tax_rate_bps,
        cgst_minor=tax.cgst_minor,
        sgst_minor=tax.sgst_minor,
        igst_minor=tax.igst_minor,
        total_minor=tax.total_minor,
        issue_date=payload.issue_date,
        due_date=payload.due_date,
        status=payload.status,
    )

    artifacts = generate_payment_artifacts(tenant, invoice)
    payment_url = artifacts.payment_url
    if payment_link_provider is not None:
        # A card gateway link is nicer but must never block the invoice: if the
        # provider is down, the zero-fee UPI link stays in place.
        try:
            provided = payment_link_provider(tenant, invoice)
        except Exception:  # noqa: BLE001
            provided = None
        if provided:
            payment_url = provided

    html_path, pdf_path = generate_invoice_pdf(
        tenant=tenant,
        invoice=invoice,
        payment_url=payment_url,
        output_dir=output_pdf_dir,
        upi_uri=artifacts.upi_uri,
    )

    if invoice.status in ("DRAFT", "READY_TO_BILL"):
        invoice = repo.update_invoice_billing_artifacts(
            invoice.id,
            payment_url=payment_url,
            payment_gateway_ref=artifacts.gateway_reference,
            pdf_path=pdf_path,
            new_status="SENT",
        )

    pdf_url = build_invoice_pdf_url(
        public_base_url, tenant.webhook_secret, tenant.id, invoice.invoice_number
    )

    repo.record_audit_event_if_new(
        invoice_id=invoice.id,
        event_type="INVOICE_GENERATED_AND_SENT",
        event_key=f"sent:{idem_key}",
        payload={
            "invoice_number": invoice.invoice_number,
            "total_minor": invoice.total_minor,
            "payment_url": payment_url,
            "pdf_path": pdf_path,
            "pdf_url": pdf_url,
        },
    )

    total_major_str = str(
        (Decimal(invoice.total_minor) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )

    return SheetRowSyncResult(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        sheet_row_number=invoice.sheet_row_number,
        created_new=created_new,
        status=invoice.status,
        subtotal_minor=invoice.subtotal_minor,
        cgst_minor=invoice.cgst_minor,
        sgst_minor=invoice.sgst_minor,
        igst_minor=invoice.igst_minor,
        total_minor=invoice.total_minor,
        total_major=total_major_str,
        payment_url=payment_url,
        upi_uri=artifacts.upi_uri,
        pdf_path=pdf_path,
        html_path=html_path,
        pdf_url=pdf_url,
    )
