"""Unattended runs: daily reminders for every tenant, plus the weekly cash-flow digest.

Before this existed the only trigger was the Google Sheets menu, so a client whose
sheet nobody opened never received a reminder. These functions are safe to call from
cron, from a systemd timer, or from the in-process loop in `src/main.py`, and are
idempotent: the dunning audit key stops the same stage from being sent twice.

Both return a JSON-able report instead of printing, so the caller decides how to log.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from src.config import get_settings
from src.database import get_db_connection, init_db
from src.dunning.scheduler import (
    evaluate_and_dispatch_reminders,
    generate_weekly_executive_digest,
    retry_failed_reminders,
)
from src.models import SheetFlowRepository
from src.notifications.mailer import Mailer, OutboundEmail, build_mailer


def _mailer_or_none(mailer: Mailer | None) -> Mailer | None:
    return mailer if mailer is not None else build_mailer(get_settings())


def run_daily(
    db_path: str,
    as_of_date: str | None = None,
    mailer: Mailer | None = None,
) -> dict:
    """Dispatches due/overdue reminders for every active tenant."""
    as_of = as_of_date or date.today().isoformat()
    active_mailer = _mailer_or_none(mailer)
    init_db(db_path)

    reminders: list[dict] = []
    tenants_processed = 0
    errors: list[dict] = []

    with get_db_connection(db_path) as conn:
        tenants = SheetFlowRepository(conn).list_tenants()
        # Retry yesterday's failures first, so today's dispatch cannot burn an attempt on
        # a message that was only just composed.
        retried = retry_failed_reminders(conn, active_mailer) if active_mailer is not None else []
        for tenant in tenants:
            if not tenant.is_active:
                continue
            tenants_processed += 1
            try:
                actions = evaluate_and_dispatch_reminders(
                    conn, tenant.id, as_of, mailer=active_mailer
                )
            except Exception as exc:  # noqa: BLE001 - one bad tenant must not stop the run
                errors.append(
                    {"tenant_id": tenant.id, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            for action in actions:
                entry = asdict(action)
                entry["tenant_id"] = tenant.id
                reminders.append(entry)

    failed = [item for item in reminders if item.get("delivery_status") == "FAILED"]
    return {
        "as_of_date": as_of,
        "tenants_processed": tenants_processed,
        "dispatched_count": len(reminders),
        "delivery_failed_count": len(failed),
        "retried_count": len(retried),
        "retries": retried,
        "reminders": reminders,
        "errors": errors,
    }


def retry_failed(db_path: str, mailer: Mailer | None = None) -> dict:
    """Resends reminders whose previous delivery failed (bounded by attempts)."""
    active_mailer = _mailer_or_none(mailer)
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        results = retry_failed_reminders(conn, active_mailer) if active_mailer else []
    return {"retried_count": len(results), "retries": results}


def run_weekly_digest(
    db_path: str,
    as_of_date: str | None = None,
    mailer: Mailer | None = None,
) -> dict:
    """Builds and emails the Monday cash-flow digest to each tenant's owner."""
    as_of = as_of_date or date.today().isoformat()
    settings = get_settings()
    active_mailer = _mailer_or_none(mailer)
    init_db(db_path)

    digests: list[dict] = []
    errors: list[dict] = []

    with get_db_connection(db_path) as conn:
        repo = SheetFlowRepository(conn)
        for tenant in repo.list_tenants():
            if not tenant.is_active:
                continue
            try:
                report = generate_weekly_executive_digest(conn, tenant.id, as_of)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"tenant_id": tenant.id, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            recipient = tenant.owner_email or settings.owner_email
            entry = asdict(report)
            entry["recipient"] = recipient
            if not recipient:
                entry["delivery_status"] = "SKIPPED"
                entry["delivery_error"] = (
                    "no owner email: pass --owner-email at onboarding or set SHEETFLOW_OWNER_EMAIL"
                )
                digests.append(entry)
                continue

            if active_mailer is None:
                entry["delivery_status"] = "SKIPPED"
                entry["delivery_error"] = "mailer not configured"
                digests.append(entry)
                continue

            period_key = as_of
            if not repo.claim_notification(tenant.id, "WEEKLY_DIGEST", period_key):
                # Re-running the job (or a retried worker) must not email the same
                # summary twice.
                entry["delivery_status"] = "DUPLICATE_SKIPPED"
                entry["delivery_error"] = "digest already delivered for this date"
                digests.append(entry)
                continue

            status, error = active_mailer.send(
                OutboundEmail(
                    to=recipient,
                    subject=f"[Cash-Flow Digest] {tenant.business_name} — {as_of}",
                    body=(
                        f"Weekly cash-flow digest for {tenant.business_name} "
                        f"({as_of})\n\n{report.formatted_summary}\n\n"
                        "Reply to this email if a figure looks wrong.\n"
                    ),
                )
            )
            if status == "FAILED":
                # Let a later run retry instead of losing the digest for the week.
                repo.release_notification(tenant.id, "WEEKLY_DIGEST", period_key)
            else:
                repo.set_notification_status(tenant.id, "WEEKLY_DIGEST", period_key, status)
            entry["delivery_status"] = status
            entry["delivery_error"] = error
            digests.append(entry)

    return {
        "as_of_date": as_of,
        "digest_count": len(digests),
        "sent_count": sum(1 for item in digests if item["delivery_status"] in ("SENT", "DRY_RUN")),
        "digests": digests,
        "errors": errors,
    }
