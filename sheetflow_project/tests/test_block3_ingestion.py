"""Unit Tests for Block 3 (M3_GOOGLE_SHEETS_BRIDGE)."""
import os
import tempfile
import unittest

from src.database import init_db, get_db_connection
from src.models import SheetFlowRepository
from src.schemas import SheetRowWebhookPayload
from src.ingestion.sheet_sync import process_sheet_row_webhook


class TestBlock3GoogleSheetsIngestion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_end_to_end_sheet_row_ingestion_and_idempotency(self):
        with get_db_connection(self.db_path) as conn:
            repo = SheetFlowRepository(conn)
            repo.upsert_tenant(
                tenant_id="tnt_bahal_01",
                business_name="Bahal Tech Automation",
                home_state="Haryana",
                google_sheet_id="sheet_123",
                webhook_secret="sec_abc",
                tax_id="06ABCDE1234F1Z5",
                upi_vpa="bahaltech@okaxis",
            )

            payload = SheetRowWebhookPayload(
                tenant_id="tnt_bahal_01",
                sheet_row_number=14,
                invoice_number="INV-2026-014",
                client_name="Apex Industrial Supplies",
                client_email="accounts@apexsupplies.in",
                client_phone="+919812345678",
                client_state="Haryana",
                item_description="Monthly Inventory & Billing Automation Retainer (Nov 2026)",
                subtotal_major="3500.00",
                tax_rate_pct="18.00",
                issue_date="2026-11-06",
                due_date="2026-11-13",
                status="READY_TO_BILL",
            )

            res1 = process_sheet_row_webhook(conn, payload, self.tmp_dir.name)
            self.assertTrue(res1.created_new)
            self.assertEqual(res1.status, "SENT")
            self.assertEqual(res1.total_minor, 413000)
            self.assertEqual(res1.total_major, "4130.00")
            self.assertTrue(os.path.exists(res1.pdf_path))

            # Second trigger of the exact same row must be idempotent
            res2 = process_sheet_row_webhook(conn, payload, self.tmp_dir.name)
            self.assertFalse(res2.created_new)
            self.assertEqual(res1.invoice_id, res2.invoice_id)

            events = repo.list_audit_events(res1.invoice_id)
            types = [e["event_type"] for e in events]
            # Generation and delivery are recorded as two distinct facts now.
            self.assertEqual(types, ["INVOICE_GENERATED_AND_SENT", "INVOICE_EMAIL"])
            # No mailer was supplied, so delivery is SKIPPED — never assumed sent.
            self.assertEqual(res1.delivery_status, "SKIPPED")


if __name__ == "__main__":
    unittest.main()
