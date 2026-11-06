"""Full Integration & Reinspection Test Suite across Blocks 1, 2, 3, and 4 via FastAPI TestClient."""
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.database import get_db_connection, init_db
from src.main import app
from src.models import SheetFlowRepository
from src.reconciliation.webhooks import compute_payment_signature, compute_sheet_signature

TENANT_SECRET = "sec_live_demo_2026"
ADMIN_TOKEN = "test-admin-token"


class TestFullSystemIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "integration.db")
        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                "SHEETFLOW_DB_PATH",
                "SHEETFLOW_PDF_DIR",
                "SHEETFLOW_ADMIN_TOKEN",
                "SHEETFLOW_PUBLIC_BASE_URL",
                "DRY_RUN",
            )
        }
        os.environ["SHEETFLOW_DB_PATH"] = self.db_path
        os.environ["SHEETFLOW_PDF_DIR"] = self.tmp_dir.name
        os.environ["SHEETFLOW_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ["SHEETFLOW_PUBLIC_BASE_URL"] = "https://billing.example.com"
        os.environ["DRY_RUN"] = "true"
        init_db(self.db_path)

        with get_db_connection(self.db_path) as conn:
            repo = SheetFlowRepository(conn)
            repo.upsert_tenant(
                tenant_id="tnt_demo_001",
                business_name="SheetFlow Automation Studio",
                home_state="Haryana",
                google_sheet_id="sheet_live_001",
                webhook_secret=TENANT_SECRET,
                tax_id="06AABCS1429B1Z1",
                upi_vpa="sheetflow@okaxis",
            )
        self.client = TestClient(app)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp_dir.cleanup()

    def _post_row(self, payload: dict, signature: str | None = None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers["X-SheetFlow-Signature"] = signature
        return self.client.post("/api/v1/webhooks/sheet-row-ready", content=body, headers=headers)

    def _row_payload(self) -> dict:
        return {
            "tenant_id": "tnt_demo_001",
            "sheet_row_number": 14,
            "invoice_number": "INV-2026-014",
            "client_name": "Apex Industrial Supplies Pvt Ltd",
            "client_email": "accounts@apexsupplies.in",
            "client_phone": "+919812345678",
            "client_state": "Haryana",
            "item_description": "Automated Spreadsheet-to-Invoice Retainer",
            "subtotal_major": "3500.00",
            "tax_rate_pct": "18.00",
            "issue_date": "2026-11-06",
            "due_date": "2026-11-13",
            "status": "READY_TO_BILL",
        }

    def test_full_http_lifecycle(self):
        # 1. Health check
        r_health = self.client.get("/health")
        self.assertEqual(r_health.status_code, 200)
        self.assertEqual(r_health.json()["status"], "ok")
        self.assertIn(r_health.json()["pdf_engine"], ("weasyprint", "reportlab"))

        # 2. Sheet row webhook -> creates invoice, calculates GST, renders PDF
        row_payload = self._row_payload()
        body = json.dumps(row_payload, separators=(",", ":")).encode("utf-8")
        r_sync = self._post_row(row_payload, compute_sheet_signature(TENANT_SECRET, body))
        self.assertEqual(r_sync.status_code, 200, r_sync.text)
        data = r_sync.json()
        self.assertEqual(data["status"], "SENT")
        self.assertEqual(data["total_minor"], 413000)
        self.assertTrue(os.path.exists(data["pdf_path"]))
        self.assertTrue(data["pdf_url"].startswith("https://billing.example.com/api/v1/invoices/"))
        self.assertIn("token=", data["pdf_url"])

        # 2b. An unsigned or forged request must be rejected
        self.assertEqual(self._post_row(row_payload).status_code, 401)
        self.assertEqual(self._post_row(row_payload, "deadbeef").status_code, 401)

        # 2c. The caller cannot self-declare an invoice as PAID
        tampered = dict(row_payload, status="PAID")
        tampered_body = json.dumps(tampered, separators=(",", ":")).encode("utf-8")
        r_tampered = self._post_row(tampered, compute_sheet_signature(TENANT_SECRET, tampered_body))
        self.assertEqual(r_tampered.status_code, 422)

        # 3. Trigger T-3 dunning reminder via API
        r_dun = self.client.post(
            "/api/v1/dunning/run",
            json={"tenant_id": "tnt_demo_001", "as_of_date": "2026-11-10"},
            headers={"X-SheetFlow-Admin-Token": ADMIN_TOKEN},
        )
        self.assertEqual(r_dun.status_code, 200, r_dun.text)
        self.assertEqual(r_dun.json()["dispatched_count"], 1)
        self.assertEqual(r_dun.json()["reminders"][0]["delivery_status"], "DRY_RUN")

        # 3b. Operator endpoints require the admin token
        r_noauth = self.client.post(
            "/api/v1/dunning/run",
            json={"tenant_id": "tnt_demo_001", "as_of_date": "2026-11-10"},
        )
        self.assertEqual(r_noauth.status_code, 401)
        self.assertEqual(self.client.get("/api/v1/digest/tnt_demo_001").status_code, 401)

        # 4. Reconcile payment via webhook
        sig = compute_payment_signature(
            TENANT_SECRET, "tnt_demo_001", "INV-2026-014", "pay_001", 413000
        )
        r_pay = self.client.post(
            "/api/v1/webhooks/payment-captured",
            json={
                "tenant_id": "tnt_demo_001",
                "invoice_number": "INV-2026-014",
                "gateway_payment_id": "pay_001",
                "amount_paid_minor": 413000,
                "paid_at_iso": "2026-11-11T10:00:00Z",
                "signature": sig,
            },
        )
        self.assertEqual(r_pay.status_code, 200, r_pay.text)
        self.assertEqual(r_pay.json()["status"], "PAID")

        # 4b. The invoice PDF is downloadable from its signed link, and only from it
        pdf_path_part = data["pdf_url"].split("https://billing.example.com", 1)[1]
        r_pdf = self.client.get(pdf_path_part)
        self.assertEqual(r_pdf.status_code, 200, r_pdf.text)
        self.assertEqual(r_pdf.headers["content-type"], "application/pdf")
        self.assertTrue(r_pdf.content.startswith(b"%PDF-"))
        self.assertEqual(self.client.get(pdf_path_part.split("?")[0]).status_code, 401)

        # 5. Check Weekly Digest shows 413000 collected and 0 outstanding
        r_dig = self.client.get(
            "/api/v1/digest/tnt_demo_001?as_of_date=2026-11-12",
            headers={"X-SheetFlow-Admin-Token": ADMIN_TOKEN},
        )
        self.assertEqual(r_dig.status_code, 200, r_dig.text)
        self.assertEqual(r_dig.json()["total_collected_minor"], 413000)
        self.assertEqual(r_dig.json()["total_outstanding_minor"], 0)


if __name__ == "__main__":
    unittest.main()
