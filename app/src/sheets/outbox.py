"""Write-back outbox for the customer's Google Sheet.

The sheet is written by Apps Script, not by the server (the server has no Google
credentials). So when an invoice becomes PAID or OVERDUE here, the change is queued
in `sheet_sync_outbox` and the sheet picks it up on its next run (menu item, or a
time-driven trigger). Changes are keyed on `invoice_number`, which the business owner
does not renumber, instead of a row position that shifts when rows are inserted.

Delivery is acknowledged explicitly: a row leaves the queue only after the sheet
confirms it wrote that invoice number.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    invoice_number: str,
    status: str,
    event_key: str,
    paid_at: str | None = None,
    payment_ref: str | None = None,
) -> bool:
    """Queues a ledger change. Returns False when this event was already queued.

    The UNIQUE event_key makes replays (a duplicate webhook, a retried run) a no-op
    rather than a second pending row for the same status change.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO sheet_sync_outbox
            (id, tenant_id, invoice_number, event_key, status, paid_at, payment_ref, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"obx_{uuid.uuid4().hex[:16]}",
            tenant_id,
            invoice_number,
            event_key,
            status,
            paid_at,
            payment_ref,
            _now(),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def pending(conn: sqlite3.Connection, tenant_id: str, limit: int = 200) -> list[dict]:
    """Undelivered changes for one tenant, oldest first."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, invoice_number, status, paid_at, payment_ref, created_at, attempts
        FROM sheet_sync_outbox
        WHERE tenant_id = ? AND delivered_at IS NULL
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (tenant_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def acknowledge(conn: sqlite3.Connection, tenant_id: str, ids: list[str]) -> int:
    """Marks the given outbox rows delivered. Scoped to the tenant that owns them."""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"""
        UPDATE sheet_sync_outbox
        SET delivered_at = ?, attempts = attempts + 1
        WHERE tenant_id = ? AND delivered_at IS NULL AND id IN ({placeholders})
        """,
        (_now(), tenant_id, *ids),
    )
    conn.commit()
    return cur.rowcount


def record_attempt(conn: sqlite3.Connection, tenant_id: str, ids: list[str]) -> None:
    """Counts a fetch that the sheet received but has not acknowledged yet."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE sheet_sync_outbox SET attempts = attempts + 1 "
        f"WHERE tenant_id = ? AND delivered_at IS NULL AND id IN ({placeholders})",
        (tenant_id, *ids),
    )
    conn.commit()
