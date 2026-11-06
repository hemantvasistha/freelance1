"""Regression tests for the payment ledger, manual reconciliation and guards.

Covers:
* verified gateway payments are appended to a payment history exactly once,
* the admin-only manual UPI path supports full and partial payments, rejects a reused
  reference, and queues the PAID write-back,
* a weekly digest cannot be emailed twice for the same date,
* production startup refuses a configuration that would produce unusable invoices.
"""
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.config import Settings, validate_runtime_settings
from src.database import get_db_connection, init_db
from src.dunning.runner import run_weekly_digest
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.main import app
from src.models import SheetFlowRepository
from src.notifications.mailer import RecordingMailer
from src.reconciliation.webhooks import compute_payment_signature, verify_and_reconcile_payment
from src.schemas import PaymentWebhookPayload, SheetRowWebhookPayload

TENANT = "tnt_ledger"
SECRET = "sec_ledger_2026"
ADMIN_TOKEN = "ledger-admin-token"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        self._env = {
            key: os.environ.get(key)
            for key in (
                "SHEETFLOW_DB_PATH",
                "SHEETFLOW_PDF_DIR",
                "SHEETFLOW_ADMIN_TOKEN",
                "SHEETFLOW_PUBLIC_BASE_URL",
                "SHEETFLOW_ENV",
                "DRY_RUN",
            )
        }
        os.environ["SHEETFLOW_DB_PATH"] = self.db
        os.environ["SHEETFLOW_PDF_DIR"] = self.pdf_dir
        os.environ["SHEETFLOW_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ["SHEETFLOW_PUBLIC_BASE_URL"] = "https://billing.example.com"
        os.environ.pop("SHEETFLOW_ENV", None)
        os.environ["DRY_RUN"] = "true"
        init_db(self.db)
        with get_db_connection(self.db) as conn:
            SheetFlowRepository(conn).upsert_tenant(
                tenant_id=TENANT,
                business_name="Ledger Traders",
                home_state="Haryana",
                google_sheet_id="sheet_l",
                webhook_secret=SECRET,
                upi_vpa="ledger@okaxis",
                owner_email="owner@ledger.in",
            )
        self.client = TestClient(app)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def payload(self, invoice_number="INV-L-001", subtotal="3500.00") -> SheetRowWebhookPayload:
        return SheetRowWebhookPayload(
            tenant_id=TENANT,
            sheet_row_number=2,
            invoice_number=invoice_number,
            client_name="Apex Supplies",
            client_email="accounts@apexsupplies.in",
            client_state="Haryana",
            item_description="Monthly retainer",
            subtotal_major=subtotal,
            tax_rate_pct="18.00",
            issue_date="2026-11-06",
            due_date="2026-11-13",
            status="READY_TO_BILL",
        )

    def create_invoice(self, invoice_number="INV-L-001", subtotal="3500.00"):
        with get_db_connection(self.db) as conn:
            return process_sheet_row_webhook(conn, self.payload(invoice_number, subtotal), self.pdf_dir)

    def manual(self, body):
        return self.client.post(
            "/api/v1/admin/manual-payment",
            json=body,
            headers={"X-SheetFlow-Admin-Token": ADMIN_TOKEN},
        )


class TestGatewayPaymentHistory(LedgerCase):
    def test_verified_payment_is_appended_once(self):
        self.create_invoice()
        with get_db_connection(self.db) as conn:
            payload = PaymentWebhookPayload(
                tenant_id=TENANT,
                invoice_number="INV-L-001",
                gateway_payment_id="pay_gw_1",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-11T10:00:00Z",
                currency="INR",
                signature=compute_payment_signature(
                    SECRET, TENANT, "INV-L-001", "pay_gw_1", 413000
                ),
            )
            verify_and_reconcile_payment(conn, payload)
            verify_and_reconcile_payment(conn, payload)  # replay

            payments = SheetFlowRepository(conn).list_payments(TENANT)
        self.assertEqual(len(payments), 1, "a replayed webhook must not duplicate the payment")
        self.assertEqual(payments[0]["source"], "GATEWAY")
        self.assertEqual(payments[0]["amount_minor"], 413000)
        self.assertEqual(payments[0]["currency"], "INR")
        self.assertEqual(payments[0]["provider_payment_id"], "pay_gw_1")


class TestManualUpiReconciliation(LedgerCase):
    def test_full_manual_payment_marks_paid_and_queues_write_back(self):
        self.create_invoice()
        response = self.manual(
            {
                "tenant_id": TENANT,
                "invoice_number": "INV-L-001",
                "amount_major": "4130.00",
                "reference": "UPI/445566778899",
                "recorded_by": "hemant",
                "note": "bank statement line 42",
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["became_paid"])
        self.assertEqual(body["status"], "PAID")
        self.assertEqual(body["outstanding_minor"], 0)

        with get_db_connection(self.db) as conn:
            repo = SheetFlowRepository(conn)
            payments = repo.list_payments(TENANT)
            pending = conn.execute(
                "SELECT invoice_number, status FROM sheet_sync_outbox WHERE delivered_at IS NULL"
            ).fetchall()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["source"], "MANUAL")
        self.assertEqual(payments[0]["recorded_by"], "hemant")
        self.assertEqual([row["status"] for row in pending], ["PAID"])

    def test_partial_payment_keeps_invoice_open(self):
        self.create_invoice()
        response = self.manual(
            {
                "tenant_id": TENANT,
                "invoice_number": "INV-L-001",
                "amount_major": "1000.00",
                "reference": "UPI/PART-1",
                "recorded_by": "hemant",
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["became_paid"])
        self.assertEqual(body["status"], "SENT")
        self.assertEqual(body["outstanding_minor"], 313000)

        with get_db_connection(self.db) as conn:
            status = conn.execute(
                "SELECT COUNT(*) AS n FROM sheet_sync_outbox WHERE delivered_at IS NULL"
            ).fetchone()["n"]
        self.assertEqual(status, 0, "an unpaid remainder must not tell the sheet it is PAID")

    def test_reused_reference_is_rejected(self):
        self.create_invoice()
        body = {
            "tenant_id": TENANT,
            "invoice_number": "INV-L-001",
            "amount_major": "500.00",
            "reference": "UPI/SAME",
            "recorded_by": "hemant",
        }
        self.assertEqual(self.manual(body).status_code, 200)
        self.assertEqual(self.manual(body).status_code, 409)

    def test_manual_payment_requires_the_admin_token(self):
        self.create_invoice()
        response = self.client.post(
            "/api/v1/admin/manual-payment",
            json={
                "tenant_id": TENANT,
                "invoice_number": "INV-L-001",
                "amount_major": "4130.00",
                "reference": "UPI/X",
                "recorded_by": "someone",
            },
        )
        self.assertEqual(response.status_code, 401)


class TestDigestDeduplication(LedgerCase):
    def test_digest_is_not_emailed_twice_for_the_same_date(self):
        mailer = RecordingMailer()
        first = run_weekly_digest(self.db, as_of_date="2026-11-09", mailer=mailer)
        self.assertEqual(first["sent_count"], 1)
        self.assertEqual(len(mailer.sent), 1)

        second = run_weekly_digest(self.db, as_of_date="2026-11-09", mailer=mailer)
        self.assertEqual(len(mailer.sent), 1, "the customer must not get two digests")
        self.assertEqual(second["digests"][0]["delivery_status"], "DUPLICATE_SKIPPED")

        # A different week is a different period and must still be delivered.
        third = run_weekly_digest(self.db, as_of_date="2026-11-16", mailer=mailer)
        self.assertEqual(len(mailer.sent), 2)
        self.assertEqual(third["sent_count"], 1)


class TestProductionConfigGuard(unittest.TestCase):
    def test_development_only_warns(self):
        errors, warnings = validate_runtime_settings(Settings(public_base_url="", admin_token=""))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)

    def test_production_refuses_missing_public_url_and_admin_token(self):
        os.environ["SHEETFLOW_ENV"] = "production"
        try:
            errors, _ = validate_runtime_settings(Settings(public_base_url="", admin_token=""))
            self.assertEqual(len(errors), 2)
            errors, warnings = validate_runtime_settings(
                Settings(
                    public_base_url="https://billing.example.com",
                    admin_token="secret",
                    dry_run=True,
                    smtp_password="",
                )
            )
            self.assertEqual(errors, [])
            self.assertEqual(len(warnings), 2)
        finally:
            os.environ.pop("SHEETFLOW_ENV", None)


if __name__ == "__main__":
    unittest.main()
