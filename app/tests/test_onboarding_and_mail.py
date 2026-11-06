"""Onboarding CLI helpers and email delivery configuration."""
import os
import tempfile
import unittest

from src.billing.document_links import (
    build_invoice_pdf_url,
    compute_invoice_pdf_token,
    verify_invoice_pdf_token,
)
from src.config import Settings
from src.notifications.mailer import (
    DELIVERY_DRY_RUN,
    MailerNotConfigured,
    OutboundEmail,
    RecordingMailer,
    SmtpMailer,
    build_mailer,
)
from src.onboarding import list_tenants, onboard_tenant


class TestOnboarding(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "onboarding.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_onboarding_creates_tenant_and_config(self):
        result = onboard_tenant(
            self.db_path,
            tenant_id="tnt_client02",
            business_name="Client Two Traders",
            home_state="Maharashtra",
            google_sheet_id="sheet_002",
            upi_vpa="client2@okaxis",
            public_base_url="https://billing.example.com",
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["secret_rotated"])
        self.assertEqual(result["tenant"]["home_state"], "Maharashtra")
        self.assertEqual(result["tenant"]["currency_code"], "INR")
        self.assertIn(f'WEBHOOK_SECRET: "{result["webhook_secret"]}"', result["apps_script_config"])
        self.assertIn('TENANT_ID: "tnt_client02"', result["apps_script_config"])
        self.assertIn("API_BASE_URL: \"https://billing.example.com\"", result["apps_script_config"])

        listed = list_tenants(self.db_path)
        self.assertEqual([t["tenant_id"] for t in listed], ["tnt_client02"])

    def test_reonboard_rotates_secret_and_reports_it(self):
        first = onboard_tenant(
            self.db_path,
            tenant_id="tnt_client02",
            business_name="Client Two Traders",
            home_state="Maharashtra",
            google_sheet_id="sheet_002",
        )
        second = onboard_tenant(
            self.db_path,
            tenant_id="tnt_client02",
            business_name="Client Two Traders",
            home_state="Maharashtra",
            google_sheet_id="sheet_002",
        )
        self.assertFalse(second["created"])
        self.assertTrue(second["secret_rotated"])
        self.assertNotEqual(first["webhook_secret"], second["webhook_secret"])

        reused = onboard_tenant(
            self.db_path,
            tenant_id="tnt_client02",
            business_name="Client Two Traders",
            home_state="Maharashtra",
            google_sheet_id="sheet_002",
            webhook_secret=second["webhook_secret"],
        )
        self.assertFalse(reused["secret_rotated"])


class TestMailer(unittest.TestCase):
    def test_dry_run_records_instead_of_sending(self):
        mailer = build_mailer(Settings(dry_run=True))
        self.assertIsInstance(mailer, RecordingMailer)
        status, error = mailer.send(OutboundEmail(to="a@b.in", subject="Hi", body="Body"))
        self.assertEqual(status, DELIVERY_DRY_RUN)
        self.assertIsNone(error)
        self.assertEqual(mailer.sent[0].to, "a@b.in")

    def test_live_mode_without_password_is_refused(self):
        with self.assertRaises(MailerNotConfigured):
            build_mailer(Settings(dry_run=False, smtp_password=""))

    def test_live_mode_with_password_builds_smtp_mailer(self):
        mailer = build_mailer(
            Settings(dry_run=False, smtp_password="secret", smtp_user="billing@example.com")
        )
        self.assertIsInstance(mailer, SmtpMailer)
        self.assertEqual(mailer.sender, "billing@example.com")


class TestInvoicePdfLinks(unittest.TestCase):
    def test_url_roundtrip_and_tamper_rejection(self):
        url = build_invoice_pdf_url("https://billing.example.com/", "s3cret", "tnt_1", "INV-1")
        self.assertTrue(url.startswith("https://billing.example.com/api/v1/invoices/tnt_1/INV-1/pdf?token="))
        token = url.rsplit("token=", 1)[1]
        self.assertTrue(verify_invoice_pdf_token("s3cret", "tnt_1", "INV-1", token))
        self.assertFalse(verify_invoice_pdf_token("s3cret", "tnt_1", "INV-2", token))
        self.assertFalse(verify_invoice_pdf_token("other", "tnt_1", "INV-1", token))
        self.assertEqual(
            build_invoice_pdf_url("", "s3cret", "tnt_1", "INV-1"),
            "",
            "no public base URL means no link should be emitted",
        )
        self.assertEqual(compute_invoice_pdf_token("s3cret", "tnt_1", "INV-1"), token)


if __name__ == "__main__":
    unittest.main()
