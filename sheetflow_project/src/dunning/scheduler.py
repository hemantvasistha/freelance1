"""Block 4: 3-Stage Automated Dunning Engine (T-3, T+0, T+7) & Weekly Cash-Flow Digest."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.models import InvoiceRecord, SheetFlowRepository
from src.notifications.mailer import (
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    Mailer,
    OutboundEmail,
)
from src.schemas import currency_symbol, format_major_currency


def _payment_instruction(invoice: InvoiceRecord) -> str:
    """One clause appended to every reminder telling the client how to actually pay."""
    if invoice.payment_url:
        return f"Pay instantly: {invoice.payment_url}"
    return "Please arrange payment against this invoice number and reply with the reference."


@dataclass(frozen=True)
class ReminderAction:
    invoice_id: str
    invoice_number: str
    client_name: str
    client_email: str
    stage: str
    days_from_due: int
    new_status: str
    subject: str
    body_preview: str
    delivery_status: str = DELIVERY_PENDING
    delivery_error: str | None = None


@dataclass(frozen=True)
class WeeklyDigestReport:
    tenant_id: str
    business_name: str
    as_of_date: str
    total_invoices_count: int
    paid_count: int
    outstanding_count: int
    overdue_count: int
    total_billed_minor: int
    total_collected_minor: int
    total_outstanding_minor: int
    total_overdue_minor: int
    formatted_summary: str


def evaluate_and_dispatch_reminders(
    conn: sqlite3.Connection,
    tenant_id: str,
    as_of_date_iso: str,
    mailer: Mailer | None = None,
) -> list[ReminderAction]:
    """
    Evaluates all active (`SENT` or `OVERDUE`) invoices for a tenant against `as_of_date_iso`:
    - `-3 <= days_from_due <= -1`: dispatches `T_MINUS_3` courtesy reminder.
    - `0 <= days_from_due <= 6`: dispatches `T_ZERO` due-date alert.
    - `days_from_due >= 7`: dispatches `T_PLUS_7` overdue escalation and sets status to `OVERDUE`.

    Each stage covers a window rather than one exact day, so a cron that misses a day
    (host restart, cold start) still catches up on the next run instead of dropping the
    reminder forever. Idempotency is enforced via `audit_events`
    (`dunning:<invoice_id>:<stage>`), so the catch-up never double-sends a stage.

    `mailer` decides what "dispatch" means: pass one to actually deliver (a
    `RecordingMailer` under DRY_RUN, `build_mailer(settings)` in the API). Without a
    mailer the reminder is still recorded and returned with `delivery_status=PENDING`,
    so callers can inspect it before wiring delivery.

    Note: the audit claim is written *before* delivery, which is what prevents double
    sends. If delivery then fails, the reminder is reported as `FAILED` and the claim
    still blocks an automatic retry of the same stage — by design (a retry loop against
    a dead SMTP server is worse than a surfaced failure), so re-sending requires
    clearing or rotating the reminder stage for that invoice.
    """
    repo = SheetFlowRepository(conn)
    tenant = repo.get_tenant(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant '{tenant_id}' not found.")

    as_of = date.fromisoformat(as_of_date_iso)
    invoices = repo.list_invoices_for_tenant(tenant_id)
    actions: list[ReminderAction] = []

    symbol = currency_symbol(tenant.currency_code)

    for inv in invoices:
        if inv.status not in ("SENT", "OVERDUE"):
            continue

        due = date.fromisoformat(inv.due_date)
        days_from_due = (as_of - due).days

        target_stage: str | None = None
        target_status = inv.status
        subject = ""
        body = ""
        amount_str = format_major_currency(inv.total_minor, symbol)

        if days_from_due >= 7:
            target_stage = "T_PLUS_7"
            target_status = "OVERDUE"
            subject = f"[OVERDUE NOTICE] Invoice {inv.invoice_number} ({amount_str}) is {days_from_due} days past due"
            body = (
                f"Hello {inv.client_name}, Invoice {inv.invoice_number} for {amount_str} was due on {inv.due_date}. "
                f"Please complete payment immediately to avoid service interruption."
            )
        elif days_from_due > 0:
            # Catch-up: the due-date run was missed and the invoice is now overdue
            # but not yet at the T+7 escalation.
            target_stage = "T_ZERO"
            subject = (
                f"[PAST DUE] Invoice {inv.invoice_number} ({amount_str}) was due on {inv.due_date}"
            )
            body = (
                f"Hello {inv.client_name}, Invoice {inv.invoice_number} for {amount_str} was due on "
                f"{inv.due_date} and is still unpaid."
            )
        elif days_from_due == 0:
            target_stage = "T_ZERO"
            subject = f"[DUE TODAY] Invoice {inv.invoice_number} ({amount_str}) is due today ({inv.due_date})"
            body = (
                f"Hello {inv.client_name}, friendly reminder that Invoice {inv.invoice_number} for {amount_str} "
                f"is due today."
            )
        elif days_from_due >= -3:
            days_left = abs(days_from_due)
            target_stage = "T_MINUS_3"
            subject = (
                f"[Upcoming Due Date] Invoice {inv.invoice_number} ({amount_str}) due on {inv.due_date}"
            )
            body = (
                f"Hello {inv.client_name}, courtesy heads-up that Invoice {inv.invoice_number} for {amount_str} "
                f"is due in {days_left} day(s) ({inv.due_date})."
            )

        if not target_stage:
            continue

        body = f"{body} {_payment_instruction(inv)}"
        event_key = f"dunning:{inv.id}:{target_stage}"
        recorded = repo.record_audit_event_if_new(
            invoice_id=inv.id,
            event_type=f"REMINDER_{target_stage}",
            event_key=event_key,
            payload={
                "stage": target_stage,
                "as_of_date": as_of_date_iso,
                "days_from_due": days_from_due,
                "client_email": inv.client_email,
                "recipient": inv.client_email,
                "subject": subject,
                "body": body,
                "delivery_status": DELIVERY_PENDING,
                "attempts": 0,
            },
        )
        if not recorded:
            # Already sent this reminder stage previously (or awaiting a retry)
            continue

        updated_inv = repo.update_reminder_stage(inv.id, stage=target_stage, new_status=target_status)

        delivery_status, delivery_error = DELIVERY_PENDING, None
        if mailer is not None:
            delivery_status, delivery_error = mailer.send(
                OutboundEmail(to=inv.client_email, subject=subject, body=body)
            )
            repo.update_audit_payload(
                event_key,
                {
                    "delivery_status": delivery_status,
                    "delivery_error": delivery_error,
                    "attempts": 1,
                    "last_attempt_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )

        actions.append(
            ReminderAction(
                invoice_id=updated_inv.id,
                invoice_number=updated_inv.invoice_number,
                client_name=updated_inv.client_name,
                client_email=updated_inv.client_email,
                stage=target_stage,
                days_from_due=days_from_due,
                new_status=updated_inv.status,
                subject=subject,
                body_preview=body,
                delivery_status=delivery_status,
                delivery_error=delivery_error,
            )
        )

    return actions


def retry_failed_reminders(
    conn: sqlite3.Connection,
    mailer: Mailer,
    max_attempts: int = 3,
    limit: int = 50,
) -> list[dict]:
    """Re-sends reminders whose delivery failed, without re-sending a delivered one.

    The audit row is written *before* the send (that is what prevents double-sends), so a
    retry updates that row instead of creating a second claim. Retries stop after
    `max_attempts` so a permanently bad address does not mail forever.
    """
    repo = SheetFlowRepository(conn)
    rows = conn.execute(
        "SELECT event_key, payload_json FROM audit_events "
        "WHERE event_type LIKE 'REMINDER_%' ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()

    results: list[dict] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("delivery_status") != DELIVERY_FAILED:
            continue
        attempts = int(payload.get("attempts") or 1)
        if attempts >= max_attempts:
            continue
        recipient = payload.get("recipient") or payload.get("client_email")
        if not recipient:
            continue

        status, error = mailer.send(
            OutboundEmail(
                to=recipient,
                subject=payload.get("subject", "Invoice reminder"),
                body=payload.get("body", ""),
            )
        )
        repo.update_audit_payload(
            row["event_key"],
            {
                "delivery_status": status,
                "delivery_error": error,
                "attempts": attempts + 1,
                "last_attempt_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        results.append(
            {
                "event_key": row["event_key"],
                "recipient": recipient,
                "attempts": attempts + 1,
                "delivery_status": status,
                "delivery_error": error,
            }
        )
    return results


def generate_weekly_executive_digest(
    conn: sqlite3.Connection,
    tenant_id: str,
    as_of_date_iso: str,
) -> WeeklyDigestReport:
    """Computes Monday 8:00 AM Cash-Flow Digest for the business owner."""
    repo = SheetFlowRepository(conn)
    tenant = repo.get_tenant(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant '{tenant_id}' not found.")

    invoices = repo.list_invoices_for_tenant(tenant_id)
    symbol = currency_symbol(tenant.currency_code)

    total_billed = 0
    total_collected = 0
    total_outstanding = 0
    total_overdue = 0
    paid_count = 0
    outstanding_count = 0
    overdue_count = 0

    for inv in invoices:
        if inv.status == "CANCELLED":
            continue
        total_billed += inv.total_minor
        if inv.status == "PAID":
            paid_count += 1
            total_collected += inv.total_minor
        elif inv.status in ("SENT", "OVERDUE", "READY_TO_BILL"):
            outstanding_count += 1
            total_outstanding += inv.total_minor
            if inv.status == "OVERDUE":
                overdue_count += 1
                total_overdue += inv.total_minor

    summary = (
        f"[{tenant.business_name} — Weekly Cash-Flow Digest ({as_of_date_iso})] "
        f"Billed: {format_major_currency(total_billed, symbol)} | "
        f"Collected: {format_major_currency(total_collected, symbol)} ({paid_count} paid) | "
        f"Outstanding: {format_major_currency(total_outstanding, symbol)} ({outstanding_count} open, "
        f"{overdue_count} overdue totaling {format_major_currency(total_overdue, symbol)})"
    )

    return WeeklyDigestReport(
        tenant_id=tenant.id,
        business_name=tenant.business_name,
        as_of_date=as_of_date_iso,
        total_invoices_count=len(invoices),
        paid_count=paid_count,
        outstanding_count=outstanding_count,
        overdue_count=overdue_count,
        total_billed_minor=total_billed,
        total_collected_minor=total_collected,
        total_outstanding_minor=total_outstanding,
        total_overdue_minor=total_overdue,
        formatted_summary=summary,
    )
