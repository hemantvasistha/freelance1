"""Block 1: Data Access Layer & Idempotent Repository Operations."""
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TenantRecord:
    id: str
    business_name: str
    tax_id: str | None
    home_state: str
    currency_code: str
    google_sheet_id: str
    upi_vpa: str | None
    webhook_secret: str
    is_active: bool
    created_at: str
    owner_email: str | None = None
    # SHA-256 of the tenant's own operator token; plaintext is never stored.
    operator_token_hash: str | None = None


@dataclass
class InvoiceRecord:
    id: str
    tenant_id: str
    sheet_row_number: int
    invoice_number: str
    idempotency_key: str
    client_name: str
    client_email: str
    client_phone: str | None
    client_state: str | None
    item_description: str
    subtotal_minor: int
    tax_rate_bps: int
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    total_minor: int
    issue_date: str
    due_date: str
    status: str
    payment_url: str | None
    payment_gateway_ref: str | None
    pdf_path: str | None
    last_reminder_stage: str
    paid_at: str | None
    created_at: str
    updated_at: str


class SheetFlowRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_tenant(
        self,
        tenant_id: str,
        business_name: str,
        home_state: str,
        google_sheet_id: str,
        webhook_secret: str,
        tax_id: str | None = None,
        currency_code: str = "INR",
        upi_vpa: str | None = None,
        owner_email: str | None = None,
    ) -> TenantRecord:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO tenants (
                id, business_name, tax_id, home_state, currency_code,
                google_sheet_id, upi_vpa, owner_email, webhook_secret, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                business_name = excluded.business_name,
                tax_id = excluded.tax_id,
                home_state = excluded.home_state,
                currency_code = excluded.currency_code,
                google_sheet_id = excluded.google_sheet_id,
                upi_vpa = excluded.upi_vpa,
                owner_email = excluded.owner_email,
                webhook_secret = excluded.webhook_secret
            """,
            (
                tenant_id,
                business_name,
                tax_id,
                home_state,
                currency_code,
                google_sheet_id,
                upi_vpa,
                owner_email,
                webhook_secret,
                now,
            ),
        )
        return self.get_tenant(tenant_id)  # type: ignore

    def list_tenants(self) -> list[TenantRecord]:
        rows = self.conn.execute("SELECT * FROM tenants ORDER BY created_at ASC").fetchall()
        return [self._row_to_tenant(r) for r in rows]

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        row = self.conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return self._row_to_tenant(row) if row else None

    def set_operator_token_hash(self, tenant_id: str, token_hash: str) -> bool:
        """Stores the hash of a freshly issued tenant operator token."""
        cur = self.conn.execute(
            "UPDATE tenants SET operator_token_hash = ? WHERE id = ?",
            (token_hash, tenant_id),
        )
        return cur.rowcount == 1

    def _row_to_tenant(self, row: sqlite3.Row) -> TenantRecord:
        return TenantRecord(
            id=row["id"],
            business_name=row["business_name"],
            tax_id=row["tax_id"],
            home_state=row["home_state"],
            currency_code=row["currency_code"],
            google_sheet_id=row["google_sheet_id"],
            upi_vpa=row["upi_vpa"],
            webhook_secret=row["webhook_secret"],
            owner_email=row["owner_email"],
            operator_token_hash=(
                row["operator_token_hash"] if "operator_token_hash" in row.keys() else None
            ),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    def create_or_get_invoice(
        self,
        *,
        tenant_id: str,
        sheet_row_number: int,
        invoice_number: str,
        idempotency_key: str,
        client_name: str,
        client_email: str,
        client_phone: str | None,
        client_state: str | None,
        item_description: str,
        subtotal_minor: int,
        tax_rate_bps: int,
        cgst_minor: int,
        sgst_minor: int,
        igst_minor: int,
        total_minor: int,
        issue_date: str,
        due_date: str,
        status: str = "READY_TO_BILL",
    ) -> tuple[InvoiceRecord, bool]:
        """
        Idempotent invoice creation. Returns (InvoiceRecord, created_new: bool).
        If idempotency_key or (tenant_id, invoice_number) already exists, returns existing record.
        """
        existing = self.conn.execute(
            "SELECT * FROM invoices WHERE idempotency_key = ? OR (tenant_id = ? AND invoice_number = ?)",
            (idempotency_key, tenant_id, invoice_number),
        ).fetchone()
        if existing:
            return self._row_to_invoice(existing), False

        inv_id = f"inv_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        try:
            self.conn.execute(
                """
            INSERT INTO invoices (
                id, tenant_id, sheet_row_number, invoice_number, idempotency_key,
                client_name, client_email, client_phone, client_state, item_description,
                subtotal_minor, tax_rate_bps, cgst_minor, sgst_minor, igst_minor, total_minor,
                issue_date, due_date, status, last_reminder_stage, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NONE', ?, ?)
            """,
                (
                    inv_id,
                    tenant_id,
                    sheet_row_number,
                    invoice_number,
                    idempotency_key,
                    client_name,
                    client_email,
                    client_phone,
                    client_state,
                    item_description,
                    subtotal_minor,
                    tax_rate_bps,
                    cgst_minor,
                    sgst_minor,
                    igst_minor,
                    total_minor,
                    issue_date,
                    due_date,
                    status,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # A concurrent webhook won the race between the SELECT above and this
            # INSERT; the UNIQUE constraints guarantee exactly one invoice exists,
            # so return that one instead of surfacing a 500.
            existing = self.conn.execute(
                "SELECT * FROM invoices WHERE idempotency_key = ? OR (tenant_id = ? AND invoice_number = ?)",
                (idempotency_key, tenant_id, invoice_number),
            ).fetchone()
            if not existing:
                raise
            return self._row_to_invoice(existing), False
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        return self._row_to_invoice(row), True

    def update_invoice_billing_artifacts(
        self,
        invoice_id: str,
        *,
        payment_url: str,
        payment_gateway_ref: str,
        pdf_path: str,
        new_status: str = "SENT",
    ) -> InvoiceRecord:
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE invoices
            SET payment_url = ?, payment_gateway_ref = ?, pdf_path = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (payment_url, payment_gateway_ref, pdf_path, new_status, now, invoice_id),
        )
        return self.get_invoice_by_id(invoice_id)  # type: ignore

    def mark_invoice_paid(
        self,
        tenant_id: str,
        invoice_number: str,
        *,
        paid_at_iso: str,
        gateway_payment_id: str,
    ) -> InvoiceRecord:
        now = utc_now_iso()
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE tenant_id = ? AND invoice_number = ?",
            (tenant_id, invoice_number),
        ).fetchone()
        if not row:
            raise ValueError(f"Invoice {invoice_number} not found for tenant {tenant_id}")
        if row["status"] == "CANCELLED":
            raise ValueError(f"Invoice {invoice_number} is CANCELLED and cannot be marked PAID.")
        self.conn.execute(
            """
            UPDATE invoices
            SET status = 'PAID', paid_at = ?, payment_gateway_ref = ?, updated_at = ?
            WHERE tenant_id = ? AND invoice_number = ?
            """,
            (paid_at_iso, gateway_payment_id, now, tenant_id, invoice_number),
        )
        updated = self.conn.execute(
            "SELECT * FROM invoices WHERE tenant_id = ? AND invoice_number = ?",
            (tenant_id, invoice_number),
        ).fetchone()
        return self._row_to_invoice(updated)

    def update_reminder_stage(
        self, invoice_id: str, stage: str, new_status: str | None = None
    ) -> InvoiceRecord:
        now = utc_now_iso()
        if new_status:
            self.conn.execute(
                "UPDATE invoices SET last_reminder_stage = ?, status = ?, updated_at = ? WHERE id = ?",
                (stage, new_status, now, invoice_id),
            )
        else:
            self.conn.execute(
                "UPDATE invoices SET last_reminder_stage = ?, updated_at = ? WHERE id = ?",
                (stage, now, invoice_id),
            )
        return self.get_invoice_by_id(invoice_id)  # type: ignore

    def record_payment(
        self,
        *,
        tenant_id: str,
        invoice: "InvoiceRecord",
        amount_minor: int,
        currency: str,
        source: str,
        provider: str = "",
        provider_payment_id: str | None = None,
        recorded_by: str = "",
        note: str = "",
    ) -> tuple[str, bool]:
        """Appends a verified payment. Returns (id, created).

        Never updates or deletes an existing payment: financial history is append-only.
        A replayed gateway event is rejected by the unique (invoice, provider payment
        id) index, so reconciliation can be retried safely.
        """
        if amount_minor <= 0:
            raise ValueError("Payment amount must be positive.")
        if source not in ("GATEWAY", "MANUAL"):
            raise ValueError("source must be GATEWAY or MANUAL")
        pid = f"pay_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO payments (
                id, tenant_id, invoice_id, invoice_number, amount_minor, currency,
                source, provider, provider_payment_id, verified_at, recorded_by, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                tenant_id,
                invoice.id,
                invoice.invoice_number,
                amount_minor,
                currency,
                source,
                provider,
                provider_payment_id,
                now,
                recorded_by,
                note,
                now,
            ),
        )
        return pid, cur.rowcount == 1

    def list_payments(self, tenant_id: str, limit: int = 100) -> list[dict]:
        conn_rows = self.conn.execute(
            "SELECT * FROM payments WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [dict(row) for row in conn_rows]

    def paid_total_minor(self, invoice_id: str) -> int:
        """Sum of verified payments against one invoice (drives partial-payment state)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) AS total FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        return int(row["total"] or 0)

    def claim_notification(self, tenant_id: str, kind: str, period_key: str) -> bool:
        """Claims the right to deliver one periodic message. False means already sent."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO notification_deliveries
                (id, tenant_id, kind, period_key, status, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (f"ntf_{uuid.uuid4().hex[:16]}", tenant_id, kind, period_key, utc_now_iso()),
        )
        return cur.rowcount == 1

    def set_notification_status(
        self, tenant_id: str, kind: str, period_key: str, status: str
    ) -> None:
        self.conn.execute(
            "UPDATE notification_deliveries SET status = ? "
            "WHERE tenant_id = ? AND kind = ? AND period_key = ?",
            (status, tenant_id, kind, period_key),
        )

    def release_notification(self, tenant_id: str, kind: str, period_key: str) -> None:
        """Drops a claim whose delivery failed, so a retry can still send it."""
        self.conn.execute(
            "DELETE FROM notification_deliveries "
            "WHERE tenant_id = ? AND kind = ? AND period_key = ? AND status = 'PENDING'",
            (tenant_id, kind, period_key),
        )

    def get_invoice_by_id(self, invoice_id: str) -> InvoiceRecord | None:
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return self._row_to_invoice(row) if row else None

    def get_invoice_by_number(self, tenant_id: str, invoice_number: str) -> InvoiceRecord | None:
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE tenant_id = ? AND invoice_number = ?",
            (tenant_id, invoice_number),
        ).fetchone()
        return self._row_to_invoice(row) if row else None

    def list_invoices_for_tenant(self, tenant_id: str) -> list[InvoiceRecord]:
        rows = self.conn.execute(
            "SELECT * FROM invoices WHERE tenant_id = ? ORDER BY sheet_row_number ASC",
            (tenant_id,),
        ).fetchall()
        return [self._row_to_invoice(r) for r in rows]

    def record_audit_event_if_new(
        self, invoice_id: str, event_type: str, event_key: str, payload: dict[str, Any]
    ) -> bool:
        """Returns True if recorded, False if event_key was already processed (idempotency guard).

        Uses INSERT OR IGNORE against the UNIQUE event_key so the guard stays correct
        under concurrent retries, instead of a SELECT-then-INSERT that can race.
        """
        ev_id = f"aud_{uuid.uuid4().hex[:16]}"
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO audit_events (id, invoice_id, event_type, event_key, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ev_id, invoice_id, event_type, event_key, json.dumps(payload, sort_keys=True), utc_now_iso()),
        )
        return cursor.rowcount == 1

    def update_audit_payload(self, event_key: str, patch: dict[str, Any]) -> bool:
        """Merges fields into an existing audit payload (used to record delivery outcome)."""
        row = self.conn.execute(
            "SELECT id, payload_json FROM audit_events WHERE event_key = ?", (event_key,)
        ).fetchone()
        if not row:
            return False
        payload = json.loads(row["payload_json"])
        payload.update(patch)
        self.conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), row["id"]),
        )
        self.conn.commit()
        return True

    def list_audit_events(self, invoice_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events WHERE invoice_id = ? ORDER BY created_at ASC",
            (invoice_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_invoice(row: sqlite3.Row) -> InvoiceRecord:
        return InvoiceRecord(**dict(row))
