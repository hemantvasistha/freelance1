"""Razorpay: signature verification, event normalisation and PAID reconciliation."""
import hashlib
import hmac
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.config import Settings
from src.database import get_db_connection, init_db
from src.ingestion.sheet_sync import process_sheet_row_webhook
from src.main import app
from src.models import SheetFlowRepository
from src.reconciliation.razorpay import (
    build_payment_link_provider,
    parse_payment_event,
    verify_webhook_signature,
)
from src.schemas import SheetRowWebhookPayload

SECRET = "whsec_test_razorpay"


def _event(amount_minor: int = 377600, payment_id: str = "pay_test_1") -> bytes:
    return json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "amount": amount_minor,
                        "currency": "INR",
                        "created_at": 1794000000,
                        "notes": {
                            "tenant_id": "tnt_client01",
                            "invoice_number": "INV-100",
                        },
                    }
                }
            },
        }
    ).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestSignatureAndParsing(unittest.TestCase):
    def test_signature_roundtrip(self):
        body = _event()
        self.assertTrue(verify_webhook_signature(body, _sign(body), SECRET))
        self.assertFalse(verify_webhook_signature(body, _sign(body, "other"), SECRET))
        self.assertFalse(verify_webhook_signature(body, "", SECRET))
        self.assertFalse(verify_webhook_signature(body, _sign(body), ""))

    def test_parse_payment_event(self):
        event = parse_payment_event(_event())
        self.assertEqual(event["tenant_id"], "tnt_client01")
        self.assertEqual(event["invoice_number"], "INV-100")
        self.assertEqual(event["amount_paid_minor"], 377600)
        self.assertEqual(event["paid_at_iso"], "2026-11-06T21:20:00Z")

    def test_non_payment_event_is_ignored(self):
        body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
        self.assertIsNone(parse_payment_event(body))

    def test_missing_notes_raises(self):
        body = json.dumps(
            {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "p", "amount": 1}}}}
        ).encode()
        with self.assertRaises(ValueError):
            parse_payment_event(body)

    def test_provider_absent_without_keys(self):
        self.assertIsNone(build_payment_link_provider(Settings()))
        provider = build_payment_link_provider(
            Settings(razorpay_key_id="rzp_test", razorpay_key_secret="secret")
        )
        self.assertTrue(callable(provider))


class TestRazorpayRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "clients.db")
        self.pdf_dir = os.path.join(self.tmp.name, "pdfs")
        os.environ["SHEETFLOW_DB_PATH"] = self.db
        os.environ["SHEETFLOW_PDF_DIR"] = self.pdf_dir
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
        os.environ["DRY_RUN"] = "true"

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
            )
            process_sheet_row_webhook(
                conn,
                SheetRowWebhookPayload(
                    tenant_id="tnt_client01",
                    sheet_row_number=2,
                    invoice_number="INV-100",
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
                ),
                self.pdf_dir,
            )
        self.client = TestClient(app)

    def tearDown(self):
        for key in ("RAZORPAY_WEBHOOK_SECRET", "SHEETFLOW_DB_PATH", "SHEETFLOW_PDF_DIR", "DRY_RUN"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _post(self, body: bytes, signature: str | None = None):
        return self.client.post(
            "/api/v1/webhooks/razorpay",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Razorpay-Signature": signature if signature is not None else _sign(body),
            },
        )

    def test_rejects_bad_signature(self):
        self.assertEqual(self._post(_event(), signature="deadbeef").status_code, 401)

    def test_marks_invoice_paid_and_is_idempotent(self):
        first = self._post(_event())
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "PAID")
        self.assertFalse(first.json()["idempotent_replay"])

        replay = self._post(_event())
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])

        with get_db_connection(self.db) as conn:
            invoice = SheetFlowRepository(conn).get_invoice_by_number("tnt_client01", "INV-100")
        self.assertEqual(invoice.status, "PAID")
        self.assertIsNotNone(invoice.paid_at)

    def test_underpayment_is_rejected(self):
        response = self._post(_event(amount_minor=100))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Underpayment", response.json()["detail"])

    def test_ignores_non_payment_event(self):
        body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")


if __name__ == "__main__":
    unittest.main()
