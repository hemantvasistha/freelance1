"""Block 1: SQLite Database Connection & Schema Initialization."""
import sqlite3
from contextlib import closing, contextmanager
from typing import Iterator

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    business_name TEXT NOT NULL,
    tax_id TEXT,
    home_state TEXT NOT NULL,
    currency_code TEXT NOT NULL DEFAULT 'INR',
    google_sheet_id TEXT NOT NULL,
    upi_vpa TEXT,
    owner_email TEXT,
    webhook_secret TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    sheet_row_number INTEGER NOT NULL,
    invoice_number TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    client_name TEXT NOT NULL,
    client_email TEXT NOT NULL,
    client_phone TEXT,
    client_state TEXT,
    item_description TEXT NOT NULL,
    subtotal_minor INTEGER NOT NULL,
    tax_rate_bps INTEGER NOT NULL,
    cgst_minor INTEGER NOT NULL DEFAULT 0,
    sgst_minor INTEGER NOT NULL DEFAULT 0,
    igst_minor INTEGER NOT NULL DEFAULT 0,
    total_minor INTEGER NOT NULL,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','READY_TO_BILL','SENT','OVERDUE','PAID','CANCELLED')),
    payment_url TEXT,
    payment_gateway_ref TEXT,
    pdf_path TEXT,
    last_reminder_stage TEXT NOT NULL DEFAULT 'NONE',
    paid_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    event_type TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(db_path: str) -> None:
    # `with sqlite3.connect(...)` commits but never closes; `closing` guarantees
    # the file handle is released (important on Windows, where an orphaned handle
    # keeps the .db file locked until garbage collection).
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations so a ledger created by an older build keeps working."""
    tenant_columns = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    if "owner_email" not in tenant_columns:
        conn.execute("ALTER TABLE tenants ADD COLUMN owner_email TEXT")


@contextmanager
def get_db_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL lets the dunning/digest readers run while a webhook writes, and
    # busy_timeout makes concurrent writers wait instead of raising
    # "database is locked".
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
