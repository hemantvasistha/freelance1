"""Unit Tests for Block 2 (M2_PDF_AND_PAYMENT_LINKS)."""
import os
import tempfile
import unittest

from src.database import init_db, get_db_connection
from src.models import SheetFlowRepository
from src.schemas import calculate_gst_breakdown, compute_idempotency_key
from src.billing.payment_links import generate_payment_artifacts
from src.billing.pdf_generator import generate_invoice_pdf


class TestBlock2BillingAndPdf(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_payment_link_and_pdf_generation(self):
        with get_db_connection(self.db_path) as conn:
            repo = SheetFlowRepository(conn)
            tenant = repo.upsert_tenant(
                tenant_id="tnt_bahal_01",
                business_name="Apex Workflow Systems",
                home_state="Haryana",
                google_sheet_id="sheet_001",
                webhook_secret="sec_001",
                tax_id="06AAACA1234A1Z5",
                upi_vpa="apexworkflows@okicici",
            )
            tax = calculate_gst_breakdown("3500.00", "18.00", tenant.home_state, "Haryana")
            idem = compute_idempotency_key(tenant.id, "INV-2026-014", "2026-11-06")
            inv, _ = repo.create_or_get_invoice(
                tenant_id=tenant.id,
                sheet_row_number=14,
                invoice_number="INV-2026-014",
                idempotency_key=idem,
                client_name="Vanguard Logistics Pvt Ltd",
                client_email="finance@vanguardlogistics.in",
                client_phone="+919812345678",
                client_state="Haryana",
                item_description="Automated Ledger-to-Invoice & Payment Chaser Setup",
                subtotal_minor=tax.subtotal_minor,
                tax_rate_bps=tax.tax_rate_bps,
                cgst_minor=tax.cgst_minor,
                sgst_minor=tax.sgst_minor,
                igst_minor=tax.igst_minor,
                total_minor=tax.total_minor,
                issue_date="2026-11-06",
                due_date="2026-11-13",
            )

            artifacts = generate_payment_artifacts(tenant, inv)
            self.assertIsNotNone(artifacts.upi_uri)
            # No hosted checkout exists; the payable link IS the UPI intent.
            self.assertEqual(artifacts.payment_url, artifacts.upi_uri)
            self.assertTrue(artifacts.payment_url.startswith("upi://pay?"))
            self.assertIn("pa=apexworkflows%40okicici", artifacts.upi_uri)
            self.assertIn("am=4130.00", artifacts.upi_uri)

            html_path, pdf_path = generate_invoice_pdf(
                tenant, inv, artifacts.payment_url, self.tmp_dir.name, upi_uri=artifacts.upi_uri
            )
            self.assertTrue(os.path.exists(html_path))
            self.assertTrue(os.path.exists(pdf_path))
            with open(pdf_path, "rb") as f:
                header = f.read(5)
            self.assertEqual(header, b"%PDF-")


if __name__ == "__main__":
    unittest.main()
