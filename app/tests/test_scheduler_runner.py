"""Unattended runners: daily reminders across all tenants, weekly digest delivery."""
import os
import sqlite3
import tempfile
import unittest

from src.database import get_db_connection, init_db
from src.dunning.runner import retry_failed, run_daily, run_weekly_digest
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.models import SheetFlowRepository
from src.notifications.mailer import RecordingMailer
from src.schemas import SheetRowWebhookPayload


def _payload(tenant_id: str, invoice_number: str = "INV-100") -> SheetRowWebhookPayload:
    return SheetRowWebhookPayload(
        tenant_id=tenant_id,
        sheet_row_number=2,
        invoice_number=invoice_number,
        client_name="Sunrise Dental Clinic",
        client_email="front.desk@sunrisedental.in",
        client_phone="+919822001122",
        client_state="Maharashtra",
        item_description="Invoicing automation retainer",
        subtotal_major="3200.00",
        tax_rate_pct="18.00",
        issue_date="2026-11-06",
        due_date="2026-11-13",
        status="READY_TO_BILL",
    )


class TestRunners(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "clients.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        init_db(self.db)
        with get_db_connection(self.db) as conn:
            repo = SheetFlowRepository(conn)
            repo.upsert_tenant(
                tenant_id="tnt_client01",
                business_name="Client One",
                home_state="Maharashtra",
                google_sheet_id="sheet_1",
                webhook_secret="secret-one",
                upi_vpa="client1@okaxis",
                owner_email="owner@clientone.in",
            )
            repo.upsert_tenant(
                tenant_id="tnt_client02",
                business_name="Client Two",
                home_state="Karnataka",
                google_sheet_id="sheet_2",
                webhook_secret="secret-two",
            )
            process_sheet_row_webhook(conn, _payload("tnt_client01"), self.pdf_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_run_covers_every_tenant(self):
        mailer = RecordingMailer()
        report = run_daily(self.db, as_of_date="2026-11-10", mailer=mailer)
        self.assertEqual(report["tenants_processed"], 2)
        self.assertEqual(report["dispatched_count"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["reminders"][0]["stage"], "T_MINUS_3")
        self.assertEqual(report["reminders"][0]["delivery_status"], "DRY_RUN")
        self.assertEqual(report["reminders"][0]["tenant_id"], "tnt_client01")

        # Idempotent: the audit key stops a second identical reminder.
        again = run_daily(self.db, as_of_date="2026-11-10", mailer=mailer)
        self.assertEqual(again["dispatched_count"], 0)

    def test_weekly_digest_emails_owner_and_skips_tenants_without_one(self):
        mailer = RecordingMailer()
        report = run_weekly_digest(self.db, as_of_date="2026-11-09", mailer=mailer)
        self.assertEqual(report["digest_count"], 2)

        by_tenant = {item["tenant_id"]: item for item in report["digests"]}
        self.assertEqual(by_tenant["tnt_client01"]["recipient"], "owner@clientone.in")
        self.assertEqual(by_tenant["tnt_client01"]["delivery_status"], "DRY_RUN")
        self.assertEqual(by_tenant["tnt_client02"]["delivery_status"], "SKIPPED")
        self.assertIn("no owner email", by_tenant["tnt_client02"]["delivery_error"])
        self.assertEqual(report["sent_count"], 1)
        self.assertEqual(len(mailer.sent), 1)
        self.assertIn("Client One", mailer.sent[0].subject)
        self.assertIn("3,776", mailer.sent[0].body)


class FailingMailer:
    """Simulates an SMTP outage: every send reports FAILED."""

    def __init__(self):
        self.attempts = 0

    def send(self, email):  # noqa: ANN001 - duck-typed Mailer
        self.attempts += 1
        return "FAILED", "smtp connection refused"


class TestFailedDeliveryRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "clients.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        init_db(self.db)
        with get_db_connection(self.db) as conn:
            SheetFlowRepository(conn).upsert_tenant(
                tenant_id="tnt_client01",
                business_name="Client One",
                home_state="Maharashtra",
                google_sheet_id="sheet_1",
                webhook_secret="secret-one",
                upi_vpa="client1@okaxis",
            )
            process_sheet_row_webhook(conn, _payload("tnt_client01"), self.pdf_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_send_is_retried_and_not_duplicated(self):
        failing = FailingMailer()
        first = run_daily(self.db, as_of_date="2026-11-10", mailer=failing)
        self.assertEqual(first["dispatched_count"], 1)
        self.assertEqual(first["reminders"][0]["delivery_status"], "FAILED")
        self.assertEqual(first["retried_count"], 0, "nothing to retry yet")

        recovering = RecordingMailer()
        retry = retry_failed(self.db, mailer=recovering)
        self.assertEqual(retry["retried_count"], 1)
        self.assertEqual(retry["retries"][0]["delivery_status"], "DRY_RUN")
        self.assertEqual(retry["retries"][0]["attempts"], 2)
        self.assertEqual(len(recovering.sent), 1)
        self.assertIn("INV-100", recovering.sent[0].subject)

        # Delivered now, so neither the daily run nor another retry sends again.
        again = retry_failed(self.db, mailer=RecordingMailer())
        self.assertEqual(again["retried_count"], 0)
        rerun = run_daily(self.db, as_of_date="2026-11-10", mailer=RecordingMailer())
        self.assertEqual(rerun["dispatched_count"], 0)

    def test_retries_stop_after_max_attempts(self):
        run_daily(self.db, as_of_date="2026-11-10", mailer=FailingMailer())
        for _ in range(2):
            retry_failed(self.db, mailer=FailingMailer())
        stuck = retry_failed(self.db, mailer=RecordingMailer())
        self.assertEqual(stuck["retried_count"], 0, "attempt cap reached (1 + 2 = 3)")


class TestOwnerEmailMigration(unittest.TestCase):
    def test_legacy_tenant_table_gains_owner_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE tenants (
                    id TEXT PRIMARY KEY, business_name TEXT NOT NULL, tax_id TEXT,
                    home_state TEXT NOT NULL, currency_code TEXT NOT NULL DEFAULT 'INR',
                    google_sheet_id TEXT NOT NULL, upi_vpa TEXT,
                    webhook_secret TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            init_db(db)

            conn = sqlite3.connect(db)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
            conn.close()
            self.assertIn("owner_email", columns)


if __name__ == "__main__":
    unittest.main()
