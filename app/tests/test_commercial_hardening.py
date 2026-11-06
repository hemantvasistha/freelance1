"""Regression tests for the commercial-hardening pass.

Covers:
* a re-ingested invoice row reuses its Razorpay link instead of creating a second one,
* payments in the wrong currency are refused (both reconciliation paths),
* onboarding refuses an unsupported currency,
* the owner ops endpoint is admin-only and reports real exceptions,
* the landing page is actually served by the app.
"""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.database import get_db_connection, init_db
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.main import app
from src.models import SheetFlowRepository
from src.onboarding import onboard_tenant
from src.reconciliation.webhooks import compute_payment_signature, verify_and_reconcile_payment
from src.schemas import PaymentWebhookPayload, SheetRowWebhookPayload

TENANT = "tnt_hardening"
SECRET = "sec_hardening_2026"
ADMIN_TOKEN = "ops-admin-token"


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "hardening.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        self._env = {
            key: os.environ.get(key)
            for key in (
                "SHEETFLOW_DB_PATH",
                "SHEETFLOW_PDF_DIR",
                "SHEETFLOW_ADMIN_TOKEN",
                "SHEETFLOW_PUBLIC_BASE_URL",
                "DRY_RUN",
            )
        }
        os.environ["SHEETFLOW_DB_PATH"] = self.db
        os.environ["SHEETFLOW_PDF_DIR"] = self.pdf_dir
        os.environ["SHEETFLOW_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ["SHEETFLOW_PUBLIC_BASE_URL"] = "https://billing.example.com"
        os.environ["DRY_RUN"] = "true"
        init_db(self.db)
        with get_db_connection(self.db) as conn:
            SheetFlowRepository(conn).upsert_tenant(
                tenant_id=TENANT,
                business_name="Hardening Traders",
                home_state="Haryana",
                google_sheet_id="sheet_h",
                webhook_secret=SECRET,
                upi_vpa="hardening@okaxis",
            )
        self.client = TestClient(app)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def payload(self, invoice_number: str = "INV-H-001") -> SheetRowWebhookPayload:
        return SheetRowWebhookPayload(
            tenant_id=TENANT,
            sheet_row_number=2,
            invoice_number=invoice_number,
            client_name="Apex Supplies",
            client_email="accounts@apexsupplies.in",
            client_state="Haryana",
            item_description="Monthly retainer",
            subtotal_major="3500.00",
            tax_rate_pct="18.00",
            issue_date="2026-11-06",
            due_date="2026-11-13",
            status="READY_TO_BILL",
        )


class TestPaymentLinkReuse(BaseCase):
    def test_reingesting_a_row_creates_only_one_gateway_link(self):
        calls = []

        def provider(tenant, invoice):
            calls.append(invoice.invoice_number)
            return ("https://rzp.example/L/one", "rzp_link_1")

        with get_db_connection(self.db) as conn:
            first = process_sheet_row_webhook(
                conn, self.payload(), self.pdf_dir, "https://billing.example.com",
                payment_link_provider=provider,
            )
            self.assertTrue(first.created_new)
            self.assertEqual(first.payment_url, "https://rzp.example/L/one")

            second = process_sheet_row_webhook(
                conn, self.payload(), self.pdf_dir, "https://billing.example.com",
                payment_link_provider=provider,
            )
            self.assertFalse(second.created_new)
            # The provider must not be called a second time for the same invoice.
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.payment_url, "https://rzp.example/L/one")

            invoice = SheetFlowRepository(conn).get_invoice_by_number(TENANT, "INV-H-001")
        self.assertEqual(invoice.payment_url, "https://rzp.example/L/one")
        self.assertEqual(invoice.payment_gateway_ref, "rzp_link_1")


class TestCurrencyValidation(BaseCase):
    def test_onboarding_refuses_an_unsupported_currency(self):
        with self.assertRaises(ValueError):
            onboard_tenant(
                self.db,
                tenant_id="tnt_bad_currency",
                business_name="Bad Currency Co",
                home_state="Haryana",
                google_sheet_id="sheet_x",
                currency_code="XYZ",
            )

    def test_payment_in_the_wrong_currency_is_refused(self):
        with get_db_connection(self.db) as conn:
            process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)

            payload = PaymentWebhookPayload(
                tenant_id=TENANT,
                invoice_number="INV-H-001",
                gateway_payment_id="pay_wrong_currency",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-11T10:00:00Z",
                currency="USD",
                signature=compute_payment_signature(
                    SECRET, TENANT, "INV-H-001", "pay_wrong_currency", 413000
                ),
            )
            with self.assertRaises(ValueError) as ctx:
                verify_and_reconcile_payment(conn, payload)
            self.assertIn("Currency mismatch", str(ctx.exception))

            invoice = SheetFlowRepository(conn).get_invoice_by_number(TENANT, "INV-H-001")
            self.assertEqual(invoice.status, "SENT", "a mismatched payment must not mark PAID")

    def test_matching_currency_is_accepted(self):
        with get_db_connection(self.db) as conn:
            process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)
            payload = PaymentWebhookPayload(
                tenant_id=TENANT,
                invoice_number="INV-H-001",
                gateway_payment_id="pay_right_currency",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-11T10:00:00Z",
                currency="INR",
                signature=compute_payment_signature(
                    SECRET, TENANT, "INV-H-001", "pay_right_currency", 413000
                ),
            )
            result = verify_and_reconcile_payment(conn, payload)
            self.assertEqual(result.status, "PAID")


class TestOwnerOpsView(BaseCase):
    def test_ops_endpoint_requires_the_admin_token(self):
        self.assertEqual(self.client.get("/api/v1/admin/ops").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/v1/admin/ops", headers={"X-SheetFlow-Admin-Token": "wrong"}
            ).status_code,
            401,
        )

    def test_ops_reports_real_state_and_exceptions(self):
        with get_db_connection(self.db) as conn:
            process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)
            payload = PaymentWebhookPayload(
                tenant_id=TENANT,
                invoice_number="INV-H-001",
                gateway_payment_id="pay_ops",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-11T10:00:00Z",
                signature=compute_payment_signature(
                    SECRET, TENANT, "INV-H-001", "pay_ops", 413000
                ),
            )
            verify_and_reconcile_payment(conn, payload)

        response = self.client.get(
            "/api/v1/admin/ops", headers={"X-SheetFlow-Admin-Token": ADMIN_TOKEN}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["active_tenants"], 1)
        self.assertEqual(body["invoices_by_status"]["PAID"]["count"], 1)
        self.assertEqual(body["outstanding_total_minor"], 0)
        # The PAID change has not been pulled into the sheet yet: that is the exception.
        self.assertEqual(body["exceptions"]["sheet_updates_not_yet_pulled"], 1)
        self.assertTrue(body["needs_attention"])


class TestLandingPage(BaseCase):
    def test_landing_page_is_served_with_pricing(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("₹999", response.text)
        # No fabricated social proof.
        self.assertNotIn("testimonial", response.text.lower())


if __name__ == "__main__":
    unittest.main()
