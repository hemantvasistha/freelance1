"""Regression tests for the security and ledger fixes.

Covers:
* the client script no longer carries the server admin token,
* operator endpoints are authenticated by a tenant token and cannot cross tenants,
* the invoice email is dispatched and its delivery recorded separately from the PDF,
* PAID status is queued for the spreadsheet and acknowledged explicitly,
* a stranded PENDING reminder claim is recoverable.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.database import get_db_connection, init_db
from src.dunning.scheduler import evaluate_and_dispatch_reminders, retry_failed_reminders
from src.main import app
from src.models import SheetFlowRepository
from src.notifications.mailer import RecordingMailer
from src.onboarding import onboard_tenant
from src.operator_auth import reset_rate_limits
from src.reconciliation.webhooks import compute_payment_signature, compute_sheet_signature

TENANT_A = "tnt_alpha"
TENANT_B = "tnt_beta"
SECRET_A = "sec_alpha_2026"
SECRET_B = "sec_beta_2026"
ADMIN_TOKEN = "server-admin-token"

REPO_ROOT = Path(__file__).resolve().parents[1]


class TenantSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "iso.db")
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
        os.environ["SHEETFLOW_PDF_DIR"] = self.tmp.name
        os.environ["SHEETFLOW_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ["SHEETFLOW_PUBLIC_BASE_URL"] = "https://billing.example.com"
        os.environ["DRY_RUN"] = "true"
        reset_rate_limits()
        init_db(self.db)

        alpha = onboard_tenant(
            self.db,
            tenant_id=TENANT_A,
            business_name="Alpha Traders",
            home_state="Haryana",
            google_sheet_id="sheet_a",
            webhook_secret=SECRET_A,
            upi_vpa="alpha@okaxis",
            public_base_url="https://billing.example.com",
        )
        beta = onboard_tenant(
            self.db,
            tenant_id=TENANT_B,
            business_name="Beta Traders",
            home_state="Haryana",
            google_sheet_id="sheet_b",
            webhook_secret=SECRET_B,
            upi_vpa="beta@okaxis",
            public_base_url="https://billing.example.com",
        )
        self.token_a = alpha["operator_token"]
        self.token_b = beta["operator_token"]
        self.config_a = alpha["apps_script_config"]
        self.client = TestClient(app)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------
    def _row_payload(self, tenant_id: str = TENANT_A, invoice_number: str = "INV-A-001") -> dict:
        return {
            "tenant_id": tenant_id,
            "sheet_row_number": 2,
            "invoice_number": invoice_number,
            "client_name": "Apex Supplies",
            "client_email": "accounts@apexsupplies.in",
            "client_state": "Haryana",
            "item_description": "Monthly retainer",
            "subtotal_major": "3500.00",
            "tax_rate_pct": "18.00",
            "issue_date": "2026-11-06",
            "due_date": "2026-11-13",
            "status": "READY_TO_BILL",
        }

    def _post_row(self, payload: dict, secret: str):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self.client.post(
            "/api/v1/webhooks/sheet-row-ready",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-SheetFlow-Signature": compute_sheet_signature(secret, body),
            },
        )

    def _pay(self, invoice_number: str, tenant_id: str = TENANT_A, secret: str = SECRET_A,
             payment_id: str = "pay_alpha_1", amount: int = 413000):
        return self.client.post(
            "/api/v1/webhooks/payment-captured",
            json={
                "tenant_id": tenant_id,
                "invoice_number": invoice_number,
                "gateway_payment_id": payment_id,
                "amount_paid_minor": amount,
                "paid_at_iso": "2026-11-11T10:00:00Z",
                "signature": compute_payment_signature(
                    secret, tenant_id, invoice_number, payment_id, amount
                ),
            },
        )

    def _operator(self, path: str, token: str, method: str = "get", payload=None):
        headers = {} if token is None else {"X-SheetFlow-Operator-Token": token}
        if method == "get":
            return self.client.get(path, headers=headers)
        return self.client.post(path, headers=headers, json=payload or {})


class TestClientScriptHasNoAdminToken(TenantSecurityTestCase):
    def test_apps_script_config_carries_only_the_tenant_token(self):
        self.assertIn(f'OPERATOR_TOKEN: "{self.token_a}"', self.config_a)
        self.assertIn(f'TENANT_ID: "{TENANT_A}"', self.config_a)
        self.assertNotIn("ADMIN_TOKEN", self.config_a)
        self.assertNotIn(ADMIN_TOKEN, self.config_a)

    def test_code_gs_never_references_the_server_admin_token(self):
        source = (REPO_ROOT / "apps_script" / "Code.gs").read_text(encoding="utf-8")
        self.assertNotIn("ADMIN_TOKEN", source)
        self.assertIn("OPERATOR_TOKEN", source)
        self.assertIn("X-SheetFlow-Operator-Token", source)


class TestOperatorAuthorization(TenantSecurityTestCase):
    def test_missing_admin_and_foreign_operator_tokens_are_rejected(self):
        self.assertEqual(self._operator("/api/v1/operator/digest", None).status_code, 401)
        self.assertEqual(
            self._operator("/api/v1/operator/digest", ADMIN_TOKEN).status_code, 401
        )
        self.assertEqual(
            self._operator("/api/v1/operator/digest", "op_not-a-real-token").status_code, 401
        )

    def test_operator_token_only_reaches_its_own_tenant(self):
        # B owns the only invoice; A's token must dispatch nothing at all.
        beta_row = self._row_payload(TENANT_B, "INV-B-001")
        self.assertEqual(self._post_row(beta_row, SECRET_B).status_code, 200)

        response = self._operator(
            "/api/v1/operator/dunning/run",
            self.token_a,
            method="post",
            payload={"as_of_date": "2026-11-10", "tenant_id": TENANT_B},
        )
        self.assertEqual(response.status_code, 200, response.text)
        # Body tenant_id is ignored; the token's tenant is used and has no invoices.
        self.assertEqual(response.json()["tenant_id"], TENANT_A)
        self.assertEqual(response.json()["dispatched_count"], 0)

    def test_digest_is_scoped_to_the_token_tenant(self):
        self._post_row(self._row_payload(TENANT_A, "INV-A-001"), SECRET_A)
        alpha = self._operator("/api/v1/operator/digest", self.token_a)
        beta = self._operator("/api/v1/operator/digest", self.token_b)
        self.assertEqual(alpha.status_code, 200, alpha.text)
        self.assertEqual(alpha.json()["tenant_id"], TENANT_A)
        self.assertEqual(alpha.json()["total_outstanding_minor"], 413000)
        self.assertEqual(beta.status_code, 200, beta.text)
        self.assertEqual(beta.json()["total_outstanding_minor"], 0)


class TestInvoiceEmailDelivery(TenantSecurityTestCase):
    def test_invoice_email_is_dispatched_and_recorded_separately(self):
        response = self._post_row(self._row_payload(), SECRET_A)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # DRY_RUN means "composed and recorded", not "delivered to the client".
        self.assertEqual(body["delivery_status"], "DRY_RUN")

        with get_db_connection(self.db) as conn:
            repo = SheetFlowRepository(conn)
            invoice = repo.get_invoice_by_number(TENANT_A, "INV-A-001")
            events = {e["event_type"]: e for e in repo.list_audit_events(invoice.id)}
        self.assertIn("INVOICE_GENERATED_AND_SENT", events)
        self.assertIn("INVOICE_EMAIL", events)
        payload = json.loads(events["INVOICE_EMAIL"]["payload_json"])
        self.assertEqual(payload["recipient"], "accounts@apexsupplies.in")
        self.assertEqual(payload["delivery_status"], "DRY_RUN")


class TestSheetPaymentWriteBack(TenantSecurityTestCase):
    def test_paid_status_is_queued_then_acknowledged(self):
        self._post_row(self._row_payload(), SECRET_A)
        self.assertEqual(self._pay("INV-A-001").status_code, 200)

        pending = self._operator("/api/v1/operator/sheet-updates", self.token_a)
        self.assertEqual(pending.status_code, 200, pending.text)
        updates = pending.json()["updates"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["invoice_number"], "INV-A-001")
        self.assertEqual(updates[0]["status"], "PAID")
        self.assertEqual(updates[0]["paid_at"], "2026-11-11T10:00:00Z")

        ack = self._operator(
            "/api/v1/operator/sheet-updates/ack",
            self.token_a,
            method="post",
            payload={"ids": [updates[0]["id"]]},
        )
        self.assertEqual(ack.status_code, 200, ack.text)
        self.assertEqual(ack.json()["acknowledged"], 1)
        self.assertEqual(
            self._operator("/api/v1/operator/sheet-updates", self.token_a).json()["count"], 0
        )

    def test_replayed_payment_does_not_queue_a_second_update(self):
        self._post_row(self._row_payload(), SECRET_A)
        self.assertEqual(self._pay("INV-A-001").status_code, 200)
        self.assertEqual(
            self._operator("/api/v1/operator/sheet-updates", self.token_a).json()["count"], 1
        )
        # Same gateway payment id: reconciled as a replay, no new ledger change.
        self.assertEqual(self._pay("INV-A-001").status_code, 200)
        self.assertEqual(
            self._operator("/api/v1/operator/sheet-updates", self.token_a).json()["count"], 1
        )

    def test_another_tenant_cannot_see_or_ack_these_updates(self):
        self._post_row(self._row_payload(), SECRET_A)
        self.assertEqual(self._pay("INV-A-001").status_code, 200)
        alpha_update = self._operator("/api/v1/operator/sheet-updates", self.token_a).json()[
            "updates"
        ][0]

        self.assertEqual(
            self._operator("/api/v1/operator/sheet-updates", self.token_b).json()["count"], 0
        )
        stolen = self._operator(
            "/api/v1/operator/sheet-updates/ack",
            self.token_b,
            method="post",
            payload={"ids": [alpha_update["id"]]},
        )
        self.assertEqual(stolen.json()["acknowledged"], 0)
        # Alpha's change is still pending: B's ack changed nothing.
        self.assertEqual(
            self._operator("/api/v1/operator/sheet-updates", self.token_a).json()["count"], 1
        )


class TestStrandedReminderRecovery(TenantSecurityTestCase):
    def test_pending_claim_from_a_crashed_run_is_retried(self):
        self._post_row(self._row_payload(), SECRET_A)

        # Simulate a worker that claimed the reminder but died before recording a
        # delivery outcome: the audit row is written with delivery_status PENDING.
        with get_db_connection(self.db) as conn:
            actions = evaluate_and_dispatch_reminders(
                conn, TENANT_A, "2026-11-10", mailer=None
            )
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].delivery_status, "PENDING")

            mailer = RecordingMailer()
            retries = retry_failed_reminders(conn, mailer, stale_pending_minutes=0)
            self.assertEqual(len(retries), 1)
            self.assertEqual(len(mailer.sent), 1)
            self.assertEqual(retries[0]["delivery_status"], "DRY_RUN")

            # A second pass must not re-send the same stage.
            self.assertEqual(retry_failed_reminders(conn, RecordingMailer(), stale_pending_minutes=0), [])


if __name__ == "__main__":
    unittest.main()
