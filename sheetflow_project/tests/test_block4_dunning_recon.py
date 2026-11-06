"""Unit Tests for Block 4 (M4_DUNNING_AND_RECONCILIATION)."""
import os
import tempfile
import unittest

from src.database import init_db, get_db_connection
from src.models import SheetFlowRepository
from src.schemas import PaymentWebhookPayload, SheetRowWebhookPayload
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.dunning.scheduler import evaluate_and_dispatch_reminders, generate_weekly_executive_digest
from src.reconciliation.webhooks import compute_payment_signature, verify_and_reconcile_payment


class TestBlock4DunningAndReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dunning_progression_and_payment_reconciliation(self):
        with get_db_connection(self.db_path) as conn:
            repo = SheetFlowRepository(conn)
            tenant = repo.upsert_tenant(
                tenant_id="tnt_bahal_01",
                business_name="Bahal Tech Automation",
                home_state="Haryana",
                google_sheet_id="sheet_123",
                webhook_secret="sec_hmac_secret_2026",
                tax_id="06ABCDE1234F1Z5",
                upi_vpa="bahaltech@okaxis",
            )

            # Create an invoice due on 2026-11-13
            payload = SheetRowWebhookPayload(
                tenant_id=tenant.id,
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
            )
            sync_res = process_sheet_row_webhook(conn, payload, self.tmp_dir.name)

            # 1. Test T-3 Reminder on 2026-11-10
            r_minus_3 = evaluate_and_dispatch_reminders(conn, tenant.id, "2026-11-10")
            self.assertEqual(len(r_minus_3), 1)
            self.assertEqual(r_minus_3[0].stage, "T_MINUS_3")
            # Re-running on same date must send 0 duplicate reminders
            self.assertEqual(len(evaluate_and_dispatch_reminders(conn, tenant.id, "2026-11-10")), 0)

            # 2. Test T+0 Due Today Reminder on 2026-11-13
            r_zero = evaluate_and_dispatch_reminders(conn, tenant.id, "2026-11-13")
            self.assertEqual(len(r_zero), 1)
            self.assertEqual(r_zero[0].stage, "T_ZERO")

            # 3. Test T+7 Overdue Escalation on 2026-11-20
            r_plus_7 = evaluate_and_dispatch_reminders(conn, tenant.id, "2026-11-20")
            self.assertEqual(len(r_plus_7), 1)
            self.assertEqual(r_plus_7[0].stage, "T_PLUS_7")
            self.assertEqual(r_plus_7[0].new_status, "OVERDUE")

            # Check weekly digest while invoice is OVERDUE
            digest_before = generate_weekly_executive_digest(conn, tenant.id, "2026-11-20")
            self.assertEqual(digest_before.overdue_count, 1)
            self.assertEqual(digest_before.total_overdue_minor, 413000)

            # 4. Reject forged webhook signature
            bad_pay = PaymentWebhookPayload(
                tenant_id=tenant.id,
                invoice_number="INV-2026-014",
                gateway_payment_id="pay_rzp_999",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-21T09:30:00Z",
                signature="forged_signature",
            )
            with self.assertRaises(PermissionError):
                verify_and_reconcile_payment(conn, bad_pay)

            # 5. Accept valid HMAC signature and reconcile to PAID
            valid_sig = compute_payment_signature(
                tenant.webhook_secret,
                tenant.id,
                "INV-2026-014",
                "pay_rzp_999",
                413000,
            )
            good_pay = PaymentWebhookPayload(
                tenant_id=tenant.id,
                invoice_number="INV-2026-014",
                gateway_payment_id="pay_rzp_999",
                amount_paid_minor=413000,
                paid_at_iso="2026-11-21T09:30:00Z",
                signature=valid_sig,
            )
            rec_res = verify_and_reconcile_payment(conn, good_pay)
            self.assertEqual(rec_res.status, "PAID")
            self.assertFalse(rec_res.idempotent_replay)

            # Replay of same payment webhook is handled idempotently
            rec_replay = verify_and_reconcile_payment(conn, good_pay)
            self.assertTrue(rec_replay.idempotent_replay)

            # Digest after payment shows 1 paid, 0 overdue
            digest_after = generate_weekly_executive_digest(conn, tenant.id, "2026-11-23")
            self.assertEqual(digest_after.paid_count, 1)
            self.assertEqual(digest_after.overdue_count, 0)
            self.assertEqual(digest_after.total_collected_minor, 413000)


if __name__ == "__main__":
    unittest.main()
