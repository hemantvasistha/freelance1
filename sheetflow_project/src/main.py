"""Unified FastAPI Server & CLI Orchestrator combining Blocks 1, 2, 3, and 4."""
import argparse
import html
import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from src.billing.document_links import verify_invoice_pdf_token
from src.billing.pdf_generator import active_pdf_engine
from src.config import get_settings
from src.database import get_db_connection, init_db
from src.dunning.scheduler import (
    evaluate_and_dispatch_reminders,
    generate_weekly_executive_digest,
)
from src.dunning.runner import run_daily, run_weekly_digest
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.models import SheetFlowRepository, TenantRecord
from src.notifications.mailer import MailerNotConfigured, build_mailer
from src.operator_auth import (
    OPERATOR_TOKEN_HEADER,
    check_rate_limit,
    resolve_tenant_by_token,
)
from src.sheets import outbox
from src.reconciliation.razorpay import (
    build_payment_link_provider,
    parse_payment_event,
    record_razorpay_payment,
    verify_webhook_signature,
)
from src.reconciliation.webhooks import (
    compute_sheet_signature,
    verify_and_reconcile_payment,
)
from src.schemas import PaymentWebhookPayload, SheetRowWebhookPayload, to_minor_units


logger = logging.getLogger("sheetflow.scheduler")

# The public landing page ships with the app so the product and its sales page are
# deployed as one artifact instead of two places to keep in sync.
SITE_INDEX = Path(__file__).resolve().parents[2] / "site" / "index.html"


async def _scheduler_loop() -> None:
    """Daily reminder pass, plus the digest on Mondays, without any external cron.

    Enabled by SHEETFLOW_SCHEDULER_ENABLED=true. Each job runs at most once per day and
    is dispatched to a worker thread so SMTP and SQLite never block the event loop.
    """
    settings = get_settings()
    last_daily: str | None = None
    last_digest: str | None = None
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if now.hour >= settings.scheduler_hour:
            if last_daily != today:
                try:
                    report = await asyncio.to_thread(run_daily, settings.db_path)
                    logger.info(
                        "daily run: %s reminders for %s tenants",
                        report["dispatched_count"],
                        report["tenants_processed"],
                    )
                    last_daily = today
                except Exception:  # noqa: BLE001 - keep the loop alive
                    logger.exception("daily reminder run failed")
            if now.weekday() == 0 and last_digest != today:
                try:
                    report = await asyncio.to_thread(run_weekly_digest, settings.db_path)
                    logger.info("weekly digest: %s digests sent", report["sent_count"])
                    last_digest = today
                except Exception:  # noqa: BLE001
                    logger.exception("weekly digest run failed")
        await asyncio.sleep(settings.scheduler_poll_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db(settings.db_path)
    task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="SheetFlow Engine API",
    version="1.0.0",
    description="B2B Spreadsheet-to-Invoice, Payment Link & Automated Dunning Pipeline",
    lifespan=lifespan,
)


class DunningTriggerRequest(BaseModel):
    tenant_id: str
    as_of_date: str


class OperatorRunRequest(BaseModel):
    # tenant_id is accepted for backwards compatibility but never trusted: the
    # token decides which tenant the call may touch.
    tenant_id: str | None = None
    as_of_date: str | None = None


class SheetAckRequest(BaseModel):
    ids: list[str] = []


class ManualPaymentRequest(BaseModel):
    tenant_id: str
    invoice_number: str
    amount_major: str = Field(..., description="Decimal string, e.g. '4130.00'")
    reference: str = Field(..., min_length=1, description="UPI/bank reference — unique per invoice")
    recorded_by: str = Field(..., min_length=1, description="Who verified the payment")
    note: str = ""


def require_admin_token(request: Request) -> None:
    """Guards server-side operator endpoints with SHEETFLOW_ADMIN_TOKEN.

    This is the owner's own credential (cron, admin tooling) and spans every tenant.
    It must never be given to a customer: customer sheets authenticate with their own
    per-tenant operator token via `require_operator_tenant`.

    If no token is configured the endpoints stay disabled rather than open, so a
    fresh deployment cannot leak other tenants' receivables by default.
    """
    expected = get_settings().admin_token.strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="SHEETFLOW_ADMIN_TOKEN is not configured on this server.",
        )
    provided = request.headers.get("X-SheetFlow-Admin-Token", "")
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-SheetFlow-Admin-Token header."
        )


def require_operator_tenant(request: Request) -> TenantRecord:
    """Authenticates a customer's sheet by that tenant's own operator token.

    Replaces the old model where every client script carried the server-wide admin
    token. The token identifies the tenant, so a caller cannot name another tenant
    and act on its invoices, reminders or digest.
    """
    token = request.headers.get(OPERATOR_TOKEN_HEADER, "")
    if not token:
        raise HTTPException(
            status_code=401, detail=f"Missing {OPERATOR_TOKEN_HEADER} header."
        )
    if not check_rate_limit(token):
        raise HTTPException(status_code=429, detail="Too many operator requests; slow down.")
    settings = get_settings()
    with get_db_connection(settings.db_path) as conn:
        tenant = resolve_tenant_by_token(conn, token)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid operator token.")
    return tenant


@app.get("/", include_in_schema=False)
def landing_page() -> HTMLResponse:
    """Serves the landing page with this deployment's real contact address.

    The HTML ships with a marker instead of an address, so publishing it without
    configuring SHEETFLOW_OWNER_EMAIL cannot show a prospective customer a placeholder
    mailbox that nobody reads.
    """
    if not SITE_INDEX.exists():
        raise HTTPException(status_code=404, detail="Landing page is not installed.")
    support_email = os.environ.get("SHEETFLOW_OWNER_EMAIL", "").strip()
    if support_email:
        contact = (
            "<p>Email "
            f'<a href="mailto:{html.escape(support_email)}?subject=SheetFlow%20walkthrough">'
            f"{html.escape(support_email)}</a>"
            " with the subject “SheetFlow walkthrough”, or reply to any message from us.</p>"
        )
    else:
        contact = "<p>Contact details are given in your onboarding email.</p>"

    text = SITE_INDEX.read_text(encoding="utf-8")
    start_marker = "<!--CONTACT_BLOCK_START-->"
    end_marker = "<!--CONTACT_BLOCK_END-->"
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start != -1 and end != -1:
        text = text[:start] + contact + text[end + len(end_marker):]
    return HTMLResponse(text)


@app.get("/health")
def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "sheetflow-engine",
        "version": "1.0.0",
        "pdf_engine": active_pdf_engine(),
        "email_delivery": "dry_run" if settings.dry_run else "smtp",
        "public_base_url": "configured" if settings.public_base_url else "unset",
        "scheduler": "enabled" if settings.scheduler_enabled else "off",
    }


@app.post("/api/v1/webhooks/sheet-row-ready")
async def api_process_sheet_row(payload: SheetRowWebhookPayload, request: Request) -> dict:
    """Creates and sends an invoice for a READY_TO_BILL spreadsheet row.

    The request must carry an `X-SheetFlow-Signature` header: hex HMAC-SHA256 of the
    exact request body, keyed with the tenant's `webhook_secret`.
    """
    raw_body = await request.body()
    provided_signature = request.headers.get("X-SheetFlow-Signature", "")
    settings = get_settings()
    try:
        mailer = build_mailer(settings)
    except MailerNotConfigured:
        # Invoicing must keep working even if SMTP is misconfigured; delivery is
        # then reported as SKIPPED instead of being silently assumed.
        mailer = None
    try:
        with get_db_connection(settings.db_path) as conn:
            tenant = SheetFlowRepository(conn).get_tenant(payload.tenant_id)
            if not tenant or not tenant.is_active:
                raise HTTPException(
                    status_code=404, detail=f"Active tenant '{payload.tenant_id}' not found."
                )
            expected_signature = compute_sheet_signature(tenant.webhook_secret, raw_body)
            if not hmac.compare_digest(expected_signature, provided_signature):
                raise HTTPException(
                    status_code=401, detail="Invalid X-SheetFlow-Signature for this tenant."
                )

            res = process_sheet_row_webhook(
                conn,
                payload,
                settings.output_pdf_dir,
                settings.public_base_url,
                payment_link_provider=build_payment_link_provider(settings),
                mailer=mailer,
            )
            return {
                "invoice_id": res.invoice_id,
                "invoice_number": res.invoice_number,
                "sheet_row_number": res.sheet_row_number,
                "created_new": res.created_new,
                "status": res.status,
                "total_minor": res.total_minor,
                "total_major": res.total_major,
                "payment_url": res.payment_url,
                "upi_uri": res.upi_uri,
                "pdf_path": res.pdf_path,
                "pdf_url": res.pdf_url,
                "delivery_status": res.delivery_status,
                "delivery_error": res.delivery_error,
            }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/invoices/{tenant_id}/{invoice_number}/pdf")
def api_invoice_pdf(tenant_id: str, invoice_number: str, token: str = "") -> FileResponse:
    """Serves the invoice PDF to the business owner and their customer.

    The `token` query parameter is an HMAC bound to this tenant and invoice number, so
    links are not enumerable. This is the URL written into the spreadsheet's PDF column.
    """
    settings = get_settings()
    with get_db_connection(settings.db_path) as conn:
        repo = SheetFlowRepository(conn)
        tenant = repo.get_tenant(tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found.")
        if not verify_invoice_pdf_token(tenant.webhook_secret, tenant.id, invoice_number, token):
            raise HTTPException(status_code=401, detail="Invalid or missing invoice PDF token.")
        invoice = repo.get_invoice_by_number(tenant.id, invoice_number)
        if not invoice or not invoice.pdf_path or not os.path.exists(invoice.pdf_path):
            raise HTTPException(status_code=404, detail="Invoice PDF not found on disk.")
        return FileResponse(
            invoice.pdf_path,
            media_type="application/pdf",
            filename=f"{invoice.invoice_number}.pdf",
        )


@app.post("/api/v1/webhooks/payment-captured")
def api_reconcile_payment(payload: PaymentWebhookPayload) -> dict:
    settings = get_settings()
    try:
        with get_db_connection(settings.db_path) as conn:
            res = verify_and_reconcile_payment(conn, payload)
            return {
                "invoice_id": res.invoice_id,
                "invoice_number": res.invoice_number,
                "sheet_row_number": res.sheet_row_number,
                "status": res.status,
                "paid_at": res.paid_at,
                "gateway_payment_id": res.gateway_payment_id,
                "idempotent_replay": res.idempotent_replay,
            }
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/webhooks/razorpay")
async def api_razorpay_webhook(request: Request) -> dict:
    """Native Razorpay reconciliation: verifies the dashboard signature, marks PAID."""
    settings = get_settings()
    secret = settings.razorpay_webhook_secret.strip()
    if not secret:
        raise HTTPException(status_code=503, detail="RAZORPAY_WEBHOOK_SECRET is not configured.")

    raw_body = await request.body()
    if not verify_webhook_signature(
        raw_body, request.headers.get("X-Razorpay-Signature", ""), secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Razorpay webhook signature.")

    try:
        event = parse_payment_event(raw_body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if event is None:
        return {"status": "ignored", "reason": "not a payment event"}

    try:
        with get_db_connection(settings.db_path) as conn:
            result = record_razorpay_payment(conn, event)
            return {
                "status": result.status,
                "invoice_number": result.invoice_number,
                "paid_at": result.paid_at,
                "gateway_payment_id": result.gateway_payment_id,
                "idempotent_replay": result.idempotent_replay,
            }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/dunning/run", dependencies=[Depends(require_admin_token)])
def api_run_dunning(req: DunningTriggerRequest) -> dict:
    settings = get_settings()
    try:
        mailer = build_mailer(settings)
    except MailerNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        with get_db_connection(settings.db_path) as conn:
            actions = evaluate_and_dispatch_reminders(
                conn, req.tenant_id, req.as_of_date, mailer=mailer
            )
            return {
                "tenant_id": req.tenant_id,
                "as_of_date": req.as_of_date,
                "dispatched_count": len(actions),
                "reminders": [a.__dict__ for a in actions],
            }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/v1/digest/{tenant_id}", dependencies=[Depends(require_admin_token)])
def api_weekly_digest(tenant_id: str, as_of_date: str | None = None) -> dict:
    settings = get_settings()
    report_date = as_of_date or date.today().isoformat()
    try:
        with get_db_connection(settings.db_path) as conn:
            digest = generate_weekly_executive_digest(conn, tenant_id, report_date)
            return digest.__dict__
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/v1/operator/dunning/run")
def api_operator_dunning_run(
    req: OperatorRunRequest,
    tenant: TenantRecord = Depends(require_operator_tenant),
) -> dict:
    """Runs the reminder pass for the token's own tenant only.

    The tenant comes from the token, not the body, so a customer's sheet cannot
    trigger another customer's reminders.
    """
    settings = get_settings()
    as_of = req.as_of_date or date.today().isoformat()
    try:
        mailer = build_mailer(settings)
    except MailerNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    with get_db_connection(settings.db_path) as conn:
        try:
            actions = evaluate_and_dispatch_reminders(conn, tenant.id, as_of, mailer=mailer)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        for action in actions:
            if action.new_status == "OVERDUE":
                outbox.enqueue(
                    conn,
                    tenant_id=tenant.id,
                    invoice_number=action.invoice_number,
                    status="OVERDUE",
                    event_key=f"sheet:{action.invoice_id}:OVERDUE",
                )

    return {
        "tenant_id": tenant.id,
        "as_of_date": as_of,
        "dispatched_count": len(actions),
        "reminders": [a.__dict__ for a in actions],
    }


@app.get("/api/v1/operator/digest")
def api_operator_digest(
    as_of_date: str | None = None,
    tenant: TenantRecord = Depends(require_operator_tenant),
) -> dict:
    settings = get_settings()
    report_date = as_of_date or date.today().isoformat()
    try:
        with get_db_connection(settings.db_path) as conn:
            return generate_weekly_executive_digest(conn, tenant.id, report_date).__dict__
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/v1/operator/sheet-updates")
def api_operator_sheet_updates(
    tenant: TenantRecord = Depends(require_operator_tenant),
) -> dict:
    """Pending ledger changes for this tenant's sheet (payment/overdue write-back)."""
    settings = get_settings()
    with get_db_connection(settings.db_path) as conn:
        updates = outbox.pending(conn, tenant.id)
        outbox.record_attempt(conn, tenant.id, [u["id"] for u in updates])
    return {"tenant_id": tenant.id, "count": len(updates), "updates": updates}


@app.post("/api/v1/operator/sheet-updates/ack")
def api_operator_sheet_ack(
    req: SheetAckRequest,
    tenant: TenantRecord = Depends(require_operator_tenant),
) -> dict:
    """Marks written changes delivered, scoped to the tenant that owns them."""
    settings = get_settings()
    with get_db_connection(settings.db_path) as conn:
        acknowledged = outbox.acknowledge(conn, tenant.id, req.ids)
    return {"tenant_id": tenant.id, "acknowledged": acknowledged}


@app.get("/api/v1/admin/ops", dependencies=[Depends(require_admin_token)])
def api_owner_ops() -> dict:
    """Exception-focused operating view for the owner.

    Deliberately not an analytics dashboard: every number here either confirms the
    books agree or tells the owner something needs attention.
    """
    settings = get_settings()
    with get_db_connection(settings.db_path) as conn:
        active_tenants = conn.execute(
            "SELECT COUNT(*) AS n FROM tenants WHERE is_active = 1"
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(total_minor), 0) AS total "
            "FROM invoices GROUP BY status"
        ).fetchall()
        by_status = {row["status"]: {"count": row["n"], "total_minor": row["total"]} for row in rows}

        pending_outbox = conn.execute(
            "SELECT COUNT(*) AS n FROM sheet_sync_outbox WHERE delivered_at IS NULL"
        ).fetchone()["n"]
        failed_reminders = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type LIKE 'REMINDER_%' "
            "AND payload_json LIKE '%\"delivery_status\": \"FAILED\"%'"
        ).fetchone()["n"]
        failed_invoice_emails = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE event_type = 'INVOICE_EMAIL' "
            "AND payload_json LIKE '%\"delivery_status\": \"FAILED\"%'"
        ).fetchone()["n"]

    outstanding = sum(
        entry["total_minor"]
        for status, entry in by_status.items()
        if status in ("READY_TO_BILL", "SENT", "OVERDUE")
    )
    exceptions = {
        "sheet_updates_not_yet_pulled": pending_outbox,
        "failed_reminder_deliveries": failed_reminders,
        "failed_invoice_emails": failed_invoice_emails,
    }
    return {
        "active_tenants": active_tenants,
        "invoices_by_status": by_status,
        "outstanding_total_minor": outstanding,
        "overdue_total_minor": by_status.get("OVERDUE", {}).get("total_minor", 0),
        "exceptions": exceptions,
        "needs_attention": any(value > 0 for value in exceptions.values()),
        "email_delivery": "dry_run" if settings.dry_run else "smtp",
        "scheduler": "enabled" if settings.scheduler_enabled else "off",
    }


@app.post("/api/v1/admin/manual-payment", dependencies=[Depends(require_admin_token)])
def api_manual_payment(req: ManualPaymentRequest) -> dict:
    """Records a payment confirmed outside a gateway (UPI transfer, bank credit).

    The zero-fee UPI rail cannot confirm itself, so this is the only way a UPI-only
    tenant's invoices become PAID. It is admin-only, demands a unique reference and the
    name of the person who verified it, and appends to the payment history rather than
    overwriting it, so a partial payment is a fact rather than a guess.
    """
    settings = get_settings()
    try:
        amount_minor = to_minor_units(req.amount_major)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if amount_minor <= 0:
        raise HTTPException(status_code=400, detail="amount_major must be positive.")

    with get_db_connection(settings.db_path) as conn:
        repo = SheetFlowRepository(conn)
        tenant = repo.get_tenant(req.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail=f"Tenant '{req.tenant_id}' not found.")
        invoice = repo.get_invoice_by_number(tenant.id, req.invoice_number)
        if not invoice:
            raise HTTPException(
                status_code=404, detail=f"Invoice '{req.invoice_number}' not found."
            )
        if invoice.status == "CANCELLED":
            raise HTTPException(status_code=409, detail="Cancelled invoices cannot receive payments.")

        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        reference = req.reference.strip()
        _, created = repo.record_payment(
            tenant_id=tenant.id,
            invoice=invoice,
            amount_minor=amount_minor,
            currency=(tenant.currency_code or "INR").upper(),
            source="MANUAL",
            provider="upi" if tenant.upi_vpa else "manual",
            provider_payment_id=reference,
            recorded_by=req.recorded_by.strip(),
            note=req.note.strip(),
        )
        if not created:
            raise HTTPException(
                status_code=409, detail=f"Reference '{reference}' is already recorded for this invoice."
            )

        paid_total = repo.paid_total_minor(invoice.id)
        became_paid = False
        if paid_total >= invoice.total_minor and invoice.status != "PAID":
            invoice = repo.mark_invoice_paid(
                tenant_id=tenant.id,
                invoice_number=invoice.invoice_number,
                paid_at_iso=recorded_at,
                gateway_payment_id=reference,
            )
            outbox.enqueue(
                conn,
                tenant_id=tenant.id,
                invoice_number=invoice.invoice_number,
                status="PAID",
                paid_at=invoice.paid_at or recorded_at,
                payment_ref=reference,
                event_key=f"sheet:{invoice.id}:PAID",
            )
            became_paid = True

        outstanding = max(invoice.total_minor - paid_total, 0)
        status = invoice.status

    return {
        "invoice_number": req.invoice_number,
        "status": status,
        "amount_recorded_minor": amount_minor,
        "paid_total_minor": paid_total,
        "outstanding_minor": outstanding,
        "became_paid": became_paid,
        "recorded_at": recorded_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SheetFlow Engine CLI")
    parser.add_argument("--serve", action="store_true", help="Run the FastAPI server with uvicorn")
    parser.add_argument("--db", default="sheetflow.db", help="SQLite DB file path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for --serve")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for --serve")
    args = parser.parse_args()

    if args.serve:
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
