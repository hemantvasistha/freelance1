"""Unified FastAPI Server & CLI Orchestrator combining Blocks 1, 2, 3, and 4."""
import argparse
import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

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
from src.schemas import PaymentWebhookPayload, SheetRowWebhookPayload


logger = logging.getLogger("sheetflow.scheduler")


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
