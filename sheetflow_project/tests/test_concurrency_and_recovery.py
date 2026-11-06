"""Concurrency and duplicate-protection tests.

These run real threads against real SQLite connections — no mocked repositories — because
the failure modes being tested (double invoices, double payments) only appear when two
transactions race.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from src.database import get_db_connection, init_db
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.models import SheetFlowRepository
from src.schemas import SheetRowWebhookPayload

TENANT = "tnt_race"
SECRET = "sec_race_2026"


class ConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "race.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        init_db(self.db)
        with get_db_connection(self.db) as conn:
            SheetFlowRepository(conn).upsert_tenant(
                tenant_id=TENANT,
                business_name="Race Traders",
                home_state="Haryana",
                google_sheet_id="sheet_race",
                webhook_secret=SECRET,
                upi_vpa="race@okaxis",
            )

    def tearDown(self):
        self.tmp.cleanup()

    def payload(
        self,
        invoice_number="INV-RACE-001",
        issue_date="2026-11-06",
        due_date="2026-11-13",
    ) -> SheetRowWebhookPayload:
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
            issue_date=issue_date,
            due_date=due_date,
            status="READY_TO_BILL",
        )


class TestConcurrentIngestion(ConcurrencyCase):
    def test_eight_simultaneous_ingests_of_one_row_create_one_invoice(self):
        def ingest(_):
            try:
                with get_db_connection(self.db) as conn:
                    process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)
                return "ok"
            except Exception as exc:  # noqa: BLE001 - surfaced in the assertion below
                return f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(ingest, range(8)))

        failures = [r for r in results if r != "ok"]
        self.assertEqual(failures, [], f"concurrent ingest failures: {failures}")

        with get_db_connection(self.db) as conn:
            repo = SheetFlowRepository(conn)
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM invoices WHERE tenant_id = ?", (TENANT,)
            ).fetchone()["n"]
            invoice = repo.get_invoice_by_number(TENANT, "INV-RACE-001")
            events = repo.list_audit_events(invoice.id)

        self.assertEqual(count, 1, "the same row must never produce two invoices")
        # One generation event and one email event, not eight of each.
        self.assertEqual(len(events), 2)
        self.assertEqual(
            sorted(e["event_type"] for e in events),
            ["INVOICE_EMAIL", "INVOICE_GENERATED_AND_SENT"],
        )

    def test_same_invoice_number_reused_on_a_different_issue_date_is_still_one_invoice(self):
        with get_db_connection(self.db) as conn:
            first = process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)
            second = process_sheet_row_webhook(
                conn,
                self.payload(issue_date="2026-12-06", due_date="2026-12-13"),
                self.pdf_dir,
            )
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM invoices WHERE tenant_id = ?", (TENANT,)
            ).fetchone()["n"]

        self.assertTrue(first.created_new)
        self.assertFalse(second.created_new, "a reused invoice number is not a new invoice")
        self.assertEqual(first.invoice_id, second.invoice_id)
        self.assertEqual(count, 1)


class TestConcurrentPaymentRecording(ConcurrencyCase):
    def test_eight_simultaneous_recordings_of_one_reference_store_one_payment(self):
        with get_db_connection(self.db) as conn:
            process_sheet_row_webhook(conn, self.payload(), self.pdf_dir)

        def pay(_):
            try:
                with get_db_connection(self.db) as conn:
                    repo = SheetFlowRepository(conn)
                    invoice = repo.get_invoice_by_number(TENANT, "INV-RACE-001")
                    _, created = repo.record_payment(
                        tenant_id=TENANT,
                        invoice=invoice,
                        amount_minor=413000,
                        currency="INR",
                        source="MANUAL",
                        provider="upi",
                        provider_payment_id="UPI/RACE-REF-1",
                        recorded_by="tester",
                    )
                    return created
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(pay, range(8)))

        errors = [r for r in results if isinstance(r, str)]
        self.assertEqual(errors, [], f"concurrent payment failures: {errors}")
        self.assertEqual(
            sum(1 for r in results if r is True), 1, "exactly one thread may record the payment"
        )

        with get_db_connection(self.db) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS total FROM payments"
            ).fetchone()
        self.assertEqual(rows["n"], 1)
        self.assertEqual(rows["total"], 413000)


if __name__ == "__main__":
    unittest.main()
