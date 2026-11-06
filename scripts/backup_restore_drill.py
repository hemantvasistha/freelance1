"""Executed backup/restore drill.

Creates a database with real rows, takes a hot backup with SQLite's backup API, restores
it into a fresh file, and compares row counts and financial totals. If this script fails,
the documented backup procedure is not trustworthy.

    py sheetflow_project/.venv/Scripts/python.exe scripts/backup_restore_drill.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "sheetflow_project"
sys.path.insert(0, str(PROJECT_ROOT))

from src.database import get_db_connection, init_db  # noqa: E402
from src.ingestion.sheet_sync import process_sheet_row_webhook  # noqa: E402
from src.models import SheetFlowRepository  # noqa: E402
from src.schemas import SheetRowWebhookPayload  # noqa: E402

TABLES = ("tenants", "invoices", "payments", "audit_events", "sheet_sync_outbox")


def snapshot(db_path: str) -> dict:
    with get_db_connection(db_path) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in TABLES
        }
        totals = {
            "invoice_total_minor": conn.execute(
                "SELECT COALESCE(SUM(total_minor), 0) AS t FROM invoices"
            ).fetchone()["t"],
            "payment_total_minor": conn.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) AS t FROM payments"
            ).fetchone()["t"],
        }
    return {"counts": counts, "totals": totals}


def hot_backup(source: str, destination: str) -> None:
    """SQLite's online backup API: safe while the service is running.

    Connections are closed explicitly: a `with sqlite3.connect(...)` block commits but
    does not close, and on Windows the leaked handle then blocks the backup file from
    being moved or deleted.
    """
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a backup/restore drill")
    parser.add_argument(
        "--source-db",
        default="",
        help="drill an existing database (e.g. a live deployment) instead of a synthetic one",
    )
    args = parser.parse_args()

    if args.source_db:
        with tempfile.TemporaryDirectory() as tmp:
            backup_db = os.path.join(tmp, "backup.db")
            restored_db = os.path.join(tmp, "restored.db")
            before = snapshot(args.source_db)
            hot_backup(args.source_db, backup_db)
            hot_backup(backup_db, restored_db)
            after = snapshot(restored_db)
            report = {
                "source": args.source_db,
                "backup_bytes": os.path.getsize(backup_db),
                "before": before,
                "restored": after,
                "match": before == after,
            }
            print(json.dumps(report, indent=2))
            return 0 if before == after else 1

    with tempfile.TemporaryDirectory() as tmp:
        live_db = os.path.join(tmp, "clients.db")
        backup_db = os.path.join(tmp, "clients-backup.db")
        restored_db = os.path.join(tmp, "clients-restored.db")
        pdf_dir = os.path.join(tmp, "pdfs")
        init_db(live_db)

        with get_db_connection(live_db) as conn:
            repo = SheetFlowRepository(conn)
            repo.upsert_tenant(
                tenant_id="tnt_drill",
                business_name="Drill Traders",
                home_state="Haryana",
                google_sheet_id="sheet_drill",
                webhook_secret="sec_drill",
                upi_vpa="drill@okaxis",
                owner_email="owner@drill.in",
            )
            for index in range(5):
                number = f"INV-DRILL-{index:03d}"
                result = process_sheet_row_webhook(
                    conn,
                    SheetRowWebhookPayload(
                        tenant_id="tnt_drill",
                        sheet_row_number=index + 2,
                        invoice_number=number,
                        client_name="Drill Client",
                        client_email="client@example.com",
                        client_state="Haryana",
                        item_description="Retainer",
                        subtotal_major="3500.00",
                        tax_rate_pct="18.00",
                        issue_date="2026-11-06",
                        due_date="2026-11-13",
                        status="READY_TO_BILL",
                    ),
                    pdf_dir,
                    "https://billing.example.com",
                )
                if index == 0:
                    repo.record_payment(
                        tenant_id="tnt_drill",
                        invoice=repo.get_invoice_by_number("tnt_drill", number),
                        amount_minor=result.total_minor,
                        currency="INR",
                        source="MANUAL",
                        provider="upi",
                        provider_payment_id="UPI/DRILL-1",
                        recorded_by="drill",
                    )

        before = snapshot(live_db)
        hot_backup(live_db, backup_db)
        hot_backup(backup_db, restored_db)
        after = snapshot(restored_db)

        report = {
            "backup_bytes": os.path.getsize(backup_db),
            "before": before,
            "restored": after,
            "match": before == after,
        }
        print(json.dumps(report, indent=2))
        return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
