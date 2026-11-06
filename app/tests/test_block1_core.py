"""Unit Tests for Block 1 (M1_CORE_SCHEMA_AND_CONFIG)."""
import os
import tempfile
import unittest
from pydantic import ValidationError

from src.database import init_db, get_db_connection
from src.models import SheetFlowRepository
from src.schemas import (
    calculate_gst_breakdown,
    compute_idempotency_key,
    format_major_currency,
    SheetRowWebhookPayload,
    to_minor_units,
)


class TestBlock1CoreSchemaAndTax(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        init_db(self.tmp_db.name)

    def tearDown(self):
        if os.path.exists(self.tmp_db.name):
            os.unlink(self.tmp_db.name)

    def test_intra_state_gst_split_haryana(self):
        # ₹3,500.00 at 18% GST inside Haryana -> 9% CGST (₹315 = 31500 paise) + 9% SGST (31500 paise)
        res = calculate_gst_breakdown("3500.00", "18.00", "Haryana", "haryana")
        self.assertTrue(res.is_intra_state)
        self.assertEqual(res.subtotal_minor, 350000)
        self.assertEqual(res.cgst_minor, 31500)
        self.assertEqual(res.sgst_minor, 31500)
        self.assertEqual(res.igst_minor, 0)
        self.assertEqual(res.total_minor, 413000)
        self.assertEqual(format_major_currency(res.total_minor), "₹4,130.00")

    def test_inter_state_igst_split(self):
        # ₹3,500.00 at 18% GST from Haryana to Delhi -> 18% IGST (₹630 = 63000 paise)
        res = calculate_gst_breakdown("3500.00", "18.00", "Haryana", "Delhi")
        self.assertFalse(res.is_intra_state)
        self.assertEqual(res.cgst_minor, 0)
        self.assertEqual(res.sgst_minor, 0)
        self.assertEqual(res.igst_minor, 63000)
        self.assertEqual(res.total_minor, 413000)

    def test_fractional_paise_rounding_half_up(self):
        # ₹199.99 at 18% intra-state -> subtotal = 19999 paise, 9% = 1799.91 -> 1800 paise
        res = calculate_gst_breakdown("199.99", "18.00", "Haryana", "Haryana")
        self.assertEqual(res.subtotal_minor, 19999)
        self.assertEqual(res.cgst_minor, 1800)
        self.assertEqual(res.sgst_minor, 1800)
        self.assertEqual(res.total_minor, 23599)

    def test_pydantic_validation_guards(self):
        with self.assertRaises(ValidationError):
            SheetRowWebhookPayload(
                tenant_id="tnt_01",
                sheet_row_number=2,
                invoice_number="INV-001",
                client_name="Bad Date Corp",
                client_email="accounts@baddate.com",
                item_description="Test",
                subtotal_major="1000.00",
                tax_rate_pct="18.00",
                issue_date="2026-11-10",
                due_date="2026-11-05",  # Earlier than issue_date!
            )

    def test_sqlite_idempotency_and_audit_log(self):
        with get_db_connection(self.tmp_db.name) as conn:
            repo = SheetFlowRepository(conn)
            tenant = repo.upsert_tenant(
                tenant_id="tnt_demo_001",
                business_name="Bahal Automation Labs",
                home_state="Haryana",
                google_sheet_id="sheet_abc123",
                webhook_secret="sec_xyz",
                tax_id="06ABCDE1234F1Z5",
                upi_vpa="bahallabs@okaxis",
            )
            self.assertEqual(tenant.home_state, "Haryana")

            idem_key = compute_idempotency_key(tenant.id, "INV-2026-014", "2026-11-06")
            tax = calculate_gst_breakdown("3500.00", "18.00", tenant.home_state, "Haryana")

            inv1, created1 = repo.create_or_get_invoice(
                tenant_id=tenant.id,
                sheet_row_number=14,
                invoice_number="INV-2026-014",
                idempotency_key=idem_key,
                client_name="Apex Industrial Supplies",
                client_email="accounts@apexsupplies.in",
                client_phone="+919812345678",
                client_state="Haryana",
                item_description="Monthly Inventory & Billing Automation Retainer",
                subtotal_minor=tax.subtotal_minor,
                tax_rate_bps=tax.tax_rate_bps,
                cgst_minor=tax.cgst_minor,
                sgst_minor=tax.sgst_minor,
                igst_minor=tax.igst_minor,
                total_minor=tax.total_minor,
                issue_date="2026-11-06",
                due_date="2026-11-13",
            )
            self.assertTrue(created1)

            # Duplicate webhook call must return existing invoice without error
            inv2, created2 = repo.create_or_get_invoice(
                tenant_id=tenant.id,
                sheet_row_number=14,
                invoice_number="INV-2026-014",
                idempotency_key=idem_key,
                client_name="Apex Industrial Supplies",
                client_email="accounts@apexsupplies.in",
                client_phone="+919812345678",
                client_state="Haryana",
                item_description="Monthly Inventory & Billing Automation Retainer",
                subtotal_minor=tax.subtotal_minor,
                tax_rate_bps=tax.tax_rate_bps,
                cgst_minor=tax.cgst_minor,
                sgst_minor=tax.sgst_minor,
                igst_minor=tax.igst_minor,
                total_minor=tax.total_minor,
                issue_date="2026-11-06",
                due_date="2026-11-13",
            )
            self.assertFalse(created2)
            self.assertEqual(inv1.id, inv2.id)

            # Audit event idempotency
            first_log = repo.record_audit_event_if_new(
                inv1.id, "INVOICE_CREATED", f"create:{idem_key}", {"total": tax.total_minor}
            )
            second_log = repo.record_audit_event_if_new(
                inv1.id, "INVOICE_CREATED", f"create:{idem_key}", {"total": tax.total_minor}
            )
            self.assertTrue(first_log)
            self.assertFalse(second_log)


if __name__ == "__main__":
    unittest.main()
